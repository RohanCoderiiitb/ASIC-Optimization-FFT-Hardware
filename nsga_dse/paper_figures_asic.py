#!/usr/bin/env python3
"""Paper figures for the ASIC mixed-precision FFT sweep.

  * at most TWO panels per figure, never a 2x3 grid
  * authored at true IEEE width (7.16 in double column) so the typesetter does
    not downscale them and 10 pt stays 10 pt on the page
  * 10 pt labels, 9 pt ticks, large markers, 2 px lines
  * vector PDF (fonttype 42, real embedded text) plus a 400 dpi PNG
  * two-colour categorical palette that stays distinguishable under colour-vision
    deficiency (Okabe-Ito blue/orange), with hatching as a secondary encoding so
    FP4/FP8 survives greyscale printing
  * one y-axis per panel, never a dual axis

    python3 nsga_dse/paper_figures_asic.py
    python3 nsga_dse/paper_figures_asic.py --results results --out results

Reads  results/fft_N/all_solutions_fftN[_fixed].csv.
Writes results/asic_fig5..fig8_*.pdf/.png and results/asic_timing_table.txt
(directly in results/, not in a sub-directory).

Figures
  5  energy per transform vs SQNR for N = 256 and 1024, Pareto-optimal designs
     coloured by FP8 share, the design chosen by select_best_design.py starred
  6  scaling: lowest energy per transform and best SQNR against N
  7  per-stage multiplier / adder precision of the chosen design at each N
  8  energy per transform and achievable f_max against the number of FP8 stages

Area is deliberately not plotted. Every design has the same post-route
floorplan area (the SRAM macros set it), so an area panel is a flat line and says
nothing about the precision assignment. Figure 8 shows the quantities that do
move with it: energy and critical path.
"""
import argparse
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap, ListedColormap
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from select_best_design import RESULTS_DIR, choose, load_designs  # noqa: E402

FP4, FP8 = "#0072B2", "#D55E00"
INK, INK2, GRID = "#141920", "#4B5666", "#D7DDE5"
NEUTRAL = "#9AA6B4"
SEQ = LinearSegmentedColormap.from_list("fp8seq", ["#DCE9F2", "#0072B2", "#003F63"])
W2 = 7.16

plt.rcParams.update({
    "figure.dpi": 150, "savefig.dpi": 400, "savefig.bbox": "tight",
    "savefig.pad_inches": 0.02, "pdf.fonttype": 42, "ps.fonttype": 42,
    "font.family": "sans-serif", "font.sans-serif": ["DejaVu Sans"],
    "font.size": 10, "axes.labelsize": 10, "axes.titlesize": 10,
    "xtick.labelsize": 9, "ytick.labelsize": 9,
    "legend.fontsize": 9, "legend.frameon": False,
    "axes.edgecolor": INK2, "axes.linewidth": 0.8,
    "axes.labelcolor": INK, "text.color": INK,
    "xtick.color": INK2, "ytick.color": INK2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "grid.linestyle": "-", "axes.axisbelow": True,
    "lines.linewidth": 2.0, "lines.markersize": 6,
    "axes.spines.top": False, "axes.spines.right": False,
})
OUT = RESULTS_DIR
PREFIX = "asic_"


def derive(data):
    """Add the quantities the plots need to every design, in place."""
    for rows in data.values():
        for r in rows:
            r["fmax"] = 1000.0 / r["crit"] if r["crit"] and r["crit"] > 0 else None
            r["nfp8"] = sum(r["mult"])
            tot = len(r["mult"]) + len(r["add"])
            r["frac8"] = (sum(r["mult"]) + sum(r["add"])) / tot if tot else 0.0
    return data


def finish(fig, name):
    os.makedirs(OUT, exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(OUT, f"{PREFIX}{name}.{ext}"))
    plt.close(fig)
    print(f"  wrote {os.path.join(OUT, PREFIX + name)}.pdf and .png")


def nice(ax, xlab, ylab, title=None):
    ax.set_xlabel(xlab)
    ax.set_ylabel(ylab)
    if title:
        ax.set_title(title, loc="left", pad=6)


def pareto_or_all(rows):
    return [r for r in rows if r["pareto"]] or rows


