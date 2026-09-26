# FP32 Baseline FFT — reviewer-requested comparison point

IEEE 754 **binary32 (E8M23, bias 127)** reference implementation of the same
FFT architecture as the NSGA-optimised mixed-precision FP4/FP8 cores, so the
paper can report area / power / energy / accuracy against a conventional
single-precision design. It is the FP32 sibling of `fp16_baseline/`, and both
use SRAM macros for their memory (rather than behavioural register arrays),
so their PPA numbers are ASIC-realistic, not just RTL-simulation results.

**Nothing in the existing tree was modified.** This directory is entirely
additive, and every module name is prefixed `fp32_` (or suffixed `_fp32`) so
the FP32, FP16 and mixed designs can all sit in one project without
collisions.

---

## What is here

```
fp32_baseline/
├── source/
│   ├── fp32_adder.v              fp32_add_sub, fp32_complex_add_sub
│   ├── fp32_multiplier.v         fp32_mul, fp32_cmul
│   ├── fp32_butterfly.v          fp32_butterfly_generation_unit (2-cycle internally pipelined), fp32_butterfly_wrapper
│   ├── fp32_memory.v             fp32_dual_bank_memory_concurrent (64-bit word, built from 4x sram_512x64_2rw macros)
│   ├── fp32_twiddle_rom.v        twiddle_factor_fp32 (synthesizable case-statement ROM; auto-generated)
│   ├── sram_512x64_2rw.v         OpenRAM-style behavioural model of the SRAM macro (blackboxed at synthesis time)
│   └── twiddles_fp32_1024.txt    512 x 64-bit ROM contents (auto-generated)
├── fp32_SRAM_MACROS/              GDS / LEF / Liberty / spice views for sram_512x64_2rw (from the SRAM compiler)
├── generated_cores/               (generated)
│   └── fp32_fft_<N>/              N = 2,4,8,16,32,64,128,256,512,1024
│       ├── fp32_fft_<N>_core.v
│       └── fp32_fft_<N>_top.v
├── sim/
│   ├── fp32_performance_evaluator.py   Icarus simulation: SQNR + exec-cycle count, per size
│   └── perf/                            (generated) fp32_sqnr_results.txt (kept) + fp32_perf_artifacts.zip (everything else)
├── synth/
│   ├── run_fp32_synthesis.py           Yosys + OpenSTA PPA extraction (Power, Area, CritDelay, Slack, NormLat, Energy/FFT)
│   └── (generated) fp32_ppa_report.txt (kept) + fp32_synth_artifacts.zip (everything else)
├── fp32_template_generator.py     regenerates all cores/tops
├── generate_fp32_twiddles.py      regenerates the ROM contents (both the .txt table and the synthesizable .v ROM)
├── run_fp32_design.py             runs all four steps above, in order, for a shared --sizes list
└── README.md
```

