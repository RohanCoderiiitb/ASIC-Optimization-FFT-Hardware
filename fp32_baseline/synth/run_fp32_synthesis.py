#!/usr/bin/env python3
"""
FP32 Baseline ASIC Synthesis -- Yosys + OpenSTA
================================================
Extracts the same four PPA metrics the mixed-precision NSGA-II loop uses in
objectiveEvaluationFFT.py (`_run_yosys_opensta` / `_compute_actual_normalized_latency`):

    Power (mW), Area (um^2), Critical Path Delay (ns), Normalized Latency

plus one derived metric this script adds on top, Energy per FFT:

    Energy/FFT (nJ) = Power(mW) * ExecCycles * ClockPeriod(ns) / 1000

ExecCycles is the compute-only "start-to-done" cycle count already reported
by sim/fp32_performance_evaluator.py (same metric the mixed-precision
evaluator calls avg_exec_cycles) -- NOT load+compute+unload. It is read from
that evaluator's output table (--cycles-file, default
sim/perf/fp32_sqnr_results.txt), so run the performance evaluator for the
same --sizes before this script (run_fp32_design.py already does this in the
right order). If a size's cycle count isn't available, Energy/FFT is
reported as N/A for that row rather than guessed.

using the exact same open-source tool methodology -- Yosys for gate-level
synthesis + area, OpenSTA for timing and power -- so the FP32 baseline numbers
are directly comparable to the mixed FP4/FP8 cores. There is no OpenROAD
floorplan/place/route step here: like the mixed-precision evaluator, this is a
pre-layout (Yosys area + liberty-based OpenSTA) PPA extraction, not a
backend/P&R flow.

Per design size N:
  1. Yosys: blackbox sram_512x64_2rw, elaborate + flatten fp32_fft_<N>_top,
     map to the standard-cell liberty with abc, report chip area (`stat`).
     This flattened netlist is what area and timing are measured from.
  2. OpenSTA (timing + flat-activity power): read the std-cell + SRAM-macro
     liberty, read the flattened netlist, create a `CLOCK_PERIOD`-ns clock,
     set I/O delay to period/4, report the worst-case timing path, and a
     flat-0.2-activity power number kept only as a fallback/reference (see
     step 4).
  3. Normalized latency = (crit_delay_ns / CLOCK_PERIOD) * max(1, stages/6),
     capped at 10.0 -- identical formula to
     MixedPrecisionFFTProblem._compute_actual_normalized_latency.
  4. Real-activity power (the number actually reported as Power (mW)):
     Yosys is re-run WITHOUT `flatten` (see run_yosys's `flatten=False` path)
     to keep RTL module/instance names intact, then the RTL testbench used
     for SQNR (fp32_performance_evaluator.py, same 11 signals) is re-run with
     `$dumpvars` on to capture a VCD, and OpenSTA's `read_vcd` annotates that
     onto the hierarchy-preserved netlist before `report_power`. A flat
     netlist matches only ~2% of nets by name after `abc`/`opt -purge`
     collapse everything into one module; keeping hierarchy gets ~10x more
     matches (see generate_activity_vcd/run_opensta_vcd_power). Falls back to
     the flat-0.2 number (PowerSrc=flat_0.2_fallback in the report) if fewer
     than MIN_ANNOTATED_PINS pins get annotated.
  5. Energy/FFT, as above, using the real-activity power from step 4.

Usage (from anywhere):
    python3 fp32_baseline/synth/run_fp32_synthesis.py                # all 10 sizes
    python3 fp32_baseline/synth/run_fp32_synthesis.py --sizes 256 1024
    python3 fp32_baseline/synth/run_fp32_synthesis.py --clock-period 8.0

Output: synth/fp32_ppa_report.txt (kept as plain text). Every other generated
file (per-size Yosys/OpenSTA scripts, logs, and the synthesized netlists) is
zipped into synth/fp32_synth_artifacts.zip so synth/ stays with just the
report and one archive.
"""

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import zipfile

