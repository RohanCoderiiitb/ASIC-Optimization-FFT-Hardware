#!/usr/bin/env python3
"""
Mixed-Precision FFT POST-ROUTE PPA -- OpenROAD place-and-route + SAIF power
============================================================================
Companion to run_mixed_synthesis.py (pre-route), mirroring
fp16_baseline/synth/run_fp16_postroute.py / fp32_baseline/synth/
run_fp32_postroute.py for the mixed-precision (all-FP8 reference chromosome)
design. Same OpenROAD stage (floorplan + 4x SRAM macro placement + PDN +
global placement + CTS + global route -- NOT full DRC-clean detailed
routing, see postroute_pnr.py's module docstring for why) and the same
SAIF-based power methodology.

Usage (from repo root):
    python3 run_mixed_postroute.py                # all 10 sizes
    python3 run_mixed_postroute.py --sizes 16
    python3 run_mixed_postroute.py --sizes 1024
"""

import argparse
import os
import shutil
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)

from postroute_pnr import PostRoutePnR  # noqa: E402
from run_mixed_synthesis import (  # noqa: E402
    MixedSynthesizer, DEFAULT_STD_LIB, DEFAULT_RAM_LIB, DEFAULT_SHARED_DIR,
    DEFAULT_GENERATED_DIR, MIN_ANNOTATED_PINS, all_fp8_chromosome,
)

ALL_SIZES = [2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]

TECH_LEF = os.path.join(REPO_ROOT, "45_nm_PDK", "cadence", "cadence_45nm",
                        "lef", "gsclib045_tech.lef")
CELL_LEF = os.path.join(REPO_ROOT, "45_nm_PDK", "cadence", "cadence_45nm",
                        "lef", "gsclib045_macro.lef")
SRAM_LEF = os.path.join(REPO_ROOT, "openram_outputs", "sram_512x24_2rw.lef")

MACRO_MODULE = "sram_512x24_2rw"
MACRO_W, MACRO_H = 196.005, 249.505  # um, from the SRAM LEF's SIZE
MACRO_INSTANCES = [
    "core.mem.b0_sub0_ram", "core.mem.b0_sub1_ram",
    "core.mem.b1_sub0_ram", "core.mem.b1_sub1_ram",
]

DEFAULT_WORK_DIR = os.path.join(REPO_ROOT, "mixed_postroute_work")
DEFAULT_OUT = os.path.join(REPO_ROOT, "mixed_postroute_ppa_report.txt")


def log(msg):
    print(f"[mixed-postroute] {msg}", flush=True)


def _archive(root_dir, work_dir, archive_name="mixed_postroute_artifacts.zip"):
    if not os.path.isdir(work_dir):
        return
    archive_path = os.path.join(root_dir, archive_name)
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(work_dir):
            for fname in files:
                full = os.path.join(root, fname)
                zf.write(full, arcname=os.path.relpath(full, root_dir))
    shutil.rmtree(work_dir)


