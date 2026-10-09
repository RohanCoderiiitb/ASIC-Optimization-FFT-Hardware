"""SOTA comparison table (TABLE 4) for results/asic_best_designs.txt.

Our selected design per N (results/asic_best_designs.csv, total power from
results/fft_N/all_solutions_fftN.csv) is compared with published FFT
accelerators using the normalisations those papers define, applied identically
to every row:

  Chen 2018 (TVLSI 26(10), Eq. 7-8):
      A_norm = Area[mm2]*1e3 / ((Process/45)^2 * M * log2 N)
      E_norm = Power[mW]*Time[us]*1e3 / ((V/0.9)^2 * M * N * log2 N)
  Beulet Paul 2014 (EURASIP JASP 2014:144, Eq. 12-13):
      A_norm = Area[mm2]*1e3 / (N * (Process/45)^2 * (Word/64))
      P_norm = Power[mW]*Tclk[ns]*1e3 / ((V/1.08)^2 * N * (Word/64))

No technology scaling beyond what those formulas contain. The CNN-NTT paper
(Prasetiyo 2023) is an FPGA CNN accelerator with no FFT size/area/energy per
transform, so it has no row; see the note under the table.

Usage: python3 nsga_dse/sota_comparison.py   (rewrites TABLE 4 in the .txt)
"""
import csv
import math
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results")
TXT = os.path.join(RES, "asic_best_designs.txt")

# This work: fast_vdd1v0 45 nm library, power measured at the 10 ns constraint,
# 1 butterfly stream, complex sample = 2 x FP8 = 16 bits at the I/O.
OUR_TECH, OUR_V, OUR_CLK_NS, OUR_M, OUR_WORD = 45, 1.0, 10.0, 1, 16

# Published designs. exec_us = execution time per transform.
# Chen: Table II/III (N=1024: 1380 ns @ 1 GHz, 91.3 mW, 2.4 mm2, 0.9 V, 2 streams,
#   FP32 -> 64-bit complex). Ref[31] via Chen Table III (65 nm, 1 V, 4.6 mm2,
#   172.38 mW, 2.81 us, 2 streams, 500 MHz; word length not reported).
# Beulet: Tables 4, 7, 8 (45 nm, 1.08 V, 0.973 mm2, 68.17 mW, 100 MHz, 64-bit).
BEULET_US = {64: 7.64, 128: 17.84, 256: 40.92, 512: 92.48, 1024: 206.44}
SOTA = [
    dict(n=n, name="Beulet'14 DA-FFT", tech=45, v=1.08, word=64, clk_ns=10.0, m=1,
         area=0.973, power=68.17, us=BEULET_US[n]) for n in BEULET_US
] + [
    dict(n=1024, name="Chen'18 MT-FFT", tech=45, v=0.9, word=64, clk_ns=1.0, m=2,
         area=2.4, power=91.3, us=1.38),
    dict(n=1024, name="Chen'18 ref[31]", tech=65, v=1.0, word=None, clk_ns=2.0, m=2,
         area=4.6, power=172.38, us=2.81),
]


def chen(d):
    lg = math.log2(d["n"])
    a = d["area"] * 1e3 / ((d["tech"] / 45) ** 2 * d["m"] * lg)
    e = d["power"] * d["us"] * 1e3 / ((d["v"] / 0.9) ** 2 * d["m"] * d["n"] * lg)
    return a, e


def beulet(d):
    if not d["word"]:
        return None, None
    w = d["word"] / 64
    a = d["area"] * 1e3 / (d["n"] * (d["tech"] / 45) ** 2 * w)
    p = d["power"] * d["clk_ns"] * 1e3 / ((d["v"] / 1.08) ** 2 * d["n"] * w)
    return a, p


def ours():
    rows = []
    with open(os.path.join(RES, "asic_best_designs.csv"), newline="") as f:
        for r in csv.DictReader(f):
            n, sid = int(r["N"]), r["solution_id"]
            tot = None
            with open(os.path.join(RES, f"fft_{n}", f"all_solutions_fft{n}.csv"), newline="") as g:
                for s in csv.DictReader(g):
                    if s["solution_id"] == sid:
                        tot = float(s["total_power_mW"])
            rows.append(dict(
                n=n, name="This work", tech=OUR_TECH, v=OUR_V, word=OUR_WORD,
                clk_ns=OUR_CLK_NS, m=OUR_M, area=float(r["area_um2"]) / 1e6, power=tot,
                us=float(r["exec_cycles"]) * OUR_CLK_NS / 1e3))
    return rows


def fmt(x, f):
    return "-" if x is None else f.format(x)