SYNTH_DIR = os.path.dirname(os.path.abspath(__file__))          # fp32_baseline/synth
BASE_DIR = os.path.dirname(SYNTH_DIR)                            # fp32_baseline
REPO_ROOT = os.path.dirname(BASE_DIR)                             # repo root

sys.path.insert(0, os.path.join(BASE_DIR, "sim"))
from fp32_performance_evaluator import FP32PerformanceEvaluator  # noqa: E402

ALL_SIZES = [2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]

DEFAULT_STD_LIB = os.path.join(REPO_ROOT, "45_nm_PDK", "cadence", "cadence_45nm",
                                "lib", "fast_vdd1v0_basicCells.lib")
DEFAULT_RAM_LIB = os.path.join(BASE_DIR, "fp32_SRAM_MACROS",
                                "sram_512x64_2rw_TT_1p0V_25C.lib")
DEFAULT_SOURCE_DIR = os.path.join(BASE_DIR, "source")
DEFAULT_SHARED_DIR = os.path.join(REPO_ROOT, "verilog_sources")
DEFAULT_GENERATED_DIR = os.path.join(BASE_DIR, "generated_cores")
DEFAULT_WORK_DIR = os.path.join(SYNTH_DIR, "work")
DEFAULT_OUT = os.path.join(SYNTH_DIR, "fp32_ppa_report.txt")
DEFAULT_CYCLES_FILE = os.path.join(BASE_DIR, "sim", "perf", "fp32_sqnr_results.txt")

SRAM_MACRO_MODULE = "sram_512x64_2rw"

CLOCK_NET_NAME = "clk"

MAX_POWER_MW = 500.0
MAX_AREA_UM2 = 600000.0

# Floor below which a `read_vcd` activity annotation is treated as too sparse
# to trust (falls back to the flat-activity power number instead, loudly).
# Chosen well below what a real run gets (hundreds of pins, see below) so it
# only trips if read_vcd finds essentially nothing -- e.g. a scope mismatch.
MIN_ANNOTATED_PINS = 20


def log(msg):
    print(f"[fp32-synth] {msg}", flush=True)


