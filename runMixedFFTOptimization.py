"""
Main Script for Mixed-Precision FFT Optimization
Orchestrates the complete NSGA-II optimization flow with Yosys + OpenSTA integration.
"""

import numpy as np
import os
import shutil
import zipfile
import csv
import glob
import math
import hashlib

import matplotlib
matplotlib.use('Agg')           # non-interactive — safe on headless servers
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.operators.sampling.rnd import IntegerRandomSampling
from pymoo.termination import get_termination
from pymoo.optimize import minimize

from globalVariablesMixedFFT import *
from objectiveEvaluationFFT import MixedPrecisionFFTProblem
# Objective shaping constants live in energyObjective, which is the single
# source of truth for them. globalVariablesMixedFFT also defines WEIGHT_* names
# for the retired 4-objective vector; importing explicitly here prevents the
# star-import above from silently supplying the stale values.
from energyObjective import (WEIGHT_ENERGY       as _W_ENERGY,
                             WEIGHT_PERFORMANCE  as _W_PERF,
                             WEIGHT_LATENCY      as _W_LATENCY,
                             REF_ENERGY_NJ       as _REF_ENERGY_NJ,
                             REF_LATENCY         as _REF_LATENCY,
                             REF_SQNR_RANGE      as _REF_SQNR_RANGE,
                             SQNR_OFFSET         as _SQNR_OFFSET)
from optimizationUtils import (
    MyCallback,
    SmartInitialSampling,
    StagewiseMutation,
    StagewiseCrossover,
)


def _sqnr_from_perf_obj(perf_obj_scaled):
    perf_obj = perf_obj_scaled / _W_PERF
    # math.sqrt(max(0.0,...)) protects the interpreter from floating-point noise (-1e-16)
    sqnr = _SQNR_OFFSET - (math.sqrt(max(0.0, perf_obj)) * _REF_SQNR_RANGE)
    return sqnr

def _crit_delay_ns_from_norm_latency(norm_latency, fft_size):
    num_stages = int(math.log2(fft_size))
    pipeline_factor = max(1.0, num_stages / 6.0)
    crit_delay = (norm_latency / pipeline_factor) * REFERENCE_CLOCK_PERIOD_NS
    return crit_delay


def _decode_objectives(obj_row, fft_size, chromosome=None):
    """Recover physical quantities from one 3-objective row:

        [ energy_nJ_perFFT * WEIGHT_ENERGY,
          sqnr_err^2 * WEIGHT_PERFORMANCE,
          (norm_latency / REF_LATENCY) * WEIGHT_LATENCY ]

    Energy, SQNR and latency invert exactly from the objective vector.

    Area and the power split do NOT appear in it any more. Area left the
    objective vector because the shared butterfly is a fixed union of both
    datapaths, so area takes only a handful of values across the whole
    chromosome space; inverting obj_row[1] into an area would now fabricate a
    number. Both are looked up in RESULT_CACHE by chromosome instead, and
    reported as -1 / 0.0 when no chromosome is supplied or the design is not
    cached.
    """
    energy_nj_per_fft = obj_row[0] / _W_ENERGY
    if _REF_ENERGY_NJ:
        energy_nj_per_fft *= _REF_ENERGY_NJ

    sqnr_db      = _sqnr_from_perf_obj(obj_row[1])
    norm_latency = (obj_row[2] / _W_LATENCY) * _REF_LATENCY

    crit_delay   = _crit_delay_ns_from_norm_latency(norm_latency, fft_size)

    cached = {}
    if chromosome is not None:
        key = ''.join(str(int(v)) for v in chromosome)
        cached = RESULT_CACHE.get(hashlib.md5(key.encode()).hexdigest(), {})

    def _f(k, default=0.0):
        v = cached.get(k)
        return default if v is None else float(v)

    # meets_timing must come from OpenSTA's own reported slack, not a naive
    # crit_delay <= clock_period comparison: the real "data required time"
    # OpenSTA checks against is NOT simply the clock period -- it is offset
    # by input/output delay constraints, clock reconvergence pessimism, and
    # (for paths launched from a macro's own negedge-characterized read
    # port, as seen post-route here) which clock edge the worst path
    # actually starts from. A design can post a crit_delay of 10.39ns
    # against a 10.0ns clock and still have positive slack for exactly
    # these reasons -- trust the cached slack_ns when it is available, and
    # only fall back to the naive comparison when no cache entry exists
    # (e.g. decoding an old run's saved objective vectors with no
    # RESULT_CACHE backing them).
    slack_ns = cached.get('slack_ns')
    if slack_ns is not None and not (isinstance(slack_ns, float) and math.isnan(slack_ns)):
        slack_ns = float(slack_ns)
        meets_timing = slack_ns >= 0.0
    else:
        slack_ns = float('nan')
        meets_timing = crit_delay <= REFERENCE_CLOCK_PERIOD_NS

    return {
        'energy_nJ_perFFT': energy_nj_per_fft,
        'power_mW':        _f('dyn_power_mw_used', _f('dyn_power_mw')),  # dynamic
        'total_power_mW':  _f('power'),
        'static_power_mW': _f('static_power_mw'),
        'area_um2':        _f('area', -1.0),
        # Prefer the measured value. Under the hinge every design at or above
        # target maps to perf_obj = 0, so the inversion cannot tell them apart;
        # the cache carries the true figure.
        'sqnr_db':        (float(cached['sqnr']) if cached.get('sqnr') is not None
                           else sqnr_db),
        'norm_latency':   norm_latency,
        'crit_delay_ns':  crit_delay,
        'slack_ns':       slack_ns,
        'meets_timing':   meets_timing,
    }

def setup_verilog_sources():
    log_message("Setting up Verilog source files")
    wrapper_src = '../verilog_sources/mixed_precision_wrappers.v'
    wrapper_dst = os.path.join(VERILOG_SOURCES_DIR, 'mixed_precision_wrappers.v')
    macro_src = '../verilog_sources/sram_512x24_2rw.v'
    macro_dst = os.path.join(VERILOG_SOURCES_DIR, 'sram_512x24_2rw.v')
    if os.path.exists(wrapper_src):
        shutil.copy(wrapper_src, wrapper_dst)
        log_message("Copied wrapper file")
    if os.path.exists(macro_src):
        shutil.copy(macro_src, macro_dst)
        log_message("Copied SRAM macro file")


