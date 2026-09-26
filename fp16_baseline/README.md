# FP16 Baseline FFT — reviewer-requested comparison point

IEEE 754 **binary16 (E5M10, bias 15)** reference implementation of the same
FFT architecture as the NSGA-optimised mixed-precision FP4/FP8 cores and the
FP32 baseline, so the paper can report area / power / energy / accuracy
against a conventional half-precision design. It is the FP16 sibling of
`fp32_baseline/`, set up identically — same directory layout, same
generator/evaluator/synthesis scripts, same architecture template — just
retargeted to binary16 arithmetic and the smaller `sram_512x32_2rw` SRAM
macro.

**Nothing in the existing tree was modified.** This directory is entirely
additive, and every module name is prefixed `fp16_` (or suffixed `_fp16`) so
the FP32, FP16 and mixed designs can all sit in one project without
collisions.

---

## What is here

```
fp16_baseline/
├── source/
│   ├── fp16_adder.v              fp16_add_sub, fp16_complex_add_sub
│   ├── fp16_multiplier.v         fp16_mul, fp16_cmul
│   ├── fp16_butterfly.v          fp16_butterfly_generation_unit (2-cycle internally pipelined), fp16_butterfly_wrapper
│   ├── fp16_memory.v             fp16_dual_bank_memory_concurrent (32-bit word, built from 4x sram_512x32_2rw macros)
│   ├── fp16_twiddle_rom.v        twiddle_factor_fp16 (synthesizable case-statement ROM; auto-generated)
│   ├── sram_512x32_2rw.v         OpenRAM-style behavioural model of the SRAM macro (blackboxed at synthesis time)
│   └── twiddles_fp16_1024.txt    512 x 32-bit ROM contents (auto-generated)
├── fp16_SRAM_MACROS/              GDS / LEF / Liberty / spice views for sram_512x32_2rw (from the SRAM compiler)
├── generated_cores/               (generated)
│   └── fp16_fft_<N>/              N = 2,4,8,16,32,64,128,256,512,1024
│       ├── fp16_fft_<N>_core.v
│       └── fp16_fft_<N>_top.v
├── sim/
│   ├── fp16_performance_evaluator.py   Icarus simulation: SQNR + exec-cycle count, per size
│   └── perf/                            (generated) fp16_sqnr_results.txt (kept) + fp16_perf_artifacts.zip (everything else)
├── synth/
│   ├── run_fp16_synthesis.py           Yosys + OpenSTA PPA extraction (Power, Area, CritDelay, Slack, NormLat, Energy/FFT)
│   └── (generated) fp16_ppa_report.txt (kept) + fp16_synth_artifacts.zip (everything else)
├── fp16_template_generator.py     regenerates all cores/tops
├── generate_fp16_twiddles.py      regenerates the ROM contents (both the .txt table and the synthesizable .v ROM)
├── run_fp16_design.py             runs all four steps above, in order, for a shared --sizes list
└── README.md
```

`fp16_baseline/` is expected to sit at the repository root, next to the
existing `verilog_sources/` and `45_nm_PDK/`.

**Path conventions.** Every script resolves its own default paths relative to
its own file location, so all of them behave the same regardless of the
working directory they're invoked from.

**Reused unchanged from `../verilog_sources/`:** `agu.v`
(`dit_fft_agu_streaming`) and `bit_reversal.v` (`bit_reverse`). Both are
format-agnostic, so they are instantiated directly rather than duplicated —
duplicating them would create conflicting module definitions when the designs
are compiled together.

---

## One-command run

```
python3 run_fp16_design.py                      # all 10 sizes, all 4 steps
python3 run_fp16_design.py --sizes 16 1024       # a subset
python3 run_fp16_design.py --clock-period 8.0    # different target clock
```

This runs, in order: `fp16_template_generator.py` → `generate_fp16_twiddles.py`
→ `sim/fp16_performance_evaluator.py` → `synth/run_fp16_synthesis.py`, and
stops at the first failing step. Each step also has its own CLI and can be
run standalone (see "How each step works" below).

