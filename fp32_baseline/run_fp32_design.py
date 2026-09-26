#!/usr/bin/env python3
"""
FP32 Baseline -- full pipeline driver
======================================
Runs the four steps needed to regenerate and (re-)characterize the FP32
baseline FFT cores, in order:

  1. fp32_template_generator.py   -- (re)generate generated_cores/fp32_fft_<N>/
  2. generate_fp32_twiddles.py    -- (re)generate source/twiddles_fp32_1024.txt
                                      and the synthesizable source/fp32_twiddle_rom.v
  3. sim/fp32_performance_evaluator.py -- Icarus simulation, SQNR + cycle counts
  4. synth/run_fp32_synthesis.py  -- Yosys + OpenSTA PPA extraction

Each step is a separate script with its own CLI; this driver just calls them
in sequence with a shared `--sizes` list and stops at the first failure (its
stderr/stdout have already been streamed to the console by then, so there is
nothing useful to add beyond which step failed).

Usage:
    python3 fp32_baseline/run_fp32_design.py                  # all 10 sizes, all steps
    python3 fp32_baseline/run_fp32_design.py --sizes 16 1024
    python3 fp32_baseline/run_fp32_design.py --skip-templates --skip-twiddles
    python3 fp32_baseline/run_fp32_design.py --clock-period 8.0
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))               # fp32_baseline
PYTHON = sys.executable

ALL_SIZES = [2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]


def run_step(label, cmd):
    print(f"\n{'=' * 70}\n[run_fp32_design] {label}\n{'=' * 70}", flush=True)
    print(f"[run_fp32_design] $ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=HERE)
    if result.returncode != 0:
        raise SystemExit(
            f"[run_fp32_design] Step failed ({label}): exit code {result.returncode}")


def main():
    ap = argparse.ArgumentParser(description="Run the full FP32 baseline pipeline.")
    ap.add_argument("--sizes", type=int, nargs="*", default=ALL_SIZES,
                     help="FFT sizes to carry through every step (default: all 10)")
    ap.add_argument("--clock-period", type=float, default=10.0,
                     help="Clock period in ns passed to the synthesis step (default 10.0)")

    ap.add_argument("--skip-templates", action="store_true",
                     help="Skip fp32_template_generator.py")
    ap.add_argument("--skip-twiddles", action="store_true",
                     help="Skip generate_fp32_twiddles.py")
    ap.add_argument("--skip-perf", action="store_true",
                     help="Skip sim/fp32_performance_evaluator.py")
    ap.add_argument("--skip-synth", action="store_true",
                     help="Skip synth/run_fp32_synthesis.py")

    ap.add_argument("--max-n", type=int, default=1024,
                     help="Forwarded to generate_fp32_twiddles.py --max-n")
    args = ap.parse_args()

    size_args = [str(n) for n in args.sizes]

    if not args.skip_templates:
        run_step(
            "1/4  fp32_template_generator.py",
            [PYTHON, "fp32_template_generator.py", "--sizes", *size_args],
        )

    if not args.skip_twiddles:
        run_step(
            "2/4  generate_fp32_twiddles.py",
            [PYTHON, "generate_fp32_twiddles.py", "--max-n", str(args.max_n)],
        )

    if not args.skip_perf:
        run_step(
            "3/4  sim/fp32_performance_evaluator.py",
            [PYTHON, os.path.join("sim", "fp32_performance_evaluator.py"),
             "--sizes", *size_args],
        )

    if not args.skip_synth:
        run_step(
            "4/4  synth/run_fp32_synthesis.py",
            [PYTHON, os.path.join("synth", "run_fp32_synthesis.py"),
             "--sizes", *size_args, "--clock-period", str(args.clock_period)],
        )

    print(f"\n{'=' * 70}\n[run_fp32_design] All requested steps completed.\n{'=' * 70}")


if __name__ == "__main__":
    main()
