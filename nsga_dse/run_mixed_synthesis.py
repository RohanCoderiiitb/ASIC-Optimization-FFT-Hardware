#!/usr/bin/env python3
"""
Mixed-Precision FFT ASIC Synthesis -- Yosys + OpenSTA
======================================================
Mixed-precision (FP4/FP8, chromosome-selected per-stage) counterpart of
fp16_baseline/synth/run_fp16_synthesis.py / fp32_baseline/synth/
run_fp32_synthesis.py. Same PPA methodology (Yosys area/timing, SAIF-based
power via a real RTL-simulation VCD converted with vcd_to_saif.py), applied
to a FIXED reference chromosome (all-FP8, the highest-precision corner of the
NSGA-II search space -- see ALL_FP8_CHROMOSOME) so the PPA numbers are
directly comparable across FFT sizes for this design, the same way the
fp16/fp32 baselines are comparable across sizes for their own fixed
precision.

Unlike the baselines, there is no persistent generated_cores/ directory here
-- the mixed core/top are chromosome-dependent, so this script generates them
itself via FFTTemplateGenerator before handing off to Yosys.

Usage (from repo root):
    python3 run_mixed_synthesis.py                # all 10 sizes
    python3 run_mixed_synthesis.py --sizes 16 1024
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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from fft_template_generator import FFTTemplateGenerator  # noqa: E402
from performance_evaluator import PerformanceEvaluator  # noqa: E402
from vcd_to_saif import vcd_to_saif  # noqa: E402

ALL_SIZES = [2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]

DEFAULT_STD_LIB = os.path.join(REPO_ROOT, "45_nm_PDK", "cadence", "cadence_45nm",
                                "lib", "fast_vdd1v0_basicCells.lib")
DEFAULT_RAM_LIB = os.path.join(REPO_ROOT, "openram_outputs",
                                "sram_512x24_2rw_TT_1p0V_25C.lib")
DEFAULT_SHARED_DIR = os.path.join(REPO_ROOT, "verilog_sources")
DEFAULT_GENERATED_DIR = os.path.join(REPO_ROOT, "generated_designs")
DEFAULT_WORK_DIR = os.path.join(REPO_ROOT, "synth_work")
DEFAULT_OUT = os.path.join(REPO_ROOT, "mixed_ppa_report.txt")

SRAM_MACRO_MODULE = "sram_512x24_2rw"
CLOCK_NET_NAME = "clk"

MAX_POWER_MW = 500.0
MAX_AREA_UM2 = 600000.0

# Same floor as the fp16/fp32 baselines -- see run_fp16_synthesis.py.
MIN_ANNOTATED_PINS = 20


def all_fp8_chromosome(n):
    """All-FP8 (highest precision) reference chromosome for size n -- see
    module docstring for why this fixed point in the search space is used."""
    num_stages = int(math.log2(n))
    return [1] * (2 * num_stages)


def log(msg):
    print(f"[mixed-synth] {msg}", flush=True)


class MixedSynthesizer:
    def __init__(self, clock_period, std_lib, ram_lib, shared_dir,
                 generated_dir, work_dir, yosys_path, sta_path, timeout=1800):
        self.clock_period = clock_period
        self.std_lib = os.path.abspath(std_lib)
        self.ram_lib = os.path.abspath(ram_lib)
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

    # ------------------------------------------------------------------
    def core_and_top_files(self, n, chromosome):
        # NOTE: the core FILE must be named exactly "mixed_fft_{n}.v" (no
        # "_core" suffix in the filename) even though the MODULE inside it
        # is "mixed_fft_{n}_core" -- generate_verilog() derives both the
        # core and top module names from the output file's stem, and
        # run_verilog_simulation()/PerformanceEvaluator derive the expected
        # top FILE path as f"{design_name}_top.v" by string-replacing '.v'
        # on this same file's path. This mirrors the exact convention
        # objectiveEvaluationFFT.py's NSGA-II loop already uses
        # (design_name = f"fft_{fft_size}_sol{sol_id}_gen{gen}",
        # core_file = f"{design_name}.v").
        d = os.path.join(self.generated_dir, f"mixed_fft_{n}")
        os.makedirs(d, exist_ok=True)
        core = os.path.join(d, f"mixed_fft_{n}.v")
        gen = FFTTemplateGenerator(n)
        core, top = gen.generate_verilog(chromosome, core)
        return core, top

    def collect_sources(self, n, chromosome):
        core, top = self.core_and_top_files(n, chromosome)
        sources = sorted(
            f for f in
            [os.path.join(self.shared_dir, f) for f in os.listdir(self.shared_dir)]
            if f.endswith(".v")
        )
        sources += [core, top]
        return sources

    # ------------------------------------------------------------------
    def run_yosys(self, n, sources, work_dir, flatten=True, tag="netlist"):
        """See run_fp16_synthesis.py's run_yosys docstring -- identical
        rationale: flatten=True for area/timing, flatten=False (hierarchy
        preserved) for VCD/SAIF-based power name matching."""
        top_module = f"mixed_fft_{n}_top"
        netlist_v = os.path.join(work_dir, f"mixed_fft_{n}_{tag}.v")
        yosys_log = os.path.join(work_dir, f"yosys_{tag}.log")
        script_path = os.path.join(work_dir, f"mixed_fft_{n}_synth_{tag}.ys")

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
                log(f"Yosys FAILED for mixed_fft_{n} ({tag}). stderr tail: {result.stderr[-500:]}")
                return None, yosys_log
            return netlist_v, yosys_log
        except Exception as e:
            log(f"Yosys invocation error for mixed_fft_{n} ({tag}): {e}")
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
        """Timing only -- see run_fp16_synthesis.py's run_opensta docstring."""
        top_module = f"mixed_fft_{n}_top"
        timing_rpt = os.path.join(work_dir, f"mixed_fft_{n}_timing.rpt")
        sta_log = os.path.join(work_dir, "sta.log")
        script_path = os.path.join(work_dir, f"mixed_fft_{n}_sta.tcl")

        sta_script = textwrap.dedent(f"""\
            read_liberty {self.std_lib}
            read_liberty {self.ram_lib}
            read_verilog {netlist_v}
            link_design {top_module}
            create_clock -name {CLOCK_NET_NAME} -period {self.clock_period} [get_ports {CLOCK_NET_NAME}]
            set_input_delay  [expr {{{self.clock_period}}} / 4.0] -clock {CLOCK_NET_NAME} [all_inputs]
            set_output_delay [expr {{{self.clock_period}}} / 4.0] -clock {CLOCK_NET_NAME} [all_outputs]
            set_false_path -from [get_ports rst]
            report_checks -path_delay max -format full_clock_expanded > {timing_rpt}
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
                log(f"OpenSTA FAILED for mixed_fft_{n}. stderr tail: {result.stderr[-500:]}")
                return False, timing_rpt
            return True, timing_rpt
        except Exception as e:
            log(f"OpenSTA invocation error for mixed_fft_{n}: {e}")
            return False, timing_rpt

    # ------------------------------------------------------------------
    def generate_activity_saif(self, n, chromosome, work_dir):
        design_name = f"mixed_fft_{n}"
        core, _top = self.core_and_top_files(n, chromosome)
        ev = PerformanceEvaluator(n, dump_vcd=True)
        # PerformanceEvaluator hardcodes './verilog_sources' and './sim'
        # (resolved relative to CWD at call time, matching how the NSGA-II
        # optimization loop already uses it) -- pin CWD to the repo root
        # regardless of how this script itself was invoked, so those
        # resolve correctly no matter what work_dir is.
        cwd = os.getcwd()
        try:
            os.chdir(REPO_ROOT)
            result = ev.run_verilog_simulation(core, design_name)
        finally:
            os.chdir(cwd)
        if result is None:
            log(f"Activity VCD simulation FAILED for {design_name}")
            return None
        vcd_file = ev.vcd_path(design_name)
        if not os.path.isfile(vcd_file):
            log(f"Activity VCD missing after simulation for {design_name}")
            return None
        saif_file = os.path.join(work_dir, f"{design_name}.saif")
        try:
            net_count, duration = vcd_to_saif(vcd_file, saif_file, design_name=f"tb_{design_name}")
        except Exception as e:
            log(f"VCD->SAIF conversion FAILED for {design_name}: {e}")
            return None
        if net_count == 0 or duration <= 0:
            log(f"VCD->SAIF conversion produced an empty/degenerate SAIF for "
                f"{design_name} (nets={net_count}, duration={duration})")
            return None
        return saif_file

    _ANNOT_RE = re.compile(r"^\s*saif\s+(\d+)", re.MULTILINE)
    _UNANNOT_RE = re.compile(r"^\s*unannotated\s+(\d+)", re.MULTILINE)

    def run_opensta_saif_power(self, n, hier_netlist_v, saif_file, work_dir):
        top_module = f"mixed_fft_{n}_top"
        power_rpt = os.path.join(work_dir, f"mixed_fft_{n}_power_saif.rpt")
        annot_rpt = os.path.join(work_dir, f"mixed_fft_{n}_activity_annotation.rpt")
        sta_log = os.path.join(work_dir, "sta_saif.log")
        script_path = os.path.join(work_dir, f"mixed_fft_{n}_sta_saif.tcl")

        sta_script = textwrap.dedent(f"""\
            read_liberty {self.std_lib}
            read_liberty {self.ram_lib}
            read_verilog {hier_netlist_v}
            link_design {top_module}
            create_clock -name {CLOCK_NET_NAME} -period {self.clock_period} [get_ports {CLOCK_NET_NAME}]
            read_saif -scope tb_{top_module[:-4]}/dut {saif_file}
            report_activity_annotation > {annot_rpt}
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
                log(f"OpenSTA (SAIF power) FAILED for mixed_fft_{n}. stderr tail: {result.stderr[-500:]}")
                return None, 0, 0, result.stderr[-500:]
            annotated, total = self._parse_activity_annotation(annot_rpt)
            warnings = "\n".join(
                line for line in (result.stdout.splitlines() + result.stderr.splitlines())
                if "warn" in line.lower() or "error" in line.lower())
            return power_rpt, annotated, total, warnings
        except Exception as e:
            log(f"OpenSTA (SAIF power) invocation error for mixed_fft_{n}: {e}")
            return None, 0, 0, str(e)

    def _parse_activity_annotation(self, annot_rpt):
        if not os.path.exists(annot_rpt):
            return 0, 0
        try:
            with open(annot_rpt, "r", errors="replace") as fh:
                content = fh.read()
            am = self._ANNOT_RE.search(content)
            um = self._UNANNOT_RE.search(content)
            annotated = int(am.group(1)) if am else 0
            unannotated = int(um.group(1)) if um else 0
            return annotated, annotated + unannotated
        except Exception as e:
            log(f"Error parsing activity annotation report {annot_rpt}: {e}")
            return 0, 0

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
    def synthesize(self, n, chromosome=None):
        chromosome = chromosome if chromosome is not None else all_fp8_chromosome(n)
        log(f"=== mixed_fft_{n} ===")
        work_dir = os.path.join(self.work_dir, f"mixed_fft_{n}")
        os.makedirs(work_dir, exist_ok=True)

        sources = self.collect_sources(n, chromosome)
        netlist_v, yosys_log = self.run_yosys(n, sources, work_dir)
        area_um2 = self.parse_yosys_area(yosys_log)

        if netlist_v is None:
            return {
                "n": n, "power_mw": MAX_POWER_MW * 2,
                "power_source": "SYNTH_FAILED", "annotated_pins": 0, "total_pins": 0,
                "area_um2": area_um2,
                "crit_delay_ns": 200.0, "slack_ns": -1.0,
                "norm_latency": 10.0, "ok": False,
            }

        sta_ok, timing_rpt = self.run_opensta(n, netlist_v, work_dir)
        crit_delay, slack_ns = self.parse_opensta_timing(timing_rpt) if sta_ok else (200.0, -1.0)

        power_mw = MAX_POWER_MW * 2
        power_source = "SAIF_ANNOTATION_FAILED"
        annotated_pins = 0
        total_pins = 0
        power_ok = False
        if sta_ok:
            hier_netlist_v, _hier_yosys_log = self.run_yosys(
                n, sources, work_dir, flatten=False, tag="netlist_hier")
            saif_file = self.generate_activity_saif(n, chromosome, work_dir) if hier_netlist_v else None
            if hier_netlist_v and saif_file:
                power_rpt, annotated_pins, total_pins, warnings = self.run_opensta_saif_power(
                    n, hier_netlist_v, saif_file, work_dir)
                if warnings:
                    log(f"mixed_fft_{n}: SAIF annotation warnings/errors:\n{warnings}")
                if power_rpt and annotated_pins >= MIN_ANNOTATED_PINS:
                    power_mw = self.parse_opensta_power(power_rpt, fallback=MAX_POWER_MW * 2)
                    power_source = "saif_measured"
                    power_ok = True
                else:
                    log(f"mixed_fft_{n}: SAIF annotation only matched {annotated_pins} "
                        f"nets (< {MIN_ANNOTATED_PINS}) - marking FAILED, no fallback")
            else:
                log(f"mixed_fft_{n}: SAIF generation FAILED - marking FAILED, no fallback")

        ok = sta_ok and power_ok

        num_stages = int(math.log2(n)) if n > 1 else 1
        norm_latency = self.normalized_latency(crit_delay, num_stages)

        pct = (100.0 * annotated_pins / total_pins) if total_pins else 0.0
        log(f"mixed_fft_{n}: P={power_mw:.4f}mW (source={power_source}, "
            f"annotated={annotated_pins}/{total_pins}={pct:.2f}%) A={area_um2:.1f}um^2 "
            f"CritDelay={crit_delay:.3f}ns Slack={slack_ns:.3f}ns NormLat={norm_latency:.3f}x "
            f"Status={'OK' if ok else 'FAILED'}")

        return {
            "n": n, "power_mw": power_mw,
            "power_source": power_source, "annotated_pins": annotated_pins,
            "total_pins": total_pins,
            "area_um2": area_um2,
            "crit_delay_ns": crit_delay, "slack_ns": slack_ns,
            "norm_latency": norm_latency, "ok": ok,
        }


def _archive_synth_artifacts(root_dir, work_dir, archive_name="mixed_synth_artifacts.zip"):
    if not os.path.isdir(work_dir):
        return
    archive_path = os.path.join(root_dir, archive_name)
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(work_dir):
            for fname in files:
                full = os.path.join(root, fname)
                zf.write(full, arcname=os.path.relpath(full, root_dir))
    shutil.rmtree(work_dir)


def main():
    ap = argparse.ArgumentParser(
        description="Mixed-precision FFT PPA extraction (Yosys + OpenSTA, all-FP8 reference chromosome)")
    ap.add_argument("--sizes", type=int, nargs="*", default=ALL_SIZES)
    ap.add_argument("--clock-period", type=float, default=10.0)
    ap.add_argument("--std-lib", default=DEFAULT_STD_LIB)
    ap.add_argument("--ram-lib", default=DEFAULT_RAM_LIB)
    ap.add_argument("--shared-dir", default=DEFAULT_SHARED_DIR)
    ap.add_argument("--generated-dir", default=DEFAULT_GENERATED_DIR)
    ap.add_argument("--work-dir", default=DEFAULT_WORK_DIR)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--yosys", default="yosys")
    ap.add_argument("--sta", default="sta")
    ap.add_argument("--keep-work", action="store_true")
    args = ap.parse_args()

    synth = MixedSynthesizer(
        clock_period=args.clock_period, std_lib=args.std_lib, ram_lib=args.ram_lib,
        shared_dir=args.shared_dir, generated_dir=args.generated_dir,
        work_dir=args.work_dir, yosys_path=args.yosys, sta_path=args.sta,
    )

    rows = [synth.synthesize(n) for n in args.sizes]

    for r in rows:
        r["exec_cycles"] = None
        r["energy_per_fft_nj"] = None

    hdr = (f"{'N':>6} | {'Area (um^2)':>12} | {'CritDelay (ns)':>14} | "
           f"{'Slack (ns)':>10} | {'Power (mW)':>11} | "
           f"{'ActivitySrc':>21} | {'AnnotatedSignals':>24} | "
           f"{'NormLat':>8} | {'Status':>7}")
    sep = "-" * len(hdr)
    lines = ["Mixed-precision FFT (all-FP8 reference chromosome) - PPA extraction",
             "(Yosys synthesis + area; OpenSTA timing; no OpenROAD P&R)",
             f"Clock period: {args.clock_period} ns",
             "Power is measured from a SAIF file converted (vcd_to_saif.py) from a real "
             "RTL-simulation VCD annotated by OpenSTA's `read_saif` onto a hierarchy-preserved "
             f"netlist (ActivitySrc=saif_measured) when at least {MIN_ANNOTATED_PINS} nets match "
             "(AnnotatedSignals=annotated/total). No flat-activity fallback -- see "
             "run_fp16_synthesis.py for the shared methodology this mirrors.",
             "", hdr, sep]
    for r in rows:
        status = "OK" if r["ok"] else "FAILED"
        power_s = f"{r['power_mw']:.4f}" if r["ok"] else "N/A"
        pct = (100.0 * r["annotated_pins"] / r["total_pins"]) if r["total_pins"] else 0.0
        annot_s = f"{r['annotated_pins']}/{r['total_pins']} ({pct:.2f}%)"
        lines.append(
            f"{r['n']:>6} | {r['area_um2']:>12.1f} | {r['crit_delay_ns']:>14.3f} | "
            f"{r['slack_ns']:>10.3f} | {power_s:>11} | "
            f"{r['power_source']:>21} | {annot_s:>24} | "
            f"{r['norm_latency']:>8.3f} | {status:>7}")
    text = "\n".join(lines) + "\n"

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(text)
    print("\n" + text + f"table: {args.out}")

    if not args.keep_work:
        _archive_synth_artifacts(REPO_ROOT, args.work_dir)


if __name__ == "__main__":
    main()
