"""
energyObjective.py (ASIC / Yosys+OpenSTA)
==========================================

Replaces the 4-objective [power, area, sqnr_error^2, norm_latency] vector
with a 3-objective [energy_nj_per_fft, sqnr_error^2, norm_latency] vector.
With a single time-multiplexed butterfly unit, area is a fixed union of the
FP4 and FP8 datapaths and takes only a handful of values over the whole
chromosome space, so it carries no search signal and is better treated as a
hard constraint. Power and area are still measured and reported every
generation - they are just no longer part of what NSGA-II is asked to
minimise.

OBJECTIVE VECTOR
----------------
Old (4): [ power_mW, area_um2, sqnr_error^2, norm_latency ]
New (3): [ energy_nj_per_fft, sqnr_error^2, norm_latency ]

energy_nj_per_fft = P_dynamic [mW] * avg_exec_cycles * crit_delay_ns [ns] / 1000

`avg_exec_cycles` is PerformanceEvaluator's measured cycle count for ONE full
FFT transform of this chromosome, so this is energy per FFT (not per cycle,
not per stage).

(mW * ns = pJ, so dividing by 1000 gives nJ - the same convention already
used by fp16_baseline/synth/run_fp16_synthesis.py and
fp32_baseline/synth/run_fp32_synthesis.py for their "Energy/FFT (nJ)" column,
so the mixed-precision numbers land in the same units as the baselines.)

WHERE THE DYNAMIC/STATIC SPLIT COMES FROM
------------------------------------------
OpenSTA's `report_power` prints one row per power group (sequential,
combinational, macro, pad, Total) with four columns: Internal, Switching,
Leakage, Total (in Watts). `_run_yosys_opensta` in objectiveEvaluationFFT.py
now keeps all three components off the Total row:

    dynamic_mW = (internal + switching) * 1000
    static_mW  = leakage               * 1000
    total_mW   = total                 * 1000

That split is real and always available (no extra tool run needed), but it
is IMPORTANT to be honest about what it is not: OpenSTA is driven by a single
flat `set_power_activity -input -activity 0.2` in `_run_opensta`, not by a
per-design switching-activity trace - there is no gate-level VCD/SAIF
activity flow in this repo yet. Toggle rate is therefore constant across the
whole chromosome space, so dynamic power still varies only with the
STRUCTURE OpenSTA sees (how many of each cell type end up in the netlist),
not with how often the current chromosome actually switches its FP4 vs FP8
cones. Until a gate-level VCD/SAIF activity flow is built for the
Yosys+OpenSTA path (see butterfly_wrapper_gated.v for the operand-isolation
half of that story), treat energy_nj_per_fft as directionally useful but
not yet as a calibrated, activity-measured number.

INTEGRATION
-----------
1. objectiveEvaluationFFT.py imports `energy_objectives`, `penalty_objectives`,
   `ENERGY_OBJECTIVES` and delegates `_compute_objectives_and_constraints` to
   `energy_objectives(results)`.
2. globalVariablesMixedFFT.py sets `OBJECTIVES = 3` (was 4).
3. `results` must carry `dyn_power_mw`, `static_power_mw`, `area`
   (um2), `sqnr`, `norm_latency`, `crit_delay_ns`, `avg_exec_cycles` - all of
   which `evaluate_solution` already assembles.

STATUS: self-contained and unit-tested at the bottom of this file
(`python3 energyObjective.py`), but not yet run inside a live NSGA-II sweep.
Run the quick smoke test (`--mode test`) before a full sweep.
"""

import math

# -----------------------------------------------------------------------------
# Tunables. Keep these in one place so the paper can quote them.
# -----------------------------------------------------------------------------

ENERGY_OBJECTIVES = 3

OBJ_NAMES = ["energy_nj_per_fft", "sqnr_error_sq", "norm_latency"]

# Normalisation reference for the energy objective, in nanojoules per
# transform. Set from a baseline run: use the all-FP8 (worst-case) design's
# energy so the objective lands in roughly [0, 1]. 0 disables normalisation.
REF_ENERGY_NJ = 0.0