class Fp32Synthesizer:
    def __init__(self, clock_period, std_lib, ram_lib, source_dir, shared_dir,
                 generated_dir, work_dir, yosys_path, sta_path, timeout=1800):
        self.clock_period = clock_period
        self.std_lib = os.path.abspath(std_lib)
        self.ram_lib = os.path.abspath(ram_lib)
        self.source_dir = os.path.abspath(source_dir)
        self.shared_dir = os.path.abspath(shared_dir)
        self.generated_dir = os.path.abspath(generated_dir)
        self.work_dir = os.path.abspath(work_dir)
        self.yosys_path = yosys_path
        self.sta_path = sta_path
        self.timeout = timeout

        for path, label in ((self.std_lib, "standard-cell liberty"),
                             (self.ram_lib, "SRAM macro liberty")):
            if not os.path.isfile(path):
                raise SystemExit(f"{label} not found: {path}")
        for f in ("agu.v", "bit_reversal.v"):
            if not os.path.isfile(os.path.join(self.shared_dir, f)):
                raise SystemExit(f"{f} not found in {self.shared_dir}")

    # ------------------------------------------------------------------
    def core_and_top_files(self, n):
        d = os.path.join(self.generated_dir, f"fp32_fft_{n}")
        core = os.path.join(d, f"fp32_fft_{n}_core.v")
        top = os.path.join(d, f"fp32_fft_{n}_top.v")
        if not os.path.isfile(core) or not os.path.isfile(top):
            raise FileNotFoundError(
                f"{core} / {top} missing -- run fp32_template_generator.py first")
        return core, top

    def collect_sources(self, n):
        core, top = self.core_and_top_files(n)
        sources = sorted(
            f for f in
            [os.path.join(self.source_dir, f) for f in os.listdir(self.source_dir)]
            if f.endswith(".v")
        )
        sources += [
            os.path.join(self.shared_dir, "agu.v"),
            os.path.join(self.shared_dir, "bit_reversal.v"),
            core,
            top,
        ]
        return sources

    # ------------------------------------------------------------------
    def run_yosys(self, n, sources, work_dir, flatten=True, tag="netlist"):
        """`flatten=True` (the default) is what area/timing are measured from
        -- unchanged from before. `flatten=False` produces a second netlist
        that keeps RTL module/instance names intact, used ONLY for VCD-based
        power annotation: a fully flattened netlist matches only a handful of
        `read_vcd` pin names against the RTL testbench's VCD (~2% of nets in
        testing), because `abc`/`opt -purge` erase most internal names when
        everything is squashed into one module. Keeping hierarchy preserves
        submodule instance paths and ports, which pushes real match coverage
        up roughly 10x, without reintroducing the dangling
        `wire signed [31:0] i`-style declarations that `opt -purge` /
        `opt_clean -purge` / `setundef -zero -undriven` exist to remove (that
        fix does not depend on flatten -- verified empirically)."""
        top_module = f"fp32_fft_{n}_top"
        netlist_v = os.path.join(work_dir, f"fp32_fft_{n}_{tag}.v")
        yosys_log = os.path.join(work_dir, f"yosys_{tag}.log")
        script_path = os.path.join(work_dir, f"fp32_fft_{n}_synth_{tag}.ys")

        flatten_cmd = "flatten -noscopeinfo\n            " if flatten else ""
        read_cmds = "\n".join(f"read_verilog -sv {f}" for f in sources)
        yosys_script = textwrap.dedent(f"""\
            {read_cmds}
            # PRE-SYNTHESIS BLACKBOX
            blackbox {SRAM_MACRO_MODULE}

            hierarchy -check -top {top_module}
            {flatten_cmd}proc
            opt -purge
            memory
            opt -purge

            async2sync
            techmap
            opt -purge
            dfflibmap -liberty {self.std_lib}
            abc -liberty {self.std_lib} -g cmos
            opt_clean -purge
            setundef -zero -undriven

            stat -liberty {self.std_lib} -liberty {self.ram_lib}
            write_verilog -noattr {netlist_v}
        """)
        with open(script_path, "w") as f:
            f.write(yosys_script)

        cmd = [self.yosys_path, "-l", yosys_log, script_path]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=self.timeout, cwd=work_dir)
            if result.returncode != 0:
                log(f"Yosys FAILED for fp32_fft_{n} ({tag}). stderr tail: {result.stderr[-500:]}")
                return None, yosys_log
            return netlist_v, yosys_log
        except Exception as e:
            log(f"Yosys invocation error for fp32_fft_{n} ({tag}): {e}")
            return None, yosys_log

    def parse_yosys_area(self, yosys_log, fallback=None):
        if fallback is None:
            fallback = MAX_AREA_UM2 * 2
        if not os.path.exists(yosys_log):
            return fallback
        try:
            with open(yosys_log, "r", errors="replace") as fh:
                content = fh.read()
            pat = re.compile(
                r"Chip area for (?:module|top module)\s+[\'\"]?[^\'\"\n]+[\'\"]?\s*:\s*([\d.eE+\-]+)",
                re.IGNORECASE)
            matches = pat.findall(content)
            if matches:
                return float(matches[-1])
        except Exception as e:
            log(f"Error parsing Yosys log {yosys_log}: {e}")
        return fallback

    # ------------------------------------------------------------------
    def run_opensta(self, n, netlist_v, work_dir):
        top_module = f"fp32_fft_{n}_top"
        timing_rpt = os.path.join(work_dir, f"fp32_fft_{n}_timing.rpt")
        power_rpt = os.path.join(work_dir, f"fp32_fft_{n}_power.rpt")
        sta_log = os.path.join(work_dir, "sta.log")
        script_path = os.path.join(work_dir, f"fp32_fft_{n}_sta.tcl")

        sta_script = textwrap.dedent(f"""\
            read_liberty {self.std_lib}
            read_liberty {self.ram_lib}
            read_verilog {netlist_v}
            link_design {top_module}
            create_clock -name {CLOCK_NET_NAME} -period {self.clock_period} [get_ports {CLOCK_NET_NAME}]
            set_input_delay  [expr {{{self.clock_period}}} / 4.0] -clock {CLOCK_NET_NAME} [all_inputs]
            set_output_delay [expr {{{self.clock_period}}} / 4.0] -clock {CLOCK_NET_NAME} [all_outputs]
            # rst is an asynchronous, once-per-run control input, not a per-cycle
            # data signal. async2sync (in the Yosys step) rewrites each async-reset
            # flip-flop as a synchronous-equivalent FF with a reset mux on its D
            # pin, so rst legitimately drives a same-cycle combinational cone in
            # the synthesized netlist -- left unconstrained, report_checks picks
            # that reset fan-out (through the AGU/control logic) as the worst
            # path, which is longer than and unrelated to the real datapath
            # timing this report exists to measure.
            set_false_path -from [get_ports rst]
            report_checks -path_delay max -format full_clock_expanded > {timing_rpt}
            set_power_activity -input -activity 0.2
            report_power > {power_rpt}
            exit
        """)
        with open(script_path, "w") as f:
            f.write(sta_script)

        cmd = [self.sta_path, "-no_init", "-no_splash", "-exit", script_path]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=600, cwd=work_dir)
            with open(sta_log, "w") as f:
                f.write(result.stdout)
                f.write(result.stderr)
            if result.returncode != 0:
                log(f"OpenSTA FAILED for fp32_fft_{n}. stderr tail: {result.stderr[-500:]}")
                return False, timing_rpt, power_rpt
            return True, timing_rpt, power_rpt
        except Exception as e:
            log(f"OpenSTA invocation error for fp32_fft_{n}: {e}")
            return False, timing_rpt, power_rpt

    # ------------------------------------------------------------------
    # Real-activity power: simulate the RTL testbench (same 11 signals used
    # for SQNR) with $dumpvars on, then hand that VCD to OpenSTA's `read_vcd`
    # against the HIERARCHY-PRESERVED netlist from run_yosys(..., flatten=False).
    # This replaces the flat `set_power_activity -input -activity 0.2` guess
    # with switching activity measured from real signal content, at whatever
    # match coverage `read_vcd` reports (see MIN_ANNOTATED_PINS).
    # ------------------------------------------------------------------
    def generate_activity_vcd(self, n, work_dir):
        design_name = f"fp32_fft_{n}"
        core, _top = self.core_and_top_files(n)
        ev = FP32PerformanceEvaluator(n, shared_sources_dir=self.shared_dir,
                                      sim_dir=work_dir, dump_vcd=True)
        result = ev.run_verilog_simulation(core, design_name)
        if result is None:
            log(f"Activity VCD simulation FAILED for {design_name}")
            return None
        vcd_file = ev.vcd_path(design_name)
        return vcd_file if os.path.isfile(vcd_file) else None

    _ANNOTATED_RE = re.compile(r"Annotated\s+(\d+)\s+pin activit")

    def run_opensta_vcd_power(self, n, hier_netlist_v, vcd_file, work_dir):
        top_module = f"fp32_fft_{n}_top"
        power_rpt = os.path.join(work_dir, f"fp32_fft_{n}_power_vcd.rpt")
        sta_log = os.path.join(work_dir, "sta_vcd.log")
        script_path = os.path.join(work_dir, f"fp32_fft_{n}_sta_vcd.tcl")

        sta_script = textwrap.dedent(f"""\
            read_liberty {self.std_lib}
            read_liberty {self.ram_lib}
            read_verilog {hier_netlist_v}
            link_design {top_module}
            create_clock -name {CLOCK_NET_NAME} -period {self.clock_period} [get_ports {CLOCK_NET_NAME}]
            read_vcd -scope tb_{top_module[:-4]}/dut {vcd_file}
            report_power > {power_rpt}
            exit
        """)
        with open(script_path, "w") as f:
            f.write(sta_script)

        cmd = [self.sta_path, "-no_init", "-no_splash", "-exit", script_path]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=600, cwd=work_dir)
            with open(sta_log, "w") as f:
                f.write(result.stdout)
                f.write(result.stderr)
            if result.returncode != 0:
                log(f"OpenSTA (VCD power) FAILED for fp32_fft_{n}. stderr tail: {result.stderr[-500:]}")
                return None, 0
            m = self._ANNOTATED_RE.search(result.stdout)
            annotated = int(m.group(1)) if m else 0
            return power_rpt, annotated
        except Exception as e:
            log(f"OpenSTA (VCD power) invocation error for fp32_fft_{n}: {e}")
            return None, 0

    def parse_opensta_power(self, power_rpt, fallback=None):
        if fallback is None:
            fallback = MAX_POWER_MW * 2
        if not os.path.exists(power_rpt):
            return fallback
        try:
            with open(power_rpt, "r") as fh:
                content = fh.read()
            pat = re.compile(
                r"^\s*Total\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)",
                re.MULTILINE)
            for m in pat.finditer(content):
                return float(m.group(4)) * 1000.0
        except Exception as e:
            log(f"Error parsing OpenSTA power report: {e}")
        return fallback

    def parse_opensta_timing(self, timing_rpt, fallback_delay=200.0, fallback_slack=-1.0):
        if not os.path.exists(timing_rpt):
            return fallback_delay, fallback_slack
        slack_vals, arr_vals = [], []
        try:
            with open(timing_rpt, "r") as fh:
                for line in fh:
                    line_lower = line.lower()
                    if "slack" in line_lower:
                        m = re.search(r"([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)", line)
                        if m:
                            slack_vals.append(float(m.group(1)))
                    elif "data arrival time" in line_lower:
                        m = re.search(r"([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)", line)
                        if m:
                            arr_vals.append(float(m.group(1)))
            if slack_vals:
                slack_ns = min(slack_vals)
                return max(self.clock_period - slack_ns, 0.0), slack_ns
            if arr_vals:
                crit_delay_ns = max(arr_vals)
                return max(crit_delay_ns, 0.0), self.clock_period - crit_delay_ns
        except Exception as e:
            log(f"Error parsing OpenSTA timing report: {e}")
        return fallback_delay, fallback_slack

    # ------------------------------------------------------------------
    def normalized_latency(self, crit_delay_ns, num_stages):
        if crit_delay_ns <= 0 or math.isnan(crit_delay_ns) or math.isinf(crit_delay_ns):
            return 10.0
        norm = crit_delay_ns / self.clock_period
        pipeline_factor = max(1.0, num_stages / 6.0)
        return min(norm * pipeline_factor, 10.0)

    # ------------------------------------------------------------------
    def synthesize(self, n):
        log(f"=== fp32_fft_{n} ===")
        work_dir = os.path.join(self.work_dir, f"fp32_fft_{n}")
        os.makedirs(work_dir, exist_ok=True)

        sources = self.collect_sources(n)
        netlist_v, yosys_log = self.run_yosys(n, sources, work_dir)
        area_um2 = self.parse_yosys_area(yosys_log)

        if netlist_v is None:
            return {
                "n": n, "power_mw": MAX_POWER_MW * 2, "power_mw_flat": MAX_POWER_MW * 2,
                "power_source": "synth_failed", "annotated_pins": 0,
                "area_um2": area_um2,
                "crit_delay_ns": 200.0, "slack_ns": -1.0,
                "norm_latency": 10.0, "ok": False,
            }

        sta_ok, timing_rpt, power_rpt = self.run_opensta(n, netlist_v, work_dir)
        power_mw_flat = self.parse_opensta_power(power_rpt) if sta_ok else MAX_POWER_MW * 2
        crit_delay, slack_ns = self.parse_opensta_timing(timing_rpt) if sta_ok else (200.0, -1.0)

        # Real-activity power: hierarchy-preserved netlist + VCD from the RTL
        # testbench's own 11 representative signals. Falls back to the flat
        # 0.2 guess (loudly) if any step here doesn't produce a usable result,
        # since a failed/absent VCD must never silently look as good as a
        # measured one.
        power_mw = power_mw_flat
        power_source = "flat_0.2_fallback"
        annotated_pins = 0
        if sta_ok:
            hier_netlist_v, hier_yosys_log = self.run_yosys(
                n, sources, work_dir, flatten=False, tag="netlist_hier")
            vcd_file = self.generate_activity_vcd(n, work_dir) if hier_netlist_v else None
            if hier_netlist_v and vcd_file:
                vcd_power_rpt, annotated_pins = self.run_opensta_vcd_power(
                    n, hier_netlist_v, vcd_file, work_dir)
                if vcd_power_rpt and annotated_pins >= MIN_ANNOTATED_PINS:
                    power_mw = self.parse_opensta_power(vcd_power_rpt, fallback=power_mw_flat)
                    power_source = "vcd_measured"
                else:
                    log(f"fp32_fft_{n}: only {annotated_pins} pins annotated "
                        f"(< {MIN_ANNOTATED_PINS}) - keeping flat-activity power")
            else:
                log(f"fp32_fft_{n}: activity VCD unavailable - keeping flat-activity power")

        num_stages = int(math.log2(n)) if n > 1 else 1
        norm_latency = self.normalized_latency(crit_delay, num_stages)

        log(f"fp32_fft_{n}: P={power_mw:.4f}mW (source={power_source}, "
            f"annotated={annotated_pins}) A={area_um2:.1f}um^2 "
            f"CritDelay={crit_delay:.3f}ns Slack={slack_ns:.3f}ns NormLat={norm_latency:.3f}x")

        return {
            "n": n, "power_mw": power_mw, "power_mw_flat": power_mw_flat,
            "power_source": power_source, "annotated_pins": annotated_pins,
            "area_um2": area_um2,
            "crit_delay_ns": crit_delay, "slack_ns": slack_ns,
            "norm_latency": norm_latency, "ok": sta_ok,
        }


