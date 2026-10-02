#!/usr/bin/env python3
"""
Three-way ASIC comparison: FP16 baseline vs FP32 baseline vs mixed FP4/FP8
==========================================================================
Joins the three tracks' PPA reports into one table per figure of merit, with the mixed core's
advantage expressed as a ratio. It measures nothing itself: run the drivers
first, then this.

    python3 fp16_baseline/synth/run_fp16_synthesis.py   # and run_fp16_postroute.py
    python3 fp32_baseline/synth/run_fp32_synthesis.py   # and run_fp32_postroute.py
    python3 nsga_dse/run_mixed_synthesis.py             # and run_mixed_postroute.py
    python3 nsga_dse/compare_precision_tracks_asic.py                   # pre-route
    python3 nsga_dse/compare_precision_tracks_asic.py --stage postroute

The ASIC drivers write fixed-width text tables, not CSVs, so this script
parses those tables (header-driven, so a reordered or
extended column set still parses). A row whose Status is not OK, or whose
numeric cell is N/A, contributes nothing: its cells print as a dash and no
ratio is formed from it.

Every ratio is oriented so that ABOVE 1.00 MEANS THE MIXED CORE WINS:

    area ratio       = baseline area / mixed area
    frequency ratio  = mixed fmax / baseline fmax
    energy ratio     = baseline energy per transform / mixed energy per transform
    throughput ratio = mixed throughput / baseline throughput
    per-watt ratio   = mixed transforms per joule / baseline's
    per-area ratio   = mixed throughput per mm^2 / baseline's

DEFINITIONS (ASIC-specific)
  fmax        1000 / CritDelay(ns). The reports' critical path, not the 10 ns
              constraint, so it is the achievable clock rather than the target.
  throughput  fmax * 1e6 / ExecCycles, i.e. transforms per second at fmax.
  energy      Power(mW) * ExecCycles * ClockPeriod(ns) / 1000, in nJ -- the
              formula the baseline drivers already use, applied identically to
              all three tracks. Power was measured at the constraint clock, so
              energy is at the constraint clock, not at fmax.
  per-watt    1 / energy, in transforms per joule.

WHERE THE MIXED NUMBERS COME FROM
  --stage postroute  results/fft_N/summary.txt, one per size. Each lists the
                     NSGA-II Pareto-front designs with post-route area, critical
                     path, slack, power, SQNR and cycles, and names the
                     chromosome of its three "best" designs. One design per N
                     is chosen with --mixed-pick, restricted to designs that
                     met timing:
                       energy (default)  lowest energy per transform
                       sqnr              highest SQNR
                       crit              shortest critical path
                     The pick changes every mixed number, so the output file
                     name carries it. Pick `energy` is the efficiency headline;
                     it trades accuracy away, which TABLE 7 shows.
  --stage synth      mixed_ppa_report.txt, which holds only the sizes
                     run_mixed_synthesis.py was run for, all with the all-FP8
                     reference chromosome. Other sizes print as dashes.
  Cycles come from the summaries for both stages (they are the same for every
  design at a given N). SQNR comes from the chosen design (post-route) or from
  the all-FP8 row of the NSGA results (pre-route).

COMPARABILITY
  --stage synth      Yosys cell area + OpenSTA timing, no placement. Area is the
                     sum of standard cells, so it responds to the datapath.
  --stage postroute  OpenROAD floorplan -> global route. Area there is the FIXED
                     FLOORPLAN CORE AREA, identical for every N within a track
                     because the 4-SRAM-macro layout dominates it. Different
                     tracks use different SRAM macros (512x32, 512x64, 512x24),
                     so the post-route area ratio mostly compares macro sizes,
                     not logic. Read it that way; the pre-route area table is
                     the logic comparison.
  Both stages use the same clock constraint and the same SAIF-based power
  methodology across the three tracks.
"""

import argparse
import csv as csv_mod
import glob
import math
import os
import re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

