#!/usr/bin/env python3
"""
Pre-route vs post-route: how far does the estimate move?  (ASIC)
================================================================
Reads each
track's pre-route report (Yosys + OpenSTA) and post-route report (OpenROAD
floorplan -> global route) and answers: by how much, and in which direction,
does physical implementation move each number the paper quotes.

Run both stages for all three tracks first:

    python3 fp16_baseline/synth/run_fp16_synthesis.py ; python3 fp16_baseline/synth/run_fp16_postroute.py
    python3 fp32_baseline/synth/run_fp32_synthesis.py ; python3 fp32_baseline/synth/run_fp32_postroute.py
    python3 nsga_dse/run_mixed_synthesis.py           ; python3 nsga_dse/run_mixed_postroute.py
    python3 nsga_dse/report_implementation_delta_asic.py

The inputs are the drivers' text tables (no CSVs exist on the ASIC side), parsed
header-first. A size is compared only when BOTH stages have an OK row for it; a
size present in one stage only is listed as unpaired rather than dropped.

HOW TO READ THE DELTAS -- read before quoting one
  1. The two stages are not one tool invocation on one netlist. They are the
     same RTL through the same Yosys flow, run separately: the pre-route
     driver times the Yosys netlist with OpenSTA, the post-route driver
     re-synthesises a flattened netlist and takes it through placement, CTS and
     global route. The delta therefore includes any difference between those two
     Yosys runs as well as implementation itself.
  2. The post-route critical path is NOT a pure routing penalty. The flow runs
     repair_timing -setup, which resizes and buffers cells until the clock
     constraint is met, then stops. A post-route path near the constraint (about
     10 ns here) means the tool met timing, not that 10 ns is the design's
     limit; a post-route path BELOW the pre-route one means repair gained back
     more than routing cost. So a negative delta is legitimate here, and the
     post-route f_max is a "met the constraint" figure, not a ceiling.
  3. Post-route area is the fixed floorplan core area (dominated by the SRAM
     macros, constant across N within a track), while pre-route area is the sum
     of standard cells. The two are different quantities, so area is shown in
     TABLE 3 for reference but is EXCLUDED from the spread summary: its
     "change" measures floorplan slack, not what implementation did to the
     logic.
  4. Mixed post-route rows come from the post-route report where present, else
     from results/fft_N/summary.txt, and only when that summary contains the
     all-FP8 design -- the chromosome the pre-route mixed run uses -- so the
     pair is the same design at both stages. Summaries carry no SAIF coverage.
  5. Power is SAIF-measured at both stages but on different netlists
     (hierarchy-preserved vs flattened), and SAIF coverage is a small fraction
     of nets at both. TABLE 3 prints the coverage next to the power so a
     power delta is never read without it.

Reading the deltas:
  critical path   positive = slower after implementation, negative = repair
                  recovered more than routing cost (see point 2).
  f_max           the same fact with the opposite sign.
  dynamic power   post-route uses placed/routed parasitics, so it is the more
                  physical number, subject to point 4.

What to conclude:
  If the median critical-path change is small AND similar across the three
  tracks, the systematic part cancels out of the ratio tables and the pre-route
  numbers stand with a footnote naming the spread. If it is large, or differs by
  track, rebuild the comparison on post-route numbers -- the mixed core's
  worst path runs through different logic than FP16/FP32's, so there is no
  guarantee it responds to repair_timing alike.
"""

import argparse
import os
import re
import statistics
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

TRACKS = [
    ("FP16",
     os.path.join(REPO_ROOT, "fp16_baseline", "synth", "fp16_ppa_report.txt"),
     os.path.join(REPO_ROOT, "fp16_baseline", "synth", "fp16_postroute_ppa_report.txt")),
    ("FP32",
     os.path.join(REPO_ROOT, "fp32_baseline", "synth", "fp32_ppa_report.txt"),
     os.path.join(REPO_ROOT, "fp32_baseline", "synth", "fp32_postroute_ppa_report.txt")),
    ("Mixed",
     os.path.join(REPO_ROOT, "mixed_ppa_report.txt"),
     os.path.join(REPO_ROOT, "mixed_postroute_ppa_report.txt")),
]
sys.path.insert(0, SCRIPT_DIR)
from compare_precision_tracks_asic import RESULTS_DIR, read_summary  # noqa: E402

DEFAULT_OUT = os.path.join(REPO_ROOT, "results", "asic_preroute_vs_postroute_delta.txt")

ANNOT_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_report(path):
    """{N: {header: cell}} from a driver's '|'-separated text table."""
    if not os.path.isfile(path):
        return {}
    rows, header = {}, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "|" not in line:
                continue
            cells = [c.strip() for c in line.split("|")]
            if header is None:
                if cells[0] == "N":
                    header = cells
                continue
            if len(cells) == len(header) and cells[0].isdigit():
                rows[int(cells[0])] = dict(zip(header, cells))
    return rows