def _archive_synth_artifacts(synth_dir, work_dir, archive_name="fp32_synth_artifacts.zip"):
    """Zip everything the synthesis run produced (per-size work/ dirs) except
    the PPA report itself, mirroring the sim/perf/ cleanup convention."""
    if not os.path.isdir(work_dir):
        return

    archive_path = os.path.join(synth_dir, archive_name)
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(work_dir):
            for fname in files:
                full = os.path.join(root, fname)
                arcname = os.path.relpath(full, synth_dir)
                zf.write(full, arcname=arcname)

    shutil.rmtree(work_dir)


def _load_exec_cycles(cycles_file):
    """Parse the '  N | Exec cycles | ...' table sim/fp32_performance_evaluator.py
    writes to fp32_sqnr_results.txt into {N: exec_cycles}. A missing file, or a
    row whose Exec cycles field isn't numeric (a FAILED simulation), simply
    leaves that N absent from the result -- callers report Energy/FFT as N/A
    rather than guess a cycle count."""
    cycles = {}
    if not os.path.isfile(cycles_file):
        return cycles
    row_pat = re.compile(r"^\s*(\d+)\s*\|\s*(\d+)\s*\|")
    with open(cycles_file, "r") as f:
        for line in f:
            m = row_pat.match(line)
            if m:
                cycles[int(m.group(1))] = int(m.group(2))
    return cycles