REPORTS = {
    "synth": {
        "FP16": os.path.join(REPO_ROOT, "fp16_baseline", "synth", "fp16_ppa_report.txt"),
        "FP32": os.path.join(REPO_ROOT, "fp32_baseline", "synth", "fp32_ppa_report.txt"),
        "Mixed": os.path.join(REPO_ROOT, "mixed_ppa_report.txt"),
    },
    "postroute": {
        "FP16": os.path.join(REPO_ROOT, "fp16_baseline", "synth", "fp16_postroute_ppa_report.txt"),
        "FP32": os.path.join(REPO_ROOT, "fp32_baseline", "synth", "fp32_postroute_ppa_report.txt"),
        "Mixed": os.path.join(REPO_ROOT, "mixed_postroute_ppa_report.txt"),
    },
}
SQNR_FILES = {
    "FP16": os.path.join(REPO_ROOT, "fp16_baseline", "sim", "perf", "fp16_sqnr_results.txt"),
    "FP32": os.path.join(REPO_ROOT, "fp32_baseline", "sim", "perf", "fp32_sqnr_results.txt"),
}
RESULTS_DIR = os.path.join(REPO_ROOT, "results")
TRACKS = ("FP16", "FP32", "Mixed")

# N | exec | load | unload | e2e | avg sqnr | ...
SQNR_ROW = re.compile(r"^\s*(\d+)\s*\|\s*\d+\s*\|\s*-?\d+\s*\|\s*-?\d+\s*\|\s*-?\d+\s*\|"
                      r"\s*(-?[\d.]+)\s*\|")
CLOCK_RE = re.compile(r"Clock period:\s*([\d.]+)\s*ns")
ANNOT_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_report(path):
    """Parse one driver's text table -> ({N: {header: cell}}, clock_period_ns).

    The table is the block of '|'-separated lines whose first header cell is
    'N'. Cells are returned as stripped strings; 'N/A' is left for fnum() to
    reject, so a failed row simply yields None downstream."""
    if not os.path.isfile(path):
        return {}, None
    rows, header, clock = {}, None, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = CLOCK_RE.search(line)
            if m and clock is None:
                clock = float(m.group(1))
            if "|" not in line:
                continue
            cells = [c.strip() for c in line.split("|")]
            if header is None:
                if cells[0] == "N":
                    header = cells
                continue
            if len(cells) == len(header) and cells[0].isdigit():
                rows[int(cells[0])] = dict(zip(header, cells))
    return rows, clock


def read_sqnr(path):
    if not path or not os.path.isfile(path):
        return {}
    out = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = SQNR_ROW.match(line)
            if m:
                out[int(m.group(1))] = float(m.group(2))
    return out


def read_mixed_reference(n):
    """(exec_cycles, sqnr_dB) of the all-FP8 chromosome at size n from the NSGA
    results, or (None, None) if the search never visited it."""
    d = os.path.join(RESULTS_DIR, f"fft_{n}")
    for name in (f"all_solutions_fft{n}_fixed.csv", f"all_solutions_fft{n}.csv"):
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        with open(p, newline="", encoding="utf-8") as f:
            rd = csv_mod.DictReader(f)
            genes = [c for c in (rd.fieldnames or []) if re.match(r"s\d+_(mult|add)$", c)]
            for r in rd:
                vals = [(r[c] or "").strip() for c in genes]
                vals = [v for v in vals if v != ""]
                if vals and all(v == "1" for v in vals):
                    return fnum(r.get("avg_exec_cycles")), fnum(r.get("sqnr_dB"))
        return None, None
    return None, None


SUMMARY_ROW = re.compile(
    r"^\s*(\d+)\s+([\d.]+)\s+([\d.]+)\s+(\d+)\s+(-?[\d.]+)\s+([\d.]+)\s+"
    r"([\d.]+)\s+(-?[\d.]+)\s+(YES|NO)\s+(\d+)\s+(\d+)")
CHROM_RE = re.compile(r"Chromosome\s*:\s*\[([^\]]*)\]")
SOLID_RE = re.compile(r"Solution ID\s*:\s*(\d+)")
CLKTGT_RE = re.compile(r"Clock target\s*:\s*([\d.]+)\s*ns")