def build():
    cols = [("FFT size", "points", lambda d: str(d["n"])),
            ("Design", "", lambda d: d["name"]),
            ("Process", "nm", lambda d: str(d["tech"])),
            ("Voltage", "V", lambda d: f"{d['v']:.2f}"),
            ("Complex word", "bits", lambda d: fmt(d["word"], "{:d}")),
            ("Clock", "MHz", lambda d: f"{1e3 / d['clk_ns']:,.0f}"),
            ("Area", "mm^2", lambda d: f"{d['area']:.3f}"),
            ("Power", "mW", lambda d: f"{d['power']:.2f}"),
            ("Time per transform", "us", lambda d: f"{d['us']:.3f}"),
            ("Energy per transform", "nJ", lambda d: f"{d['power'] * d['us']:.2f}"),
            ("Throughput", "M transforms/s", lambda d: f"{1 / d['us']:.3f}"),
            ("Chen norm. area", "mm^2*1e3/(M*logN)", lambda d: f"{chen(d)[0]:.2f}"),
            ("Chen norm. energy", "nJ*1e3/(M*N*logN)", lambda d: f"{chen(d)[1]:.3f}"),
            ("Beulet norm. area", "mm^2*1e3/N", lambda d: fmt(beulet(d)[0], "{:.3f}")),
            ("Beulet norm. power", "mW*ns*1e3/N", lambda d: fmt(beulet(d)[1], "{:.3f}"))]
    mine = ours()
    rows = []
    for n in sorted({d["n"] for d in mine}):
        rows += [d for d in mine if d["n"] == n] + [d for d in SOTA if d["n"] == n]
    labels = [c[0] for c in cols]
    units = [c[1] for c in cols]
    body = [[c[2](r) for c in cols] for r in rows]
    w = [max(len(labels[i]), len(units[i]), *(len(b[i]) for b in body)) for i in range(len(cols))]
    L = ["", "TABLE 4  COMPARISON WITH PUBLISHED FFT ACCELERATORS (published normalisations)",
         "  ".join(l.rjust(x) for l, x in zip(labels, w)),
         "  ".join(u.rjust(x) for u, x in zip(units, w)),
         "-" * (sum(w) + 2 * (len(w) - 1))]
    prev = None
    for r, b in zip(rows, body):
        if prev is not None and r["n"] != prev:
            L.append("")
        prev = r["n"]
        L.append("  ".join(v.rjust(x) for v, x in zip(b, w)))
    L += [
        "  Lower is better for every normalised column. Published rows are quoted from their",
        "  papers (Beulet'14: Tables 4/7/8; Chen'18: Tables I-III) and normalised with the formulas below.",
        "  Chen'18 (Eq. 7-8): A = Area*1e3/((Process/45)^2*M*log2 N); E = Power*Time*1e3/((V/0.9)^2*M*N*log2 N).",
        "  Beulet'14 (Eq. 12-13): A = Area*1e3/(N*(Process/45)^2*(Word/64)); P = Power*Tclk*1e3/((V/1.08)^2*N*(Word/64)).",
        "  This work: 45 nm library at 1.0 V, M = 1 stream, complex word 16 bit (2 x FP8 at the I/O), total power",
        "  (dynamic+static) at the 10 ns constraint clock, time = cycles x 10 ns (compute only, no DMA/SoC transfer).",
        "  Area is the post-route core, fixed by the SRAM macros, so it does not shrink with N; the area-normalised",
        "  columns therefore favour larger N. Chen'18 time includes DDR transfer; Beulet'14 and this work do not.",
        "  Beulet'14 quote energy per FFT point (their Eq. 14 divides by N, e.g. 13 nJ at N=1024); the energy column",
        "  here is per transform (Power x Time) for every row, so their figure x N is what appears.",
        "  Chen'18 ref[31]: word length not reported, so the Beulet columns are left blank.",
        "  Prasetiyo'23 (NTT CNN accelerator, Alveo U50 FPGA, 26 W, 110 GOPS/W on VGG-16) is a CNN engine with no",
        "  FFT size, area or per-transform energy, so these metrics cannot be applied to it; it is not tabulated.",
    ]
    return L


def main():
    text = open(TXT, encoding="utf-8").read().rstrip("\n").split("\n")
    cut = next((i for i, x in enumerate(text) if x.startswith("TABLE 4")), len(text))
    # drop the blank line that preceded an existing TABLE 4 block
    while cut > 0 and not text[cut - 1].strip():
        cut -= 1
    out = text[:cut] + build()
    open(TXT, "w", encoding="utf-8").write("\n".join(out) + "\n")
    print("\n".join(build()))


if __name__ == "__main__":
    main()