def fig_front(data, sizes=(256, 1024)):
    sizes = [n for n in sizes if n in data]
    if not sizes:
        return
    fig, axes = plt.subplots(1, len(sizes), figsize=(W2, 2.9), constrained_layout=True)
    axes = np.atleast_1d(axes)
    sc = None
    for ax, n in zip(axes, sizes):
        rows = [r for r in data[n] if r["energy"] and r["sqnr"] is not None]
        off = [r for r in rows if not r["pareto"]]
        on = [r for r in rows if r["pareto"]]
        if off:
            ax.scatter([r["sqnr"] for r in off], [r["energy"] for r in off],
                       s=26, c=NEUTRAL, alpha=.55, linewidths=0, zorder=2)
        if on:
            sc = ax.scatter([r["sqnr"] for r in on], [r["energy"] for r in on],
                            s=64, c=[r["frac8"] for r in on], cmap=SEQ,
                            vmin=0, vmax=1, edgecolors="white", linewidths=1.1, zorder=4)
        best, _ = choose(data[n])
        if best:
            ax.scatter([best["sqnr"]], [best["energy"]], s=190, marker="*", c=FP8,
                       edgecolors=INK, linewidths=0.8, zorder=6)
        nice(ax, "SQNR (dB)", "Energy per transform (nJ)", f"N = {n}")
        ax.margins(x=.10, y=.12)
    if sc is not None:
        cb = fig.colorbar(sc, ax=axes.tolist(), pad=0.015, aspect=28)
        cb.set_label("fraction of genes at FP8", size=9)
        cb.ax.tick_params(labelsize=8)
    fig.legend(handles=[
        Line2D([], [], marker="o", linestyle="none", markersize=5,
               color=NEUTRAL, alpha=.65, label="dominated"),
        Line2D([], [], marker="o", linestyle="none", markersize=8,
               markerfacecolor="#2E86C1", markeredgecolor="white",
               markeredgewidth=1.1, color="none", label="Pareto-optimal (energy, SQNR, delay)"),
        Line2D([], [], marker="*", linestyle="none", markersize=12,
               markerfacecolor=FP8, markeredgecolor=INK, color="none",
               label="selected design")],
        loc="outside upper center", ncol=3, handletextpad=.4, columnspacing=1.2)
    finish(fig, "fig5_pareto_energy_sqnr")


def fig_scaling(data):
    ns = sorted(data)
    if len(ns) < 2:
        return
    best_e, best_s = [], []
    for n in ns:
        rows = [r for r in data[n] if r["energy"]]
        best_e.append(min(r["energy"] for r in pareto_or_all(rows)))
        sq = [r["sqnr"] for r in rows if r["sqnr"] is not None]
        best_s.append(max(sq) if sq else np.nan)
    x = np.log2(ns)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(W2, 2.9), constrained_layout=True)
    a1.plot(x, best_e, "-o", color=FP4, markeredgecolor="white", markeredgewidth=1)
    a1.set_yscale("log")
    nice(a1, "Transform size N", "Lowest energy per transform (nJ)", "(a) Energy scaling")
    a2.plot(x, best_s, "-s", color=FP8, markeredgecolor="white", markeredgewidth=1)
    nice(a2, "Transform size N", "Best achievable SQNR (dB)", "(b) Accuracy ceiling")
    for ax in (a1, a2):
        ax.set_xticks(x)
        ax.set_xticklabels([str(n) for n in ns], rotation=45, ha="right")
    finish(fig, "fig6_scaling")


def fig_schedule(data):
    ns = [n for n in sorted(data) if n >= 16]
    if not ns:
        return
    picks = {}
    for n in ns:
        best, _ = choose(data[n])
        picks[n] = best or min(pareto_or_all([r for r in data[n] if r["energy"]]),
                               key=lambda r: r["energy"])
    smax = max(len(picks[n]["mult"]) for n in ns)
    cmap = ListedColormap([FP4, FP8])
    fig, axes = plt.subplots(1, 2, figsize=(W2, 3.1), constrained_layout=True)
    for ax, key, lab in zip(axes, ("mult", "add"),
                            ("(a) Multiplier precision", "(b) Adder precision")):
        grid = np.full((len(ns), smax), np.nan)
        for i, n in enumerate(ns):
            v = picks[n][key]
            grid[i, :len(v)] = v
        ax.imshow(np.ma.masked_invalid(grid), cmap=cmap, vmin=0, vmax=1,
                  aspect="auto", interpolation="nearest")
        for i in range(len(ns)):
            for j in range(smax):
                if grid[i, j] == 1:
                    ax.add_patch(plt.Rectangle((j - .5, i - .5), 1, 1, fill=False,
                                               hatch="///", edgecolor="white",
                                               linewidth=0, alpha=.55))
        ax.set_xticks(range(smax))
        ax.set_xticklabels(range(smax))
        ax.set_yticks(range(len(ns)))
        ax.set_yticklabels(ns)
        ax.set_xlabel("FFT stage index")
        ax.set_ylabel("Transform size N")
        ax.set_title(lab, loc="left", pad=6)
        ax.grid(False)
        ax.set_xticks(np.arange(-.5, smax, 1), minor=True)
        ax.set_yticks(np.arange(-.5, len(ns), 1), minor=True)
        ax.grid(which="minor", color="white", linewidth=1.4)
        ax.tick_params(which="minor", length=0)
    fig.legend(handles=[Patch(facecolor=FP4, label="FP4 (E2M1)"),
                        Patch(facecolor=FP8, hatch="///", edgecolor="white",
                              label="FP8 (E4M3)")],
               loc="outside upper center", ncol=2)
    finish(fig, "fig7_precision_schedule")