`fp32_baseline/` is expected to sit at the repository root, next to the
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
python3 run_fp32_design.py                      # all 10 sizes, all 4 steps
python3 run_fp32_design.py --sizes 16 1024       # a subset
python3 run_fp32_design.py --clock-period 8.0    # different target clock
```

This runs, in order: `fp32_template_generator.py` → `generate_fp32_twiddles.py`
→ `sim/fp32_performance_evaluator.py` → `synth/run_fp32_synthesis.py`, and
stops at the first failing step. Each step also has its own CLI and can be
run standalone (see "How each step works" below).

Requires on `$PATH`: `iverilog`/`vvp` (simulation), `yosys` (synthesis),
`sta` (OpenSTA, timing/power). Needs `numpy` (the system `python3`, not this
repo's `venv/`, has it in this environment).

---

## Architecture

Structurally matched to `mixed_fft_<N>_core` at the repository root, and to
the FP16 baseline:

| | Mixed FP4/FP8 | FP16 baseline | FP32 baseline |
|---|---|---|---|
| Address generation | `dit_fft_agu_streaming` | same instance | same instance |
| Input reordering | `bit_reverse` | same instance | same instance |
| Memory | dual-bank ping-pong, 2 sub-banks, TDP, 1-cycle read, built from SRAM macros | identical | identical |
| Memory word | 24-bit unified | 32-bit | 64-bit |
| Datapath alignment | `TOTAL_LATENCY = 11` | **13** | **13** |
| Inter-stage flush | 12-cycle stall | **14-cycle** | **14-cycle** |
| Control FSM | IDLE / RUN / FLUSH / DONE, async active-low reset | identical | identical |
| Butterfly | one shared unit, II = 1 | one shared unit, II = 1, **internally 2-cycle pipelined** | one shared unit, II = 1, **internally 2-cycle pipelined** |

The control path (AGU, twiddle pipeline, write-back pipeline, FSM) is
generated from the same template as the FP16 baseline — only the datapath
widths, module names, arithmetic units and the result-bank constant differ.

### Why `TOTAL_LATENCY = 13`, not 11 (the butterfly is internally pipelined)

The mixed-precision reference and an early version of this baseline compute
the whole butterfly (`X = A + W*B`, `Y = A - W*B`) as one purely
combinational block: 4 multiplies → 2 adds (the complex-multiply combine)
→ 2 more adds (the final combine). At 45nm generic-cell synthesis, chaining a
24x24 multiply straight into two dependent FP32 adds measures **~13.4ns**
end to end — over a 10ns clock budget, and NOT a false path: this is genuine
register-to-register combinational delay through the arithmetic (confirmed by
excluding an unrelated `rst`-driven false path — see "Synthesis methodology"
below — which had been inflating the apparent critical path to 15.4ns).

The fix (`fp32_butterfly.v`) splits that one combinational block into three
single-primitive pipeline stages, each of which meets timing on its own:

```
cycle T   : 4 real multiplies (B x W)                     -> register
cycle T+1 : complex-multiply combine (ac-bd, ad+bc = W*B)  -> register
cycle T+2 : final complex add/sub, X = A + WB, Y = A - WB  (combinational)
```

`A` is carried alongside in shift registers so it reaches the final add in
lock-step with the now-2-cycle-delayed `W*B` product. This adds 2 cycles of
latency to the shared butterfly, which is why `TOTAL_LATENCY` (the write-back
address/enable pipeline depth, which only needs to wait for the butterfly's
*output*) grows from 11 to 13, and `STALL_CYCLES` (`TOTAL_LATENCY + 1`) from
12 to 14. The **operand (A/B) and twiddle pipelines feed the butterfly's
INPUTS and are unchanged** at depth 11 — only the pipe waiting on the output
needed to grow. Cycle counts per transform are therefore `2 * num_stages`
higher than the un-pipelined mixed-precision baseline's, not identical — a
genuine, expected trade-off for meeting timing, not a bug.

The FP16 baseline keeps the **same** `TOTAL_LATENCY = 13` / 2-cycle butterfly
pipeline depth even though FP16's smaller/faster arithmetic would likely meet
timing with fewer (or zero) pipeline stages — this is a deliberate choice for
architectural parity across the precision sweep, not a re-derived minimum.

### SRAM-macro memory

`fp32_dual_bank_memory_concurrent` (in `fp32_memory.v`) is built from 4
instances of the `sram_512x64_2rw` OpenRAM-style macro (9-bit address, 64-bit
word) — the same 2-read/2-write-port, negedge-timed integration pattern used
by the mixed-precision design's `mixed_dual_bank_memory_concurrent` in
`verilog_sources/memory.v`, just with the full 64-bit word (no `rd_precision`
output mux is needed since there's only one precision). Every FFT size
instantiates the same 4 full 512-deep macros regardless of `N` — a size-2
transform only ever uses address 0 of each — which is why area and power are
nearly flat across the whole size range (see "Measured results" below): the
macros, not the size-dependent control logic, dominate both.

### Synthesizable twiddle ROM

`fp32_twiddle_rom.v` is a combinational, registered-case-statement ROM
(auto-generated by `generate_fp32_twiddles.py` from the same computed
entries as `twiddles_fp32_1024.txt`), not a `$readmemb`-based memory array.
A `$readmemb` ROM depends on a *relative* file path being present next to
whatever tool's working directory it happens to run from — fine for Icarus
simulation, but fragile across Yosys/OpenSTA invocations from different
working directories. The case-statement form has no external file dependency
at synthesis time at all, and the `.txt` table and the `.v` ROM are always
regenerated together from the same source so they can't drift apart.

### Numeric conventions

- Round-to-Nearest-Even on both add and multiply.
- **Overflow saturates** to the largest finite normal (±3.4028235e38,
  `0x7F7FFFFF`). No Inf/NaN encodings are produced.
- Subnormal **inputs** are handled correctly in both units; subnormal
  **results** are produced by the adder and correctly carried up to the
  smallest normal (or flushed) by the multiplier's `exp_norm == 0` boundary
  case.

---

## How each step works

**1. `fp32_template_generator.py`** — emits `fp32_fft_<N>_core.v` /
`_top.v` for each requested size from an f-string template (same template,
different `N`/`RESULT_BANK`/latency constants substituted in).

```
python3 fp32_template_generator.py                 # all 10 sizes
python3 fp32_template_generator.py --sizes 256 1024
```

**2. `generate_fp32_twiddles.py`** — computes `W_1024^k` for `k = 0..511` in
float64, rounds to FP32 (round-to-nearest-even), and writes both
`source/twiddles_fp32_1024.txt` (binary table, useful for reference/other
tooling) and `source/fp32_twiddle_rom.v` (the synthesizable RTL, from the
same computed words).

```
python3 generate_fp32_twiddles.py
```

**3. `sim/fp32_performance_evaluator.py`** — for each size, generates a
testbench, compiles with `iverilog` (`source/*.v` + `../verilog_sources/agu.v`
+ `bit_reversal.v` + the generated core/top), runs 11 test signals (impulse,
tones, chirp, radar-style pulses, etc.) through the real RTL, and reports
per-signal and average SQNR against `numpy.fft.fft` of the FP32-quantised
input, plus the exec-cycle count (load → `start` → `done`).

```
python3 sim/fp32_performance_evaluator.py                # all sizes
python3 sim/fp32_performance_evaluator.py --sizes 256 1024
```

Output: `sim/perf/fp32_sqnr_results.txt` (kept as plain text — this is also
where `synth/run_fp32_synthesis.py` reads `ExecCycles` from for Energy/FFT).
Everything else the run produces (compiled `.vvp`, generated testbenches, raw
unload dumps) is zipped into `sim/perf/fp32_perf_artifacts.zip`.

**4. `synth/run_fp32_synthesis.py`** — for each size: **Yosys** blackboxes
`sram_512x64_2rw`, elaborates + flattens `fp32_fft_<N>_top`, maps to the
45nm standard-cell liberty with `abc`, and reports chip area (`stat`). Then
**OpenSTA** reads the standard-cell + SRAM-macro liberty and the synthesized
netlist, creates a clock at `--clock-period` (default 10ns), sets I/O delay
to period/4, and reports the worst-case timing path and power (0.2 input
activity) — matching the mixed-precision evaluator's methodology exactly
(`objectiveEvaluationFFT.py`'s `_run_yosys_opensta`). **There is no OpenROAD
floorplan/place/route step**: like the mixed-precision evaluator, this is a
pre-layout PPA extraction, not a backend/P&R flow.

```
python3 synth/run_fp32_synthesis.py                # all 10 sizes
python3 synth/run_fp32_synthesis.py --sizes 256 1024
python3 synth/run_fp32_synthesis.py --clock-period 8.0
```

Output: `synth/fp32_ppa_report.txt` (kept as plain text), everything else
(per-size Yosys/OpenSTA scripts, logs, synthesized netlists) zipped into
`synth/fp32_synth_artifacts.zip`.

Metrics reported, one row per size:

| Metric | Definition |
|---|---|
| Power (mW) | OpenSTA `report_power`, 0.2 input activity |
| Area (µm²) | Yosys `stat`, includes the 4 SRAM macros |
| CritDelay (ns) | Worst `report_checks -path_delay max` arrival time |
| Slack (ns) | `clock_period - CritDelay` (positive = timing met) |
| NormLat | `(CritDelay / clock_period) * max(1, stages/6)`, capped at 10 — identical formula to the mixed-precision evaluator's `_compute_actual_normalized_latency` |
| ExecCycles | From step 3's output (compute-only, start-to-done) |
| Energy/FFT (nJ) | `Power(mW) * ExecCycles * ClockPeriod(ns) / 1000` |

**Energy/FFT uses compute-only cycles** (`avg_exec_cycles`, i.e. the
"start-to-done" latency), not load+compute+unload, so it's the same cycle
metric the mixed-precision evaluator already treats as canonical — not a
second, incompatible convention. (Throughput / Throughput-per-Area /
Throughput-per-Watt are deliberately not reported yet; whether to define
"a transform" as compute-only or load+compute+unload for this specific
non-overlapped batch architecture is still an open call — see conversation
history if picking this back up.)

### Synthesis methodology note: `rst` is excluded from timing

The OpenSTA script constrains `rst` with `set_false_path -from [get_ports rst]`.
`rst` is an asynchronous, once-per-run control input, not a per-cycle data
signal — but Yosys's `async2sync` pass (run before `abc`) rewrites each
async-reset flip-flop as a synchronous-equivalent FF with a reset mux on its
D pin, so in the synthesized netlist `rst` legitimately drives a same-cycle
combinational cone through the AGU/control logic. Left unconstrained,
`report_checks` picks that reset fan-out as the worst path (~15.4ns for
`fp32_fft_1024`) — longer than, and unrelated to, the real datapath timing
this report exists to measure (~13.4ns before the butterfly pipelining fix,
~7.98ns after). Excluding it is what surfaces the real number.

---

## Measured results (10ns clock, all 10 sizes passing)

Full tables: `sim/perf/fp32_sqnr_results.txt`, `synth/fp32_ppa_report.txt`.
Representative rows:

| N | ExecCycles | Avg SQNR (dB) | Power (mW) | Area (µm²) | CritDelay (ns) | Slack (ns) | Energy/FFT (nJ) |
|---|---|---|---|---|---|---|---|
| 2 | 18 | 100.00 (all exact) | 14.00 | 453,842 | 7.98 | +2.02 | 2.520 |
| 16 | 91 | 152.72 | 14.10 | 486,082 | 7.98 | +2.02 | 12.831 |
| 256 | 1139 | 145.45 | 14.20 | 492,306 | 7.98 | +2.02 | 161.738 |
| 1024 | 5263 | 143.83 | 14.20 | 494,512 | 7.98 | +2.02 | 747.346 |

Notes on why the numbers look the way they do (see "Architecture" above for
the full explanation):

- **CritDelay/Slack are identical for every N** (7.98ns / +2.02ns): the
  critical path runs through the same shared butterfly + memory-write logic
  regardless of transform size — `N` is only ever a runtime constant fed into
  address comparators, never a change in hardware structure or bit width.
- **Area and power are nearly flat** (~454k→495k µm², ~14.0→14.2mW): 4 fixed
  512-deep SRAM macros (~432k µm² combined) dominate area at ~87-95% of the
  total, and dominate the static-activity power estimate even more (~96% in
  one size's breakdown), for every N.
- **Energy/FFT is where size-dependence actually shows up**: it scales
  ~linearly with ExecCycles (since power is nearly flat), spanning ~2.5nJ at
  N=2 to ~747nJ at N=1024 — a ~300x range that reflects the real cost of a
  bigger transform in this time-multiplexed (one shared butterfly, reused
  over many cycles), not spatially-parallel, architecture.
- **SQNR sits in the ~140-155dB range** — the FP32 noise floor, comfortably
  above the FP16 baseline's ~65-120dB and the mixed-precision cores' typical
  operating range, as expected for a 23-bit mantissa.