# If non-zero, evaluate every design at this fixed clock period (ns) instead
# of at its own critical path - useful for iso-frequency comparisons across
# chromosomes. Matches the mixed-precision evaluator's own CLOCK_PERIOD
# (globalVariablesMixedFFT.CLOCK_PERIOD = 10.0 ns), kept as a literal here so
# this module stays self-contained.
ISO_FREQUENCY_NS = 10.0

# Objective weights. Energy carries the weight that power+area used to share.
WEIGHT_ENERGY = 2.0
WEIGHT_PERFORMANCE = 30.0
WEIGHT_LATENCY = 8.0

# Constraint limits (area and total power are now constraints only).
MAX_AREA_UM2 = 600000.0
MAX_ENERGY_NJ = 0.0          # 0 = no energy cap
MIN_SQNR_DB = 10.0

# SQNR shaping, unchanged from the original formulation.
SQNR_OFFSET = 50.0
REF_SQNR_RANGE = 50.0
REF_LATENCY = 10.0

# Heavy penalty returned when synthesis or simulation failed.
PENALTY_ENERGY_NJ = 1.0e9


# -----------------------------------------------------------------------------
# Core computation
# -----------------------------------------------------------------------------
def compute_energy_nj_per_fft(dyn_power_mw, avg_exec_cycles, crit_delay_ns,
                              iso_period_ns=None):
    """
    Dynamic energy per FFT transform, in nanojoules.

    `avg_exec_cycles` is the number of clock cycles PerformanceEvaluator
    measures to complete one full FFT execution for this chromosome (see
    performance_evaluator.py), so this is energy per FFT, not per cycle or
    per stage:

        E = P_dyn [mW] * N_cycles_per_fft * T_clk [ns] / 1000

    Returns PENALTY_ENERGY_NJ for any non-physical input, so a failed
    synthesis is dominated rather than silently rewarded.
    """
    period_ns = ISO_FREQUENCY_NS if iso_period_ns is None else iso_period_ns
    if not period_ns:
        period_ns = crit_delay_ns

    for v in (dyn_power_mw, avg_exec_cycles, period_ns):
        if v is None:
            return PENALTY_ENERGY_NJ
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return PENALTY_ENERGY_NJ
        if math.isnan(fv) or math.isinf(fv) or fv <= 0:
            return PENALTY_ENERGY_NJ

    # mW * cycles * ns  ->  pJ  ->  / 1000 => nJ
    return float(dyn_power_mw) * float(avg_exec_cycles) * float(period_ns) / 1.0e3


def parse_power_fields(results):
    """
    Pull the dynamic-power figure out of a results dict, falling back to
    total power with a loud marker if the Internal/Switching/Leakage split
    was not available (e.g. OpenSTA failed or printed an unexpected report).

    Returns (dyn_power_mw, degraded) where `degraded` is True when we had to
    fall back - those runs must not be mixed with genuinely split runs in a
    table.
    """
    dyn = results.get('dyn_power_mw')

    if dyn is None:
        total = results.get('power', 0.0)
        static_guess = results.get('static_power_mw')
        if static_guess is not None:
            dyn = max(float(total) - float(static_guess), 0.0)
        else:
            # No split available. On this part leakage is usually a small
            # fraction of total at the flat 0.2 activity guess, but using
            # total power as "dynamic" would silently overstate energy, so
            # this is flagged rather than assumed.
            return (float(total), True)
        return (float(dyn), True)

    return (float(dyn), False)


