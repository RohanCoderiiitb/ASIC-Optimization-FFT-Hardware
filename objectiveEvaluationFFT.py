"""
Objective Evaluation for Mixed-Precision FFT Optimization
ASIC version: uses Cadence Genus logical synthesis for metric extraction.

Metrics extracted from Genus (replaces Vivado):
  - Power           : total power in mW  (report_power)
  - Area            : cell area in µm²   (report_area)
  - Critical delay  : worst-case path in ns (report_timing)

SQNR computation is unchanged — still handled by performance_evaluator.py
with no EDA tool dependency.

Every other aspect of the file (threading, caching, NSGA-II interface,
chromosome hashing, result serialisation) is identical to the original.
"""

import numpy as np
import subprocess
import os
import re
import hashlib
import math
from pymoo.core.problem import Problem
from concurrent.futures import ThreadPoolExecutor, as_completed

from globalVariablesMixedFFT import *
from fft_template_generator import FFTTemplateGenerator
# PerformanceEvaluator (SQNR/iverilog) is no longer imported — SQNR disabled


class MixedPrecisionFFTProblem(Problem):
    def __init__(self, fft_size=8, **kwargs):
        self.fft_size     = fft_size
        self.template_gen = FFTTemplateGenerator(fft_size)
        # perf_eval removed — no iverilog available

        chrom_length = self.template_gen.get_chromosome_length()

        super().__init__(
            n_var=chrom_length,
            n_obj=OBJECTIVES,           # 3 objectives: Power, Area, CritDelay
            n_ieq_constr=2,             # Power ≤ MAX, Area ≤ MAX  (SQNR constraint removed)
            xl=[0] * chrom_length,
            xu=[1] * chrom_length,
            vtype=int,
            elementwise_evaluation=False,
            **kwargs
        )

        log_message(
            f"Initialized FFT-{fft_size} problem — "
            f"Cadence Genus ASIC synthesis for Power/Area/Delay extraction "
            f"[SQNR disabled — no iverilog]"
        )

    # ------------------------------------------------------------------
    # NSGA-II interface: called once per generation with the full population
    # ------------------------------------------------------------------
    def _evaluate(self, X, out, *args, **kwargs):
        global CURRENT_GEN
        log_message(f"=== Generation {CURRENT_GEN} ===", level='GEN')
        with open('generation.txt', 'w') as f:
            f.write(str(CURRENT_GEN))
        CURRENT_GEN += 1

        F = [None] * len(X)
        G = [None] * len(X)

        with ThreadPoolExecutor(max_workers=SOLUTION_THREADS) as executor:
            futures = {
                executor.submit(self.evaluate_solution, X[i], i): i
                for i in range(len(X))
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    f_vals, g_vals = future.result()
                    F[idx] = f_vals
                    G[idx] = g_vals
                except Exception as e:
                    log_message(f"Solution {idx} failed: {e}", level='ERROR')
                    # Penalty values use ASIC-scale units (mW, µm²)
                    F[idx] = [MAX_POWER_MW * 2, MAX_AREA_UM2 * 2, 50.0]
                    G[idx] = [MAX_POWER_MW, MAX_AREA_UM2]

        out["F"] = np.array(F)
        out["G"] = np.array(G)
        log_message(f"Generation {CURRENT_GEN-1} complete")

    # ------------------------------------------------------------------
    # Per-solution evaluation
    # ------------------------------------------------------------------
    def evaluate_solution(self, chromosome, sol_id):
        log_message(f"Evaluating solution {sol_id}: {list(chromosome)}")

        chrom_hash = self._hash_chromosome(chromosome)
        if ENABLE_RESULT_CACHE and chrom_hash in RESULT_CACHE:
            return self._compute_objectives_and_constraints(RESULT_CACHE[chrom_hash])

        design_name = f"fft_{self.fft_size}_sol{sol_id}_gen{CURRENT_GEN}"

        # Generate RTL for this chromosome (unchanged)
        core_file = os.path.join(GENERATED_DESIGNS_DIR, f"{design_name}.v")
        core_file, top_file = self.template_gen.generate_verilog(chromosome, core_file)

        # --- CHANGED: call Genus instead of Vivado ---
        power_mw, area_um2, crit_delay = self._run_genus_synthesis(
            design_name, core_file, top_file
        )

        # SQNR skipped — no iverilog available

        norm_latency = self._compute_actual_normalized_latency(crit_delay)

        results = {
            'power':        power_mw,
            'area':         area_um2,
            'sqnr':         None,       # disabled
            'norm_latency': norm_latency,
            'crit_delay_ns': crit_delay
        }

        RESULT_CACHE[chrom_hash] = results
        self._save_solution_result(sol_id, chromosome, results)

        log_message(
            f"Solution {sol_id}: "
            f"P={power_mw:.3f}mW, A={area_um2:.1f}µm², "
            f"CritDelay={crit_delay:.3f}ns "
            f"→ NormLat={norm_latency:.3f}x"
        )

        return self._compute_objectives_and_constraints(results)

    # ------------------------------------------------------------------
    # Chromosome hashing (unchanged)
    # ------------------------------------------------------------------
    def _hash_chromosome(self, chromosome):
        return hashlib.md5(''.join(map(str, chromosome)).encode()).hexdigest()

    # ------------------------------------------------------------------
    # Cadence Genus synthesis  (replaces _run_vivado_synthesis)
    # ------------------------------------------------------------------
    def _run_genus_synthesis(self, design_name, core_file, top_file):
        """
        Invoke Cadence Genus in batch (legacy_ui) mode using genus_synthesis.tcl.
        Genus writes three plain-text report files; we parse them afterwards.

        Returns
        -------
        power_mw   : float  — total power in milliwatts
        area_um2   : float  — total cell area in µm²
        crit_delay : float  — worst-case critical-path delay in ns
        """
        log_message(f"Running Cadence Genus synthesis for {design_name}")

        rpt_dir     = os.path.abspath(REPORTS_DIR)
        genus_work  = os.path.abspath(os.path.join(GENUS_WORK_DIR, design_name))
        os.makedirs(genus_work, exist_ok=True)

        core_abs    = os.path.abspath(core_file)
        top_abs     = os.path.abspath(top_file)
        verilog_dir = os.path.abspath(VERILOG_SOURCES_DIR)
        lib_abs     = os.path.abspath(LIBERTY_LIB_PATH)
        tcl_script  = os.path.abspath('./genus_synthesis.tcl')

        # Genus is invoked in legacy_ui batch mode.
        # All design-specific parameters are passed via -execute so that a
        # single shared TCL script handles every chromosome evaluation.
        cmd = [
            GENUS_PATH,
            '-legacy_ui',
            '-batch',
            '-log', os.path.join(genus_work, 'genus.log'),
            '-execute',
            (
                f"set DESIGN_NAME {design_name}; "
                f"set CORE_FILE   {core_abs};    "
                f"set TOP_FILE    {top_abs};     "
                f"set VERILOG_DIR {verilog_dir}; "
                f"set LIB_FILE    {lib_abs};     "
                f"set CLK_PERIOD  {CLOCK_PERIOD}; "
                f"set RPT_DIR     {rpt_dir};     "
                f"set WORK_DIR    {genus_work};  "
                f"source {tcl_script}"
            )
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=1800          # 30-minute ceiling per solution
            )
            if result.returncode != 0:
                log_message(
                    f"Genus failed for {design_name}. "
                    f"stderr tail: {result.stderr[-500:]}",
                    level='ERROR'
                )
                return MAX_POWER_MW * 2, MAX_AREA_UM2 * 2, 200.0

            return self._parse_genus_metrics(design_name)

        except subprocess.TimeoutExpired:
            log_message(f"Genus timeout for {design_name}", level='ERROR')
            return MAX_POWER_MW * 2, MAX_AREA_UM2 * 2, 200.0
        except Exception as e:
            log_message(f"Genus invocation error for {design_name}: {e}", level='ERROR')
            return MAX_POWER_MW * 2, MAX_AREA_UM2 * 2, 200.0

    # ------------------------------------------------------------------
    # Report parsers for Cadence Genus output  (replaces _parse_vivado_metrics)
    # ------------------------------------------------------------------
    def _parse_genus_metrics(self, design_name):
        """
        Parse the three report files written by genus_synthesis.tcl and
        return (power_mw, area_um2, crit_delay_ns).

        Report files (written by the TCL script):
          <REPORTS_DIR>/<design_name>_power.rpt
          <REPORTS_DIR>/<design_name>_area.rpt
          <REPORTS_DIR>/<design_name>_timing.rpt
        """
        power_mw   = MAX_POWER_MW * 2
        area_um2   = MAX_AREA_UM2 * 2
        crit_delay = 200.0

        rpt_power  = os.path.join(REPORTS_DIR, f"{design_name}_power.rpt")
        rpt_area   = os.path.join(REPORTS_DIR, f"{design_name}_area.rpt")
        rpt_timing = os.path.join(REPORTS_DIR, f"{design_name}_timing.rpt")

        # --- Power report ---
        # Genus report_power emits a table; the total power row looks like:
        #   Total         <dynamic_mw>   <leakage_mw>   <total_mw>   mW
        # We capture the last numeric column on the "Total" summary line.
        power_mw = self._parse_genus_power(rpt_power, power_mw)

        # --- Area report ---
        # Genus report_area emits a line:
        #   Total cell area:    <value>
        area_um2 = self._parse_genus_area(rpt_area, area_um2)

        # --- Timing report ---
        # Genus report_timing emits a line near the top:
        #   Timing Path : ...
        #   ... (slack) ...
        #   Data Path Delay : <value> ns
        # or the critical-path arrival time on the line:
        #   slack (MET|VIOLATED) <slack_value>
        # We derive delay = CLOCK_PERIOD - slack (if slack line present),
        # or parse "Data Path Delay" directly.
        crit_delay = self._parse_genus_timing(rpt_timing, crit_delay)

        log_message(
            f"Genus metrics parsed — "
            f"Power={power_mw:.3f}mW  Area={area_um2:.1f}µm²  "
            f"CritDelay={crit_delay:.3f}ns"
        )
        return power_mw, area_um2, crit_delay

    def _parse_genus_power(self, rpt_file, fallback):
        """
        Extract total power (mW) from a Genus report_power file.

        Genus power report excerpt (legacy_ui):
          ---------------------------------------------------------------
          Instance       Dynamic   Leakage    Total     Units
          ---------------------------------------------------------------
          top_design     0.1234    0.0056     0.1290    mW
          ...
          Totals         0.1234    0.0056     0.1290    mW
          ---------------------------------------------------------------
        We match the "Totals" line and take the third numeric column.
        If units are reported as 'W' instead of 'mW', we convert.
        """
        if not os.path.exists(rpt_file):
            log_message(f"Power report not found: {rpt_file}", level='ERROR')
            return fallback

        try:
            with open(rpt_file, 'r') as fh:
                content = fh.read()

            # Match the Totals summary line
            # Pattern: "Totals" followed by whitespace-separated numbers
            pattern = re.compile(
                r'(?i)^[ \t]*totals?\s+'          # "Totals" / "Total"
                r'([\d.eE+\-]+)\s+'               # dynamic
                r'([\d.eE+\-]+)\s+'               # leakage
                r'([\d.eE+\-]+)',                  # total
                re.MULTILINE
            )
            m = pattern.search(content)
            if m:
                total_val = float(m.group(3))
                # Check reported units on the same line
                line = content[m.start():content.find('\n', m.start())]
                if 'W' in line and 'mW' not in line:
                    total_val *= 1000.0            # convert W → mW
                return total_val

            # Fallback: look for "Total Power" labelled line
            pattern2 = re.compile(
                r'(?i)total\s+power[^\d]*([\d.eE+\-]+)\s*(m?W)',
                re.MULTILINE
            )
            m2 = pattern2.search(content)
            if m2:
                val = float(m2.group(1))
                if m2.group(2).strip() == 'W':
                    val *= 1000.0
                return val

        except Exception as e:
            log_message(f"Error parsing power report {rpt_file}: {e}", level='ERROR')

        return fallback

    def _parse_genus_area(self, rpt_file, fallback):
        """
        Extract total cell area (µm²) from a Genus report_area file.

        Genus area report excerpt (legacy_ui):
          ...
          Total cell area:        1234.567
          ...
        """
        if not os.path.exists(rpt_file):
            log_message(f"Area report not found: {rpt_file}", level='ERROR')
            return fallback

        try:
            with open(rpt_file, 'r') as fh:
                content = fh.read()

            pattern = re.compile(
                r'(?i)total\s+cell\s+area\s*:\s*([\d.eE+\-]+)',
                re.MULTILINE
            )
            m = pattern.search(content)
            if m:
                return float(m.group(1))

        except Exception as e:
            log_message(f"Error parsing area report {rpt_file}: {e}", level='ERROR')

        return fallback

    def _parse_genus_timing(self, rpt_file, fallback):
        """
        Extract the worst-case critical-path delay (ns) from a Genus
        report_timing file.

        Genus timing report (legacy_ui) contains lines such as:

          Startpoint: ...
          Endpoint  : ...
          ...
          Data Path Delay : 2.345 ns
          ...
          slack (MET)  0.655

        Strategy:
          1. Try to parse "Data Path Delay" directly — most reliable.
          2. If absent, compute delay = CLOCK_PERIOD − slack from the
             "slack" line (works for both MET and VIOLATED paths).
        """
        if not os.path.exists(rpt_file):
            log_message(f"Timing report not found: {rpt_file}", level='ERROR')
            return fallback

        try:
            with open(rpt_file, 'r') as fh:
                content = fh.read()

            # Strategy 1: "Data Path Delay" line
            pat_delay = re.compile(
                r'(?i)data\s+path\s+delay\s*:\s*([\d.eE+\-]+)\s*ns',
                re.MULTILINE
            )
            m = pat_delay.search(content)
            if m:
                return float(m.group(1))

            # Strategy 2: derive from slack
            # Matches: "slack (MET) 0.655"  or  "slack (VIOLATED) -0.123"
            pat_slack = re.compile(
                r'(?i)slack\s*\((?:MET|VIOLATED)\)\s*([-\d.eE+]+)',
                re.MULTILINE
            )
            m2 = pat_slack.search(content)
            if m2:
                slack = float(m2.group(1))
                delay = CLOCK_PERIOD - slack
                return max(delay, 0.0)

        except Exception as e:
            log_message(f"Error parsing timing report {rpt_file}: {e}", level='ERROR')

        return fallback

    # ------------------------------------------------------------------
    # Latency normalisation (unchanged logic, updated docstring)
    # ------------------------------------------------------------------
    def _compute_actual_normalized_latency(self, crit_delay_ns):
        """
        Normalise the Genus critical-path delay against the target clock period.
        Returns a dimensionless value; 1.0 means the path exactly meets timing.
        Values > 1.0 indicate a violation.
        """
        if crit_delay_ns <= 0 or math.isnan(crit_delay_ns) or math.isinf(crit_delay_ns):
            return 10.0

        norm = crit_delay_ns / REFERENCE_CLOCK_PERIOD_NS

        num_stages     = self.template_gen.num_stages
        pipeline_factor = max(1.0, num_stages / 6.0)

        return min(norm * pipeline_factor, 10.0)

    # ------------------------------------------------------------------
    # Objective / constraint assembly
    # ------------------------------------------------------------------
    def _compute_objectives_and_constraints(self, results):
        power_mw     = results['power']          # mW  (Genus)
        area_um2     = results['area']           # µm² (Genus)
        norm_latency = results.get('norm_latency', 10.0)

        objectives = [
            power_mw    * WEIGHT_POWER,        # F[0]: minimise power
            area_um2    * WEIGHT_AREA,          # F[1]: minimise area
            norm_latency * WEIGHT_LATENCY       # F[2]: minimise latency
        ]

        constraints = [
            power_mw  - MAX_POWER_MW,           # G[0] ≤ 0
            area_um2  - MAX_AREA_UM2,           # G[1] ≤ 0
            # SQNR constraint removed — no iverilog
        ]
        return objectives, constraints

    # ------------------------------------------------------------------
    # Result serialisation
    # ------------------------------------------------------------------
    def _save_solution_result(self, sol_id, chromosome, results):
        result_file = os.path.join(RESULTS_DIR, f"gen{CURRENT_GEN}_sol{sol_id}.txt")
        stats = self.template_gen.analyze_chromosome_statistics(chromosome)

        with open(result_file, 'w') as f:
            f.write(f"FFT Size          : {self.fft_size}\n")
            f.write(f"Generation        : {CURRENT_GEN}\n")
            f.write(f"Solution ID       : {sol_id}\n")
            f.write(f"Chromosome        : {[int(gene) for gene in chromosome]}\n\n")
            f.write(f"Results (ASIC - Cadence Genus):\n")
            f.write(f"  Power             : {results['power']:.4f} mW\n")
            f.write(f"  Area              : {results['area']:.2f} um2\n")
            f.write(f"  SQNR              : N/A (iverilog not available)\n")
            f.write(f"  Crit Path Delay   : {results.get('crit_delay_ns', 0):.3f} ns\n")
            f.write(f"  Norm Latency      : {results.get('norm_latency', 0):.4f}x\n")
            f.write(f"\nPrecision Stats:\n")
            for k, v in stats.items():
                if not isinstance(v, list):
                    f.write(f"  {k}: {v}\n")


# Quick test
if __name__ == "__main__":
    problem = MixedPrecisionFFTProblem(fft_size=8)
    test_chrom = np.array([0, 0, 1, 0, 1, 1, 0, 0])
    objectives, constraints = problem.evaluate_solution(test_chrom, 0)
    print(f"3 Objectives (Power, Area, CritDelay): {objectives}")
    print(f"Constraints  : {constraints}")