def mixed_allfp8_postroute(n):
    """Post-route row for the all-FP8 mixed design at size n, from the NSGA
    summary, or None. Pre-route mixed is always the all-FP8 chromosome, so a
    summary design is only a like-for-like partner if it is all-FP8 too; the
    summary records chromosomes only for its 'best' designs, so that is where
    it can be recognised."""
    sm = read_summary(n)
    for r in (sm or {"rows": []})["rows"]:
        if r["meets"] and r["chrom"] and set(r["chrom"]) == {"1"}:
            return {"Area (um^2)": str(r["area"]), "CritDelay (ns)": str(r["crit"]),
                    "Slack (ns)": str(r["slack"]), "Power (mW)": str(r["power"]),
                    "Status": "OK", "AnnotatedSignals": ""}
    return None


def coverage(row):
    m = ANNOT_RE.search(row.get("AnnotatedSignals", ""))
    return 100.0 * int(m.group(1)) / int(m.group(2)) if m and int(m.group(2)) else None


def pct(new, old):
    if new is None or old is None or old == 0:
        return None
    return 100.0 * (new - old) / old


def pair(track, pre, post):
    """Flat record for one size where both stages parsed, else None."""
    ok = lambda r: r is not None and r.get("Status") == "OK"
    rec = {"track": track}
    for stage, r in (("pre", pre), ("post", post)):
        good = ok(r)
        crit = fnum(r.get("CritDelay (ns)")) if good else None
        rec[f"{stage}_crit"] = crit
        rec[f"{stage}_fmax"] = 1000.0 / crit if crit else None
        rec[f"{stage}_area"] = fnum(r.get("Area (um^2)")) if good else None
        rec[f"{stage}_power"] = fnum(r.get("Power (mW)")) if good else None
        rec[f"{stage}_slack"] = fnum(r.get("Slack (ns)")) if good else None
        rec[f"{stage}_cov"] = coverage(r) if r is not None else None
        rec[f"{stage}_status"] = "-" if r is None else r.get("Status", "?")
    rec["d_crit"] = pct(rec["post_crit"], rec["pre_crit"])
    rec["d_fmax"] = pct(rec["post_fmax"], rec["pre_fmax"])
    rec["d_area"] = pct(rec["post_area"], rec["pre_area"])
    rec["d_power"] = pct(rec["post_power"], rec["pre_power"])
    return rec


def render(title, cols, rows, notes=()):
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