def read_summary(n):
    """Parse results/fft_N/summary.txt -> {"rows": [...], "clock": ns} or None.

    Each row is one Pareto-front design from the NSGA-II run (post-route
    evaluator numbers); "chrom" is filled for the designs the 'Best Solutions by
    Objective' blocks name, which is all that file records chromosomes for."""
    path = os.path.join(RESULTS_DIR, f"fft_{n}", "summary.txt")
    if not os.path.isfile(path):
        return None
    rows, chrom, clock, cur = [], {}, None, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = CLKTGT_RE.search(line)
            if m and clock is None:
                clock = float(m.group(1))
            m = SUMMARY_ROW.match(line)
            if m:
                g = m.groups()
                rows.append({"id": int(g[0]), "energy": float(g[1]), "power": float(g[2]),
                             "area": float(g[3]), "sqnr": float(g[4]), "crit": float(g[6]),
                             "slack": float(g[7]), "meets": g[8] == "YES",
                             "cycles": int(g[9]), "chrom": None})
                continue
            m = SOLID_RE.search(line)
            if m:
                cur = int(m.group(1))
            m = CHROM_RE.search(line)
            if m and cur is not None:
                chrom[cur] = "".join(c.strip() for c in m.group(1).split(","))
    for r in rows:
        r["chrom"] = chrom.get(r["id"])
    return {"rows": rows, "clock": clock} if rows else None


MIXED_PICKS = {
    "energy": ("lowest energy per transform", lambda r: (r["energy"], -r["sqnr"])),
    "sqnr":   ("highest SQNR",                lambda r: (-r["sqnr"], r["energy"])),
    "crit":   ("shortest critical path",      lambda r: (r["crit"], r["energy"])),
}


def mixed_postroute_rows(pick):
    """({N: report-style row}, clock) for the mixed track from the NSGA summaries.

    One Pareto-front design is chosen per N by `pick`, restricted to designs that
    met timing, and returned in the same header-keyed form read_report() yields
    so the rest of the script treats it like any other track."""
    out, clock = {}, None
    for d in sorted(glob.glob(os.path.join(RESULTS_DIR, "fft_*"))):
        m = re.match(r"fft_(\d+)$", os.path.basename(d))
        if not m:
            continue
        n = int(m.group(1))
        sm = read_summary(n)
        if not sm:
            continue
        clock = clock or sm["clock"]
        ok = [r for r in sm["rows"] if r["meets"]]
        if not ok:
            out[n] = {"Status": "TIMING_FAIL"}
            continue
        r = min(ok, key=MIXED_PICKS[pick][1])
        out[n] = {"Area (um^2)": str(r["area"]), "CritDelay (ns)": str(r["crit"]),
                  "Slack (ns)": str(r["slack"]), "Power (mW)": str(r["power"]),
                  "ExecCyc": str(r["cycles"]), "Status": "OK", "AnnotatedSignals": "",
                  "_sqnr": r["sqnr"], "_chrom": r["chrom"]}
    return out, clock


def render(rows, cols, title, notes=()):
    labels = [c[0] for c in cols]
    units = [c[1] for c in cols]
    body = [[c[2](r) for c in cols] for r in rows]
    widths = [max(len(labels[i]), len(units[i]),
                  max((len(b[i]) for b in body), default=0))
              for i in range(len(cols))]
    L = ["", title,
         "  ".join(l.rjust(w) for l, w in zip(labels, widths)),
         "  ".join(u.rjust(w) for u, w in zip(units, widths)),
         "-" * (sum(widths) + 2 * (len(widths) - 1))]
    for b in body:
        L.append("  ".join(v.rjust(w) for v, w in zip(b, widths)))
    for n in notes:
        L.append("  " + n)
    return L


def ratio(num, den):
    """num / den, or None if either side is missing or the denominator is zero."""
    if num is None or den is None or den == 0:
        return None
    return num / den


