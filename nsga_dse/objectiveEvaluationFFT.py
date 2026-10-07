"""
Objective Evaluation for Mixed-Precision FFT Optimization
Uses open-source EDA tools (Yosys + OpenSTA) for Area/Power/Timing extraction.
Simulation tracking (execution cycles) is strictly maintained.

Objectives are [energy_nJ_perFFT, sqnr_error^2, norm_latency] (see
energyObjective.py). Area and total/dynamic/static power are still measured
via Yosys+OpenSTA every generation and reported in the results dict, CSVs and
per-solution logs - they are just no longer part of the objective vector.
"""

import numpy as np
import subprocess
import os
import re
import hashlib
import math
import textwrap
from pymoo.core.problem import Problem
from concurrent.futures import ThreadPoolExecutor, as_completed

from globalVariablesMixedFFT import *
from fft_template_generator import FFTTemplateGenerator
from performance_evaluator import PerformanceEvaluator
from energyObjective import (energy_objectives, penalty_objectives,
                             ENERGY_OBJECTIVES, NUM_CONSTRAINTS)
from postroute_pnr import PostRoutePnR
from vcd_to_saif import vcd_to_saif
from run_mixed_postroute import (
    TECH_LEF as MIXED_TECH_LEF, CELL_LEF as MIXED_CELL_LEF,
    SRAM_LEF as MIXED_SRAM_LEF, MACRO_MODULE as MIXED_MACRO_MODULE,
    MACRO_INSTANCES as MIXED_MACRO_INSTANCES, MACRO_W as MIXED_MACRO_W,
    MACRO_H as MIXED_MACRO_H,
)
from run_mixed_synthesis import MIN_ANNOTATED_PINS

# Shared, cached fixed-LEF directory (see postroute_pnr.py's prepare_fixed_lefs)
# -- reused across every solution/generation instead of re-fixing the same
# tech/SRAM LEF files per evaluation. Same directory run_mixed_postroute.py
# uses standalone, so an existing cache from that script is reused too.
PNR_FIXED_LEF_DIR = os.path.abspath("./_mixed_pnr_fixed_lef")