# -----------------------------------------------------------------------------
# Objective / constraint assembly
# -----------------------------------------------------------------------------
def energy_objectives(results):
    """
    Drop-in replacement for
    MixedPrecisionFFTProblem._compute_objectives_and_constraints.

    Returns (objectives, constraints) with 3 objectives and 3 constraints.
    """
    area = results.get('area', MAX_AREA_UM2 * 2)
    sqnr = results.get('sqnr', -100.0)
    norm_latency = results.get('norm_latency', 10.0)
    cycles = results.get('avg_exec_cycles', -1)
    crit_delay_ns = results.get('crit_delay_ns', 0.0)

    dyn_power_mw, _degraded = parse_power_fields(results)

    energy_nj_per_fft = compute_energy_nj_per_fft(dyn_power_mw, cycles, crit_delay_ns)

    e_norm = energy_nj_per_fft / REF_ENERGY_NJ if REF_ENERGY_NJ else energy_nj_per_fft
    # Hinge, not a parabola: monotone non-increasing in SQNR, no penalty at or
    # above target. ((50 - sqnr)/50)**2 would have its minimum AT 50 dB and
    # climb again above it, wrongly penalising an exact design as much as an
    # inaccurate one.
    perf_obj = (max(0.0, SQNR_OFFSET - sqnr) / REF_SQNR_RANGE) ** 2

    objectives = [
        e_norm * WEIGHT_ENERGY,
        perf_obj * WEIGHT_PERFORMANCE,
        (norm_latency / REF_LATENCY) * WEIGHT_LATENCY,
    ]

    constraints = [
        area - MAX_AREA_UM2,
        (energy_nj_per_fft - MAX_ENERGY_NJ) if MAX_ENERGY_NJ else -1.0,
        MIN_SQNR_DB - sqnr,
    ]

    # stash for logging / CSV so the paper can report the raw number
    results['energy_nj_per_fft'] = energy_nj_per_fft
    results['dyn_power_mw_used'] = dyn_power_mw

    return objectives, constraints


def penalty_objectives():
    """Objective vector for a solution whose evaluation failed."""
    return (
        [PENALTY_ENERGY_NJ * WEIGHT_ENERGY, 50.0 * WEIGHT_PERFORMANCE,
         10.0 * WEIGHT_LATENCY],
        [MAX_AREA_UM2, 1.0, MIN_SQNR_DB],
    )


# -----------------------------------------------------------------------------
# Self-test
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    print("energyObjective (ASIC) self-test")

    # Illustrative numbers in the same ballpark as the mixed-precision
    # evaluator's own MAX_POWER_MW / CLOCK_PERIOD, scaled so dynamic power
    # rises with FP8 usage (expressed directly in mW since there is no
    # activity trace yet).
    base_cycles = 1123
    cases = [
        # label,            dyn_mW,  crit_ns
        ("all-FP4  (0/8)",  1.80,    8.6),
        ("mixed    (4/8)",  2.56,    9.4),
        ("all-FP8  (8/8)",  3.32,    9.9),
    ]
    prev = -1.0
    for label, dw, cd in cases:
        e = compute_energy_nj_per_fft(dw, base_cycles, cd)
        res = dict(area=125000, sqnr=20.0, norm_latency=0.6,
                   avg_exec_cycles=base_cycles, crit_delay_ns=cd,
                   dyn_power_mw=dw, static_power_mw=0.05)
        objs, cons = energy_objectives(res)
        print(f"  {label}: E = {e:11.4f} nJ/FFT   objs[0] = {objs[0]:.4g}")
        assert e > prev, "energy must increase with FP8 usage"
        prev = e
        assert len(objs) == ENERGY_OBJECTIVES
        assert len(cons) == 3

    # degradation path: no dynamic/static split available
    r = dict(area=125000, sqnr=20.0, norm_latency=0.6, avg_exec_cycles=1123,
             crit_delay_ns=9.9, power=3.4)
    dyn, degraded = parse_power_fields(r)
    assert degraded, "must flag a total-power fallback"
    print(f"  fallback path: dyn={dyn} degraded={degraded}")

    # failure path
    assert compute_energy_nj_per_fft(0.0, 1123, 9.9) == PENALTY_ENERGY_NJ
    assert compute_energy_nj_per_fft(3.0, -1, 9.9) == PENALTY_ENERGY_NJ
    assert compute_energy_nj_per_fft(float('nan'), 1123, 9.9) == PENALTY_ENERGY_NJ
    print("  penalty paths OK")

    print("ALL SELF-TESTS PASSED")
