#!/usr/bin/env python3
"""
Best-design selection from the NSGA-II results
==============================================
Every Pareto-optimal design is "best" for some trade-off, so choosing one needs
an explicit rule. This script applies one, per FFT size, and writes the choice
with its reasoning.

    python3 nsga_dse/select_best_design.py
    python3 nsga_dse/select_best_design.py --min-sqnr 20      # stricter accuracy floor
    python3 nsga_dse/select_best_design.py --pool all         # search every evaluated design
    python3 nsga_dse/select_best_design.py --allow-uniform    # do not insist on a mixed design

Reads  results/fft_N/all_solutions_fftN.csv   (on_pareto_front marks the front)
Writes results/asic_best_designs.txt and results/asic_best_designs.csv

THE RULE (default, --method balanced)
  1. Pool: Pareto-optimal designs that meet timing (slack >= --min-slack, default
     0). --pool all widens this to every evaluated design that meets timing.
  2. Accuracy floor: keep designs with SQNR >= --min-sqnr (default 15 dB). If
     nothing clears the floor, the single highest-SQNR design is taken instead
     and the row is flagged FLOOR NOT MET, so a missed requirement is visible.
  3. Mixed preference: if any design left uses BOTH precisions (at least one FP4
     gene and one FP8 gene), only those are considered and the row is labelled
     Mixed. If every survivor is uniform (all FP4 or all FP8) the row falls back
     to them and is labelled Fallback -- an all-FP8 design is not a mixed-
     precision result, so it is never silently reported as one. --allow-uniform
     switches the preference off.
  4. Balanced score: within what is left, rescale energy, area, critical path
     (lower is better) and SQNR (higher is better) to 0..1, so that 0 is the
     best end of each. The score is the Euclidean distance to the ideal point,
         score = sqrt( E_bad^2 + A_bad^2 + D_bad^2 + (1 - SQNR_scaled)^2 ),
     with equal weights, and the LOWEST score wins. Ties go to higher SQNR, then
     lower energy.

NOTES ON THE RULE
  * Energy per transform is used in place of dynamic power. Cycle count is the
    same for every design at a given N, so energy is power times a constant and
    the scaled values, hence the ranking, are identical -- and energy is the
    figure the paper reports.
  * Area is a criterion in the score but is the same for every design here (the
    post-route floorplan is set by the SRAM macros). A criterion with no spread
    cannot separate designs, so it is skipped; skipping a constant term does not
    change the ranking.
  * Equal weights mean no accuracy priority beyond the 15 dB floor: a design
    can win by being cheap and just above the floor. Raise --min-sqnr to demand
    more, or use --method score to weight SQNR above energy.
  * Scaling is within the pool at each N, so the score ranks designs within a
    size and is not comparable across sizes; it also stretches a narrow range to
    the full 0..1, so TABLE 1 prints the real nJ and dB gaps beside each choice.

OTHER METHODS
  --method score      0.7 * SQNR_scaled + 0.3 * energy_scaled (--w-sqnr), highest
                      wins. SQNR-priority, energy still counts. Prints a table
                      showing whether the pick moves with the weight.
  --method sqnr       within --sqnr-tol (0.5 dB) of the best SQNR, lowest energy.
  --method tolerance  within --energy-tol (2 %) of the lowest energy, highest SQNR.
"""