def f(v, fmt="{:.2f}", dash="-"):
    return dash if v is None else fmt.format(v)


def build_rows(stage, pick="energy"):
    """One dict per N with every derived quantity, keyed by track."""
    raw, clocks = {}, {}
    for t in TRACKS:
        raw[t], clocks[t] = read_report(REPORTS[stage][t])
    if stage == "postroute":
        # The mixed post-route numbers live in the NSGA summaries (all sizes),
        # not in a driver report.
        raw["Mixed"], clocks["Mixed"] = mixed_postroute_rows(pick)
    else:
        # Pre-route mixed rows exist only where run_mixed_synthesis.py was run;
        # the report has no cycle column, so take cycles from the summary.
        for n, row in raw["Mixed"].items():
            sm = read_summary(n)
            if sm:
                row["ExecCyc"] = str(sm["rows"][0]["cycles"])
    sqnr = {t: read_sqnr(SQNR_FILES.get(t)) for t in TRACKS}

    sizes = sorted(set().union(*[set(d) for d in raw.values()]))
    rows = []
    for n in sizes:
        r = {"N": n, "area": {}, "crit": {}, "fmax": {}, "pwr": {}, "cyc": {},
             "thr": {}, "energy": {}, "tpj": {}, "tpa": {}, "sqnr": {},
             "status": {}, "annot": {}}
        mrow = raw["Mixed"].get(n) or {}
        if "_sqnr" in mrow:
            mixed_sqnr = mrow["_sqnr"]
        else:
            mixed_sqnr = read_mixed_reference(n)[1]   # all-FP8 row of the NSGA results
        r["chrom"] = mrow.get("_chrom") or ("1" * 2 * int(math.log2(n)) if stage == "synth" and mrow else "")
        for t in TRACKS:
            row = raw[t].get(n)
            ok = row is not None and row.get("Status") == "OK"
            g = (lambda k: fnum(row.get(k))) if row else (lambda k: None)
            r["status"][t] = "-" if row is None else row.get("Status", "?")
            m = ANNOT_RE.search(row.get("AnnotatedSignals", "")) if row else None
            r["annot"][t] = (100.0 * int(m.group(1)) / int(m.group(2))
                             if m and int(m.group(2)) else None)
            # Area and timing come from the same row, so they stand or fall together.
            area = g("Area (um^2)") if ok else None
            crit = g("CritDelay (ns)") if ok else None
            pwr = g("Power (mW)") if ok else None
            cyc = g("ExecCyc") if ok else None
            fmax = ratio(1000.0, crit)
            clk = clocks[t]
            r["area"][t], r["crit"][t], r["pwr"][t], r["cyc"][t] = area, crit, pwr, cyc
            r["fmax"][t] = fmax
            r["thr"][t] = (fmax * 1e6 / cyc) if fmax and cyc else None
            r["energy"][t] = (pwr * cyc * clk / 1000.0) if pwr and cyc and clk else None
            r["tpj"][t] = ratio(1e9, r["energy"][t])
            r["tpa"][t] = (r["thr"][t] / (area / 1e6)) if r["thr"][t] and area else None
            r["sqnr"][t] = (mixed_sqnr if ok else None) if t == "Mixed" else sqnr[t].get(n)
        rows.append(r)
    return rows, raw