class MixedPrecisionFFTProblem(Problem):
    def __init__(self, fft_size=8, **kwargs):
        if OBJECTIVES != ENERGY_OBJECTIVES:
            raise ValueError(
                f"OBJECTIVES={OBJECTIVES} in globalVariablesMixedFFT.py but "
                f"energyObjective supplies {ENERGY_OBJECTIVES}. These must "
                f"agree or pymoo will silently mis-shape the objective array.")
        self.fft_size     = fft_size
        self.template_gen = FFTTemplateGenerator(fft_size)
        # dump_vcd=True: the RTL simulation this evaluator runs for SQNR is
        # reused to produce the VCD that post-route power annotation needs
        # (see _generate_activity_saif) -- avoids a second simulation run
        # per solution just to get switching activity.
        self.perf_eval    = PerformanceEvaluator(fft_size, dump_vcd=True)

        chrom_length = self.template_gen.get_chromosome_length()

        super().__init__(
            n_var=chrom_length,
            n_obj=OBJECTIVES,           # energy, sqnr_error^2, latency
            n_ieq_constr=NUM_CONSTRAINTS,  # area, energy cap, SQNR floor, timing (slack >= 0)
            xl=[0] * chrom_length,
            xu=[1] * chrom_length,
            vtype=int,
            elementwise_evaluation=False,
            **kwargs
        )

        log_message(f"Initialized FFT-{fft_size} problem with post-route "
                    f"OpenROAD P&R + SAIF-measured power: 3 objectives "
                    f"(energy/FFT, SQNR error^2, latency); area and power "
                    f"are constraints/reported only")

    def _evaluate(self, X, out, *args, **kwargs):
        global CURRENT_GEN
        log_message(f"=== Generation {CURRENT_GEN} ===", level='GEN')
        with open('generation.txt', 'w') as f:
            f.write(str(CURRENT_GEN))
        CURRENT_GEN += 1

        F = [None] * len(X)
        G = [None] * len(X)

        with ThreadPoolExecutor(max_workers=SOLUTION_THREADS) as executor:
            futures = {executor.submit(self.evaluate_solution, X[i], i): i for i in range(len(X))}
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    f_vals, g_vals = future.result()
                    F[idx] = f_vals
                    G[idx] = g_vals
                except Exception as e:
                    log_message(f"Solution {idx} failed: {e}", level='ERROR')
                    pf, pg = penalty_objectives()
                    F[idx] = pf
                    G[idx] = pg

        out["F"] = np.array(F)
        out["G"] = np.array(G)
        log_message(f"Generation {CURRENT_GEN-1} complete")

    def evaluate_solution(self, chromosome, sol_id):
        log_message(f"Evaluating solution {sol_id}: {[int(x) for x in chromosome]}")

        chrom_hash = self._hash_chromosome(chromosome)
        if ENABLE_RESULT_CACHE and chrom_hash in RESULT_CACHE:
            return self._compute_objectives_and_constraints(RESULT_CACHE[chrom_hash])

        design_name = f"fft_{self.fft_size}_sol{sol_id}_gen{CURRENT_GEN}"

        core_file = os.path.join(GENERATED_DESIGNS_DIR, f"{design_name}.v")
        core_file, top_file = self.template_gen.generate_verilog(chromosome, core_file)

        # Run RTL simulation FIRST (self.perf_eval has dump_vcd=True, so this
        # also produces a VCD) -- the post-route P&R below reuses that same
        # VCD for SAIF-based power annotation instead of re-simulating.
        perf = self._run_performance_evaluation(core_file, design_name, chromosome)
        sqnr           = perf['sqnr']
        avg_exec_cycles = perf['avg_exec_cycles']
        tot_sim_cycles  = perf['tot_sim_cycles']

        (power_mw, dyn_power_mw, static_power_mw, area_um2, crit_delay,
         slack_ns) = self._run_postroute_pnr(design_name, core_file, top_file)

        norm_latency = self._compute_actual_normalized_latency(crit_delay)

        results = {
            'power':            power_mw,
            'dyn_power_mw':     dyn_power_mw,
            'static_power_mw':  static_power_mw,
            'area':             area_um2,
            'sqnr':             sqnr,
            'norm_latency':     norm_latency,
            'crit_delay_ns':    crit_delay,
            'slack_ns':         slack_ns,
            'avg_exec_cycles':  avg_exec_cycles,
            'tot_sim_cycles':   tot_sim_cycles,
        }

        RESULT_CACHE[chrom_hash] = results
        objs, cons = self._compute_objectives_and_constraints(results)
        self._save_solution_result(sol_id, chromosome, results)

        stats = self.template_gen.analyze_chromosome_statistics(chromosome)
        log_message(
            f"Solution {sol_id}: E={results.get('energy_nj_per_fft', -1.0):.4f} nJ/FFT "
            f"(dyn={dyn_power_mw:.4f}mW static={static_power_mw:.4f}mW "
            f"total={power_mw:.4f}mW), A={area_um2} µm², SQNR={sqnr:.2f}dB, "
            f"CritDelay={crit_delay:.3f}ns -> NormLat={norm_latency:.3f}x, "
            f"ExecCycles={avg_exec_cycles}, TotSimCycles={tot_sim_cycles}"
        )

        return objs, cons

    def _hash_chromosome(self, chromosome):
        return hashlib.md5(''.join(map(str, chromosome)).encode()).hexdigest()

    # ------------------------------------------------------------------
    # Post-route PPA (OpenROAD P&R + SAIF-measured power), replacing the
    # pre-route Yosys+OpenSTA flat-activity estimate in _run_yosys_opensta
    # (kept below, unused, in case a fast pre-route mode is wanted again).
    # Mirrors run_mixed_postroute.py's methodology exactly, adapted to reuse
    # this problem's own per-solution generated core/top and RTL-simulation
    # VCD instead of regenerating them under a separate naming convention.
    # ------------------------------------------------------------------
    def _generate_activity_saif(self, design_name, work_dir):
        """Converts the VCD self.perf_eval already produced (dump_vcd=True,
        via _run_performance_evaluation, called before this) into a SAIF
        file for post-route power annotation -- no second simulation run."""
        vcd_file = self.perf_eval.vcd_path(design_name)
        if not os.path.isfile(vcd_file):
            log_message(f"Activity VCD missing for {design_name} (expected "
                        f"at {vcd_file}) -- was _run_performance_evaluation "
                        f"called first?", level='ERROR')
            return None
        saif_file = os.path.join(work_dir, f"{design_name}.saif")
        try:
            net_count, duration = vcd_to_saif(vcd_file, saif_file,
                                               design_name=f"tb_{design_name}")
        except Exception as e:
            log_message(f"VCD->SAIF conversion FAILED for {design_name}: {e}", level='ERROR')
            return None
        finally:
            # VCDs are large and only needed to produce the SAIF above --
            # delete right away so hundreds of generations don't fill disk.
            try:
                os.remove(vcd_file)
            except OSError:
                pass
        if net_count == 0 or duration <= 0:
            log_message(f"VCD->SAIF conversion produced an empty/degenerate "
                        f"SAIF for {design_name} (nets={net_count}, "
                        f"duration={duration})", level='ERROR')
            return None
        return saif_file

    _POWER_SPLIT_RE = re.compile(
        r"^\s*Total\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)",
        re.MULTILINE)

    def _parse_power_split(self, power_rpt):
        """Re-parses PostRoutePnR's own power report for the Internal/
        Switching/Leakage split report_power always prints (PostRoutePnR's
        own _parse_power keeps only the Total column). Returns
        (dyn_power_mw, static_power_mw) or (None, None)."""
        if not os.path.exists(power_rpt):
            return None, None
        try:
            with open(power_rpt, 'r') as fh:
                content = fh.read()
            m = self._POWER_SPLIT_RE.search(content)
            if m:
                internal_mw  = float(m.group(1)) * 1000.0
                switching_mw = float(m.group(2)) * 1000.0
                static_mw    = float(m.group(3)) * 1000.0
                return internal_mw + switching_mw, static_mw
        except Exception as e:
            log_message(f"Error parsing post-route power split {power_rpt}: {e}", level='ERROR')
        return None, None

    def _run_postroute_pnr(self, design_name, core_file, top_file):
        log_message(f"Running post-route P&R for {design_name}")

        work_dir = os.path.abspath(os.path.join(SYNTH_WORK_DIR, design_name))
        os.makedirs(work_dir, exist_ok=True)

        core_abs    = os.path.abspath(core_file)
        top_abs     = os.path.abspath(top_file)
        verilog_dir = os.path.abspath(VERILOG_SOURCES_DIR)
        lib_abs     = os.path.abspath(LIBERTY_LIB_PATH)
        ram_lib_abs = os.path.abspath(RAM_LIBERTY_PATH)

        top_module = f"{design_name}{TOP_MODULE_SUFFIX}"
        netlist_v  = os.path.join(work_dir, f"{design_name}_netlist.v")
        yosys_log  = os.path.join(work_dir, "yosys.log")

        verilog_sources = self._collect_verilog_sources(verilog_dir, core_abs, top_abs)

        # Flat netlist: same _run_yosys used by the old pre-route path
        # (already does flatten -noscopeinfo unconditionally), which is
        # exactly what OpenROAD P&R needs as its single top-level netlist.
        yosys_ok = self._run_yosys(design_name, top_module, verilog_sources,
                                    lib_abs, ram_lib_abs, netlist_v, yosys_log, work_dir)
        if not yosys_ok:
            return MAX_POWER_MW * 2, MAX_POWER_MW * 2, 0.0, MAX_AREA_UM2 * 2, 200.0, -1.0

        area_um2_fallback = self._parse_yosys_area(yosys_log)

        saif_file = self._generate_activity_saif(design_name, work_dir)
        if saif_file is None:
            log_message(f"{design_name}: activity SAIF generation FAILED - "
                        f"P&R will run but power will be FAILED", level='ERROR')
            saif_file = os.path.join(work_dir, "missing.saif")

        # PostRoutePnR internally names every report f"{design_prefix}_{n}"
        # and reads the SAIF at scope tb_{design_prefix}_{n}/dut -- that MUST
        # equal this solution's actual testbench module name, tb_{design_name}
        # (see performance_evaluator.py's _generate_testbench). Splitting
        # design_name at its last underscore and feeding the two halves back
        # in as (design_prefix, n) reconstructs it exactly, whatever n
        # itself is (it is never used numerically inside postroute_pnr.py,
        # only for this string interpolation), unlike the standalone
        # run_mixed_postroute.py where design_prefix="mixed_fft" and n=<size>
        # already coincide with its own mixed_fft_<n> naming convention.
        design_prefix, _, pnr_n = design_name.rpartition('_')
        pnr = PostRoutePnR(
            design_prefix=design_prefix, std_lib=lib_abs, tech_lef=MIXED_TECH_LEF,
            cell_lef=MIXED_CELL_LEF, sram_lef=MIXED_SRAM_LEF, sram_liberty=ram_lib_abs,
            macro_module=MIXED_MACRO_MODULE, macro_instances=MIXED_MACRO_INSTANCES,
            macro_w=MIXED_MACRO_W, macro_h=MIXED_MACRO_H, clock_period=CLOCK_PERIOD,
            fixed_lef_dir=PNR_FIXED_LEF_DIR, openroad_path=OPENROAD_PATH,
            sta_path=OPENSTA_PATH, min_annotated_pins=MIN_ANNOTATED_PINS,
        )
        result = pnr.run(pnr_n, netlist_v, top_module, saif_file, work_dir)

        if not result["ok"]:
            log_message(f"{design_name}: post-route P&R FAILED "
                        f"(power_source={result['power_source']})", level='ERROR')
            area_fallback = result["area_um2"] if result["area_um2"] is not None else area_um2_fallback
            crit_fallback = result["crit_delay_ns"] if result["crit_delay_ns"] is not None else 200.0
            slack_fallback = result["slack_ns"] if result["slack_ns"] is not None else -1.0
            return (MAX_POWER_MW * 2, MAX_POWER_MW * 2, 0.0, area_fallback,
                    crit_fallback, slack_fallback)

        power_rpt = os.path.join(work_dir, f"{design_name}_postroute_power.rpt")
        dyn_mw, static_mw = self._parse_power_split(power_rpt)
        if dyn_mw is None:
            # Degraded fallback: no split available, use total as dynamic
            # (see energyObjective.parse_power_fields's own "degraded" path).
            dyn_mw, static_mw = result["power_mw"], 0.0

        return (result["power_mw"], dyn_mw, static_mw, result["area_um2"],
                result["crit_delay_ns"], result["slack_ns"])

    def _run_yosys_opensta(self, design_name, core_file, top_file):
        log_message(f"Running Yosys+OpenSTA for {design_name}")

        work_dir = os.path.abspath(os.path.join(SYNTH_WORK_DIR, design_name))
        os.makedirs(work_dir, exist_ok=True)

        core_abs    = os.path.abspath(core_file)
        top_abs     = os.path.abspath(top_file)
        verilog_dir = os.path.abspath(VERILOG_SOURCES_DIR)
        
        # 1. Resolve absolute paths for BOTH Liberty files
        lib_abs     = os.path.abspath(LIBERTY_LIB_PATH)
        ram_lib_abs = os.path.abspath(RAM_LIBERTY_PATH) 
        
        rpt_dir     = os.path.abspath(REPORTS_DIR)

        top_module  = f"{design_name}{TOP_MODULE_SUFFIX}"
        netlist_v   = os.path.join(work_dir, f"{design_name}_netlist.v")
        yosys_log   = os.path.join(work_dir, "yosys.log")
        sta_log     = os.path.join(work_dir, "sta.log")

        verilog_sources = self._collect_verilog_sources(verilog_dir, core_abs, top_abs)

        # 2. Pass ram_lib_abs into Yosys
        yosys_ok = self._run_yosys(design_name, top_module, verilog_sources, lib_abs, ram_lib_abs, netlist_v, yosys_log, work_dir)
        if not yosys_ok:
            return MAX_POWER_MW * 2, MAX_POWER_MW * 2, 0.0, MAX_AREA_UM2 * 2, 200.0, -1.0

        area_um2 = self._parse_yosys_area(yosys_log)

        # 3. Pass ram_lib_abs into OpenSTA
        sta_ok = self._run_opensta(design_name, top_module, lib_abs, ram_lib_abs, netlist_v, rpt_dir, sta_log, work_dir)
        if not sta_ok:
            return MAX_POWER_MW * 2, MAX_POWER_MW * 2, 0.0, area_um2, 200.0, -1.0

        power_mw, dyn_power_mw, static_power_mw = self._parse_opensta_power(rpt_dir, design_name)
        crit_delay, slack_ns = self._parse_opensta_timing(rpt_dir, design_name)

        return power_mw, dyn_power_mw, static_power_mw, area_um2, crit_delay, slack_ns

    def _collect_verilog_sources(self, verilog_dir, core_abs, top_abs):
        import glob as _glob
        sources = []
        for f in sorted(_glob.glob(os.path.join(verilog_dir, '*.v'))):
            sources.append(os.path.abspath(f))
        for f in [core_abs, top_abs]:
            if f not in sources and os.path.exists(f):
                sources.append(f)
        return sources

    def _run_yosys(self, design_name, top_module, verilog_sources, lib_file, ram_lib_file, netlist_v, yosys_log, work_dir):
        # Matches fp16_baseline/synth/run_fp16_synthesis.py and
        # fp32_baseline/synth/run_fp32_synthesis.py exactly (down to the
        # -purge/setundef passes): without them, Yosys leaves dangling
        # `wire signed [31:0] i;`-style loop-counter declarations (from
        # `integer i, j, t_idx;` used only to unroll `for` loops) in the
        # written netlist. Those are harmless to Yosys but OpenSTA's Verilog
        # reader cannot parse a signed wire declaration at all, so
        # read_verilog fails, every design silently falls back to the
        # MAX_POWER_MW*2 / 200 ns penalty values in _run_yosys_opensta, and
        # the energy objective goes inert exactly the way its docstring warns
        # about - just one synthesis pass earlier than SAIF coverage. Matching
        # the baseline script here is also what makes mixed-precision designs
        # directly comparable to the FP16/FP32 baselines in the first place.
        read_cmds = '\n'.join(f'read_verilog -sv {f}' for f in verilog_sources)
        yosys_script = textwrap.dedent(f"""\
            {read_cmds}
            # PRE-SYNTHESIS BLACKBOX
            # Force blackbox before proc/opt, even if loaded as source
            blackbox sram_512x24_2rw

            hierarchy -check -top {top_module}
            flatten -noscopeinfo

            proc
            opt -purge
            memory
            opt -purge

            async2sync
            techmap
            opt -purge
            dfflibmap -liberty {lib_file}
            abc -liberty {lib_file} -g cmos
            opt_clean -purge
            setundef -zero -undriven

            stat -liberty {lib_file} -liberty {ram_lib_file}
            write_verilog -noattr {netlist_v}
        """)

        script_path = os.path.join(work_dir, f"{design_name}_synth.ys")
        with open(script_path, 'w') as f:
            f.write(yosys_script)

        cmd = [YOSYS_PATH, '-l', yosys_log, script_path]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, cwd=work_dir)
            if result.returncode != 0:
                log_message(f"Yosys FAILED for {design_name}. stderr tail: {result.stderr[-500:]}", level='ERROR')
                return False
            return True
        except Exception as e:
            log_message(f"Yosys invocation error for {design_name}: {e}", level='ERROR')
            return False

    # Update signature to accept ram_lib_file
    def _run_opensta(self, design_name, top_module, lib_file, ram_lib_file, netlist_v, rpt_dir, sta_log, work_dir):
        timing_rpt = os.path.join(rpt_dir, f"{design_name}_timing.rpt")
        power_rpt  = os.path.join(rpt_dir, f"{design_name}_power.rpt")

        sta_script = textwrap.dedent(f"""\
            read_liberty {lib_file}
            read_liberty {ram_lib_file}
            read_verilog {netlist_v}
            link_design {top_module}
            create_clock -name {CLOCK_NET_NAME} -period {CLOCK_PERIOD} [get_ports {CLOCK_NET_NAME}]
            set_input_delay  [expr {{{CLOCK_PERIOD}}} / 4.0] -clock {CLOCK_NET_NAME} [all_inputs]
            set_output_delay [expr {{{CLOCK_PERIOD}}} / 4.0] -clock {CLOCK_NET_NAME} [all_outputs]
            report_checks -path_delay max -format full_clock_expanded > {timing_rpt}
            set_power_activity -input -activity 0.2
            report_power > {power_rpt}
            exit
        """)

        script_path = os.path.join(work_dir, f"{design_name}_sta.tcl")
        with open(script_path, 'w') as f:
            f.write(sta_script)

        cmd = [OPENSTA_PATH, '-no_init', '-no_splash', '-exit', script_path]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=work_dir)
            if result.returncode != 0:
                log_message(f"OpenSTA FAILED for {design_name}. stderr tail: {result.stderr[-500:]}", level='ERROR')
                return False
            with open(sta_log, 'w') as f:
                f.write(result.stdout)
            return True
        except Exception as e:
            log_message(f"OpenSTA invocation error for {design_name}: {e}", level='ERROR')
            return False

    def _parse_yosys_area(self, yosys_log, fallback=None):
        if fallback is None:
            fallback = MAX_AREA_UM2 * 2
        if not os.path.exists(yosys_log):
            return fallback
        try:
            with open(yosys_log, 'r', errors='replace') as fh:
                content = fh.read()
            pat = re.compile(r'Chip area for (?:module|top module)\s+[\'"]?[^\'"\n]+[\'"]?\s*:\s*([\d.eE+\-]+)', re.IGNORECASE)
            matches = pat.findall(content)
            if matches:
                return float(matches[-1])
            pat2 = re.compile(r'Number of cells\s*:\s*(\d+)', re.IGNORECASE)
            m2 = pat2.search(content)
            if m2:
                return float(m2.group(1))
        except Exception as e:
            log_message(f"Error parsing Yosys log {yosys_log}: {e}", level='ERROR')
        return fallback

    def _parse_opensta_power(self, rpt_dir, design_name, fallback=None):
        """Returns (total_mw, dynamic_mw, static_mw) from the "Total" row of
        OpenSTA's report_power: Internal, Switching, Leakage, Total (Watts).
        dynamic = Internal + Switching, static = Leakage. This split is real
        (no extra tool run needed) but is still driven by the flat
        `set_power_activity -input -activity 0.2` guess in _run_opensta, not
        by a per-design switching trace - see energyObjective.py."""
        if fallback is None:
            fallback = MAX_POWER_MW * 2
        rpt_file = os.path.join(rpt_dir, f"{design_name}_power.rpt")
        if not os.path.exists(rpt_file):
            return fallback, fallback, 0.0
        try:
            with open(rpt_file, 'r') as fh:
                content = fh.read()
            pat = re.compile(r'^\s*Total\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)', re.MULTILINE)
            for m in pat.finditer(content):
                internal_mw  = float(m.group(1)) * 1000.0
                switching_mw = float(m.group(2)) * 1000.0
                static_mw    = float(m.group(3)) * 1000.0
                total_mw     = float(m.group(4)) * 1000.0
                return total_mw, internal_mw + switching_mw, static_mw
        except Exception as e:
            log_message(f"Error parsing OpenSTA power report: {e}", level='ERROR')
        return fallback, fallback, 0.0

    def _parse_opensta_timing(self, rpt_dir, design_name, fallback_delay=200.0, fallback_slack=-1.0):
        rpt_file = os.path.join(rpt_dir, f"{design_name}_timing.rpt")
        if not os.path.exists(rpt_file):
            return fallback_delay, fallback_slack
        slack_vals, arr_vals = [], []
        try:
            with open(rpt_file, 'r') as fh:
                for line in fh:
                    line_lower = line.lower()
                    if 'slack' in line_lower:
                        match = re.search(r'([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)', line)
                        if match: slack_vals.append(float(match.group(1)))
                    elif 'data arrival time' in line_lower:
                        match = re.search(r'([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)', line)
                        if match: arr_vals.append(float(match.group(1)))
            if slack_vals:
                slack_ns = min(slack_vals)
                return max(CLOCK_PERIOD - slack_ns, 0.0), slack_ns
            if arr_vals:
                crit_delay_ns = max(arr_vals)
                return max(crit_delay_ns, 0.0), CLOCK_PERIOD - crit_delay_ns
        except Exception as e:
            log_message(f"Error parsing OpenSTA timing report: {e}", level='ERROR')
        return fallback_delay, fallback_slack

    def _compute_actual_normalized_latency(self, crit_delay_ns):
        if crit_delay_ns <= 0 or math.isnan(crit_delay_ns) or math.isinf(crit_delay_ns):
            return 10.0
        norm = crit_delay_ns / REFERENCE_CLOCK_PERIOD_NS
        num_stages = self.template_gen.num_stages
        pipeline_factor = max(1.0, num_stages / 6.0)
        return min(norm * pipeline_factor, 10.0)

    def _run_performance_evaluation(self, verilog_file, design_name, chromosome=None):
        try:
            return self.perf_eval.evaluate_design(verilog_file, design_name, chromosome=chromosome)
        except Exception as e:
            log_message(f"Performance evaluation failed: {e}", level='ERROR')
            return {'sqnr': -100.0, 'avg_exec_cycles': -1, 'tot_sim_cycles': -1}

    def _compute_objectives_and_constraints(self, results):
        """Delegated to energyObjective.energy_objectives:
            [ energy_nJ_perFFT, sqnr_error^2, norm_latency ]
        Area and total power are constraints/reported values now, not
        objectives - the shared butterfly is a fixed union of both datapaths,
        so area takes only a handful of values over the whole chromosome
        space and carried no search signal."""
        return energy_objectives(results)

    def _save_solution_result(self, sol_id, chromosome, results):
        result_file = os.path.join(RESULTS_DIR, f"gen{CURRENT_GEN}_sol{sol_id}.txt")
        stats = self.template_gen.analyze_chromosome_statistics(chromosome)

        avg_exec = results.get('avg_exec_cycles', -1)
        tot_sim  = results.get('tot_sim_cycles',  -1)
        slack_ns = results.get('slack_ns', float('nan'))

        with open(result_file, 'w') as f:
            f.write(f"FFT Size          : {self.fft_size}\n")
            f.write(f"Generation        : {CURRENT_GEN}\n")
            f.write(f"Solution ID       : {sol_id}\n")
            f.write(f"Chromosome        : {[int(x) for x in chromosome]}\n\n")
            f.write(f"Results:\n")
            f.write(f"  Energy/FFT        : {results.get('energy_nj_per_fft', -1.0):.4f} nJ\n")
            f.write(f"  Dynamic Power     : {results.get('dyn_power_mw_used', results.get('dyn_power_mw', 0.0)):.6f} mW\n")
            f.write(f"  Static Power      : {results.get('static_power_mw', 0.0):.6f} mW\n")
            f.write(f"  Power             : {results['power']:.6f} mW\n")
            f.write(f"  Area              : {results['area']} um2\n")
            f.write(f"  SQNR              : {results['sqnr']:.2f} dB\n")
            f.write(f"  Crit Path Delay   : {results.get('crit_delay_ns', 0):.3f} ns\n")
            f.write(f"  Timing Slack      : {slack_ns * 1000 if not math.isnan(slack_ns) else 'N/A'} ps\n")
            f.write(f"  Norm Latency      : {results.get('norm_latency', 0):.4f}x\n")
            f.write(f"  Avg Exec Cycles   : {avg_exec}\n")
            f.write(f"  Tot Sim Cycles    : {tot_sim}\n")
            f.write(f"\nPrecision Stats:\n")
            for k, v in stats.items():
                if not isinstance(v, list):
                    f.write(f"  {k}: {v}\n")