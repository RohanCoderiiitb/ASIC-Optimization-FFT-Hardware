#!/usr/bin/env python3
"""
run_soc_eval.py
================
Measures END-TO-END (load+compute+unload, through an actual RISC-V core
issuing instructions) throughput cycles for one (track, N) configuration,
by compiling and simulating the PicoRV32 + FFT SoC with Icarus Verilog.

HARD CONSTRAINT: this script calls `iverilog` and `vvp` ONLY. It never
calls yosys/sta/openroad on picorv32_fft_soc.v or anything instantiating
it -- the SoC is a simulation-only integration target, never synthesized.

Per config:
  1. Get the RTL (mixed: regenerated on demand from asic_best_designs.csv's
     chosen chromosome via fft_template_generator; fp16/fp32: already on
     disk under <track>_baseline/generated_cores/).
  2. Generate a matching fft_pcpi_wrapper.v (soc_wrapper_gen.py) and batch
     firmware (soc_firmware_gen.py + rv32i_asm.assemble), pack the same 11
     test-vector signals each track's own standalone evaluator already
     uses into firmware.hex.
  3. Compile with iverilog, run with vvp against tb_fft_soc_perf.v.
  4. Parse `PERF ...` lines -> end-to-end cycles/transform
     (frame_cycles_sum/frames_done) and a compute-only cross-check
     (cycles_wait/frames_done, compared against that design's already-
     recorded standalone avg_exec_cycles).
  5. Decode results.hex with the track's own unpack + calculate_sqnr,
     compare against that design's already-recorded standalone SQNR --
     SQNR must not drop by more than SQNR_DROP_TOLERANCE_DB.
  6. Append one row to results/asic_soc_throughput.csv.

Usage:
    python3 run_soc_eval.py --track mixed --n 256
    python3 run_soc_eval.py --track fp16 --n 256
    python3 run_soc_eval.py --track fp32 --n 256
    python3 run_soc_eval.py --all                  # all 30 configs
"""
import argparse
import csv
import glob
import math
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
NSGA_DIR = os.path.join(REPO_ROOT, "nsga_dse")
RESULTS_DIR = os.path.join(REPO_ROOT, "results")

sys.path.insert(0, HERE)
sys.path.insert(0, NSGA_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "fp16_baseline", "sim"))
sys.path.insert(0, os.path.join(REPO_ROOT, "fp32_baseline", "sim"))

from soc_wrapper_gen import generate_wrapper, _addr_width
from soc_firmware_gen import compute_layout, build_asm_lines
from rv32i_asm import assemble

ALL_N = [2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]
NUM_TESTS = 11
SQNR_DROP_TOLERANCE_DB = 0.5
CYCLES_WAIT_TOLERANCE = 0.15   # 15% -- PCPI/handshake overhead budget

OUT_CSV = os.path.join(RESULTS_DIR, "asic_soc_throughput.csv")