def main():
    ap = argparse.ArgumentParser(
        description="FP32 baseline PPA extraction (Yosys + OpenSTA, mixed-evaluator methodology)")
    ap.add_argument("--sizes", type=int, nargs="*", default=ALL_SIZES)
    ap.add_argument("--clock-period", type=float, default=10.0,
                     help="Clock period in ns (default 10.0, matches the mixed-precision flow)")
    ap.add_argument("--std-lib", default=DEFAULT_STD_LIB)
    ap.add_argument("--ram-lib", default=DEFAULT_RAM_LIB)
    ap.add_argument("--source-dir", default=DEFAULT_SOURCE_DIR)
    ap.add_argument("--shared-dir", default=DEFAULT_SHARED_DIR,
                     help="directory with agu.v and bit_reversal.v")
    ap.add_argument("--generated-dir", default=DEFAULT_GENERATED_DIR)
    ap.add_argument("--work-dir", default=DEFAULT_WORK_DIR)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--cycles-file", default=DEFAULT_CYCLES_FILE,
                     help="Per-size ExecCycles table used for Energy/FFT "
                          "(default: sim/perf/fp32_sqnr_results.txt, written "
                          "by sim/fp32_performance_evaluator.py)")
    ap.add_argument("--yosys", default="yosys")
    ap.add_argument("--sta", default="sta")
    ap.add_argument("--keep-work", action="store_true",
                     help="Skip zipping/deleting the per-size work/ directories")
    args = ap.parse_args()

    synth = Fp32Synthesizer(
        clock_period=args.clock_period, std_lib=args.std_lib, ram_lib=args.ram_lib,
        source_dir=args.source_dir, shared_dir=args.shared_dir,
        generated_dir=args.generated_dir, work_dir=args.work_dir,
        yosys_path=args.yosys, sta_path=args.sta,
    )

    rows = [synth.synthesize(n) for n in args.sizes]

    exec_cycles_by_n = _load_exec_cycles(args.cycles_file)
    for r in rows:
        cycles = exec_cycles_by_n.get(r["n"])
        r["exec_cycles"] = cycles
        if cycles is not None and r["ok"]:
            r["energy_per_fft_nj"] = r["power_mw"] * cycles * args.clock_period / 1000.0
        else:
            r["energy_per_fft_nj"] = None

    hdr = (f"{'N':>6} | {'Power (mW)':>11} | {'PowerSrc':>13} | {'AnnotPins':>9} | "
           f"{'Area (um^2)':>12} | "
           f"{'CritDelay (ns)':>14} | {'Slack (ns)':>10} | {'NormLat':>8} | "
           f"{'ExecCyc':>8} | {'Energy/FFT (nJ)':>16} | {'Status':>6}")
    sep = "-" * len(hdr)
    lines = ["FP32 baseline - PPA extraction using the mixed-precision evaluator methodology",
             "(Yosys synthesis + area; OpenSTA timing + power; no OpenROAD P&R)",
             f"Clock period: {args.clock_period} ns",
             "Energy/FFT = Power(mW) * ExecCycles * ClockPeriod(ns) / 1000, "
             f"ExecCycles from {os.path.relpath(os.path.abspath(args.cycles_file), SYNTH_DIR)}",
             "Power is measured from a real RTL-simulation VCD (the same 11 signals used for "
             "SQNR) annotated onto a hierarchy-preserved netlist via OpenSTA's `read_vcd`, when "
             f"at least {MIN_ANNOTATED_PINS} pins match (PowerSrc=vcd_measured, AnnotPins shown); "
             "otherwise it falls back to a flat 0.2 input-activity guess (PowerSrc=flat_0.2_fallback) "
             "-- see run_yosys()/generate_activity_vcd() for why flattening breaks name matching.",
             "", hdr, sep]
    for r in rows:
        status = "OK" if r["ok"] else "FAILED"
        cyc_s = str(r["exec_cycles"]) if r["exec_cycles"] is not None else "N/A"
        e_s = f"{r['energy_per_fft_nj']:.3f}" if r["energy_per_fft_nj"] is not None else "N/A"
        lines.append(
            f"{r['n']:>6} | {r['power_mw']:>11.4f} | {r['power_source']:>13} | "
            f"{r['annotated_pins']:>9} | {r['area_um2']:>12.1f} | "
            f"{r['crit_delay_ns']:>14.3f} | {r['slack_ns']:>10.3f} | "
            f"{r['norm_latency']:>8.3f} | {cyc_s:>8} | {e_s:>16} | {status:>6}")
    text = "\n".join(lines) + "\n"

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(text)
    print("\n" + text + f"table: {args.out}")

    if not args.keep_work:
        _archive_synth_artifacts(SYNTH_DIR, args.work_dir)


if __name__ == "__main__":
    main()
