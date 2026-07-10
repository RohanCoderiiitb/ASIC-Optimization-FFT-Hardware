"""
Objective Evaluation for Mixed-Precision FFT Optimization
Uses open-source EDA tools (Yosys + OpenSTA) for Area/Power/Timing extraction.
Simulation tracking (execution cycles) is strictly maintained.
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

class MixedPrecisionFFTProblem(Problem):
    def __init__(self, fft_size=8, **kwargs):
        self.fft_size     = fft_size
        self.template_gen = FFTTemplateGenerator(fft_size)
        self.perf_eval    = PerformanceEvaluator(fft_size)

        chrom_length = self.template_gen.get_chromosome_length()

        super().__init__(
            n_var=chrom_length,
            n_obj=OBJECTIVES,
            n_ieq_constr=3,
            xl=[0] * chrom_length,
            xu=[1] * chrom_length,
            vtype=int,
            elementwise_evaluation=False,
            **kwargs
        )

        log_message(f"Initialized FFT-{fft_size} problem with Yosys+OpenSTA timing")

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
                    F[idx] = [MAX_POWER_MW*2, MAX_AREA_UM2*2, 1e6, 50.0]
                    G[idx] = [MAX_POWER_MW, MAX_AREA_UM2, MIN_SQNR_DB]

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

        power_mw, area_um2, crit_delay, slack_ns = self._run_yosys_opensta(design_name, core_file, top_file)
        
        perf = self._run_performance_evaluation(core_file, design_name, chromosome)
        sqnr           = perf['sqnr']
        avg_exec_cycles = perf['avg_exec_cycles']
        tot_sim_cycles  = perf['tot_sim_cycles']

        norm_latency = self._compute_actual_normalized_latency(crit_delay)

        results = {
            'power':            power_mw,
            'area':             area_um2,
            'sqnr':             sqnr,
            'norm_latency':     norm_latency,
            'crit_delay_ns':    crit_delay,
            'slack_ns':         slack_ns,
            'avg_exec_cycles':  avg_exec_cycles,
            'tot_sim_cycles':   tot_sim_cycles,
        }

        RESULT_CACHE[chrom_hash] = results
        self._save_solution_result(sol_id, chromosome, results)

        stats = self.template_gen.analyze_chromosome_statistics(chromosome)
        log_message(
            f"Solution {sol_id}: P={power_mw:.4f}mW, A={area_um2} µm², SQNR={sqnr:.2f}dB, "
            f"CritDelay={crit_delay:.3f}ns -> NormLat={norm_latency:.3f}x, "
            f"ExecCycles={avg_exec_cycles}, TotSimCycles={tot_sim_cycles}"
        )

        return self._compute_objectives_and_constraints(results)

    def _hash_chromosome(self, chromosome):
        return hashlib.md5(''.join(map(str, chromosome)).encode()).hexdigest()

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
            return MAX_POWER_MW * 2, MAX_AREA_UM2 * 2, 200.0, -1.0

        area_um2 = self._parse_yosys_area(yosys_log)

        # 3. Pass ram_lib_abs into OpenSTA
        sta_ok = self._run_opensta(design_name, top_module, lib_abs, ram_lib_abs, netlist_v, rpt_dir, sta_log, work_dir)
        if not sta_ok:
            return MAX_POWER_MW * 2, area_um2, 200.0, -1.0

        power_mw             = self._parse_opensta_power(rpt_dir, design_name)
        crit_delay, slack_ns = self._parse_opensta_timing(rpt_dir, design_name)

        return power_mw, area_um2, crit_delay, slack_ns

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
        read_cmds = '\n'.join(f'read_verilog -sv {f}' for f in verilog_sources)
        yosys_script = textwrap.dedent(f"""\
            {read_cmds}
            # PRE-SYNTHESIS BLACKBOX
            # Force blackbox before proc/opt, even if loaded as source
            blackbox sram_512x24_2rw
            
            hierarchy -check -top {top_module}
            
            proc
            opt
            memory
            opt
            
            async2sync
            techmap
            opt
            dfflibmap -liberty {lib_file}
            abc -liberty {lib_file} -g cmos
            opt_clean
            
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
        if fallback is None:
            fallback = MAX_POWER_MW * 2
        rpt_file = os.path.join(rpt_dir, f"{design_name}_power.rpt")
        if not os.path.exists(rpt_file):
            return fallback
        try:
            with open(rpt_file, 'r') as fh:
                content = fh.read()
            pat = re.compile(r'^\s*Total\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)', re.MULTILINE)
            for m in pat.finditer(content):
                return float(m.group(4)) * 1000.0   
        except Exception as e:
            log_message(f"Error parsing OpenSTA power report: {e}", level='ERROR')
        return fallback

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
        power_mw = results['power']
        area_um2 = results['area']
        sqnr = results['sqnr']
        norm_latency = results.get('norm_latency', 10.0)

        perf_obj = ((SQNR_OFFSET - sqnr) / REF_SQNR_RANGE) ** 2 

        objectives = [
            (power_mw / REF_POWER_MW)    * WEIGHT_POWER,
            (area_um2 / REF_AREA_UM2)    * WEIGHT_AREA,
            perf_obj                     * WEIGHT_PERFORMANCE, # Now non-linear
            (norm_latency / REF_LATENCY) * WEIGHT_LATENCY
        ]

        constraints = [
            power_mw - MAX_POWER_MW,
            area_um2 - MAX_AREA_UM2,
            MIN_SQNR_DB - sqnr
        ]
        return objectives, constraints

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