def log(msg):
    print(f"[soc-eval] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Track adapters
# ---------------------------------------------------------------------------

def _best_designs_rows():
    path = os.path.join(RESULTS_DIR, "asic_best_designs.csv")
    with open(path, newline="", encoding="utf-8") as f:
        return {int(r["N"]): r for r in csv.DictReader(f)}


def _read_sqnr_cycles_file(path, n):
    """Parses fp16/fp32 sim/perf/<track>_sqnr_results.txt -> (sqnr_db, exec_cycles) for N."""
    row_pat = re.compile(r"^\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(-?[\d.]+)")
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = row_pat.match(line)
            if m and int(m.group(1)) == n:
                return float(m.group(3)), int(m.group(2))
    raise RuntimeError(f"N={n} not found in {path}")


class MixedAdapter:
    track = "mixed"
    data_width = 16
    transfer_mode = "single"

    def __init__(self, n):
        self.n = n
        from fft_template_generator import FFTTemplateGenerator
        row = _best_designs_rows()[n]
        self.chromosome = [int(c) for c in row["chromosome"]]
        self.standalone_sqnr = float(row["sqnr_dB"])
        self.standalone_cycles = int(float(row["exec_cycles"]))
        self.first_stage_fp8 = bool(self.chromosome[1])
        self.final_stage_fp8 = bool(self.chromosome[-1])

        from performance_evaluator import PerformanceEvaluator
        self.pe = PerformanceEvaluator(n)
        self.top_module = f"mixed_fft_{n}_top"
        self.gen = FFTTemplateGenerator(n)

    def rtl_files(self, scratch_dir):
        top_file = self.gen.generate_complete_fft(self.chromosome, output_dir=scratch_dir)
        core_file = top_file.replace("_top.v", "_core.v")
        return core_file, top_file

    def shared_sources(self):
        verilog_dir = os.path.join(REPO_ROOT, "verilog_sources")
        exclude = {"fft_test.v", "tb_fft_test.v"}
        return sorted(f for f in glob.glob(os.path.join(verilog_dir, "*.v"))
                      if os.path.basename(f) not in exclude)

    def golden(self):
        return self.pe._compute_golden_for_precision(fp8_input=self.first_stage_fp8)

    def pack_sample(self, sample):
        """One 16-bit word, always FP8-packed on the wire (matches
        performance_evaluator.py's vec_init -- the first-stage precision
        only affects internal conversion, not the port encoding)."""
        re_fp8 = self.pe.float_to_fp8_e4m3(sample.real) & 0xFF
        im_fp8 = self.pe.float_to_fp8_e4m3(sample.imag) & 0xFF
        return [(re_fp8 << 8) | im_fp8]

    def unpack_result(self, words):
        word = words[0] & 0xFFFF
        if self.final_stage_fp8:
            real = self.pe.fp8_to_float((word >> 8) & 0xFF)
            imag = self.pe.fp8_to_float(word & 0xFF)
        else:
            real = self.pe.fp4_to_float((word >> 4) & 0xF)
            imag = self.pe.fp4_to_float(word & 0xF)
        return complex(real, imag)

    def sqnr(self, golden, approx):
        return self.pe.calculate_sqnr(golden, approx, final_stage_is_fp8=self.final_stage_fp8)


class Fp16Adapter:
    track = "fp16"
    data_width = 32
    transfer_mode = "single"

    def __init__(self, n):
        self.n = n
        from fp16_performance_evaluator import FP16PerformanceEvaluator
        self.pe = FP16PerformanceEvaluator(n)
        self.top_module = f"fp16_fft_{n}_top"
        sqnr_file = os.path.join(REPO_ROOT, "fp16_baseline", "sim", "perf", "fp16_sqnr_results.txt")
        self.standalone_sqnr, self.standalone_cycles = _read_sqnr_cycles_file(sqnr_file, n)

    def rtl_files(self, scratch_dir):
        d = os.path.join(REPO_ROOT, "fp16_baseline", "generated_cores", f"fp16_fft_{self.n}")
        return (os.path.join(d, f"fp16_fft_{self.n}_core.v"),
                os.path.join(d, f"fp16_fft_{self.n}_top.v"))

    def shared_sources(self):
        source_dir = os.path.join(REPO_ROOT, "fp16_baseline", "source")
        shared = [os.path.join(REPO_ROOT, "verilog_sources", f) for f in ("agu.v", "bit_reversal.v")]
        return sorted(glob.glob(os.path.join(source_dir, "*.v"))) + shared

    def golden(self):
        return self.pe._compute_golden_outputs()

    def pack_sample(self, sample):
        word = (self.pe.float_to_fp16(sample.real) << 16) | self.pe.float_to_fp16(sample.imag)
        return [word]

    def unpack_result(self, words):
        word = words[0] & 0xFFFFFFFF
        real = self.pe.fp16_to_float(word >> 16)
        imag = self.pe.fp16_to_float(word & 0xFFFF)
        return complex(real, imag)

    def sqnr(self, golden, approx):
        return self.pe.calculate_sqnr(golden, approx)


class Fp32Adapter:
    track = "fp32"
    data_width = 64
    transfer_mode = "hilo64"

    def __init__(self, n):
        self.n = n
        from fp32_performance_evaluator import FP32PerformanceEvaluator
        self.pe = FP32PerformanceEvaluator(n)
        self.top_module = f"fp32_fft_{n}_top"
        sqnr_file = os.path.join(REPO_ROOT, "fp32_baseline", "sim", "perf", "fp32_sqnr_results.txt")
        self.standalone_sqnr, self.standalone_cycles = _read_sqnr_cycles_file(sqnr_file, n)

    def rtl_files(self, scratch_dir):
        d = os.path.join(REPO_ROOT, "fp32_baseline", "generated_cores", f"fp32_fft_{self.n}")
        return (os.path.join(d, f"fp32_fft_{self.n}_core.v"),
                os.path.join(d, f"fp32_fft_{self.n}_top.v"))

    def shared_sources(self):
        source_dir = os.path.join(REPO_ROOT, "fp32_baseline", "source")
        shared = [os.path.join(REPO_ROOT, "verilog_sources", f) for f in ("agu.v", "bit_reversal.v")]
        return sorted(glob.glob(os.path.join(source_dir, "*.v"))) + shared

    def golden(self):
        return self.pe._compute_golden_outputs()

    def pack_sample(self, sample):
        # hi/lo protocol (see soc_wrapper_gen.generate_hilo64_wrapper): the
        # FIRST word (FFTLOAD, latched into the wrapper's stage_lo) carries
        # IMAG; the SECOND word (FFTLOADHI, combined as {rs1, stage_lo})
        # carries REAL -- so load_data ends up {real, imag}, matching
        # fp32_to_float(word>>32)=real / (word&0xFFFFFFFF)=imag below.
        real_bits = self.pe.float_to_fp32(sample.real)
        imag_bits = self.pe.float_to_fp32(sample.imag)
        return [imag_bits, real_bits]

    def unpack_result(self, words):
        # FFTSTORE returns fft_unload_data[31:0] (=imag, stored first by the
        # firmware); FFTSTOREHI returns fft_unload_data[63:32] (=real,
        # stored second). Recombine in the same order the standalone
        # evaluator's word64 expects: (real<<32)|imag.
        imag_bits, real_bits = words[0] & 0xFFFFFFFF, words[1] & 0xFFFFFFFF
        word64 = (real_bits << 32) | imag_bits
        real = self.pe.fp32_to_float(word64 >> 32)
        imag = self.pe.fp32_to_float(word64 & 0xFFFFFFFF)
        return complex(real, imag)

    def sqnr(self, golden, approx):
        return self.pe.calculate_sqnr(golden, approx)


ADAPTERS = {"mixed": MixedAdapter, "fp16": Fp16Adapter, "fp32": Fp32Adapter}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

_SAFE_TOOLS = {"iverilog", "vvp"}


def _run(cmd, cwd, timeout=600):
    tool = os.path.basename(cmd[0])
    if tool not in _SAFE_TOOLS:
        raise RuntimeError(
            f"refusing to run {tool!r}: run_soc_eval.py may only invoke "
            f"{_SAFE_TOOLS} (never yosys/sta/openroad -- the SoC is "
            f"simulation-only, see module docstring)")
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def run_one(track, n, keep_work=False, work_root=None):
    log(f"=== {track} N={n} ===")
    adapter = ADAPTERS[track](n)

    work_dir = work_root or os.path.join(HERE, "build", f"{track}_{n}")
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir)
    os.makedirs(work_dir)

    # 1. RTL
    core_file, top_file = adapter.rtl_files(work_dir)

    # 2. Wrapper
    wrapper_src = generate_wrapper(adapter.top_module, n, adapter.data_width)
    wrapper_path = os.path.join(work_dir, "fft_pcpi_wrapper.v")
    with open(wrapper_path, "w") as f:
        f.write(wrapper_src)

    # 3. Firmware: pack this track's own 11 test vectors, build+assemble asm
    layout = compute_layout(n, NUM_TESTS, adapter.transfer_mode)
    asm_lines = build_asm_lines(n, NUM_TESTS, adapter.transfer_mode, layout)
    words = assemble(asm_lines)

    firmware = ["00000013"] * layout.mem_words   # NOP filler
    for i, w in enumerate(words):
        firmware[i] = f"{w:08x}"
    sample_words = []
    for vec in adapter.pe.test_vectors:
        for sample in vec:
            sample_words.extend(adapter.pack_sample(sample))
    assert len(sample_words) == NUM_TESTS * n * layout.words_per_sample
    for i, w in enumerate(sample_words):
        firmware[layout.samples_base_word + i] = f"{w & 0xFFFFFFFF:08x}"

    firmware_hex = os.path.join(work_dir, "firmware.hex")
    with open(firmware_hex, "w") as f:
        f.write("\n".join(firmware) + "\n")

    # 4. Compile + simulate (iverilog/vvp ONLY -- see _run's allowlist)
    results_hex = os.path.join(work_dir, "results.hex")
    vvp_path = os.path.join(work_dir, "soc.vvp")
    # `FFT_N` = total store-ops-per-frame = n*words_per_sample + store_pad_ops.
    # For hilo64 (FP32) this works because FFTSTOREHI reuses FFTSTORE's own
    # funct3 (010) -- see encode_insn.py's module docstring -- so
    # tb_fft_soc_perf.v's funct3-only c0_store decode counts both halves
    # (and the padding ops) correctly without any changes to that shared
    # file. dump_results()'s word count is also EXPECTED = FFT_N*NUM_TESTS,
    # so it dumps the padding words too -- the unpack loop below skips them
    # via the same per-frame stride.
    fft_n_define = n * layout.words_per_sample + layout.store_pad_ops
    compile_cmd = [
        "iverilog", "-o", vvp_path, "-g2012",
        f"-DFFT_N={fft_n_define}", f"-DNUM_TESTS={NUM_TESTS}",
        f"-DSAMPLES_BASE_WORD={layout.samples_base_word}",
        f"-DRESULTS_BASE_WORD={layout.results_base_word}",
        f"-DMEM_WORDS={layout.mem_words}",
        f"-DFIRMWARE_HEX=\"{firmware_hex}\"", f"-DRESULTS_HEX=\"{results_hex}\"",
        os.path.join(HERE, "tb_fft_soc_perf.v"),
        os.path.join(HERE, "picorv32_fft_soc.v"),
        wrapper_path,
        os.path.join(HERE, "picorv32.v"),
        core_file, top_file,
    ] + adapter.shared_sources()

    res = _run(compile_cmd, cwd=work_dir)
    if res.returncode != 0:
        raise RuntimeError(f"{track} N={n}: iverilog FAILED\n{res.stderr[-3000:]}")

    sim_res = _run(["vvp", vvp_path], cwd=work_dir, timeout=900)
    stdout = sim_res.stdout

    # 5. Parse PERF lines
    perf = {}
    for line in stdout.splitlines():
        m = re.match(r"^PERF (\S+) (\S+)$", line)
        if m:
            perf[m.group(1)] = m.group(2)
    if not perf:
        raise RuntimeError(f"{track} N={n}: no PERF lines in vvp output "
                            f"(sim may have crashed)\n{stdout[-3000:]}\n{sim_res.stderr[-1000:]}")

    complete = perf.get("complete") == "1"
    frames_done = int(perf["frames_done"])
    frame_cycles_sum = int(perf["frame_cycles_sum"])
    cycles_wait = int(perf["cycles_wait"])
    cycles_total = int(perf["cycles_total"])

    if not complete or frames_done < NUM_TESTS:
        raise RuntimeError(f"{track} N={n}: SIMULATION DID NOT COMPLETE "
                            f"(complete={complete}, frames_done={frames_done}/{NUM_TESTS})")

    e2e_cycles_per_xform = frame_cycles_sum / frames_done
    compute_cycles_per_xform = cycles_wait / frames_done

    # 6. SQNR: decode results.hex, compare to the standalone figure
    with open(results_hex) as f:
        all_words = [int(line.strip(), 16) for line in f if line.strip()]
    wps = layout.words_per_sample
    # Real results are DENSELY packed (store_pad_ops never advances s2/sw --
    # see compute_layout): stride n*wps, same as the unpadded case. The dump
    # may contain MORE words than that (tb_fft_soc_perf.v's own EXPECTED is
    # inflated by store_pad_ops), which is fine -- those trailing words are
    # simply never read here.
    expected_words = NUM_TESTS * n * wps
    if len(all_words) < expected_words:
        raise RuntimeError(f"{track} N={n}: results.hex has {len(all_words)} words, "
                            f"expected at least {expected_words}")

    goldens = adapter.golden()
    total_sqnr, n_signals = 0.0, len(adapter.pe.test_vectors)
    for ti in range(n_signals):
        approx = []
        for si in range(n):
            idx = (ti * n + si) * wps
            approx.append(adapter.unpack_result(all_words[idx:idx + wps]))
        import numpy as np
        approx_arr = np.array(approx, dtype=np.complex128)
        sqnr = adapter.sqnr(goldens[ti], approx_arr)
        total_sqnr += 100.0 if math.isinf(sqnr) else sqnr
    soc_sqnr = total_sqnr / n_signals

    sqnr_drop = adapter.standalone_sqnr - soc_sqnr
    sqnr_ok = sqnr_drop <= SQNR_DROP_TOLERANCE_DB
    cyc_disagreement = abs(compute_cycles_per_xform - adapter.standalone_cycles) / adapter.standalone_cycles
    # Informational only, not a pass/fail gate: cycles_wait only starts
    # counting once the CPU's FFTWAIT instruction begins polling, which is
    # a handful of cycles (fftstart handshake + li + fetch/decode) after the
    # FFT core's own `start` pulse. For small N that gap is comparable to
    # the whole compute time, so the core can finish before FFTWAIT ever
    # starts polling and cycles_wait legitimately under-reports -- it is
    # not a sign of incorrect data (SQNR is the authoritative correctness
    # signal; see sqnr_ok above).
    cyc_ok = cyc_disagreement <= CYCLES_WAIT_TOLERANCE

    status = "OK" if sqnr_ok else "FAILED"
    log(f"{track} N={n}: e2e={e2e_cycles_per_xform:.1f} cyc/xform  "
        f"compute={compute_cycles_per_xform:.1f} (standalone={adapter.standalone_cycles}, "
        f"{'ok' if cyc_ok else 'diverges -- expected at small N, see comment'})  "
        f"SQNR soc={soc_sqnr:.2f}dB standalone={adapter.standalone_sqnr:.2f}dB "
        f"drop={sqnr_drop:+.2f}dB ({'ok' if sqnr_ok else 'DROPPED'})  status={status}")

    row = {
        "track": track, "n": n, "status": status,
        "e2e_cycles_per_xform": round(e2e_cycles_per_xform, 2),
        "compute_cycles_per_xform_soc": round(compute_cycles_per_xform, 2),
        "compute_cycles_standalone": adapter.standalone_cycles,
        "sqnr_soc_db": round(soc_sqnr, 3),
        "sqnr_standalone_db": round(adapter.standalone_sqnr, 3),
        "sqnr_drop_db": round(sqnr_drop, 3),
        "cycles_total": cycles_total,
        "frames_done": frames_done,
    }

    if not keep_work:
        shutil.rmtree(work_dir, ignore_errors=True)

    return row


