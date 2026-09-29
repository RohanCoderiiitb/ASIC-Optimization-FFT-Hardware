#!/usr/bin/env python3
"""
FP16 Baseline POST-ROUTE PPA -- OpenROAD place-and-route + SAIF power
========================================================================
Companion to run_fp16_synthesis.py (pre-route). Writes its OWN report file
(fp16_postroute_ppa_report.txt) -- the pre-route report is untouched, so
both are available side by side.

Stage reached and why full detailed routing is not: see postroute_pnr.py's
module docstring. Short version: floorplan, 4x SRAM macro placement, PDN,
global placement, CTS, and global routing all complete; full DRC-clean
TritonRoute is blocked on this PDK by a ~2.3%-of-cells pin-access issue
(fixed cell set, independent of PDN/routing-layers/seed/density -- all
tested) that a `catch`-and-continue workaround does not actually fix (it
contaminates the critical-path number rather than just losing precision).
"Post-route" here therefore means post-placement/post-CTS/post-global-route,
with parasitics estimated from the global-route topology -- a large,
genuine accuracy improvement over pre-route (real macro placement, real
clock tree, real approximate routing), short of full signoff.

Power is measured the same way as the pre-route flow: the RTL testbench
(same 11 SQNR signals, same timing) is simulated with $dumpvars on, the
resulting VCD is converted to SAIF (vcd_to_saif.py), and OpenSTA's
`read_saif` annotates it onto the netlist actually used for P&R (the
FLATTENED one -- P&R needs a flat netlist, unlike the pre-route power flow's
hierarchy-preserved netlist, so coverage is the low single-digit-percent
figure the flat-netlist case always gets; see run_fp16_synthesis.py's
run_yosys docstring). No flat-activity fallback: below MIN_ANNOTATED_PINS
matched nets, the row is marked FAILED.

Usage (from anywhere):
    python3 fp16_baseline/synth/run_fp16_postroute.py                # all 10 sizes
    python3 fp16_baseline/synth/run_fp16_postroute.py --sizes 2 8
"""

import argparse
import math
import os
import re
import shutil
import sys
import zipfile

SYNTH_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SYNTH_DIR)
REPO_ROOT = os.path.dirname(BASE_DIR)

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, SYNTH_DIR)
from postroute_pnr import PostRoutePnR  # noqa: E402
from run_fp16_synthesis import (  # noqa: E402
    Fp16Synthesizer, DEFAULT_STD_LIB, DEFAULT_RAM_LIB, DEFAULT_SOURCE_DIR,
    DEFAULT_SHARED_DIR, DEFAULT_GENERATED_DIR, DEFAULT_CYCLES_FILE,
    MIN_ANNOTATED_PINS, MAX_POWER_MW, _load_exec_cycles,
)

ALL_SIZES = [2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]

TECH_LEF = os.path.join(REPO_ROOT, "45_nm_PDK", "cadence", "cadence_45nm",
                        "lef", "gsclib045_tech.lef")
CELL_LEF = os.path.join(REPO_ROOT, "45_nm_PDK", "cadence", "cadence_45nm",
                        "lef", "gsclib045_macro.lef")
SRAM_LEF = os.path.join(BASE_DIR, "fp16_SRAM_MACROS", "sram_512x32_2rw.lef")

MACRO_MODULE = "sram_512x32_2rw"
MACRO_W, MACRO_H = 239.105, 251.745  # um, from the SRAM LEF's SIZE
MACRO_INSTANCES = [
    "core.mem.b0_sub0_ram", "core.mem.b0_sub1_ram",
    "core.mem.b1_sub0_ram", "core.mem.b1_sub1_ram",
]

DEFAULT_WORK_DIR = os.path.join(SYNTH_DIR, "postroute_work")
DEFAULT_OUT = os.path.join(SYNTH_DIR, "fp16_postroute_ppa_report.txt")


def log(msg):
    print(f"[fp16-postroute] {msg}", flush=True)


