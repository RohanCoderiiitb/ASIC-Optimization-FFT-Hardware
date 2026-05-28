"""
Objective Evaluation for Mixed-Precision FFT Optimization
ASIC version: uses Cadence Genus logical synthesis for metric extraction.

Metrics extracted from Genus (replaces Vivado):
  - Power           : total power in mW  (report_power)
  - Area            : cell area in µm²   (report_area)
  - Critical delay  : worst-case path in ns (report_timing)

SQNR computation is unchanged — still handled by performance_evaluator.py
with no EDA tool dependency.

LEC (Logical Equivalence Check) parsing:
  - Parses the Cadence Conformal LEC log written by genus_synthesis.tcl.
  - If the LEC result is NOT "PASS" (or the log is absent/unreadable),
    the solution is immediately rejected with maximum penalty values.
  - The LEC result ("PASS" / "FAIL" / "ERROR") is recorded in every
    per-solution .txt file and propagated to the summary CSV.

Timing slack check:
  - Parses the Genus timing report for the slack value.
  - If slack <= 0 (negative or zero), the solution violates the clock
    constraint and is immediately rejected with maximum penalty values.
  - Positive slack means the design meets timing and is accepted normally.
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
from fft_template_generator_tb_RAM import FFTTemplateGenerator
from performance_evaluator_tb_RAM import PerformanceEvaluator


# ---------------------------------------------------------------------------
# LEC result sentinel values
# ---------------------------------------------------------------------------
LEC_PASS  = "PASS"
LEC_FAIL  = "FAIL"
LEC_ERROR = "ERROR"   # log absent, unreadable, or LEC process itself crashed


class MixedPrecisionFFTProblem(Problem):
    def __init__(self, fft_size=8, **kwargs):
        self.fft_size     = fft_size
        self.template_gen = FFTTemplateGenerator(fft_size)
        self.perf_eval    = PerformanceEvaluator(fft_size)

        chrom_length = self.template_gen.get_chromosome_length()

        super().__init__(
            n_var=chrom_length,
            n_obj=OBJECTIVES,           # 4 objectives: Power, Area, PerfError, CritDelay
            n_ieq_constr=2,             # Power ≤ MAX, Area ≤ MAX
            xl=[0] * chrom_length,
            xu=[1] * chrom_length,
            vtype=int,
            elementwise_evaluation=False,
            **kwargs
        )

        log_message(
            f"Initialized FFT-{fft_size} problem — "
            f"Cadence Genus ASIC synthesis for Power/Area/Delay extraction; "
            f"Conformal LEC for equivalence checking."
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
                    F[idx] = [MAX_POWER_MW * 2, MAX_AREA_UM2 * 2, 1.0, 10.0]
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

        # Generate RTL for this chromosome
        core_file = os.path.join(GENERATED_DESIGNS_DIR, f"{design_name}.v")
        core_file, top_file = self.template_gen.generate_verilog(chromosome, core_file)

        # ── Genus synthesis (Power / Area / Timing) ──────────────────────
        power_mw, area_um2, crit_delay, slack_ns = self._run_genus_synthesis(
            design_name, core_file, top_file
        )

        # ── TIMING SLACK CHECK ────────────────────────────────────────────
        # Reject immediately if the critical path violates the clock constraint.
        if slack_ns <= 0.0:
            log_message(
                f"Solution {sol_id} REJECTED: timing violated "
                f"(slack = {slack_ns*1000:.0f} ps ≤ 0)",
                level='WARN'
            )
            reject_results = {
                'power':         MAX_POWER_MW * 2,
                'area':          MAX_AREA_UM2 * 2,
                'sqnr':          MIN_SQNR_DB,
                'norm_latency':  MAX_LATENCY_NORM * 2,
                'crit_delay_ns': crit_delay,
                'slack_ns':      slack_ns,
                'lec_result':    LEC_ERROR,   # never reached LEC
            }
            self._save_solution_result(sol_id, chromosome, reject_results)
            return self._compute_objectives_and_constraints(reject_results)

        # ── LEC PARSING ───────────────────────────────────────────────────
        lec_log  = os.path.join(REPORTS_DIR, f"{design_name}_lec.log")
        lec_result = self._parse_lec_log(lec_log)

        if lec_result != LEC_PASS:
            log_message(
                f"Solution {sol_id} REJECTED: LEC result = {lec_result} "
                f"(log: {lec_log})",
                level='WARN'
            )
            reject_results = {
                'power':         MAX_POWER_MW * 2,
                'area':          MAX_AREA_UM2 * 2,
                'sqnr':          MIN_SQNR_DB,
                'norm_latency':  MAX_LATENCY_NORM * 2,
                'crit_delay_ns': crit_delay,
                'slack_ns':      slack_ns,
                'lec_result':    lec_result,
            }
            self._save_solution_result(sol_id, chromosome, reject_results)
            return self._compute_objectives_and_constraints(reject_results)

        # ── SQNR (iVerilog simulation) ────────────────────────────────────
        sqnr = self._run_performance_evaluation(core_file, design_name, chromosome)

        norm_latency = self._compute_actual_normalized_latency(crit_delay)

        results = {
            'power':         power_mw,
            'area':          area_um2,
            'sqnr':          sqnr,
            'norm_latency':  norm_latency,
            'crit_delay_ns': crit_delay,
            'slack_ns':      slack_ns,
            'lec_result':    lec_result,
        }

        RESULT_CACHE[chrom_hash] = results
        self._save_solution_result(sol_id, chromosome, results)

        log_message(
            f"Solution {sol_id} ACCEPTED: "
            f"P={power_mw:.3f}mW, A={area_um2:.1f}µm², "
            f"SQNR={sqnr:.2f}dB, CritDelay={crit_delay:.3f}ns "
            f"(slack={slack_ns*1000:.0f}ps), LEC={lec_result} "
            f"→ NormLat={norm_latency:.3f}x"
        )

        return self._compute_objectives_and_constraints(results)

    # ------------------------------------------------------------------
    # Chromosome hashing
    # ------------------------------------------------------------------
    def _hash_chromosome(self, chromosome):
        return hashlib.md5(''.join(map(str, chromosome)).encode()).hexdigest()

    # ------------------------------------------------------------------
    # Cadence Genus synthesis
    # ------------------------------------------------------------------
    def _run_genus_synthesis(self, design_name, core_file, top_file):
        """
        Invoke Cadence Genus in batch (legacy_ui) mode using genus_synthesis.tcl.
        Genus writes three plain-text report files and one LEC log; we parse
        them afterwards.

        Returns
        -------
        power_mw   : float  — total power in milliwatts
        area_um2   : float  — total cell area in µm²
        crit_delay : float  — worst-case critical-path delay in ns
        slack_ns   : float  — timing slack in ns  (positive ⟹ meets timing)
        """
        log_message(f"Running Cadence Genus synthesis for {design_name}")

        rpt_dir    = os.path.abspath(REPORTS_DIR)
        genus_work = os.path.abspath(os.path.join(GENUS_WORK_DIR, design_name))
        os.makedirs(genus_work, exist_ok=True)

        core_abs    = os.path.abspath(core_file)
        top_abs     = os.path.abspath(top_file)
        verilog_dir = os.path.abspath(VERILOG_SOURCES_DIR)
        lib_abs     = os.path.abspath(LIBERTY_LIB_PATH)
        tcl_script  = os.path.abspath('./genus_synthesis.tcl')

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
                timeout=1800,          # 30-minute ceiling per solution
                cwd=genus_work
            )
            if result.returncode != 0:
                log_message(
                    f"Genus failed for {design_name}. "
                    f"stderr tail: {result.stderr[-500:]}",
                    level='ERROR'
                )
                return MAX_POWER_MW * 2, MAX_AREA_UM2 * 2, 200.0, -1.0

            return self._parse_genus_metrics(design_name)

        except subprocess.TimeoutExpired:
            log_message(f"Genus timeout for {design_name}", level='ERROR')
            return MAX_POWER_MW * 2, MAX_AREA_UM2 * 2, 200.0, -1.0
        except Exception as e:
            log_message(f"Genus invocation error for {design_name}: {e}", level='ERROR')
            return MAX_POWER_MW * 2, MAX_AREA_UM2 * 2, 200.0, -1.0
        finally:
            # <--- 2. ADD THIS BLOCK (Guarantees cleanup even if Genus crashes)
            self._cleanup_lec_databases(genus_work)
    
    def _cleanup_lec_databases(self, work_dir):
        """
        Immediately removes heavy Conformal LEC databases after synthesis.
        This prevents massive disk bloat across thousands of generations.
        """
        import shutil
        import glob
        
        # Target the DB folders inside the isolated thread directory
        patterns = [
            os.path.join(work_dir, "fv_map_*_db"),   # Design-specific netlist DB
            os.path.join(work_dir, "rtl_fv_map_db"), # Generic RTL DB
            os.path.join(work_dir, "fv")             # Mapping logic folder
        ]
        
        for pat in patterns:
            for path in glob.glob(pat):
                if os.path.isdir(path):
                    try:
                        shutil.rmtree(path)
                    except OSError:
                        pass
                    
    # ------------------------------------------------------------------
    # Report parsers for Cadence Genus output
    # ------------------------------------------------------------------
    def _parse_genus_metrics(self, design_name):
        """
        Parse the three report files written by genus_synthesis.tcl and
        return (power_mw, area_um2, crit_delay_ns, slack_ns).
        """
        power_mw   = MAX_POWER_MW * 2
        area_um2   = MAX_AREA_UM2 * 2
        crit_delay = 200.0
        slack_ns   = -1.0

        rpt_power  = os.path.join(REPORTS_DIR, f"{design_name}_power.rpt")
        rpt_area   = os.path.join(REPORTS_DIR, f"{design_name}_area.rpt")
        rpt_timing = os.path.join(REPORTS_DIR, f"{design_name}_timing.rpt")

        power_mw             = self._parse_genus_power(rpt_power, power_mw)
        area_um2             = self._parse_genus_area(rpt_area, design_name, area_um2)
        crit_delay, slack_ns = self._parse_genus_timing(rpt_timing, crit_delay, -1.0)

        log_message(
            f"Genus metrics parsed — "
            f"Power={power_mw:.3f}mW  Area={area_um2:.1f}µm²  "
            f"CritDelay={crit_delay:.3f}ns  Slack={slack_ns*1000:.0f}ps"
        )
        return power_mw, area_um2, crit_delay, slack_ns

    def _parse_genus_power(self, rpt_file, fallback):
        """
        Extract total power from a Genus report_power file.
        Matches the 'Subtotal' line and checks the 'Power Unit' header.
        """
        if not os.path.exists(rpt_file):
            log_message(f"Power report not found: {rpt_file}", level='ERROR')
            return fallback

        try:
            with open(rpt_file, 'r') as fh:
                content = fh.read()

            # Find power unit (W or mW)
            unit_match = re.search(r'Power Unit:\s*(m?W)', content)
            is_watts   = unit_match and unit_match.group(1) == 'W'

            # Match the Subtotal line — 4th numeric column = total power
            pattern = re.compile(
                r'^\s*Subtotal\s+[\d.eE+\-]+\s+[\d.eE+\-]+\s+[\d.eE+\-]+\s+([\d.eE+\-]+)',
                re.MULTILINE
            )
            m = pattern.search(content)
            if m:
                total_val = float(m.group(1))
                if is_watts:
                    total_val *= 1000.0   # W → mW
                return total_val

        except Exception as e:
            log_message(f"Error parsing power report {rpt_file}: {e}", level='ERROR')

        return fallback

    def _parse_genus_area(self, rpt_file, design_name, fallback):
        """
        Extract total cell area (µm²) from a Genus report_area file.
        Finds the table row corresponding to the top module.
        """
        if not os.path.exists(rpt_file):
            log_message(f"Area report not found: {rpt_file}", level='ERROR')
            return fallback

        try:
            with open(rpt_file, 'r') as fh:
                content = fh.read()

            pattern = re.compile(
                rf'^\s*{re.escape(design_name)}_top\s+(?:\S+\s+)?\d+\s+([\d.eE+\-]+)',
                re.MULTILINE
            )
            m = pattern.search(content)
            if m:
                return float(m.group(1))

        except Exception as e:
            log_message(f"Error parsing area report {rpt_file}: {e}", level='ERROR')

        return fallback

    def _parse_genus_timing(self, rpt_file, fallback_delay, fallback_slack):
        """
        Extract the worst-case critical-path delay **and** timing slack from a
        Genus report_timing file.

        The Genus timing report contains a line of the form:
            Timing slack :    1377ps
        or (if timing is violated):
            Timing slack :    -250ps

        Returns
        -------
        (crit_delay_ns, slack_ns) : (float, float)
            crit_delay_ns  = CLOCK_PERIOD - slack_ns   (always ≥ 0)
            slack_ns       = slack in ns; NEGATIVE means timing violated
        """
        if not os.path.exists(rpt_file):
            log_message(f"Timing report not found: {rpt_file}", level='ERROR')
            return fallback_delay, fallback_slack

        try:
            with open(rpt_file, 'r') as fh:
                content = fh.read()

            # ── Primary strategy: "Timing slack : <value><unit>" ─────────
            pat_slack = re.compile(
                r'(?i)Timing\s+slack\s*:\s*([-\d.eE+]+)\s*(ps|ns)',
                re.MULTILINE
            )
            m = pat_slack.search(content)
            if m:
                slack_raw = float(m.group(1))
                unit      = m.group(2).lower()

                # Convert to nanoseconds
                slack_ns  = slack_raw / 1000.0 if unit == 'ps' else slack_raw
                # Derive critical-path delay from slack
                crit_delay_ns = CLOCK_PERIOD - slack_ns
                return max(crit_delay_ns, 0.0), slack_ns

            # ── Fallback: "Data Path Delay : <value> ns" ─────────────────
            pat_delay = re.compile(
                r'(?i)data\s+path\s+delay\s*:\s*([\d.eE+\-]+)\s*ns',
                re.MULTILINE
            )
            m2 = pat_delay.search(content)
            if m2:
                crit_delay_ns = float(m2.group(1))
                # No slack line found — derive slack as best estimate
                slack_ns = CLOCK_PERIOD - crit_delay_ns
                return crit_delay_ns, slack_ns

        except Exception as e:
            log_message(f"Error parsing timing report {rpt_file}: {e}", level='ERROR')

        return fallback_delay, fallback_slack

    # ------------------------------------------------------------------
    # LEC log parser  (NEW)
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # LEC log parser (Updated)
    # ------------------------------------------------------------------
    def _parse_lec_log(self, lec_log_path):
        """
        Parse the Cadence Conformal LEC log file.
        Returns LEC_PASS if diff points and abort points are both 0.
        Ignores the false 'child process exited abnormally' TCL catch error.
        """
        if not os.path.exists(lec_log_path):
            log_message(
                f"LEC log not found: {lec_log_path} — treating as ERROR",
                level='WARN'
            )
            return LEC_ERROR

        try:
            with open(lec_log_path, 'r', errors='replace') as fh:
                content = fh.read()
        except Exception as e:
            log_message(
                f"Could not read LEC log {lec_log_path}: {e} — treating as ERROR",
                level='ERROR'
            )
            return LEC_ERROR

        # ── Primary check: Explicitly look for 0 diff and 0 abort points ──
        # Using regex to account for variable whitespace in the log output
        pat_diff  = re.compile(r'No of diff points\s*=\s*(\d+)', re.IGNORECASE)
        pat_abort = re.compile(r'No of abort points\s*=\s*(\d+)', re.IGNORECASE)

        diff_matches  = pat_diff.findall(content)
        abort_matches = pat_abort.findall(content)

        if diff_matches and abort_matches:
            # Use the last printed occurrence in case of multiple runs in one log
            final_diff  = int(diff_matches[-1])
            final_abort = int(abort_matches[-1])

            if final_diff == 0 and final_abort == 0:
                log_message(f"LEC result: PASS (0 diff, 0 abort points) ({lec_log_path})")
                return LEC_PASS
            else:
                log_message(
                    f"LEC result: FAIL ({final_diff} diff, {final_abort} abort points) ({lec_log_path})",
                    level='WARN'
                )
                return LEC_FAIL

        # ── Fallback 1: Check for license failures ────────────────────────
        if 'License check failed!' in content or 'Fail to check out' in content:
            log_message(f"LEC log indicates license failure: {lec_log_path}", level='WARN')
            return LEC_ERROR

        # ── Fallback 2: Real crashes ──────────────────────────────────────
        # If we didn't find the diff/abort point summaries, and the script 
        # appended the failure string, it means the tool actually crashed.
        if '--- LEC EXECUTION FAILED ---' in content:
            log_message(
                f"LEC log indicates true process crash (no compare points found): {lec_log_path}",
                level='WARN'
            )
            return LEC_ERROR

        # ── Indeterminate ─────────────────────────────────────────────────
        log_message(
            f"LEC log present but result indeterminate: {lec_log_path} — treating as ERROR",
            level='WARN'
        )
        return LEC_ERROR

    # ------------------------------------------------------------------
    # Latency normalisation
    # ------------------------------------------------------------------
    def _compute_actual_normalized_latency(self, crit_delay_ns):
        """
        Normalise the Genus critical-path delay against the target clock period.
        Returns a dimensionless value; 1.0 means the path exactly meets timing.
        Values > 1.0 indicate a violation (but those are already rejected above).
        """
        if crit_delay_ns <= 0 or math.isnan(crit_delay_ns) or math.isinf(crit_delay_ns):
            return 10.0

        norm = crit_delay_ns / REFERENCE_CLOCK_PERIOD_NS

        num_stages      = self.template_gen.num_stages
        pipeline_factor = max(1.0, num_stages / 6.0)

        return min(norm * pipeline_factor, 10.0)

    # ------------------------------------------------------------------
    # SQNR evaluation
    # ------------------------------------------------------------------
    def _run_performance_evaluation(self, verilog_file, design_name, chromosome=None):
        try:
            return self.perf_eval.evaluate_design(
                verilog_file, design_name, chromosome=chromosome
            )
        except Exception as e:
            log_message(f"Performance evaluation failed: {e}", level='ERROR')
            return -100.0

    # ------------------------------------------------------------------
    # Objective / constraint assembly
    # ------------------------------------------------------------------
    def _compute_objectives_and_constraints(self, results):
        power_mw     = results['power']
        area_um2     = results['area']
        sqnr         = results['sqnr']
        norm_latency = results.get('norm_latency', 10.0)

        sqnr_clamped = max(sqnr, 0.0)
        perf_error   = 1.0 / (sqnr_clamped + 1.0)

        objectives = [
            power_mw     * WEIGHT_POWER,        # F[0]: minimise power
            area_um2     * WEIGHT_AREA,          # F[1]: minimise area
            perf_error   * WEIGHT_PERFORMANCE,   # F[2]: maximise SQNR
            norm_latency * WEIGHT_LATENCY        # F[3]: minimise latency
        ]

        constraints = [
            power_mw - MAX_POWER_MW,             # G[0] ≤ 0
            area_um2 - MAX_AREA_UM2,             # G[1] ≤ 0
        ]
        return objectives, constraints

    # ------------------------------------------------------------------
    # Result serialisation
    # ------------------------------------------------------------------
    def _save_solution_result(self, sol_id, chromosome, results):
        result_file = os.path.join(RESULTS_DIR, f"gen{CURRENT_GEN}_sol{sol_id}.txt")
        stats       = self.template_gen.analyze_chromosome_statistics(chromosome)

        slack_ns   = results.get('slack_ns', float('nan'))
        lec_result = results.get('lec_result', LEC_ERROR)

        # Derive human-readable timing verdict
        if not math.isnan(slack_ns):
            timing_verdict = "PASS" if slack_ns > 0.0 else "FAIL"
            slack_str      = f"{slack_ns * 1000:.0f} ps"
        else:
            timing_verdict = "UNKNOWN"
            slack_str      = "N/A"

        with open(result_file, 'w') as f:
            f.write(f"FFT Size          : {self.fft_size}\n")
            f.write(f"Generation        : {CURRENT_GEN}\n")
            f.write(f"Solution ID       : {sol_id}\n")
            f.write(f"Chromosome        : {[int(gene) for gene in chromosome]}\n\n")
            f.write(f"Results (ASIC - Cadence Genus):\n")
            f.write(f"  Power             : {results['power']:.4f} mW\n")
            f.write(f"  Area              : {results['area']:.2f} um2\n")
            f.write(f"  SQNR              : {results['sqnr']:.2f} dB\n")
            f.write(f"  Crit Path Delay   : {results.get('crit_delay_ns', 0):.3f} ns\n")
            f.write(f"  Timing Slack      : {slack_str}\n")
            f.write(f"  Timing            : {timing_verdict}\n")
            f.write(f"  LEC               : {lec_result}\n")
            f.write(f"  Norm Latency      : {results.get('norm_latency', 0):.4f}x\n")
            f.write(f"\nPrecision Stats:\n")
            for k, v in stats.items():
                if not isinstance(v, list):
                    f.write(f"  {k}: {v}\n")


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    problem = MixedPrecisionFFTProblem(fft_size=8)
    test_chrom = np.array([0, 0, 1, 0, 1, 1, 0, 0])
    objectives, constraints = problem.evaluate_solution(test_chrom, 0)
    print(f"4 Objectives (Power, Area, PerfError, NormLatency): {objectives}")
    print(f"Constraints                                        : {constraints}")