import argparse
import csv
import glob
import os
import re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
RESULTS_DIR = os.path.join(REPO_ROOT, "results")


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def load_designs(root=RESULTS_DIR):
    """{N: [design dict]} from every results/fft_N/all_solutions_fftN[_fixed].csv.

    The _fixed file is preferred when one exists, as for the other result readers."""
    data = {}
    for d in sorted(glob.glob(os.path.join(root, "fft_*"))):
        m = re.match(r"fft_(\d+)$", os.path.basename(d))
        if not m:
            continue
        n = int(m.group(1))
        path = next((p for p in (os.path.join(d, f"all_solutions_fft{n}_fixed.csv"),
                                 os.path.join(d, f"all_solutions_fft{n}.csv"))
                     if os.path.isfile(p)), None)
        if not path:
            continue
        rows = []
        with open(path, newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            stages = sorted({int(re.match(r"s(\d+)_", c).group(1)) for c in rd.fieldnames
                             if re.match(r"s\d+_(mult|add)$", c)})
            for r in rd:
                mult, add = [], []
                for s in stages:
                    mu, ad = (r.get(f"s{s}_mult") or "").strip(), (r.get(f"s{s}_add") or "").strip()
                    if mu != "":
                        mult.append(int(mu))
                    if ad != "":
                        add.append(int(ad))
                genes = mult + add
                rows.append({
                    "n": n, "id": int(r["solution_id"]), "mult": mult, "add": add,
                    "mixed": (0 in genes) and (1 in genes),
                    "chrom": "".join(f"{a}{b}" for a, b in zip(mult, add)),
                    "energy": num(r.get("energy_nJ_perFFT")), "power": num(r.get("dyn_power_mW")),
                    "area": num(r.get("area_um2")), "sqnr": num(r.get("sqnr_dB")),
                    "crit": num(r.get("crit_delay_ns")), "slack": num(r.get("slack_ns")),
                    "meets": (r.get("meets_timing") or "0").strip() == "1",
                    "cycles": num(r.get("avg_exec_cycles")),
                    "pareto": (r.get("on_pareto_front") or "0").strip() == "1",
                })
        if rows:
            data[n] = rows
    return data


def candidate_pool(rows, min_slack=0.0, min_sqnr=None, pool_kind="pareto"):
    """(pool, floor_met). Timing-clean designs (Pareto-only unless pool_kind='all'),
    then the SQNR floor."""
    pool = [r for r in rows if (r["pareto"] or pool_kind == "all") and r["meets"]
            and r["energy"] is not None and r["sqnr"] is not None
            and r["crit"] is not None and (r["slack"] is None or r["slack"] >= min_slack)]
    if min_sqnr is None:
        return pool, True
    above = [r for r in pool if r["sqnr"] >= min_sqnr]
    if above:
        return above, True
    return ([max(pool, key=lambda r: r["sqnr"])] if pool else []), False


def scaled(pool, key, higher_better):
    """0..1 with 1 = best end of the pool; 0.5 for all if the pool has no spread."""
    vals = [r[key] for r in pool]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return {id(r): 0.5 for r in pool}
    return {id(r): ((r[key] - lo) if higher_better else (hi - r[key])) / (hi - lo)
            for r in pool}


def select_balanced(pool):
    """Equal-weight distance to the ideal point over energy, area, critical path
    and SQNR. Criteria with no spread across the pool are dropped (they add the
    same amount to every score, so the ranking is unchanged)."""
    crit = [("energy", False), ("area", False), ("crit", False), ("sqnr", True)]
    good = []
    for key, hb in crit:
        if len({r[key] for r in pool}) > 1:
            good.append(scaled(pool, key, hb))   # 1 = best

    def dist(r):
        return sum((1.0 - g[id(r)]) ** 2 for g in good) ** 0.5
    best = min(pool, key=lambda r: (dist(r), -r["sqnr"], r["energy"]))
    return best, len(pool), dist(best)


def select_tolerance(pool, energy_tol):
    e_min = min(r["energy"] for r in pool)
    cheap = [r for r in pool if r["energy"] <= e_min * (1.0 + energy_tol)]
    return min(cheap, key=lambda r: (-r["sqnr"], r["crit"], r["energy"])), len(cheap)


def select_sqnr(pool, sqnr_tol):
    s_max = max(r["sqnr"] for r in pool)
    close = [r for r in pool if r["sqnr"] >= s_max - sqnr_tol]
    return min(close, key=lambda r: (r["energy"], r["crit"], -r["sqnr"])), len(close)


def select_score(pool, w_sqnr, w_energy):
    ss, se = scaled(pool, "sqnr", True), scaled(pool, "energy", False)
    return max(pool, key=lambda r: (w_sqnr * ss[id(r)] + w_energy * se[id(r)],
                                    r["sqnr"], -r["energy"])), len(pool)


def choose(rows, method="balanced", energy_tol=0.02, min_slack=0.0, min_sqnr=15.0,
           w_sqnr=0.7, w_energy=0.3, sqnr_tol=0.5, pool_kind="pareto", prefer_mixed=True):
    """(best design or None, info dict). Pure function so other scripts can reuse it."""
    pool, floor_met = candidate_pool(rows, min_slack, min_sqnr, pool_kind)
    if not pool:
        return None, {"pool": 0, "floor_met": floor_met, "tied": 0, "config": "-",
                      "uniform_dropped": 0, "score": None}
    config, dropped = "Mixed", 0
    if prefer_mixed:
        mixed = [r for r in pool if r["mixed"]]
        if mixed:
            dropped, pool = len(pool) - len(mixed), mixed
        else:
            config = "Fallback"
    else:
        config = "Any"
    score = None
    if method == "balanced":
        best, tied, score = select_balanced(pool)
    elif method == "score":
        best, tied = select_score(pool, w_sqnr, w_energy)
    elif method == "sqnr":
        best, tied = select_sqnr(pool, sqnr_tol)
    else:
        best, tied = select_tolerance(pool, energy_tol)
    return best, {"pool": len(pool), "floor_met": floor_met, "tied": tied, "config": config,
                  "uniform_dropped": dropped, "score": score,
                  "e_min": min(r["energy"] for r in pool),
                  "s_max": max(r["sqnr"] for r in pool)}


def render(title, cols, rows, notes=()):
    labels = [c[0] for c in cols]
    units = [c[1] for c in cols]
    body = [[c[2](r) for c in cols] for r in rows]
    widths = [max(len(labels[i]), len(units[i]), max((len(b[i]) for b in body), default=0))
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
    ap = argparse.ArgumentParser(description="Pick the best design per FFT size")
    ap.add_argument("--results", default=RESULTS_DIR)
    ap.add_argument("--method", choices=("balanced", "score", "sqnr", "tolerance"),
                    default="balanced")
    ap.add_argument("--pool", choices=("pareto", "all"), default="pareto",
                    help="designs to choose from: the Pareto front (default) or every "
                         "evaluated design that meets timing")
    ap.add_argument("--min-sqnr", type=float, default=15.0,
                    help="accuracy floor in dB (default 15; 0 effectively disables it)")
    ap.add_argument("--min-slack", type=float, default=0.0,
                    help="required timing slack in ns (default 0 = just met)")
    ap.add_argument("--allow-uniform", action="store_true",
                    help="do not prefer designs that mix FP4 and FP8")
    ap.add_argument("--w-sqnr", type=float, default=0.7, help="score method: SQNR weight")
    ap.add_argument("--w-energy", type=float, default=None,
                    help="score method: energy weight (default 1 - w-sqnr)")
    ap.add_argument("--sqnr-tol", type=float, default=0.5, help="sqnr method: tolerance in dB")
    ap.add_argument("--energy-tol", type=float, default=0.02,
                    help="tolerance method: fraction above the lowest energy (default 0.02)")
    ap.add_argument("--out", default=os.path.join(RESULTS_DIR, "asic_best_designs.txt"))
    args = ap.parse_args()
    if args.w_energy is None:
        args.w_energy = 1.0 - args.w_sqnr
    prefer_mixed = not args.allow_uniform

    data = load_designs(args.results)
    if not data:
        raise SystemExit(f"no all_solutions_fftN.csv under {args.results}")

    def run(n, **over):
        kw = dict(method=args.method, energy_tol=args.energy_tol, min_slack=args.min_slack,
                  min_sqnr=args.min_sqnr, w_sqnr=args.w_sqnr, w_energy=args.w_energy,
                  sqnr_tol=args.sqnr_tol, pool_kind=args.pool, prefer_mixed=prefer_mixed)
        kw.update(over)
        return choose(data[n], **kw)

    picks = []
    for n in sorted(data):
        best, info = run(n)
        picks.append({"n": n, "best": best, "info": info,
                      "front": sum(1 for r in data[n] if r["pareto"])})

    rule = {
        "balanced": "balanced: equal-weight distance to the ideal point over energy, area, "
                    "critical path and SQNR (lowest wins)",
        "score": f"score: SQNR weight {args.w_sqnr:g}, energy weight {args.w_energy:g} "
                 "(each scaled 0..1 within the pool)",
        "sqnr": f"accuracy first: lowest energy within {args.sqnr_tol:g} dB of the best SQNR",
        "tolerance": f"energy first: highest SQNR within {args.energy_tol * 100:g} % of the "
                     "lowest energy",
    }[args.method]
    L = ["=" * 78, "BEST DESIGN PER FFT SIZE (from the NSGA-II results)", "=" * 78, "",
         "Rule      : " + rule,
         "Pool      : %s designs that meet timing (slack >= %g ns), SQNR >= %g dB%s" % (
             "Pareto-optimal" if args.pool == "pareto" else "all evaluated",
             args.min_slack, args.min_sqnr,
             "" if args.allow_uniform else "; mixed FP4+FP8 designs preferred"),
         "Area      : equal for every design (fixed floorplan), so it cannot separate them.", ""]

    def stat(p):
        if p["best"] is None:
            return "NO CANDIDATE"
        return "ok" if p["info"]["floor_met"] else "FLOOR NOT MET"

    def vs(p, k, ref, fmt):
        b = p["best"]
        return "-" if b is None or ref not in p["info"] else fmt.format(b[k] - p["info"][ref])

    L += render("TABLE 1  SELECTED DESIGN", [
        ("FFT size", "points", lambda p: str(p["n"])),
        ("Chromosome", "1 = FP8, 0 = FP4; mult/add per stage",
         lambda p: p["best"]["chrom"] if p["best"] else "-"),
        ("Config", "", lambda p: p["info"]["config"]),
        ("Energy per transform", "nJ", lambda p: f"{p['best']['energy']:.2f}" if p["best"] else "-"),
        ("SQNR", "dB", lambda p: f"{p['best']['sqnr']:.2f}" if p["best"] else "-"),
        ("Critical path", "ns", lambda p: f"{p['best']['crit']:.3f}" if p["best"] else "-"),
        ("Slack", "ns", lambda p: f"{p['best']['slack']:.3f}" if p["best"] else "-"),
        ("Cycles", "clock cycles", lambda p: f"{p['best']['cycles']:.0f}" if p["best"] else "-"),
        ("Energy above pool minimum", "nJ", lambda p: vs(p, "energy", "e_min", "{:+.2f}")),
        ("SQNR below pool maximum", "dB", lambda p: vs(p, "sqnr", "s_max", "{:+.2f}")),
        ("Balance score", "0 = ideal",
         lambda p: f"{p['info']['score']:.3f}" if p["info"]["score"] is not None else "-"),
        ("Status", "", stat),
    ], picks, notes=[
        "Config: Mixed = uses both FP4 and FP8; Fallback = no mixed design cleared the",
        "filters, so a uniform one is shown; Any = mixed preference switched off.",
        "'Above pool minimum' and 'below pool maximum' are measured within the pool the",
        "choice was made from (after the mixed preference), i.e. what was given up.",
    ])

    L += render("TABLE 2  HOW MUCH CHOICE THERE WAS", [
        ("FFT size", "points", lambda p: str(p["n"])),
        ("Pareto-front designs", "count", lambda p: str(p["front"])),
        ("Candidates in pool", "count", lambda p: str(p["info"]["pool"])),
        ("Uniform designs set aside", "count", lambda p: str(p["info"]["uniform_dropped"])),
        ("Selected solution id", "", lambda p: str(p["best"]["id"]) if p["best"] else "-"),
    ], picks, notes=[
        "Uniform = all FP4 or all FP8 (not mixed precision). They are set aside only when",
        "a mixed design clears the timing and SQNR filters.",
    ])

    if args.method == "score":
        ws = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
        sens = []
        for n in sorted(data):
            ids = []
            for w in ws:
                b, _ = run(n, w_sqnr=w, w_energy=1.0 - w)
                ids.append(b["id"] if b else None)
            sens.append({"n": n, "ids": ids, "stable": len(set(ids)) == 1})
        L += render("TABLE 3  DOES THE WEIGHT MATTER?  (solution id picked per SQNR weight)", [
            ("FFT size", "points", lambda r: str(r["n"]))]
            + [(f"w_sqnr {w:g}", "", (lambda r, i=i: str(r["ids"][i]))) for i, w in enumerate(ws)]
            + [("Pick moves?", "", lambda r: "no" if r["stable"] else "YES")], sens, notes=[
            "'no' = the same design wins for every weight shown. 'YES' = a genuine",
            "SQNR-versus-energy trade-off at that size.",
        ])

    bad = [p for p in picks if stat(p) != "ok" or p["info"]["config"] == "Fallback"]
    if bad:
        L += ["", "ATTENTION: " + ", ".join(
            f"N={p['n']} ({stat(p) if stat(p) != 'ok' else 'no mixed design available'})"
            for p in bad)]
    text = "\n".join(L) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    csv_path = os.path.splitext(args.out)[0] + ".csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["N", "solution_id", "chromosome", "config", "energy_nJ", "sqnr_dB",
                    "crit_delay_ns", "slack_ns", "exec_cycles", "balance_score", "pool", "status"])
        for p in picks:
            b = p["best"]
            w.writerow([p["n"]] + ([b["id"], b["chrom"], p["info"]["config"], b["energy"],
                                    b["sqnr"], b["crit"], b["slack"], b["cycles"],
                                    "" if p["info"]["score"] is None else p["info"]["score"]]
                                   if b else [""] * 9)
                       + [p["info"]["pool"], stat(p)])
    print("\n" + text)
    print(f"[best-design] table: {args.out}\n[best-design] csv  : {csv_path}")


if __name__ == "__main__":
    main()