def main():
    ap = argparse.ArgumentParser(description="Mixed-precision FFT POST-ROUTE PPA extraction (OpenROAD)")
    ap.add_argument("--sizes", type=int, nargs="*", default=ALL_SIZES)
    ap.add_argument("--clock-period", type=float, default=10.0)
    ap.add_argument("--std-lib", default=DEFAULT_STD_LIB)
    ap.add_argument("--ram-lib", default=DEFAULT_RAM_LIB)
    ap.add_argument("--shared-dir", default=DEFAULT_SHARED_DIR)
    ap.add_argument("--generated-dir", default=DEFAULT_GENERATED_DIR)
    ap.add_argument("--work-dir", default=DEFAULT_WORK_DIR)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--yosys", default="yosys")
    ap.add_argument("--sta", default="sta")
    ap.add_argument("--openroad", default="openroad")
    ap.add_argument("--keep-work", action="store_true")
    args = ap.parse_args()

    yosys_synth = MixedSynthesizer(
        clock_period=args.clock_period, std_lib=args.std_lib, ram_lib=args.ram_lib,
        shared_dir=args.shared_dir, generated_dir=args.generated_dir,
        work_dir=args.work_dir, yosys_path=args.yosys, sta_path=args.sta,
    )
    pnr = PostRoutePnR(
        design_prefix="mixed_fft", std_lib=args.std_lib, tech_lef=TECH_LEF,
        cell_lef=CELL_LEF, sram_lef=SRAM_LEF, sram_liberty=args.ram_lib,
        macro_module=MACRO_MODULE, macro_instances=MACRO_INSTANCES,
        macro_w=MACRO_W, macro_h=MACRO_H, clock_period=args.clock_period,
        fixed_lef_dir=os.path.join(REPO_ROOT, "_mixed_pnr_fixed_lef"),
        openroad_path=args.openroad, sta_path=args.sta,
        min_annotated_pins=MIN_ANNOTATED_PINS,
    )

    rows = []
    for n in args.sizes:
        log(f"=== mixed_fft_{n} ===")
        work_dir = os.path.join(args.work_dir, f"mixed_fft_{n}")
        os.makedirs(work_dir, exist_ok=True)

        chromosome = all_fp8_chromosome(n)
        sources = yosys_synth.collect_sources(n, chromosome)
        netlist_v, _yosys_log = yosys_synth.run_yosys(n, sources, work_dir, flatten=True, tag="netlist")
        if netlist_v is None:
            log(f"mixed_fft_{n}: Yosys FAILED - skipping P&R")
            rows.append({"n": n, "area_um2": None, "crit_delay_ns": None, "slack_ns": None,
                        "power_mw": None, "power_source": "SYNTH_FAILED",
                        "annotated_pins": 0, "total_pins": 0, "ok": False})
            continue

        saif_file = yosys_synth.generate_activity_saif(n, chromosome, work_dir)
        if saif_file is None:
            log(f"mixed_fft_{n}: activity SAIF generation FAILED - P&R will run but power will be FAILED")
            saif_file = os.path.join(work_dir, "missing.saif")

        result = pnr.run(n, netlist_v, f"mixed_fft_{n}_top", saif_file, work_dir)
        result["n"] = n
        pct = (100.0 * result["annotated_pins"] / result["total_pins"]) if result["total_pins"] else 0.0
        log(f"mixed_fft_{n}: ok={result['ok']} area={result['area_um2']} "
            f"crit_delay={result['crit_delay_ns']} power={result['power_mw']} "
            f"(source={result['power_source']}, annotated={result['annotated_pins']}/"
            f"{result['total_pins']}={pct:.2f}%)")
        rows.append(result)

    hdr = (f"{'N':>6} | {'Area (um^2)':>12} | {'CritDelay (ns)':>14} | "
           f"{'Slack (ns)':>10} | {'Power (mW)':>11} | "
           f"{'ActivitySrc':>21} | {'AnnotatedSignals':>24} | {'Status':>7}")
    sep = "-" * len(hdr)
    lines = [
        "Mixed-precision FFT (all-FP8 reference chromosome) - POST-ROUTE PPA "
        "(OpenROAD: floorplan + macro placement + PDN + global placement + CTS + "
        "global route; SAIF-measured power)",
        "NOT full DRC-clean detailed routing -- see postroute_pnr.py's module "
        "docstring for exactly why and what stage 'post-route' refers to here.",
        f"Clock period: {args.clock_period} ns",
        "Area is the fixed floorplan's core area (same for every N -- the 4-macro "
        "SRAM layout dominates area and does not change with N), not a cell-area "
        "sum. Power: SAIF conversion of a real RTL-simulation VCD annotated via "
        "OpenSTA's read_saif onto the FLATTENED P&R netlist (ActivitySrc="
        f"saif_measured, >= {MIN_ANNOTATED_PINS} nets); no flat-activity fallback "
        "-- below that floor, or on any P&R failure, Status=FAILED.",
        "", hdr, sep,
    ]
    for r in rows:
        status = "OK" if r["ok"] else "FAILED"
        area_s = f"{r['area_um2']:.1f}" if r["area_um2"] is not None else "N/A"
        delay_s = f"{r['crit_delay_ns']:.3f}" if r["crit_delay_ns"] is not None else "N/A"
        slack_s = f"{r['slack_ns']:.3f}" if r["slack_ns"] is not None else "N/A"
        power_s = f"{r['power_mw']:.4f}" if r["ok"] else "N/A"
        pct = (100.0 * r["annotated_pins"] / r["total_pins"]) if r["total_pins"] else 0.0
        annot_s = f"{r['annotated_pins']}/{r['total_pins']} ({pct:.2f}%)"
        lines.append(
            f"{r['n']:>6} | {area_s:>12} | {delay_s:>14} | {slack_s:>10} | "
            f"{power_s:>11} | {r['power_source']:>21} | "
            f"{annot_s:>24} | {status:>7}")
    text = "\n".join(lines) + "\n"

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(text)
    print("\n" + text + f"table: {args.out}")

    if not args.keep_work:
        _archive(REPO_ROOT, args.work_dir)


if __name__ == "__main__":
    main()