def _write_csv_row(row):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    fieldnames = list(row.keys())
    exists = os.path.isfile(OUT_CSV)
    existing = []
    if exists:
        with open(OUT_CSV, newline="", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))
        existing = [r for r in existing if not (r["track"] == row["track"] and int(r["n"]) == row["n"])]
    existing.append({k: str(v) for k, v in row.items()})
    existing.sort(key=lambda r: (r["track"], int(r["n"])))
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(existing)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", choices=sorted(ADAPTERS))
    ap.add_argument("--n", type=int)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--keep-work", action="store_true")
    args = ap.parse_args()

    if args.all:
        configs = [(t, n) for t in ("mixed", "fp16", "fp32") for n in ALL_N]
    else:
        if not args.track or not args.n:
            raise SystemExit("pass --track/--n or --all")
        configs = [(args.track, args.n)]

    failures = []
    for track, n in configs:
        try:
            row = run_one(track, n, keep_work=args.keep_work)
            _write_csv_row(row)
            if row["status"] != "OK":
                failures.append((track, n))
        except Exception as e:
            log(f"{track} N={n}: EXCEPTION: {e}")
            failures.append((track, n))

    if failures:
        log(f"FAILED configs: {failures}")
        raise SystemExit(1)
    log("all configs OK")


if __name__ == "__main__":
    main()