def _archive(synth_dir, work_dir, archive_name="fp16_postroute_artifacts.zip"):
    if not os.path.isdir(work_dir):
        return
    archive_path = os.path.join(synth_dir, archive_name)
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(work_dir):
            for fname in files:
                full = os.path.join(root, fname)
                zf.write(full, arcname=os.path.relpath(full, synth_dir))
    shutil.rmtree(work_dir)


def main():
    ap = argparse.ArgumentParser(description="FP16 baseline POST-ROUTE PPA extraction (OpenROAD)")
    ap.add_argument("--sizes", type=int, nargs="*", default=ALL_SIZES)
    ap.add_argument("--clock-period", type=float, default=10.0)
    ap.add_argument("--std-lib", default=DEFAULT_STD_LIB)
    ap.add_argument("--ram-lib", default=DEFAULT_RAM_LIB)
    ap.add_argument("--source-dir", default=DEFAULT_SOURCE_DIR)
    ap.add_argument("--shared-dir", default=DEFAULT_SHARED_DIR)
    ap.add_argument("--generated-dir", default=DEFAULT_GENERATED_DIR)
    ap.add_argument("--work-dir", default=DEFAULT_WORK_DIR)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--cycles-file", default=DEFAULT_CYCLES_FILE)
    ap.add_argument("--yosys", default="yosys")
    ap.add_argument("--sta", default="sta")
    ap.add_argument("--openroad", default="openroad")
    ap.add_argument("--keep-work", action="store_true")
    args = ap.parse_args()

    yosys_synth = Fp16Synthesizer(
        clock_period=args.clock_period, std_lib=args.std_lib, ram_lib=args.ram_lib,
        source_dir=args.source_dir, shared_dir=args.shared_dir,
        generated_dir=args.generated_dir, work_dir=args.work_dir,
        yosys_path=args.yosys, sta_path=args.sta,
    )
    pnr = PostRoutePnR(
        design_prefix="fp16_fft", std_lib=args.std_lib, tech_lef=TECH_LEF,
        cell_lef=CELL_LEF, sram_lef=SRAM_LEF, sram_liberty=args.ram_lib,
        macro_module=MACRO_MODULE, macro_instances=MACRO_INSTANCES,
        macro_w=MACRO_W, macro_h=MACRO_H, clock_period=args.clock_period,
        fixed_lef_dir=os.path.join(SYNTH_DIR, "_pnr_fixed_lef"),
        openroad_path=args.openroad, sta_path=args.sta,
        min_annotated_pins=MIN_ANNOTATED_PINS,
    )

    rows = []
    for n in args.sizes:
        log(f"=== fp16_fft_{n} ===")
        work_dir = os.path.join(args.work_dir, f"fp16_fft_{n}")
        os.makedirs(work_dir, exist_ok=True)

        sources = yosys_synth.collect_sources(n)
        netlist_v, _yosys_log = yosys_synth.run_yosys(n, sources, work_dir, flatten=True, tag="netlist")
        if netlist_v is None:
            log(f"fp16_fft_{n}: Yosys FAILED - skipping P&R")
            rows.append({"n": n, "area_um2": None, "crit_delay_ns": None, "slack_ns": None,
                        "power_mw": None, "power_source": "SYNTH_FAILED",
                        "annotated_pins": 0, "total_pins": 0, "ok": False})
            continue

        saif_file = yosys_synth.generate_activity_saif(n, work_dir)
        if saif_file is None:
            log(f"fp16_fft_{n}: activity SAIF generation FAILED - P&R will run but power will be FAILED")
            saif_file = os.path.join(work_dir, "missing.saif")  # read_saif will just fail cleanly

        result = pnr.run(n, netlist_v, f"fp16_fft_{n}_top", saif_file, work_dir)
        result["n"] = n
        pct = (100.0 * result["annotated_pins"] / result["total_pins"]) if result["total_pins"] else 0.0
        log(f"fp16_fft_{n}: ok={result['ok']} area={result['area_um2']} "
            f"crit_delay={result['crit_delay_ns']} power={result['power_mw']} "
            f"(source={result['power_source']}, annotated={result['annotated_pins']}/"
            f"{result['total_pins']}={pct:.2f}%)")
        rows.append(result)

    exec_cycles_by_n = _load_exec_cycles(args.cycles_file)
    for r in rows:
        cycles = exec_cycles_by_n.get(r["n"])
        r["exec_cycles"] = cycles
        if cycles is not None and r["ok"]:
            r["energy_per_fft_nj"] = r["power_mw"] * cycles * args.clock_period / 1000.0
        else:
            r["energy_per_fft_nj"] = None

    hdr = (f"{'N':>6} | {'Area (um^2)':>12} | {'CritDelay (ns)':>14} | "
           f"{'Slack (ns)':>10} | {'Power (mW)':>11} | {'ExecCyc':>8} | "
           f"{'Energy/FFT (nJ)':>16} | {'ActivitySrc':>21} | "
           f"{'AnnotatedSignals':>24} | {'Status':>7}")
    sep = "-" * len(hdr)
    lines = [
        "FP16 baseline - POST-ROUTE PPA (OpenROAD: floorplan + macro placement + PDN + "
        "global placement + CTS + global route; SAIF-measured power)",
        "NOT full DRC-clean detailed routing -- see this script's module docstring and "
        "postroute_pnr.py for exactly why and what stage 'post-route' refers to here.",
        f"Clock period: {args.clock_period} ns",
        "Energy/FFT = Power(mW) * ExecCycles * ClockPeriod(ns) / 1000, "
        f"ExecCycles from {os.path.relpath(os.path.abspath(args.cycles_file), SYNTH_DIR)}",
        "Area is the fixed floorplan's core area (same for every N -- the 4-macro SRAM "
        "layout dominates area and does not change with N; see module docstring), not a "
        "cell-area sum. Power: SAIF conversion of a real RTL-simulation VCD (11 SQNR "
        "signals) annotated via OpenSTA's read_saif onto the FLATTENED P&R netlist "
        f"(ActivitySrc=saif_measured, >= {MIN_ANNOTATED_PINS} nets); no flat-activity "
        "fallback -- below that floor, or on any P&R failure, Status=FAILED.",
        "", hdr, sep,
    ]
    for r in rows:
        status = "OK" if r["ok"] else "FAILED"
        cyc_s = str(r["exec_cycles"]) if r["exec_cycles"] is not None else "N/A"
        e_s = f"{r['energy_per_fft_nj']:.3f}" if r["energy_per_fft_nj"] is not None else "N/A"
        area_s = f"{r['area_um2']:.1f}" if r["area_um2"] is not None else "N/A"
        delay_s = f"{r['crit_delay_ns']:.3f}" if r["crit_delay_ns"] is not None else "N/A"
        slack_s = f"{r['slack_ns']:.3f}" if r["slack_ns"] is not None else "N/A"
        power_s = f"{r['power_mw']:.4f}" if r["ok"] else "N/A"
        pct = (100.0 * r["annotated_pins"] / r["total_pins"]) if r["total_pins"] else 0.0
        annot_s = f"{r['annotated_pins']}/{r['total_pins']} ({pct:.2f}%)"
        lines.append(
            f"{r['n']:>6} | {area_s:>12} | {delay_s:>14} | {slack_s:>10} | "
            f"{power_s:>11} | {cyc_s:>8} | {e_s:>16} | {r['power_source']:>21} | "
            f"{annot_s:>24} | {status:>7}")
    text = "\n".join(lines) + "\n"

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(text)
    print("\n" + text + f"table: {args.out}")

    if not args.keep_work:
        _archive(SYNTH_DIR, args.work_dir)


if __name__ == "__main__":
    main()