def export_solutions_csv(result, fft_size, results_subdir):
    from fft_template_generator import FFTTemplateGenerator
    num_stages = FFTTemplateGenerator(fft_size).num_stages

    gene_headers = []
    for s in range(num_stages):
        gene_headers += [f"s{s}_mult", f"s{s}_add"]

    csv_path = os.path.join(results_subdir, f"all_solutions_fft{fft_size}.csv")

    pareto_set = set()
    if result.X is not None:
        for row in result.X:
            pareto_set.add(tuple(int(v) for v in row))

    pop   = result.pop
    all_X = pop.get("X") if pop is not None else np.empty((0, len(gene_headers)))
    all_F = pop.get("F") if pop is not None else np.empty((0, OBJECTIVES))

    if result.X is not None and result.F is not None:
        combined_X = np.vstack([result.X, all_X])
        combined_F = np.vstack([result.F, all_F])
    else:
        combined_X = all_X
        combined_F = all_F

    if len(combined_X) > 0:
        _, unique_idx = np.unique(combined_X, axis=0, return_index=True)
        combined_X = combined_X[unique_idx]
        combined_F = combined_F[unique_idx]

    with open(csv_path, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(
            ['solution_id', 'fft_size'] + gene_headers +
            ['energy_nJ_perFFT', 'dyn_power_mW', 'static_power_mW', 'total_power_mW',
             'area_um2', 'sqnr_dB',
             'norm_latency', 'crit_delay_ns', 'slack_ns', 'meets_timing',
             'avg_exec_cycles', 'tot_sim_cycles',
             'on_pareto_front']
        )
        for idx, (x_row, f_row) in enumerate(zip(combined_X, combined_F)):
            dec      = _decode_objectives(f_row, fft_size, chromosome=x_row)
            sqnr_val = dec['sqnr_db']
            on_pf    = int(tuple(int(v) for v in x_row) in pareto_set)

            chrom_key = ''.join(str(int(v)) for v in x_row)
            cached    = RESULT_CACHE.get(
                hashlib.md5(chrom_key.encode()).hexdigest(), {}
            )
            avg_exec = cached.get('avg_exec_cycles', -1)
            tot_sim  = cached.get('tot_sim_cycles',  -1)

            writer.writerow(
                [idx, fft_size] +
                [int(v) for v in x_row] +
                [f"{dec['energy_nJ_perFFT']:.4f}",
                 f"{dec['power_mW']:.6f}",
                 f"{dec['static_power_mW']:.6f}",
                 f"{dec['total_power_mW']:.6f}",
                 int(dec['area_um2']),
                 f"{sqnr_val:.4f}" if not math.isinf(sqnr_val) else "inf",
                 f"{dec['norm_latency']:.4f}",
                 f"{dec['crit_delay_ns']:.3f}",
                 (f"{dec['slack_ns']:.4f}" if not math.isnan(dec['slack_ns']) else "nan"),
                 int(dec['meets_timing']),
                 avg_exec,
                 tot_sim,
                 on_pf]
            )

    log_message(f"Solution CSV saved → {csv_path}  ({len(combined_X)} rows)")
    return csv_path


def parse_solution_txts_to_csv(fft_size, results_subdir):
    import ast as _ast, re

    num_stages   = int(math.log2(fft_size))
    gene_headers = []
    for s in range(num_stages):
        gene_headers += [f"s{s}_mult", f"s{s}_add"]

    csv_path  = os.path.join(results_subdir, f"all_generations_fft{fft_size}.csv")
    pattern   = os.path.join(RESULTS_DIR, "gen*_sol*.txt")
    txt_files = sorted(glob.glob(pattern))

    rows = []
    for fpath in txt_files:
        try:
            with open(fpath) as f:
                content = f.read()

            def _field(label):
                m = re.search(rf'^{label}\s*:\s*(.+)$', content, re.MULTILINE)
                return m.group(1).strip() if m else None

            if _field('FFT Size') is None or int(_field('FFT Size')) != fft_size:
                continue

            generation  = int(_field('Generation') or -1)
            solution_id = int(_field('Solution ID') or -1)
            chrom_raw   = _field('Chromosome')
            chromosome  = _ast.literal_eval(chrom_raw) if chrom_raw else []

            # Anchored to the start of the line (after leading whitespace) so
            # "Power" does not also match the "Dynamic Power" / "Static Power"
            # lines that now precede it in the per-solution .txt file.
            energy_m = re.search(r'^\s*Energy/FFT\s*:\s*([\d.\-]+)\s*nJ',      content, re.MULTILINE)
            dynp_m   = re.search(r'^\s*Dynamic Power\s*:\s*([\d.]+)\s*mW',     content, re.MULTILINE)
            statp_m  = re.search(r'^\s*Static Power\s*:\s*([\d.]+)\s*mW',     content, re.MULTILINE)
            power_m  = re.search(r'^\s*Power\s*:\s*([\d.]+)\s*mW',            content, re.MULTILINE)
            area_m   = re.search(r'^\s*Area\s*:\s*([\d.]+)\s*um2',           content, re.MULTILINE)
            sqnr_m   = re.search(r'^\s*SQNR\s*:\s*([\d.\-]+)\s*dB',          content, re.MULTILINE)
            cpd_m    = re.search(r'^\s*Crit Path Delay\s*:\s*([\d.]+)\s*ns', content, re.MULTILINE)
            slack_m  = re.search(r'^\s*Timing Slack\s*:\s*([\d.\-]+|N/A)\s*ps', content, re.MULTILINE)
            nlat_m   = re.search(r'^\s*Norm Latency\s*:\s*([\d.]+)',        content, re.MULTILINE)
            aec_m    = re.search(r'^\s*Avg Exec Cycles\s*:\s*([-\d]+)',     content, re.MULTILINE)
            tsc_m    = re.search(r'^\s*Tot Sim Cycles\s*:\s*([-\d]+)',      content, re.MULTILINE)
            fp4mt_m  = re.search(r'FP4 Multipliers:\s*\d+\s*\(([\d.]+)%\)', content)
            fp8mt_m  = re.search(r'FP8 Multipliers:\s*\d+\s*\(([\d.]+)%\)', content)
            fp4ad_m  = re.search(r'FP4 Adders\s*:\s*\d+\s*\(([\d.]+)%\)',   content)
            fp8ad_m  = re.search(r'FP8 Adders\s*:\s*\d+\s*\(([\d.]+)%\)',   content)

            energy       = float(energy_m.group(1)) if energy_m else float('nan')
            dyn_power    = float(dynp_m.group(1))   if dynp_m   else float('nan')
            static_power = float(statp_m.group(1))  if statp_m  else float('nan')
            power        = float(power_m.group(1))  if power_m  else float('nan')
            area         = float(area_m.group(1))     if area_m   else -1
            sqnr         = float(sqnr_m.group(1))   if sqnr_m   else float('nan')
            crit_delay   = float(cpd_m.group(1))    if cpd_m    else float('nan')
            slack_ns     = (float(slack_m.group(1)) / 1000.0
                             if slack_m and slack_m.group(1) != 'N/A' else float('nan'))
            norm_latency = float(nlat_m.group(1))   if nlat_m   else float('nan')
            avg_exec     = int(aec_m.group(1))      if aec_m    else -1
            tot_sim      = int(tsc_m.group(1))      if tsc_m    else -1
            # Prefer the real OpenSTA slack over a naive crit_delay<=period
            # comparison -- see _decode_objectives's comment for why they can
            # disagree (I/O delay constraints, clock reconvergence
            # pessimism, negedge-launched macro read paths, etc).
            meets_timing = int(slack_ns >= 0.0) if not math.isnan(slack_ns) else \
                           int(crit_delay <= REFERENCE_CLOCK_PERIOD_NS) \
                           if not math.isnan(crit_delay) else -1

            rows.append({
                'generation':    generation,
                'solution_id':   solution_id,
                'fft_size':      fft_size,
                'chromosome':    chromosome,
                'energy_nJ_perFFT': energy,
                'dyn_power_mW':  dyn_power,
                'static_power_mW': static_power,
                'power_mW':      power,
                'area_um2':      area,
                'sqnr_dB':       sqnr,
                'norm_latency':  norm_latency,
                'crit_delay_ns': crit_delay,
                'slack_ns':      slack_ns,
                'meets_timing':  meets_timing,
                'avg_exec_cycles': avg_exec,
                'tot_sim_cycles':  tot_sim,
                'fp4_mult':      float(fp4mt_m.group(1)) if fp4mt_m else float('nan'),
                'fp8_mult':      float(fp8mt_m.group(1)) if fp8mt_m else float('nan'),
                'fp4_add':       float(fp4ad_m.group(1)) if fp4ad_m else float('nan'),
                'fp8_add':       float(fp8ad_m.group(1)) if fp8ad_m else float('nan'),
            })
        except Exception as e:
            log_message(f"  Could not parse {fpath}: {e}", level='WARN')

    rows.sort(key=lambda r: (r['generation'], r['solution_id']))

    with open(csv_path, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(
            ['generation', 'solution_id', 'fft_size'] +
            gene_headers +
            ['energy_nJ_perFFT', 'dyn_power_mW', 'static_power_mW', 'power_mW',
             'area_um2', 'sqnr_dB',
             'norm_latency', 'crit_delay_ns', 'slack_ns', 'meets_timing',
             'avg_exec_cycles', 'tot_sim_cycles',
             'fp4_mult_pct', 'fp8_mult_pct', 'fp4_add_pct', 'fp8_add_pct']
        )
        n = num_stages * 2
        for r in rows:
            chrom = (r['chromosome'] + [0] * n)[:n]
            sqnr_str = f"{r['sqnr_dB']:.4f}" if not math.isnan(r['sqnr_dB']) else "nan"
            writer.writerow(
                [r['generation'], r['solution_id'], r['fft_size']] +
                chrom +
                [f"{r['energy_nJ_perFFT']:.4f}",
                 f"{r['dyn_power_mW']:.6f}",
                 f"{r['static_power_mW']:.6f}",
                 f"{r['power_mW']:.6f}",
                 r['area_um2'],
                 sqnr_str,
                 f"{r['norm_latency']:.4f}",
                 f"{r['crit_delay_ns']:.3f}",
                 (f"{r['slack_ns']:.4f}" if not math.isnan(r['slack_ns']) else "nan"),
                 r['meets_timing'],
                 r['avg_exec_cycles'],
                 r['tot_sim_cycles'],
                 f"{r['fp4_mult']:.1f}",
                 f"{r['fp8_mult']:.1f}",
                 f"{r['fp4_add']:.1f}",
                 f"{r['fp8_add']:.1f}"]
            )

    log_message(
        f"All-generations CSV saved → {csv_path}  ({len(rows)} solutions)"
    )
    return txt_files


def compress_solution_txt_files(fft_size, results_subdir, txt_files):
    if not txt_files:
        log_message("No solution .txt files to compress.", level='WARN')
        return
    zip_path = os.path.join(results_subdir, f"solution_logs_fft{fft_size}.zip")
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for fpath in txt_files:
            zf.write(fpath, os.path.basename(fpath))
    if os.path.exists(zip_path):
        deleted = 0
        for fpath in txt_files:
            try:
                os.remove(fpath)
                deleted += 1
            except OSError as e:
                log_message(f"  Warning: could not remove {fpath}: {e}", level='WARN')
        log_message(
            f"Compressed {len(txt_files)} solution log(s) → {zip_path} "
            f"({deleted} deleted)"
        )
    else:
        log_message("solution_logs zip failed — cleanup skipped.", level='WARN')


def compress_rtl_files(results_subdir, fft_size):
    zip_path    = os.path.join(results_subdir, f"rtl_fft{fft_size}.zip")
    zipped_files = []
    sim_dir      = os.path.abspath('./sim')

    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:

        def _add(filepath, arcdir):
            arcname = os.path.join(arcdir, os.path.basename(filepath))
            zf.write(filepath, arcname)
            zipped_files.append(filepath)

        for f in glob.glob(os.path.join(GENERATED_DESIGNS_DIR,
                                        f"fft_{fft_size}_*.v")):
            _add(f, 'generated_designs')
        for f in glob.glob(os.path.join(results_subdir, '**', '*.v'),
                           recursive=True):
            arcname = os.path.relpath(f, results_subdir)
            zf.write(f, arcname)
            zipped_files.append(f)
        for f in glob.glob(os.path.join(sim_dir, f"tb_fft_{fft_size}_*.v")):
            _add(f, 'sim')
        for f in glob.glob(os.path.join(sim_dir,
                                        f"fft_{fft_size}_*_output.txt")):
            _add(f, 'sim')
        for f in glob.glob(os.path.join(sim_dir, f"fft_{fft_size}_*.vvp")):
            _add(f, 'sim')
        twiddle = os.path.join(sim_dir, 'twiddles_1024.txt')
        if os.path.exists(twiddle):
            zf.write(twiddle, os.path.join('sim', 'twiddles_1024.txt'))

    if os.path.exists(zip_path):
        for f in zipped_files:
            try:
                os.remove(f)
            except OSError:
                pass
        log_message(
            f"RTL zip: {len(zipped_files)} file(s) → {zip_path} "
            f"(originals deleted)"
        )
    else:
        log_message("RTL zip creation failed — cleanup skipped.", level='WARN')


def compress_reports(results_subdir, fft_size):
    zip_path = os.path.join(results_subdir, f"reports_fft{fft_size}.zip")
    zipped_files = []

    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        pattern = os.path.join(REPORTS_DIR, f"fft_{fft_size}_*")
        for f in glob.glob(pattern):
            if os.path.isfile(f):
                arcname = os.path.join('reports', os.path.basename(f))
                zf.write(f, arcname)
                zipped_files.append(f)

    if os.path.exists(zip_path) and zipped_files:
        for f in zipped_files:
            try:
                os.remove(f)
            except OSError:
                pass
        log_message(
            f"Reports zip: {len(zipped_files)} file(s) → {zip_path} "
            f"(originals deleted)"
        )
    elif not zipped_files:
        log_message(f"No reports found to zip for FFT-{fft_size}.", level='WARN')
    else:
        log_message("Reports zip creation failed — cleanup skipped.", level='WARN')


def compress_synth_work(results_subdir, fft_size):
    zip_path = os.path.join(results_subdir, f"synth_work_fft{fft_size}.zip")
    zipped_dirs = []

    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        pattern = os.path.join(SYNTH_WORK_DIR, f"fft_{fft_size}_*")
        for target_dir in glob.glob(pattern):
            if os.path.isdir(target_dir):
                for root, _, files in os.walk(target_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, SYNTH_WORK_DIR)
                        arcname = os.path.join('synth_work', arcname)
                        zf.write(file_path, arcname)
                zipped_dirs.append(target_dir)

    if os.path.exists(zip_path) and zipped_dirs:
        for d in zipped_dirs:
            try:
                shutil.rmtree(d)
            except OSError as e:
                log_message(f"Could not remove directory {d}: {e}", level='WARN')
        log_message(
            f"Synth work zip: {len(zipped_dirs)} directory/ies → {zip_path} "
            f"(originals deleted)"
        )
    elif not zipped_dirs:
        log_message(f"No synth_work directories found to zip for FFT-{fft_size}.", level='WARN')
    else:
        log_message("Synth work zip creation failed — cleanup skipped.", level='WARN')


_OBJ_COLORS = {
    'power':   '#2196F3',   
    'area':    '#FF9800',   
    'sqnr':    '#4CAF50',   
    'latency': '#E91E63',   
}

_TIMING_OK_COLOR  = '#4CAF50'   
_TIMING_BAD_COLOR = '#E91E63'   


def _scatter_with_timing(ax, xdata, ydata, meets_timing_arr,
                         xlabel, ylabel, title, size=60):
    ok  = meets_timing_arr.astype(bool)
    bad = ~ok

    if ok.any():
        ax.scatter(xdata[ok],  ydata[ok],
                   c=_TIMING_OK_COLOR,  alpha=0.80, edgecolors='k',
                   linewidths=0.5, s=size, label='Meets timing', zorder=3)
    if bad.any():
        ax.scatter(xdata[bad], ydata[bad],
                   c=_TIMING_BAD_COLOR, alpha=0.80, edgecolors='k',
                   linewidths=0.5, s=size, marker='X',
                   label='Violates timing', zorder=3)

    ax.set_xlabel(xlabel, fontsize=10)
    ax.set_ylabel(ylabel, fontsize=10)
    ax.set_title(title,   fontsize=10)
    ax.grid(True, linestyle='--', alpha=0.4)
    if ok.any() or bad.any():
        ax.legend(fontsize=8, loc='best')


def plot_pareto_front(pareto_objectives, fft_size, results_subdir, feasible=True,
                      pareto_solutions=None):
    """Plot the front. All quantities come from _decode_objectives so there is
    exactly one place that knows the objective-vector layout.

    `pareto_solutions` (the matching X rows) is what lets area and dynamic
    power be looked up in RESULT_CACHE; without it those axes are unavailable
    and the panels using them are skipped rather than drawn from fabricated
    values."""
    if pareto_objectives is None or len(pareto_objectives) == 0:
        log_message("No objectives to plot — Pareto plots skipped.", level='WARN')
        return

    obj = np.array(pareto_objectives)
    n   = len(obj)

    sol = pareto_solutions if pareto_solutions is not None else []
    dec = [_decode_objectives(obj[i], fft_size,
                              chromosome=sol[i] if i < len(sol) else None)
           for i in range(n)]

    energy       = np.array([d['energy_nJ_perFFT'] for d in dec])
    power        = np.array([d['power_mW']         for d in dec])   # dynamic
    area         = np.array([d['area_um2']         for d in dec], dtype=float)
    norm_latency = np.array([d['norm_latency']      for d in dec])
    crit_delay   = np.array([d['crit_delay_ns']     for d in dec])
    sqnr         = np.array([d['sqnr_db']           for d in dec])
    sqnr         = np.where(np.isinf(sqnr), np.nan, sqnr)
    meets_timing = np.array([int(d['meets_timing']) for d in dec])

    # -1 is the decoder's "not cached" marker. Treat it as missing, never as
    # an area of -1 um2, and say so once rather than drawing a nonsense axis.
    area = np.where(area < 0, np.nan, area)
    area_ok  = bool(np.isfinite(area).any())
    power_ok = bool((power > 0).any())
    if not area_ok:
        log_message("plot_pareto_front: no cached area for these solutions "
                    "(pareto_solutions not supplied, or a fresh process) - "
                    "area panels skipped", level='WARN')

    status_label  = "Pareto Front" if feasible else "Least-Infeasible Solutions"
    pct_ok        = 100.0 * meets_timing.sum() / n

    _E = "Dynamic energy (nJ/FFT)"
    _A = "Area (µm²)"
    _S = "SQNR (dB)"
    _C = "Crit path delay (ns)"

    pairs = [
        (energy, sqnr,       _E, _S, "Energy vs SQNR"),
        (energy, crit_delay, _E, _C, "Energy vs Crit-Delay"),
        (sqnr,   crit_delay, _S, _C, "SQNR vs Crit-Delay"),
    ]
    if area_ok:
        pairs += [
            (energy, area, _E, _A, "Energy vs Area"),
            (area,   sqnr, _A, _S, "Area vs SQNR"),
        ]
    if power_ok:
        pairs += [(power, energy, "Dynamic power (mW)", _E, "Power vs Energy")]

    fig, axes = plt.subplots(2, 3, figsize=(18, 11))
    fig.suptitle(f"FFT-{fft_size}  |  {status_label}  ({n} solutions)  |  Timing pass: {pct_ok:.0f}%  |  Clock target: {REFERENCE_CLOCK_PERIOD_NS:.1f} ns", fontsize=13, fontweight='bold')

    for ax, (xd, yd, xl, yl, title) in zip(axes.flat, pairs):
        _scatter_with_timing(ax, xd, yd, meets_timing, xl, yl, title)

    for ax in [axes[0, 2], axes[1, 2]]:
        ylo, yhi = ax.get_ylim()
        ax.axhline(REFERENCE_CLOCK_PERIOD_NS, color='navy', linestyle='--', linewidth=1.2, label=f'Clock = {REFERENCE_CLOCK_PERIOD_NS:.1f} ns')
        ax.legend(fontsize=8, loc='best')
        ax.set_ylim(ylo, yhi)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    path_2d = os.path.join(results_subdir, f"pareto_2d_fft{fft_size}.png")
    fig.savefig(path_2d, dpi=DPI, bbox_inches='tight')
    plt.close(fig)
    log_message(f"2-D Pareto plot saved → {path_2d}")

    fig3d = plt.figure(figsize=(10, 8))
    ax3d  = fig3d.add_subplot(111, projection='3d')
    clim_max = 2.0 * REFERENCE_CLOCK_PERIOD_NS
    c_vals   = np.clip(crit_delay, 0, clim_max)
    sc = ax3d.scatter(power, area, sqnr, c=c_vals, cmap='RdYlGn_r', vmin=0, vmax=clim_max, alpha=0.85, edgecolors='k', linewidths=0.4, s=70)
    ax3d.set_xlabel("Dynamic power (mW)", fontsize=9, labelpad=8)
    ax3d.set_ylabel("Area (µm²)", fontsize=9, labelpad=8)
    ax3d.set_zlabel("SQNR (dB)",   fontsize=9, labelpad=8)
    ax3d.set_title(f"FFT-{fft_size}  |  {status_label}\nColour = Critical Path Delay (ns)  |  Clock target = {REFERENCE_CLOCK_PERIOD_NS:.1f} ns", fontsize=11)
    cbar = fig3d.colorbar(sc, ax=ax3d, pad=0.12, shrink=0.6, label='Crit Path Delay (ns)')
    cbar.ax.axhline(REFERENCE_CLOCK_PERIOD_NS, color='navy', linewidth=2, linestyle='--')
    cbar.ax.text(1.35, REFERENCE_CLOCK_PERIOD_NS / clim_max, f' ← {REFERENCE_CLOCK_PERIOD_NS:.0f} ns target', transform=cbar.ax.transAxes, va='center', fontsize=8, color='navy')

    path_3d = os.path.join(results_subdir, f"pareto_3d_fft{fft_size}.png")
    fig3d.savefig(path_3d, dpi=DPI, bbox_inches='tight')
    plt.close(fig3d)
    log_message(f"3-D Pareto plot saved → {path_3d}")

    fig_lat, axes_lat = plt.subplots(1, 3, figsize=(18, 5))
    fig_lat.suptitle(f"FFT-{fft_size}  |  Critical Path Delay Analysis  |  {status_label}  ({n} solutions)\nClock target: {REFERENCE_CLOCK_PERIOD_NS:.1f} ns  |  Timing pass rate: {pct_ok:.0f}%", fontsize=12, fontweight='bold')

    ax_hist = axes_lat[0]
    bins    = min(20, max(5, n // 3))
    ok_vals  = crit_delay[meets_timing.astype(bool)]
    bad_vals = crit_delay[~meets_timing.astype(bool)]
    if len(ok_vals):
        ax_hist.hist(ok_vals,  bins=bins, color=_TIMING_OK_COLOR, alpha=0.75, label='Meets timing', edgecolor='k', linewidth=0.4)
    if len(bad_vals):
        ax_hist.hist(bad_vals, bins=bins, color=_TIMING_BAD_COLOR, alpha=0.75, label='Violates timing', edgecolor='k', linewidth=0.4)
    ax_hist.axvline(REFERENCE_CLOCK_PERIOD_NS, color='navy', linestyle='--', linewidth=1.5, label=f'Target = {REFERENCE_CLOCK_PERIOD_NS:.1f} ns')
    ax_hist.set_xlabel("Critical Path Delay (ns)", fontsize=10)
    ax_hist.set_ylabel("Count",                    fontsize=10)
    ax_hist.set_title("Delay Distribution",         fontsize=11)
    ax_hist.legend(fontsize=9)
    ax_hist.grid(True, linestyle='--', alpha=0.4)

    ax_nlat = axes_lat[1]
    _scatter_with_timing(ax_nlat, sqnr, norm_latency, meets_timing, xlabel="SQNR (dB)", ylabel=f"Norm Latency  (×{REFERENCE_CLOCK_PERIOD_NS:.0f} ns clock)", title="Norm Latency vs SQNR")
    ax_nlat.axhline(1.0, color='navy', linestyle='--', linewidth=1.2, label='Timing budget = 1.0')
    ax_nlat.legend(fontsize=8)

    ax_cpd = axes_lat[2]
    area_for_size = np.where(np.isfinite(area), area, np.nanmin(area) if np.isfinite(area).any() else 0.0)
    area_norm = (area_for_size - area_for_size.min()) / (area_for_size.max() - area_for_size.min() + 1e-9)
    sizes_cpd = 30 + 200 * area_norm
    sc_cpd = ax_cpd.scatter(power, crit_delay, c=np.where(meets_timing, _TIMING_OK_COLOR, _TIMING_BAD_COLOR), s=sizes_cpd, alpha=0.80, edgecolors='k', linewidths=0.5)
    ax_cpd.axhline(REFERENCE_CLOCK_PERIOD_NS, color='navy', linestyle='--', linewidth=1.5, label=f'Target = {REFERENCE_CLOCK_PERIOD_NS:.1f} ns')
    ax_cpd.set_xlabel("Dynamic power (mW)",       fontsize=10)
    ax_cpd.set_ylabel("Critical Path Delay (ns)", fontsize=10)
    ax_cpd.set_title("Crit Delay vs Power\n(bubble size proportional to Area)", fontsize=11)
    ax_cpd.legend(fontsize=8)
    ax_cpd.grid(True, linestyle='--', alpha=0.4)

    from matplotlib.lines import Line2D
    legend_handles = [
        Line2D([0], [0], marker='o', color='w', markerfacecolor=_TIMING_OK_COLOR, markersize=9, label='Meets timing'),
        Line2D([0], [0], marker='X', color='w', markerfacecolor=_TIMING_BAD_COLOR, markersize=9, label='Violates timing'),
    ]
    ax_cpd.legend(handles=legend_handles, fontsize=8, loc='upper right')

    plt.tight_layout(rect=[0, 0, 1, 0.93])
    path_lat = os.path.join(results_subdir, f"pareto_latency_fft{fft_size}.png")
    fig_lat.savefig(path_lat, dpi=DPI, bbox_inches='tight')
    plt.close(fig_lat)
    log_message(f"Latency dashboard saved → {path_lat}")


def save_optimization_results(result, callback, fft_size):
    log_message("Saving optimization results...")

    results_subdir = os.path.join(RESULTS_DIR, f"fft_{fft_size}")
    os.makedirs(results_subdir, exist_ok=True)

    pareto_objectives = result.F
    pareto_solutions  = result.X
    feasible = pareto_solutions is not None

    if not feasible:
        log_message(
            "WARNING: No feasible solutions — saving least-infeasible fallback.",
            level='WARN'
        )
        pop = result.pop
        if pop is not None and len(pop) > 0:
            pareto_objectives = pop.get("F")
            pareto_solutions  = pop.get("X")
            cv_vals           = pop.get("CV")
            if cv_vals is not None:
                order             = np.argsort(cv_vals.ravel())
                pareto_objectives = pareto_objectives[order]
                pareto_solutions  = pareto_solutions[order]
        else:
            pareto_objectives = np.empty((0, OBJECTIVES))
            pareto_solutions  = np.empty((0,), dtype=int)

    np.save(os.path.join(results_subdir, 'pareto_objectives.npy'), pareto_objectives)
    np.save(os.path.join(results_subdir, 'pareto_solutions.npy'),  pareto_solutions)
    np.savez(os.path.join(results_subdir, 'fitness_history.npz'), *callback.data)

    front_label  = "Pareto" if feasible else "Fallback"
    summary_file = os.path.join(results_subdir, 'summary.txt')

    with open(summary_file, 'w') as f:
        f.write("Mixed-Precision FFT Optimization Results\n")
        f.write(f"{'='*72}\n\n")
        f.write(f"FFT Size              : {fft_size}\n")
        f.write(f"Population            : {POPULATION}\n")
        f.write(f"Generations           : {GENERATIONS}\n")
        f.write(f"Objectives            : {OBJECTIVES}  "
                f"(Energy/FFT, SQNR, Critical-Path Delay)\n")
        f.write(f"Area                  : hard constraint "
                f"(<= {MAX_AREA_UM2:.0f} µm²), not an objective\n")
        f.write(f"Clock target          : {REFERENCE_CLOCK_PERIOD_NS:.1f} ns\n")
        f.write(f"ASIC Process          : {ASIC_PROCESS}\n\n")

        if not feasible:
            f.write("*** WARNING: No feasible solutions found. ***\n")
            f.write("Showing least-infeasible solutions from the final population.\n\n")

        n_sol = len(pareto_solutions)
        f.write(f"{front_label} Front Solutions: {n_sol}\n\n")

        if n_sol == 0:
            f.write("No solutions to report.\n")
        else:
            decoded = [_decode_objectives(pareto_objectives[i], fft_size,
                                          chromosome=pareto_solutions[i])
                       for i in range(n_sol)]

            n_timing_ok = sum(1 for d in decoded if d['meets_timing'])
            f.write(f"Timing pass rate      : {n_timing_ok}/{n_sol} "
                    f"({100*n_timing_ok/n_sol:.0f}%)\n\n")

            hdr = (f"{'ID':<5} {'Energy(nJ)':<11} {'Power(mW)':<10} "
                   f"{'Area(µm²)':<11} {'SQNR(dB)':<10} {'NormLat':<9} "
                   f"{'CritDelay(ns)':<14} {'Slack(ns)':<10} {'MeetsTiming':<12} "
                   f"{'ExecCycles':<11} {'TotSimCycles':<12}")
            f.write(hdr + "\n")
            f.write('-' * len(hdr) + '\n')

            for i, d in enumerate(decoded):
                sqnr_str = (f"{d['sqnr_db']:.2f}"
                            if not math.isinf(d['sqnr_db']) else "  inf")
                crit_str = f"{d['crit_delay_ns']:.3f}"
                if d['norm_latency'] >= 10.0:
                    crit_str = f">={crit_str}"
                slack_str = (f"{d['slack_ns']:.3f}" if not math.isnan(d['slack_ns']) else "n/a")
                timing_str = "YES" if d['meets_timing'] else "NO "

                chrom_key = ''.join(str(int(v)) for v in pareto_solutions[i])
                cached    = RESULT_CACHE.get(
                    hashlib.md5(chrom_key.encode()).hexdigest(), {}
                )
                avg_exec = cached.get('avg_exec_cycles', -1)
                tot_sim  = cached.get('tot_sim_cycles',  -1)

                f.write(
                    f"{i:<5} {d['energy_nJ_perFFT']:<11.4f} {d['power_mW']:<10.6f} "
                    f"{int(d['area_um2']):<11} {sqnr_str:<10} {d['norm_latency']:<9.4f} "
                    f"{crit_str:<14} {slack_str:<10} {timing_str:<12} "
                    f"{str(avg_exec):<11} {str(tot_sim):<12}\n"
                )

            obj_arr = np.array(pareto_objectives)
            f.write("\n\nBest Solutions by Objective:\n")
            f.write('-' * 60 + '\n')

            # Objective columns are [energy_nJ, sqnr_error^2, norm_latency];
            # all three are minimised, so argmin on each is the best design for
            # it. Area left the objective vector (it takes only a handful of
            # values across the whole chromosome space), so there is no column
            # to take an argmin over - area is reported per solution in the
            # table above and enforced as a constraint instead.
            best_specs = [
                ("Best Energy/FFT (min)",       0, "energy_nJ_perFFT", "nJ"),
                ("Best SQNR (max perf)",        1, "sqnr_db",          "dB"),
                ("Best Crit-Path (min)",        2, "norm_latency",     "norm"),
            ]

            for label, col, key, unit in best_specs:
                idx = int(np.argmin(obj_arr[:, col]))
                d   = decoded[idx]

                chrom_key = ''.join(str(int(v)) for v in pareto_solutions[idx])
                cached    = RESULT_CACHE.get(
                    hashlib.md5(chrom_key.encode()).hexdigest(), {}
                )
                avg_exec = cached.get('avg_exec_cycles', -1)
                tot_sim  = cached.get('tot_sim_cycles',  -1)

                f.write(f"\n{label}:\n")
                f.write(f"  Solution ID       : {idx}\n")
                f.write(f"  Energy/FFT        : {d['energy_nJ_perFFT']:.4f} nJ\n")
                f.write(f"  Dynamic Power     : {d['power_mW']:.6f} mW\n")
                f.write(f"  Static Power      : {d['static_power_mW']:.6f} mW\n")
                f.write(f"  Total Power       : {d['total_power_mW']:.6f} mW\n")
                f.write(f"  Area              : {int(d['area_um2'])} µm²\n")
                sqnr_str = (f"{d['sqnr_db']:.2f} dB"
                            if not math.isinf(d['sqnr_db']) else "inf dB")
                f.write(f"  SQNR              : {sqnr_str}\n")
                f.write(f"  Norm Latency      : {d['norm_latency']:.4f}x "
                        f"(clock = {REFERENCE_CLOCK_PERIOD_NS:.1f} ns)\n")
                slack_str = (f"{d['slack_ns']:.3f} ns" if not math.isnan(d['slack_ns']) else "n/a")
                f.write(f"  Crit Path Delay   : {d['crit_delay_ns']:.3f} ns  "
                        f"(slack {slack_str})"
                        f"{'  <- MEETS TIMING' if d['meets_timing'] else '  <- VIOLATES TIMING'}\n")
                f.write(f"  Avg Exec Cycles   : {avg_exec}\n")
                f.write(f"  Tot Sim Cycles    : {tot_sim}\n")
                f.write(f"  Chromosome        : {[int(v) for v in pareto_solutions[idx]]}\n")

    log_message(f"Summary saved → {summary_file}")

    export_solutions_csv(result, fft_size, results_subdir)
    plot_pareto_front(pareto_objectives, fft_size, results_subdir, feasible,
                      pareto_solutions=pareto_solutions)
    txt_files = parse_solution_txts_to_csv(fft_size, results_subdir)
    compress_solution_txt_files(fft_size, results_subdir, txt_files)
    compress_rtl_files(results_subdir, fft_size)
    compress_reports(results_subdir, fft_size)
    compress_synth_work(results_subdir, fft_size)

    log_message(
        f"Results saved to {results_subdir}  "
        f"({front_label} front: {n_sol} solutions)"
    )


def run_optimization_for_fft_size(fft_size):
    import globalVariablesMixedFFT
    globalVariablesMixedFFT.CURRENT_FFT_SIZE = fft_size

    log_message(f"\n{'='*60}")
    log_message(f"Starting optimisation for {fft_size}-point FFT")
    log_message(f"{'='*60}\n")

    problem  = MixedPrecisionFFTProblem(fft_size=fft_size)
    callback = MyCallback()

    algorithm = NSGA2(
        pop_size=POPULATION,
        sampling=SmartInitialSampling(),
        crossover=StagewiseCrossover(fft_size=fft_size, prob=CROSSOVER_RATE),
        mutation=StagewiseMutation(fft_size=fft_size),
    )
    termination = get_termination("n_gen", GENERATIONS)

    log_message("NSGA-II Configuration:")
    log_message(f"  Population size : {POPULATION}")
    log_message(f"  Generations     : {GENERATIONS}")
    log_message(f"  Crossover rate  : {CROSSOVER_RATE}")
    log_message(f"  Mutation rate   : {MUTATION_RATE}")
    log_message(f"  Objectives      : {OBJECTIVES}  (Energy/FFT, SQNR, CritDelay); area is a constraint")
    log_message(f"  Parallel threads: {SOLUTION_THREADS}")
    log_message(f"  Clock target    : {REFERENCE_CLOCK_PERIOD_NS:.1f} ns")

    result = minimize(
        problem,
        algorithm,
        termination,
        save_history=False,
        callback=callback,
        seed=SEED,
        verbose=VERBOSE,
    )

    log_message(f"Optimisation complete for {fft_size}-point FFT")
    save_optimization_results(result, callback, fft_size)
    return result


def generate_comprehensive_summary(all_results):
    summary_file = os.path.join(RESULTS_DIR, 'comprehensive_summary.txt')
    with open(summary_file, 'w') as f:
        f.write("Mixed-Precision FFT Optimization — Comprehensive Summary\n")
        f.write("=" * 72 + "\n\n")
        f.write(f"Clock target: {REFERENCE_CLOCK_PERIOD_NS:.1f} ns  |  "
                f"ASIC Process: {ASIC_PROCESS}\n\n")

        for fft_size, result in sorted(all_results.items()):
            f.write(f"\nFFT Size: {fft_size}\n")
            f.write("-" * 72 + "\n")
            if result is None:
                f.write("  Optimisation failed\n")
                continue

            pf = result.F if result.F is not None else np.empty((0, OBJECTIVES))
            px = result.X if result.X is not None else np.empty((0, result.pop.get("X").shape[1] if result.pop else 0))
            n  = len(pf)
            f.write(f"  Pareto front size  : {n}\n")

            if n == 0:
                continue

            decoded = [_decode_objectives(pf[i], fft_size,
                                          chromosome=px[i] if i < len(px) else None)
                       for i in range(n)]

            energies = np.array([d['energy_nJ_perFFT'] for d in decoded])
            powers  = np.array([d['power_mW']      for d in decoded])
            areas   = np.array([d['area_um2']     for d in decoded])
            sqnrs   = np.array([d['sqnr_db']       for d in decoded
                                if not math.isinf(d['sqnr_db'])])
            delays  = np.array([d['crit_delay_ns'] for d in decoded])
            n_ok    = sum(1 for d in decoded if d['meets_timing'])

            f.write(f"  Energy/FFT range   : {energies.min():.4f} – {energies.max():.4f} nJ\n")
            f.write(f"  Power range        : {powers.min():.6f} – {powers.max():.6f} mW\n")
            f.write(f"  Area range         : {areas.min():.0f} – {areas.max():.0f} µm²\n")
            if len(sqnrs):
                f.write(f"  SQNR range         : {sqnrs.min():.2f} – {sqnrs.max():.2f} dB\n")
            f.write(f"  Crit-path range    : {delays.min():.3f} – {delays.max():.3f} ns\n")
            f.write(f"  Timing pass rate   : {n_ok}/{n} ({100*n_ok/n:.0f}%)\n")

    log_message(f"Comprehensive summary → {summary_file}")

    combined_csv = os.path.join(RESULTS_DIR, 'all_pareto_solutions.csv')
    with open(combined_csv, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['fft_size', 'solution_id',
                         'energy_nJ_perFFT', 'power_mW', 'area_um2', 'sqnr_dB',
                         'norm_latency', 'crit_delay_ns', 'slack_ns', 'meets_timing'])
        for fft_size, result in sorted(all_results.items()):
            if result is None or result.F is None:
                continue
            for i, (obj, chrom) in enumerate(zip(result.F, result.X)):
                d = _decode_objectives(obj, fft_size, chromosome=chrom)
                sqnr_str = (f"{d['sqnr_db']:.4f}"
                            if not math.isinf(d['sqnr_db']) else "inf")
                writer.writerow([
                    fft_size, i,
                    f"{d['energy_nJ_perFFT']:.4f}",
                    f"{d['power_mW']:.6f}",
                    int(d['area_um2']),
                    sqnr_str,
                    f"{d['norm_latency']:.4f}",
                    f"{d['crit_delay_ns']:.3f}",
                    (f"{d['slack_ns']:.4f}" if not math.isnan(d['slack_ns']) else "nan"),
                    int(d['meets_timing']),
                ])
    log_message(f"Combined Pareto CSV → {combined_csv}")

    sizes      = []
    best_energy, best_power, best_area, best_sqnr, best_delay = [], [], [], [], []

    for fft_size, result in sorted(all_results.items()):
        if result is None or result.F is None or len(result.F) == 0:
            continue
        pf      = result.F
        px      = result.X if result.X is not None else []
        decoded = [_decode_objectives(pf[i], fft_size,
                                      chromosome=px[i] if i < len(px) else None)
                   for i in range(len(pf))]

        energies = [d['energy_nJ_perFFT'] for d in decoded]
        powers  = [d['power_mW']      for d in decoded]
        areas   = [d['area_um2']      for d in decoded]
        delays  = [d['crit_delay_ns'] for d in decoded]
        sqnrs_f = [d['sqnr_db']       for d in decoded
                   if not math.isinf(d['sqnr_db']) and not math.isnan(d['sqnr_db'])]

        sizes.append(fft_size)
        best_energy.append(min(energies))
        best_power.append(min(powers))
        best_area.append(min(areas))
        best_delay.append(min(delays))
        best_sqnr.append(max(sqnrs_f) if sqnrs_f else 0.0)

    if sizes:
        fig, axes = plt.subplots(2, 2, figsize=(16, 11))
        fig.suptitle(
            "Best Achievable Metrics vs FFT Size\n"
            f"(Clock target = {REFERENCE_CLOCK_PERIOD_NS:.1f} ns  |  "
            f"ASIC Process: {ASIC_PROCESS})",
            fontsize=13, fontweight='bold'
        )

        metrics = [
            (best_energy, "Min Energy/FFT (nJ)",     '#9C27B0',              axes[0, 0], False),
            (best_area,  "Min Area (µm²)",            _OBJ_COLORS['area'],    axes[0, 1], False),
            (best_sqnr,  "Max SQNR (dB)",             _OBJ_COLORS['sqnr'],    axes[1, 0], False),
            (best_delay, "Min Crit Path Delay (ns)",  _OBJ_COLORS['latency'], axes[1, 1], True),
        ]

        for ydata, ylabel, color, ax, add_ref in metrics:
            ax.plot(sizes, ydata, 'o-', color=color, linewidth=2,
                    markersize=8, markeredgecolor='k', markeredgewidth=0.6)
            ax.set_xlabel("FFT Size (points)", fontsize=10)
            ax.set_ylabel(ylabel,              fontsize=10)
            ax.set_title(ylabel,               fontsize=11)
            ax.set_xscale('log', base=2)
            ax.set_xticks(sizes)
            ax.set_xticklabels([str(s) for s in sizes], rotation=45, ha='right')
            ax.grid(True, linestyle='--', alpha=0.4)

            if add_ref:
                ax.axhline(REFERENCE_CLOCK_PERIOD_NS, color='navy',
                           linestyle='--', linewidth=1.5,
                           label=f'Clock = {REFERENCE_CLOCK_PERIOD_NS:.1f} ns')
                ax.legend(fontsize=9)

            for sx, sy in zip(sizes, ydata):
                ax.annotate(
                    f"{sy:.3g}", (sx, sy),
                    textcoords="offset points", xytext=(0, 7),
                    ha='center', fontsize=8, color=color
                )

        plt.tight_layout()
        comp_plot = os.path.join(RESULTS_DIR, 'comparison_all_fft_sizes.png')
        fig.savefig(comp_plot, dpi=DPI, bbox_inches='tight')
        plt.close(fig)
        log_message(f"4-metric comparison plot → {comp_plot}")


def run_full_optimization_sweep():
    log_message("\n" + "=" * 60)
    log_message("Mixed-Precision FFT Optimization Framework")
    log_message("=" * 60 + "\n")

    setup_verilog_sources()
    all_results = {}

    for fft_size in FFT_SIZES:
        try:
            global CURRENT_GEN
            CURRENT_GEN = 0
            result = run_optimization_for_fft_size(fft_size)
            all_results[fft_size] = result
        except Exception as e:
            log_message(
                f"ERROR: Optimisation failed for {fft_size}-point FFT: {e}",
                level='ERROR'
            )
            all_results[fft_size] = None

    generate_comprehensive_summary(all_results)

    log_message("\n" + "=" * 60)
    log_message("Optimisation sweep complete!")
    log_message("=" * 60)


def quick_test():
    log_message("Running quick test with 256-point FFT")
    setup_verilog_sources()

    global CURRENT_GEN, POPULATION, GENERATIONS
    CURRENT_GEN = 0
    orig_pop, orig_gen = POPULATION, GENERATIONS
    POPULATION, GENERATIONS = 6, 3

    run_optimization_for_fft_size(fft_size=256)

    POPULATION, GENERATIONS = orig_pop, orig_gen
    log_message("Quick test complete")


def quick_test_postroute_1024():
    """Validates the post-route OpenROAD P&R pipeline now wired into
    MixedPrecisionFFTProblem.evaluate_solution (see
    objectiveEvaluationFFT.py's _run_postroute_pnr) end-to-end through the
    real NSGA-II machinery, for the 1024-point FFT specifically: if N=1024
    (the largest/slowest case, most register-heavy pipeline) closes at the
    10ns clock, every smaller N will too. POPULATION/GENERATIONS are cut
    down hard (2/1, vs quick_test()'s 6/3) because each solution now costs a
    full OpenROAD P&R run (floorplan+PDN+placement+CTS+global route), not
    just a Yosys+OpenSTA estimate -- this is a pipeline/closure smoke test,
    not a real search."""
    log_message("Running post-route P&R quick test with 1024-point FFT")
    setup_verilog_sources()

    global CURRENT_GEN, POPULATION, GENERATIONS
    CURRENT_GEN = 0
    orig_pop, orig_gen = POPULATION, GENERATIONS
    POPULATION, GENERATIONS = 2, 1

    run_optimization_for_fft_size(fft_size=1024)

    POPULATION, GENERATIONS = orig_pop, orig_gen
    log_message("Post-route P&R quick test (1024) complete")


def main():
    run_full_optimization_sweep()
    #quick_test_postroute_1024()
    #quick_test()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description='Mixed-Precision FFT Optimization using NSGA-II'
    )
    parser.add_argument(
        '--mode',
        choices=['test', 'single', 'full'],
        default='full',
        help=(
            'test   – quick 16-pt smoke test | '
            'single – one FFT size (--fft-size) | '
            'full   – complete sweep 2→1024 (default)'
        ),
    )
    parser.add_argument(
        '--fft-size',
        type=int,
        default=8,
        choices=[2, 4, 8, 16, 32, 64, 128, 256, 512, 1024],
        help='FFT size for --mode single',
    )

    args = parser.parse_args()

    if args.mode == 'test':
        quick_test()
    elif args.mode == 'single':
        setup_verilog_sources()
        run_optimization_for_fft_size(args.fft_size)
    else:
        main()