Requires on `$PATH`: `iverilog`/`vvp` (simulation), `yosys` (synthesis),
`sta` (OpenSTA, timing/power). Needs `numpy` (the system `python3`, not this
repo's `venv/`, has it in this environment).

---

## Architecture

Structurally matched to `mixed_fft_<N>_core` at the repository root, and to
the FP32 baseline — see `fp32_baseline/README.md`'s architecture table for
the full three-way comparison. In short:

| | Mixed FP4/FP8 | FP16 baseline |
|---|---|---|
| Address generation / input reordering / memory organisation | `dit_fft_agu_streaming`, `bit_reverse`, dual-bank ping-pong TDP | same instances / organisation |
| Memory word | 24-bit unified | 32-bit |
| Datapath alignment | `TOTAL_LATENCY = 11` | **13** |
| Inter-stage flush | 12-cycle stall | **14-cycle** |
| Butterfly | one shared unit, II = 1 | one shared unit, II = 1, **internally 2-cycle pipelined** |

### Why `TOTAL_LATENCY = 13`, not 11 — same pipeline depth as FP32, by choice

The FP32 baseline's butterfly needed splitting into a 3-stage internal
pipeline (multiply → combine-add → final-add, one arithmetic primitive per
stage) because its unpipelined critical path measured ~13.4ns at 45nm
generic-cell synthesis — over a 10ns budget (see `fp32_baseline/README.md`
for the full investigation). FP16's multiply/add primitives are individually
much smaller (11x11 significand multiply vs FP32's 24x24) and almost
certainly would have met 10ns with fewer pipeline stages, or none at all.

**This baseline deliberately keeps the identical `TOTAL_LATENCY = 13` / 2-cycle
butterfly pipeline anyway**, rather than re-deriving a shallower one from
FP16's smaller arithmetic — for architectural parity across the precision
sweep, so that FP32 vs FP16 vs mixed-precision comparisons aren't confounded
by different pipeline depths on top of the precision difference itself. One
consequence: cycle counts per transform are **identical** to the FP32
baseline's, size for size (18 / 91 / 1139 / 5263 for N = 2 / 16 / 256 / 1024)
— only the arithmetic width differs.

`fp16_butterfly.v`'s internal structure (registers exactly mirror
`fp32_butterfly.v`'s):

```
cycle T   : 4 real multiplies (B x W)                     -> register
cycle T+1 : complex-multiply combine (ac-bd, ad+bc = W*B)  -> register
cycle T+2 : final complex add/sub, X = A + WB, Y = A - WB  (combinational)
```

### SRAM-macro memory

`fp16_dual_bank_memory_concurrent` (in `fp16_memory.v`) is built from 4
instances of the `sram_512x32_2rw` OpenRAM-style macro (9-bit address, 32-bit
word) — the same integration pattern as the FP32 baseline's
`fp32_dual_bank_memory_concurrent`, just with the narrower word (no
`rd_precision` output mux is needed since there's only one precision). Every
FFT size instantiates the same 4 full 512-deep macros regardless of `N`,
which is why area and power are nearly flat across the whole size range (see
"Measured results" below): the macros dominate both, not the size-dependent
control logic. Each `sram_512x32_2rw` macro is ~60,180 µm² (239.105 x
251.745), about 56% of the FP32 macro's ~107,977 µm² footprint.

### Synthesizable twiddle ROM

`fp16_twiddle_rom.v` is a combinational, registered-case-statement ROM
(auto-generated by `generate_fp16_twiddles.py` from the same computed
entries as `twiddles_fp16_1024.txt`), not a `$readmemb`-based memory array —
see the FP32 README's "Synthesizable twiddle ROM" section for why. Python's
`struct` module natively supports binary16 packing (`'>e'` format), but
raises `OverflowError` instead of silently producing Inf for out-of-range
values (unlike `'>f'` for FP32), so `generate_fp16_twiddles.py` pre-clamps to
±65504 before packing rather than post-checking for an Inf bit pattern.

### Numeric conventions

- Round-to-Nearest-Even on both add and multiply.
- **Overflow saturates** to the largest finite normal (±65504, `0x7BFF`). No
  Inf/NaN encodings are produced.
- Subnormal **inputs** are handled correctly in both units; subnormal
  **results** are produced by the adder and correctly carried up to the
  smallest normal (or flushed) by the multiplier's `exp_norm == 0` boundary
  case — the same algorithm as `fp32_mul`, scaled to binary16 widths (this
  is a fresh implementation, not descended from any historical FP16
  multiplier in this repository, so it does not carry forward any prior
  known rounding gaps).

---

## How each step works

Identical in shape to the FP32 baseline's four steps (see
`fp32_baseline/README.md` for the full explanation of each); only the sizes
of things differ:

**1. `fp16_template_generator.py`** — emits `fp16_fft_<N>_core.v` / `_top.v`
per size.
```
python3 fp16_template_generator.py                 # all 10 sizes
python3 fp16_template_generator.py --sizes 256 1024
```

**2. `generate_fp16_twiddles.py`** — computes `W_1024^k` in float64, rounds
to FP16, writes `source/twiddles_fp16_1024.txt` and `source/fp16_twiddle_rom.v`.
```
python3 generate_fp16_twiddles.py
```

**3. `sim/fp16_performance_evaluator.py`** — same 11 test signals, same
SQNR/exec-cycle methodology as the FP32 evaluator, quantising to FP16 instead.
```
python3 sim/fp16_performance_evaluator.py                # all sizes
python3 sim/fp16_performance_evaluator.py --sizes 256 1024
```
Output: `sim/perf/fp16_sqnr_results.txt` (kept) + `sim/perf/fp16_perf_artifacts.zip`.

**4. `synth/run_fp16_synthesis.py`** — Yosys (blackbox `sram_512x32_2rw`,
`abc`-map to the 45nm liberty) + OpenSTA (timing + power, `rst` excluded via
`set_false_path` — see the FP32 README's "Synthesis methodology" note, which
applies identically here). No OpenROAD P&R.
```
python3 synth/run_fp16_synthesis.py                # all 10 sizes
python3 synth/run_fp16_synthesis.py --sizes 256 1024
python3 synth/run_fp16_synthesis.py --clock-period 8.0
```
Output: `synth/fp16_ppa_report.txt` (kept) + `synth/fp16_synth_artifacts.zip`.
Same metrics table as the FP32 baseline (Power, Area, CritDelay, Slack,
NormLat, ExecCycles, Energy/FFT — see `fp32_baseline/README.md` for each
metric's definition; they're computed identically here).

---

## Measured results (10ns clock, all 10 sizes passing)

Full tables: `sim/perf/fp16_sqnr_results.txt`, `synth/fp16_ppa_report.txt`.
Representative rows:

| N | ExecCycles | Avg SQNR (dB) | Power (mW) | Area (µm²) | CritDelay (ns) | Slack (ns) | Energy/FFT (nJ) |
|---|---|---|---|---|---|---|---|
| 2 | 18 | 112.71 (8/11 exact) | 7.43 | 253,086 | 7.89 | +2.11 | 1.337 |
| 16 | 91 | 79.87 | 7.48 | 263,190 | 7.89 | +2.11 | 6.807 |
| 256 | 1139 | 71.39 | 7.53 | 265,610 | 7.89 | +2.11 | 85.767 |
| 1024 | 5263 | 69.78 | 7.53 | 266,175 | 7.89 | +2.11 | 396.304 |

Compared to the FP32 baseline at the same sizes:

- **ExecCycles are identical** (18 / 91 / 1139 / 5263) — the deliberate
  same-pipeline-depth choice above means the two baselines differ only in
  arithmetic width, not in cycle count, so PPA/SQNR comparisons at a given N
  are apples-to-apples on timing.
- **Area is ~46-56% of FP32's** (253k-266k µm² vs 454k-495k µm²), because the
  `sram_512x32_2rw` macro is ~56% the footprint of `sram_512x64_2rw`, and the
  macros dominate total area in both baselines.
- **Power is ~53% of FP32's** (7.43-7.53mW vs 14.0-14.2mW), for the same
  macro-dominated reason plus smaller arithmetic.
- **CritDelay/Slack are slightly better than FP32's** (7.89ns/+2.11ns vs
  7.98ns/+2.02ns) even at the *same* 2-cycle pipeline depth — FP16's smaller
  per-stage arithmetic (11x11 vs 24x24 multiply) is faster even split into
  the same number of stages.
- **Energy/FFT is correspondingly lower** (~1.3nJ at N=2 to ~396nJ at
  N=1024, vs FP32's ~2.5nJ to ~747nJ) — roughly half, tracking the power
  ratio since ExecCycles are identical.
- **SQNR is ~65-75dB lower than FP32's** (69.78dB vs 143.83dB at N=1024) —
  expected from a 10-bit vs 23-bit mantissa (~13 bits ≈ ~78dB of noise-floor
  difference, matching what's measured), and comfortably still usable for
  many applications despite being far short of FP32's precision.
