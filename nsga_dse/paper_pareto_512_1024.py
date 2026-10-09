"""Energy-vs-SQNR Pareto plot for N = 512 and 1024 (paper figure).

Same data and selection rule as paper_figures_asic.py's fig5, with saturated
point colours and no area/power/delay panels. Writes to the repository root
(git-ignored), not to results/.

Usage: python3 nsga_dse/paper_pareto_512_1024.py
"""
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.lines import Line2D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_figures_asic as pf  # noqa: E402  (applies the shared rcParams)
from select_best_design import choose, load_designs  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "pareto_energy_sqnr_N512_N1024")

DOMINATED = "#FF7A00"
STAR = "#FFD400"
VIVID = LinearSegmentedColormap.from_list("vivid", ["#00B2FF", "#6A00FF", "#FF0090"])


def main():
    data = pf.derive(load_designs(pf.RESULTS_DIR))
    sizes = [n for n in (512, 1024) if n in data]
    fig, axes = plt.subplots(1, len(sizes), figsize=(pf.W2, 3.0), constrained_layout=True)
    axes = np.atleast_1d(axes)
    sc = None
    for ax, n in zip(axes, sizes):
        rows = [r for r in data[n] if r["energy"] and r["sqnr"] is not None]
        off = [r for r in rows if not r["pareto"]]
        on = [r for r in rows if r["pareto"]]
        ax.scatter([r["sqnr"] for r in off], [r["energy"] for r in off], s=40,
                   c=DOMINATED, edgecolors=pf.INK, linewidths=0.5, zorder=2)
        sc = ax.scatter([r["sqnr"] for r in on], [r["energy"] for r in on], s=80,
                        c=[r["frac8"] for r in on], cmap=VIVID, vmin=0, vmax=1,
                        edgecolors=pf.INK, linewidths=0.8, zorder=4)
        best, _ = choose(data[n])
        if best:
            ax.scatter([best["sqnr"]], [best["energy"]], s=240, marker="*", c=STAR,
                       edgecolors="black", linewidths=1.1, zorder=6)
        pf.nice(ax, "SQNR (dB)", "Energy per transform (nJ)", f"N = {n}")
        ax.margins(x=.10, y=.12)
    cb = fig.colorbar(sc, ax=axes.tolist(), pad=0.015, aspect=28)
    cb.set_label("fraction of genes at FP8", size=9)
    cb.ax.tick_params(labelsize=8)
    fig.legend(handles=[
        Line2D([], [], marker="o", linestyle="none", markersize=7, markerfacecolor=DOMINATED,
               markeredgecolor=pf.INK, color="none", label="dominated"),
        Line2D([], [], marker="o", linestyle="none", markersize=9, markerfacecolor="#6A00FF",
               markeredgecolor=pf.INK, color="none", label="Pareto-optimal"),
        Line2D([], [], marker="*", linestyle="none", markersize=14, markerfacecolor=STAR,
               markeredgecolor="black", color="none", label="selected design")],
        loc="outside upper center", ncol=3, handletextpad=.4, columnspacing=1.2)
    fig.savefig(OUT + ".png")
    plt.close(fig)
    print("wrote", OUT + ".png")


if __name__ == "__main__":
    main()