def fig_energy_freq(data, n=256):
    if n not in data:
        return
    rows = [r for r in data[n] if r["energy"] and r["fmax"]]
    if not rows:
        return
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(W2, 2.9), constrained_layout=True)
    k = np.array([r["nfp8"] for r in rows], float)
    jit = (np.random.default_rng(0).random(len(k)) - .5) * .28
    ks = sorted(set(int(v) for v in k))
    a1.scatter(k + jit, [r["energy"] for r in rows], s=46, c=FP4, alpha=.75,
               edgecolors="white", linewidths=.8)
    med = [np.median([r["energy"] for r in rows if r["nfp8"] == v]) for v in ks]
    a1.plot(ks, med, linestyle="none", marker="D", markersize=8, color=INK,
            markeredgecolor="white", markeredgewidth=1, zorder=5, label="median")
    nice(a1, "Stages using FP8 multiply", "Energy per transform (nJ)",
         "(a) Energy vs FP8 stages")
    a1.legend(loc="upper left")
    a2.scatter(k + jit, [r["fmax"] for r in rows], s=46, c=FP8, alpha=.75,
               edgecolors="white", linewidths=.8)
    medf = [np.median([r["fmax"] for r in rows if r["nfp8"] == v]) for v in ks]
    a2.plot(ks, medf, linestyle="none", marker="D", markersize=8, color=INK,
            markeredgecolor="white", markeredgewidth=1, zorder=5, label="median")
    nice(a2, "Stages using FP8 multiply", "Achievable $f_{max}$ (MHz)",
         "(b) $f_{max}$ vs FP8 stages")
    a2.legend(loc="upper right")
    finish(fig, "fig8_energy_frequency")


def timing_table(data):
    L = ["Timing, measured (fmax = 1000 / crit_delay_ns), all evaluated designs",
         f"{'N':>6} {'all-FP4 fmax':>13} {'any-FP8 fmax':>13} "
         f"{'all-FP4 crit':>13} {'any-FP8 crit':>13}",
         "-" * 62]
    for n in sorted(data):
        rows = data[n]
        f4 = [r for r in rows if r["nfp8"] == 0 and r["fmax"]]
        f8 = [r for r in rows if r["nfp8"] > 0 and r["fmax"]]
        g = lambda xs: f"{max(x['fmax'] for x in xs):.1f}" if xs else "-"
        c = lambda xs: f"{min(x['crit'] for x in xs):.2f}" if xs else "-"
        L.append(f"{n:>6} {g(f4):>13} {g(f8):>13} {c(f4):>13} {c(f8):>13}")
    L.append("Best fmax / shortest critical path within each group; '-' means no design "
             "in the group.")
    text = "\n".join(L) + "\n"
    print("\n" + text)
    path = os.path.join(OUT, PREFIX + "timing_table.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"  wrote {path}")


def main():
    global OUT
    ap = argparse.ArgumentParser(description="Paper figures for the ASIC mixed-precision sweep")
    ap.add_argument("--results", default=RESULTS_DIR, help="directory holding fft_N/ sub-directories")
    ap.add_argument("--out", default=RESULTS_DIR, help="where the figures are written")
    args = ap.parse_args()
    OUT = args.out
    data = derive(load_designs(args.results))
    if not data:
        sys.exit(f"no data under {args.results!r}")
    print(f"loaded {len(data)} sizes: {sorted(data)}")
    fig_front(data)
    fig_scaling(data)
    fig_schedule(data)
    fig_energy_freq(data)
    timing_table(data)


if __name__ == "__main__":
    main()