def main():
    ap = argparse.ArgumentParser(
        description="Join the FP16, FP32 and mixed ASIC PPA reports into one table")
    ap.add_argument("--stage", choices=("synth", "postroute"), default="synth",
                    help="which pair of reports to compare (default: pre-route "
                         "synthesis; see the module docstring for what post-route "
                         "area means)")
    ap.add_argument("--mixed-pick", choices=sorted(MIXED_PICKS), default="energy",
                    help="post-route only: which Pareto-front design represents the "
                         "mixed core at each N (default: lowest energy)")
    ap.add_argument("--out", default=None,
                    help="output text file (default: results/asic_fp16_fp32_mixed_comparison_<stage>[_<pick>].txt)")
    ap.add_argument("--csv", default=None, help="also write the joined rows as CSV")
    args = ap.parse_args()
    suffix = f"_{args.mixed_pick}" if args.stage == "postroute" else ""
    out_path = args.out or os.path.join(
        RESULTS_DIR, f"asic_fp16_fp32_mixed_comparison_{args.stage}{suffix}.txt")

    rows, raw = build_rows(args.stage, args.mixed_pick)
    missing = [(t, REPORTS[args.stage][t]) for t in TRACKS if not raw[t]]
    if not rows:
        raise SystemExit("no PPA reports found; run the three drivers first:\n  "
                         + "\n  ".join(p for _, p in missing))

    # Only sizes where at least one baseline AND the mixed track have a row can
    # form a ratio; keep the rest visible rather than dropping them silently.
    stage_name = ("pre-route (Yosys + OpenSTA)" if args.stage == "synth"
                  else "post-route (OpenROAD floorplan -> global route)")
    L = ["=" * 78,
         "FP16 vs FP32 vs MIXED FP4/FP8 - ASIC COMPARISON, " + stage_name.upper(),
         "=" * 78, "",
         *(["Mixed track: NSGA-II Pareto-front design per N from results/fft_N/summary.txt,",
            "chosen by '%s' (%s); chromosome shown in TABLE 1." %
            (args.mixed_pick, MIXED_PICKS[args.mixed_pick][0])]
           if args.stage == "postroute" else
           ["Mixed track: the all-FP8 reference chromosome (highest-precision corner of",
            "the NSGA-II space), only for the sizes run_mixed_synthesis.py was run on."]),
         "All three tracks: same 45 nm library, same clock constraint, SAIF-measured power.",
         "Ratios above 1.00 favour the mixed core.", ""]
    if missing:
        L.append("MISSING reports for: " + ", ".join(f"{t} ({p})" for t, p in missing))
        L.append("")
    if args.stage == "postroute":
        L += ["NOTE: post-route area is the fixed floorplan core area (set by the SRAM",
              "macros, constant across N within a track). Its ratio compares macro",
              "sizes, not logic; use the pre-route table for the logic comparison.",
              "NOTE: post-route runs repair_timing, which stops once the 10 ns constraint",
              "is met. Post-route critical paths near 10 ns therefore mean 'met timing',",
              "not 'cannot go faster', so post-route frequency and throughput ratios are",
              "not ceilings. See report_implementation_delta_asic.py.", ""]

    N = ("FFT size", "points", lambda r: str(r["N"]))

    def trio(label, unit, key, fmt):
        return [(f"{t} {label}", unit, (lambda r, t=t: f(r[key][t], fmt))) for t in TRACKS]

    def ratios(label, unit, key, baseline_over_mixed):
        out = []
        for t in ("FP16", "FP32"):
            if baseline_over_mixed:
                fn = (lambda r, t=t: f(ratio(r[key][t], r[key]["Mixed"])))
            else:
                fn = (lambda r, t=t: f(ratio(r[key]["Mixed"], r[key][t])))
            out.append((f"{label} vs {t}", unit, fn))
        return out

    L += render(rows, [N, ("Mixed chromosome", "1 = FP8, 0 = FP4; mult/add per stage",
                           lambda r: r["chrom"] or "-")]
                + trio("area", "um^2", "area", "{:,.0f}")
                + ratios("Area ratio", "x smaller for mixed", "area", True),
                "TABLE 1  AREA")

    L += render(rows, [N] + trio("critical path", "ns", "crit", "{:.3f}")
                + trio("maximum frequency", "MHz", "fmax", "{:.1f}")
                + ratios("Frequency ratio", "x faster for mixed", "fmax", False),
                "TABLE 2  ACHIEVABLE CLOCK FREQUENCY")

    L += render(rows, [N] + trio("cycles per transform", "clock cycles", "cyc", "{:.0f}")
                + trio("throughput", "transforms per second", "thr", "{:,.0f}")
                + ratios("Throughput ratio", "x faster for mixed", "thr", False),
                "TABLE 3  THROUGHPUT", notes=[
                    "Throughput = fmax / cycles. Cycles for the mixed track come from the",
                    "NSGA results; a dash there means the all-FP8 chromosome was not visited.",
                ])

    L += render(rows, [N] + trio("power", "mW", "pwr", "{:.3f}")
                + trio("energy per transform", "nanojoules", "energy", "{:.2f}")
                + ratios("Energy ratio", "x lower for mixed", "energy", True),
                "TABLE 4  POWER AND ENERGY PER TRANSFORM", notes=[
                    "Energy = power x cycles x constraint clock. Power is SAIF-measured; see",
                    "TABLE 8 for how much of each netlist the SAIF actually covered.",
                ])

    L += render(rows, [N] + trio("throughput per watt", "transforms per joule", "tpj", "{:,.0f}")
                + ratios("Per-watt ratio", "x better for mixed", "tpj", False),
                "TABLE 5  ENERGY EFFICIENCY", notes=[
                    "Throughput per watt is 1 / energy per transform, so this table carries",
                    "the same information as the energy ratios, in units reviewers ask for.",
                ])

    L += render(rows, [N] + trio("throughput per mm^2", "transforms per second", "tpa", "{:,.0f}")
                + ratios("Per-area ratio", "x better for mixed", "tpa", False),
                "TABLE 6  AREA EFFICIENCY")

    def lost(t):
        return lambda r: f(None if r["sqnr"][t] is None or r["sqnr"]["Mixed"] is None
                           else r["sqnr"][t] - r["sqnr"]["Mixed"])

    L += render(rows, [N] + trio("average SQNR", "dB", "sqnr", "{:.2f}")
                + [("Accuracy lost vs FP16", "dB", lost("FP16")),
                   ("Accuracy lost vs FP32", "dB", lost("FP32"))],
                "TABLE 7  ACCURACY - WHAT THE EFFICIENCY COSTS", notes=[
                    "This decides whether the efficiency ratios are a real win. Baseline SQNR",
                    "is read from sim/perf/<track>_sqnr_results.txt; mixed SQNR is the NSGA",
                    "evaluator's figure for the mixed design shown. A bit-exact signal is",
                    "credited 100 dB, so an average near 100 dB means mostly exact signals.",
                ])

    L += render(rows, [N] + [(f"{t} status", "", (lambda r, t=t: r["status"][t])) for t in TRACKS]
                + [(f"{t} SAIF coverage", "% of nets", (lambda r, t=t: f(r["annot"][t], "{:.2f}")))
                   for t in TRACKS],
                "TABLE 8  VALIDITY", notes=[
                    "A track whose Status is not OK contributes dashes above, never a number.",
                    "SAIF coverage is the share of netlist nets the activity file annotated;",
                    "it is low for every track, so absolute power carries that uncertainty and",
                    "the energy ratios are only as good as the three coverages are alike.",
                    "The mixed post-route summaries do not record SAIF coverage, so it is a",
                    "dash there; that is missing information, not zero coverage.",
                ])

    L += ["", "READING THE RATIOS",
          "  Above 1.00  the mixed core is better on that figure of merit.",
          "  Below 1.00  the baseline is better.",
          "  A dash      one of the two numbers is missing or untrustworthy; it is",
          "              never a 1.00 and never an assumption of equality."]

    text = "\n".join(L) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print("\n" + text)
    print(f"[compare-asic] table: {out_path}")

    if args.csv:
        flat = []
        for r in rows:
            d = {"N": r["N"], "stage": args.stage}
            for field in ("area", "crit", "fmax", "pwr", "cyc", "thr", "energy",
                          "tpj", "tpa", "sqnr", "annot", "status"):
                for t in TRACKS:
                    d[f"{field}_{t.lower()}"] = r[field][t]
            flat.append(d)
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv_mod.DictWriter(fh, fieldnames=list(flat[0].keys()))
            w.writeheader()
            w.writerows(flat)
        print(f"[compare-asic] csv  : {args.csv}")


if __name__ == "__main__":
    main()