def main():
    ap = argparse.ArgumentParser(
        description="Pre-route vs post-route comparison across the three ASIC tracks")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    rows, unpaired, missing = [], [], []
    for name, pre_path, post_path in TRACKS:
        pre, post = read_report(pre_path), read_report(post_path)
        if name == "Mixed":
            # Fill sizes the post-route driver was not run for from the NSGA
            # summaries (all-FP8 design only, see mixed_allfp8_postroute).
            for n in pre:
                if n not in post:
                    row = mixed_allfp8_postroute(n)
                    if row:
                        post[n] = row
        for stage, d, p in (("pre-route", pre, pre_path), ("post-route", post, post_path)):
            if not d:
                missing.append((name, stage, p))
        for n in sorted(set(pre) | set(post)):
            rec = pair(name, pre.get(n), post.get(n))
            rec["N"] = n
            if pre.get(n) is not None and post.get(n) is not None:
                rows.append(rec)
            else:
                unpaired.append((name, n, "pre-route" if n in pre else "post-route"))

    if not rows:
        raise SystemExit(
            "no size has both a pre-route and a post-route row. Run the drivers:\n"
            "  python3 fp16_baseline/synth/run_fp16_synthesis.py ; ...run_fp16_postroute.py\n"
            "  python3 fp32_baseline/synth/run_fp32_synthesis.py ; ...run_fp32_postroute.py\n"
            "  python3 nsga_dse/run_mixed_synthesis.py           ; ...run_mixed_postroute.py")

    def g(r, k, fmt="{:.2f}", dash="-"):
        v = r.get(k)
        return dash if v is None else fmt.format(v)

    L = ["=" * 78,
         "PRE-ROUTE vs POST-ROUTE (ASIC)",
         "=" * 78, "",
         "Pre-route : Yosys synthesis + OpenSTA timing, no placement.",
         "Post-route: OpenROAD floorplan, placement, CTS, global route, repair_timing.",
         "Same RTL and Yosys flow, separate invocations -- see the module docstring for",
         "what that means for reading the deltas (repair_timing, floorplan area).", ""]
    if missing:
        L.append("MISSING reports: " + ", ".join(f"{n} {s} ({p})" for n, s, p in missing))
        L.append("")
    if unpaired:
        L.append("UNPAIRED (one stage only, so no delta): "
                 + ", ".join(f"{t} N={n} ({s} only)" for t, n, s in unpaired))
        L.append("")

    key = [("Precision track", "", lambda r: r["track"]),
           ("FFT size", "points", lambda r: str(r["N"]))]

    L += render("TABLE 1  CRITICAL PATH AND ACHIEVABLE FREQUENCY", key + [
        ("Critical path, pre-route", "ns", lambda r: g(r, "pre_crit", "{:.3f}")),
        ("Critical path, post-route", "ns", lambda r: g(r, "post_crit", "{:.3f}")),
        ("Change in critical path", "percent", lambda r: g(r, "d_crit", "{:+.2f}")),
        ("Maximum frequency, pre-route", "MHz", lambda r: g(r, "pre_fmax", "{:.1f}")),
        ("Maximum frequency, post-route", "MHz", lambda r: g(r, "post_fmax", "{:.1f}")),
        ("Change in maximum frequency", "percent", lambda r: g(r, "d_fmax", "{:+.2f}")),
        ("Slack, post-route", "ns", lambda r: g(r, "post_slack", "{:.3f}")),
    ], rows, notes=[
        "repair_timing stops once the constraint is met, so a post-route path near the",
        "constraint means timing was met, not that the design cannot go faster. A",
        "negative change is legitimate: repair recovered more than routing cost.",
    ])

    L += render("TABLE 2  DYNAMIC POWER", key + [
        ("Power, pre-route", "mW", lambda r: g(r, "pre_power", "{:.4f}")),
        ("Power, post-route", "mW", lambda r: g(r, "post_power", "{:.4f}")),
        ("Change in power", "percent", lambda r: g(r, "d_power", "{:+.2f}")),
        ("SAIF coverage, pre-route", "% of nets", lambda r: g(r, "pre_cov")),
        ("SAIF coverage, post-route", "% of nets", lambda r: g(r, "post_cov")),
    ], rows, notes=[
        "The two stages annotate different netlists (hierarchy-preserved vs flattened)",
        "and both cover only a small share of nets, so a power change here mixes",
        "implementation with annotation differences. Treat it as indicative.",
    ])

    L += render("TABLE 3  AREA (REFERENCE ONLY - NOT LIKE FOR LIKE)", key + [
        ("Cell area, pre-route", "um^2", lambda r: g(r, "pre_area", "{:,.0f}")),
        ("Floorplan core area, post-route", "um^2", lambda r: g(r, "post_area", "{:,.0f}")),
        ("Post-route / pre-route", "ratio",
         lambda r: g({"v": (r["post_area"] / r["pre_area"]) if r["post_area"] and r["pre_area"] else None}, "v")),
    ], rows, notes=[
        "Pre-route is a sum of standard cells; post-route is the fixed floorplan core,",
        "set by the SRAM macros. The ratio is how much floorplan surrounds the logic, not",
        "what implementation did to it, so it is left out of the spread summary below.",
    ])

    # ---- the summary that actually decides the question ----
    summary = []
    for name, _, _ in TRACKS:
        for k, label in (("d_crit", "critical path"),
                         ("d_fmax", "maximum frequency"),
                         ("d_power", "dynamic power")):
            vals = [r[k] for r in rows if r["track"] == name and r[k] is not None]
            if vals:
                summary.append({"track": name, "what": label, "n": len(vals),
                                "min": min(vals), "med": statistics.median(vals),
                                "max": max(vals)})

    if summary:
        L += render("TABLE 4  SPREAD OF THE CHANGE, BY TRACK", [
            ("Precision track", "", lambda r: r["track"]),
            ("Quantity", "", lambda r: r["what"]),
            ("Designs compared", "count", lambda r: str(r["n"])),
            ("Smallest change", "percent", lambda r: "{:+.2f}".format(r["min"])),
            ("Median change", "percent", lambda r: "{:+.2f}".format(r["med"])),
            ("Largest change", "percent", lambda r: "{:+.2f}".format(r["max"])),
        ], summary, notes=[
            "Compare the median critical-path row across the tracks: if they agree, the",
            "effect is systematic and largely cancels in the ratio tables; if they",
            "disagree, the comparison should be rebuilt on post-route numbers. A track",
            "with a single compared design has min = median = max and says little.",
        ])

        crit = {s["track"]: s for s in summary if s["what"] == "critical path"}
        if len(crit) >= 2:
            meds = {k: v["med"] for k, v in crit.items()}
            L += ["", "VERDICT INPUT",
                  "  Median critical-path change per track: "
                  + ", ".join(f"{k} {v:+.2f} % (n={crit[k]['n']})" for k, v in meds.items()),
                  f"  Spread between tracks: {max(meds.values()) - min(meds.values()):.2f} percentage points.",
                  "  Small spread -> the ratio tables are safe; quote the median as the",
                  "  uncertainty on any absolute frequency. Large spread -> rebuild the",
                  "  comparison on post-route numbers."]
        else:
            L += ["", "VERDICT INPUT",
                  "  Fewer than two tracks have a paired design, so no cross-track spread",
                  "  can be formed yet. Run the missing stage for the other tracks."]

    text = "\n".join(L) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print("\n" + text)
    print(f"[impl-delta-asic] table: {args.out}")


if __name__ == "__main__":
    main()
