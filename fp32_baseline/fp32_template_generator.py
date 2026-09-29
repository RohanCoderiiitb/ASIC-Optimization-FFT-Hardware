#!/usr/bin/env python3
"""
FP32 baseline FFT core/top generator.

Emits a fully-pipelined (II=1) IEEE-754 binary32 FFT core + top for each FFT
size, structurally matched to the NSGA-optimised mixed-precision cores in
generated_cores/ (repository root) so that FPGA/ASIC results can be compared directly:

  * same streaming AGU (dit_fft_agu_streaming) and bit_reverse front end
  * same dual-bank ping-pong memory organisation and 1-cycle read
  * same IDLE/RUN/FLUSH/DONE control FSM and active-low async reset
  * TOTAL_LATENCY = 15 (11 cycles operand alignment + 4 cycles for the
    internally-pipelined FP32 butterfly -- see fp32_butterfly.v) and
    STALL_CYCLES = TOTAL_LATENCY + 1 inter-stage stall. This is 4 cycles more
    than the mixed-precision core's TOTAL_LATENCY = 11 (unpipelined
    butterfly); cycle counts per transform are correspondingly 4*num_stages
    higher, not identical. The FP16 baseline shares this same
    TOTAL_LATENCY = 15 (also a 4-cycle butterfly) for architectural parity
    across the precision sweep -- see fp16_template_generator.py.
    The butterfly is 4 cycles deep (not 2) specifically to meet 10ns POST-
    ROUTE timing: registering the final complex add/sub's output (rather
    than driving the SRAM write port combinationally from it) closed most
    of the gap, but the final add/sub ALONE still measured over budget
    post-route, so it is itself now internally 2-cycle pipelined (align+add
    | normalize+round) via fp32_complex_add_sub_pipe -- see
    fp32_butterfly.v's header and postroute_pnr.py.

Differences are exactly the ones the baseline is supposed to have:
  * 64-bit complex FP32 memory word instead of the 24-bit unified FP8+FP4 word
  * no per-stage precision localparams, no precision muxes, no FP4<->FP8
    converters in the datapath

Usage:
    python fp32_baseline/fp32_template_generator.py                 # all sizes
    python fp32_baseline/fp32_template_generator.py --sizes 256 1024
    python fp32_baseline/fp32_template_generator.py --outdir some/other/dir

The script lives directly in fp32_baseline/ and, by default, writes to
fp32_baseline/generated_cores/fp32_fft_<N>/ regardless of the working directory.
"""

import argparse
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTDIR = os.path.join(HERE, "generated_cores")

ALL_SIZES = [2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]

MAX_N = 1024
ADDR_WIDTH = 11

# The shared butterfly (fp32_butterfly.v) is internally pipelined 4 cycles
# deep (multiply -> combine-add -> final-add[align+add] -> final-add[normalize
# +round], each registered) to meet a 10ns clock post-route at 45nm
# generic-cell synthesis -- see the header comment in fp32_butterfly.v for
# the timing rationale. The operand (A/B) and twiddle pipelines feed the
# butterfly's INPUTS and are unchanged; only the write-back address/enable
# pipeline, which waits for the butterfly's OUTPUT, needs to grow by these
# same 2 extra cycles (2 -> 4).
BUTTERFLY_LATENCY = 4
TOTAL_LATENCY = 11 + BUTTERFLY_LATENCY
# TWIDDLE_LATENCY is 9, not 10: the twiddle ROM (fp32_twiddle_rom.v) is now
# itself internally 1-cycle pipelined (address-compute -> ROM lookup, added
# to fix a high-fanout combinational case() timing violation post-route), so
# its total k-to-twiddle-output depth is 1 (ROM) + TWIDDLE_LATENCY (this
# pipe's own register count) flops. Shrinking TWIDDLE_LATENCY by the same 1
# cycle the ROM gained keeps that total at 11 flops -- exactly matching the
# A/B operand path's fixed 11-flop depth (1 SRAM sync-read register + 10
# A_64_pipe/B_64_pipe stages, unchanged) so operands and twiddle still arrive
# at the butterfly on the same cycle.
TWIDDLE_LATENCY = 9
# AGU_PIPE_LATENCY: idx_a/idx_b/k (from dit_fft_agu_streaming) are runtime
# multiply/shift results (k = butterfly * (N >> shift_amt), idx_a's
# group_offset = group * group_size) computed combinationally and previously
# fed straight into the SRAM read address / twiddle ROM input in the same
# cycle -- measured on a real post-route netlist (FP16 baseline, identical
# AGU) as a ~40-gate-deep combinational chain from the AGU's registers all
# the way through the twiddle ROM's scaling/symmetry logic, the actual
# dominant post-route timing violation (the twiddle ROM's own lookup was NOT
# the bottleneck -- restructuring it changed nothing). Registered one cycle
# after the AGU (idx_a_r/idx_b_r/k_r/streaming_enable_r below) so that
# multiply/shift is isolated into its own cycle. done_stage/done_fft/
# pipeline_stall_cnt/agu_stall are deliberately NOT delayed -- they gate the
# AGU's OWN internal FSM via its `stall` input, and delaying them would let
# that FSM advance one extra (invalid) step before the stall engages.
# Instead, STALL_CYCLES and flush_counter's target are widened by
# AGU_PIPE_LATENCY so the (unchanged-timing) stall/flush triggers wait the 1
# extra cycle the actual data now needs to reach the write-back pipe.
AGU_PIPE_LATENCY = 1
STALL_CYCLES = TOTAL_LATENCY + AGU_PIPE_LATENCY + 1


def log2i(n: int) -> int:
    return n.bit_length() - 1


def result_bank(n: int) -> int:
    """Bank select that reads the final result: 1 for even log2(N), 0 for odd."""
    return 1 if log2i(n) % 2 == 0 else 0


def gen_core(n: int) -> str:
    stages = log2i(n)
    res_bank = result_bank(n)
    return f"""// =============================================================================
// FP32 Baseline FFT Core - {n}-point FULLY PIPELINED II=1 ARCHITECTURE
//
// IEEE 754 binary32 (E8M23) throughout.  Reference design for benchmarking
// against the NSGA-optimised mixed-precision FP4/FP8 core mixed_fft_{n}_core.
//
// Cycle-for-cycle identical control: same AGU, same TOTAL_LATENCY = {TOTAL_LATENCY},
// same {STALL_CYCLES}-cycle inter-stage pipeline flush, same FSM.
// Active-low asynchronous reset (negedge rst).
//
// Memory word: [63:32] FP32 Real, [63:0] FP32 Imag
// =============================================================================
`timescale 1ns/1ps

module fp32_fft_{n}_core #(
    parameter MAX_N      = {MAX_N},
    parameter ADDR_WIDTH = {ADDR_WIDTH}
)(
    input  wire        clk,
    input  wire        rst,

    input  wire        start,
    output reg         done,

    input  wire                  ext_wr_en,
    input  wire [ADDR_WIDTH-1:0] ext_wr_addr,
    input  wire [63:0]           ext_wr_data,

    input  wire                  ext_reading,
    input  wire [ADDR_WIDTH-1:0] ext_rd_addr,
    output wire [63:0]           ext_rd_data,

    input  wire                  ext_bank_sel
);

    // Single-precision baseline: {stages} stages, all FP32.
    localparam TOTAL_STAGES = {stages};

    // Bank holding the final result.  Load writes bank-select 0 (sub-arrays
    // b1_*) and every stage writes the opposite bank from the one it reads,
    // so the result ends up in b1_* (select 1) after an even number of
    // stages and in b0_* (select 0) after an odd number.  fft_bank_sel is
    // parked on this value after the transform because it also decides
    // which arrays see the READ addresses during unload.
    localparam RESULT_BANK = 1'b{res_bank};

    reg  start_agu_reg;
    wire streaming_enable;
    wire [ADDR_WIDTH-1:0] idx_a, idx_b, k;
    wire done_stage, done_fft;
    wire [ADDR_WIDTH-1:0] curr_stage;

    reg [5:0] pipeline_stall_cnt;
    wire agu_stall = (pipeline_stall_cnt > 0);

    // GHOST STALL FIX: Prevents the AGU from double-triggering after a flush
    reg just_unstalled;
    always @(posedge clk or negedge rst) begin
        if (!rst) just_unstalled <= 1'b0;
        else if (agu_stall) just_unstalled <= 1'b1;
        else just_unstalled <= 1'b0;
    end

    wire safe_done_stage = done_stage && !just_unstalled;
    wire safe_done_fft   = done_fft   && !just_unstalled;

    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            pipeline_stall_cnt <= 0;
        end else if (safe_done_stage && !safe_done_fft) begin
            pipeline_stall_cnt <= {STALL_CYCLES};
        end else if (pipeline_stall_cnt > 0) begin
            pipeline_stall_cnt <= pipeline_stall_cnt - 1;
        end
    end

    dit_fft_agu_streaming #(
        .MAX_N     (MAX_N),
        .ADDR_WIDTH(ADDR_WIDTH)
    ) agu (
        .clk          (clk),
        .reset        (rst),
        .stall        (agu_stall),
        .start        (start_agu_reg),
        .N            (11'd{n}),
        .stream_en    (streaming_enable),
        .idx_a        (idx_a),
        .idx_b        (idx_b),
        .k            (k),
        .done_stage   (done_stage),
        .done_fft     (done_fft),
        .curr_stage   (curr_stage)
    );

    // Register idx_a/idx_b/k/streaming_enable one cycle after the AGU (see
    // AGU_PIPE_LATENCY above) so the AGU's runtime multiply/shift logic
    // (k = butterfly * num_groups, group_offset = group * group_size) lands
    // in its own cycle instead of combining with the twiddle ROM's
    // scaling/symmetry logic and the SRAM read address in the same cycle.
    // done_stage/done_fft/pipeline_stall_cnt/agu_stall above are NOT
    // delayed -- see AGU_PIPE_LATENCY's comment for why.
    reg [ADDR_WIDTH-1:0] idx_a_r, idx_b_r, k_r;
    reg                  streaming_enable_r;

    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            idx_a_r            <= 0;
            idx_b_r            <= 0;
            k_r                <= 0;
            streaming_enable_r <= 1'b0;
        end else begin
            idx_a_r            <= idx_a;
            idx_b_r            <= idx_b;
            k_r                <= k;
            streaming_enable_r <= streaming_enable;
        end
    end

    // -------------------------------------------------------------------------
    // Twiddle ROM (1-cycle internally pipelined) + latency-matching pipeline
    // -------------------------------------------------------------------------
    wire [63:0] twiddle_comb;
    localparam TWIDDLE_LATENCY = {TWIDDLE_LATENCY};

    twiddle_factor_fp32 #(
        .MAX_N     (MAX_N),
        .ADDR_WIDTH(ADDR_WIDTH)
    ) twiddle_gen (
        .clk        (clk),
        .k          (k_r),
        .n          (11'd{n}),
        .twiddle_out(twiddle_comb)
    );

    // streaming_enable must be delayed by the same 1 cycle the ROM now takes
    // internally (address-compute -> lookup), otherwise v_pipe (which tracks
    // validity) falls 1 cycle out of lockstep with twiddle_pipe (which
    // carries the now-1-cycle-later ROM output) as they shift through the
    // pipe below together.
    reg streaming_enable_d;
    always @(posedge clk or negedge rst) begin
        if (!rst) streaming_enable_d <= 1'b0;
        else      streaming_enable_d <= streaming_enable_r;
    end

    (* srl_style = "srl" *) reg [63:0] twiddle_pipe [0:TWIDDLE_LATENCY];
    (* srl_style = "srl" *) reg        v_pipe       [0:TWIDDLE_LATENCY];
    integer t_idx;

    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            for (t_idx = 0; t_idx <= TWIDDLE_LATENCY; t_idx = t_idx + 1) begin
                twiddle_pipe[t_idx] <= 64'd0;
                v_pipe[t_idx]       <= 1'b0;
            end
        end else begin
            twiddle_pipe[0] <= twiddle_comb;
            v_pipe[0]       <= streaming_enable_d;

            for (t_idx = 1; t_idx <= TWIDDLE_LATENCY; t_idx = t_idx + 1) begin
                twiddle_pipe[t_idx] <= twiddle_pipe[t_idx-1];
                v_pipe[t_idx]       <= v_pipe[t_idx-1];
            end
        end
    end

    wire [63:0] twiddle = v_pipe[TWIDDLE_LATENCY] ? twiddle_pipe[TWIDDLE_LATENCY] : 64'h0000000000000000;

    // -------------------------------------------------------------------------
    // Write-back address / enable pipeline
    // -------------------------------------------------------------------------
    localparam TOTAL_LATENCY = {TOTAL_LATENCY};
    localparam AGU_PIPE_LATENCY = {AGU_PIPE_LATENCY};

    (* srl_style = "srl" *) reg [TOTAL_LATENCY-1:0]  wr_en_pipe;
    (* srl_style = "srl" *) reg [ADDR_WIDTH-1:0]     wr_addr_a_pipe [0:TOTAL_LATENCY-1];
    (* srl_style = "srl" *) reg [ADDR_WIDTH-1:0]     wr_addr_b_pipe [0:TOTAL_LATENCY-1];

    reg fft_bank_sel;

    integer i;
    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            wr_en_pipe <= 0;
            for (i = 0; i < TOTAL_LATENCY; i = i + 1) begin
                wr_addr_a_pipe[i] <= 0;
                wr_addr_b_pipe[i] <= 0;
            end
        end else begin
            wr_en_pipe <= {{wr_en_pipe[TOTAL_LATENCY-2:0], streaming_enable_r}};

            wr_addr_a_pipe[0] <= idx_a_r;
            wr_addr_b_pipe[0] <= idx_b_r;

            for (i = 1; i < TOTAL_LATENCY; i = i + 1) begin
                wr_addr_a_pipe[i] <= wr_addr_a_pipe[i-1];
                wr_addr_b_pipe[i] <= wr_addr_b_pipe[i-1];
            end
        end
    end

    wire                  mem_wr_en     = wr_en_pipe[TOTAL_LATENCY-1];
    wire [ADDR_WIDTH-1:0] mem_wr_addr_a = wr_addr_a_pipe[TOTAL_LATENCY-1];
    wire [ADDR_WIDTH-1:0] mem_wr_addr_b = wr_addr_b_pipe[TOTAL_LATENCY-1];

    // Stalls guarantee writes finish before the next stage starts, so the
    // write bank needs no extra delay.
    wire mem_wr_bank = fft_bank_sel;

    // -------------------------------------------------------------------------
    // Memory
    // -------------------------------------------------------------------------
    wire                  active_rd_bank = ext_reading ? ext_bank_sel : fft_bank_sel;
    wire [ADDR_WIDTH-1:0] mem_rd_addr_a  = ext_reading ? ext_rd_addr  : idx_a_r;
    wire [ADDR_WIDTH-1:0] mem_rd_addr_b  = ext_reading ? ext_rd_addr  : idx_b_r;

    wire [63:0] rd_data_a_64, rd_data_b_64;
    wire [63:0] X_wr_64, Y_wr_64;
    assign ext_rd_data = rd_data_a_64;

    fp32_dual_bank_memory_concurrent #(
        .n         ({n}),
        .ADDR_WIDTH(ADDR_WIDTH)
    ) mem (
        .clk          (clk),
        .rst          (rst),

        .bank_pingpong (active_rd_bank),
        .stage_mask    (11'h001),
        .rd_addr_a     (mem_rd_addr_a),
        .rd_addr_b     (mem_rd_addr_b),
        .rd_data_a     (rd_data_a_64),
        .rd_data_b     (rd_data_b_64),

        .wr_en         (ext_wr_en ? 1'b1 : mem_wr_en),
        .wr_addr_a     (ext_wr_en ? ext_wr_addr : mem_wr_addr_a),
        .wr_addr_b     (ext_wr_en ? ext_wr_addr : mem_wr_addr_b),
        .wr_data_a     (ext_wr_en ? ext_wr_data : X_wr_64),
        .wr_data_b     (ext_wr_en ? ext_wr_data : Y_wr_64),

        .bank_pingpong_wr (ext_wr_en ? 1'b0 : mem_wr_bank),
        .stage_mask_wr    (11'h001)
    );

    // -------------------------------------------------------------------------
    // Operand alignment pipeline (memory read latency 1 + 10 = 11 cycles)
    // -------------------------------------------------------------------------
    (* srl_style = "srl" *) reg [63:0] A_64_pipe [0:9];
    (* srl_style = "srl" *) reg [63:0] B_64_pipe [0:9];
    integer j;
    always @(posedge clk) begin
        A_64_pipe[0] <= rd_data_a_64;
        B_64_pipe[0] <= rd_data_b_64;
        for (j = 1; j < 10; j = j + 1) begin
            A_64_pipe[j] <= A_64_pipe[j-1];
            B_64_pipe[j] <= B_64_pipe[j-1];
        end
    end
    wire [63:0] A_64_aligned = A_64_pipe[9];
    wire [63:0] B_64_aligned = B_64_pipe[9];

    // -------------------------------------------------------------------------
    // SINGLE SHARED BUTTERFLY UNIT (FP32)
    // -------------------------------------------------------------------------
    wire [63:0] X_bf, Y_bf;
    fp32_butterfly_wrapper shared_bf (
        .clk (clk),
        .A (A_64_aligned),
        .B (B_64_aligned),
        .W (twiddle),
        .X (X_bf),
        .Y (Y_bf)
    );

    assign X_wr_64 = X_bf;
    assign Y_wr_64 = Y_bf;

    // -------------------------------------------------------------------------
    // Control FSM
    // -------------------------------------------------------------------------
    localparam IDLE_ST  = 2'd0,
               RUN_ST   = 2'd1,
               FLUSH_ST = 2'd2,
               DONE_ST  = 2'd3;

    reg [1:0] state;
    reg [5:0] flush_counter;

    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            state         <= IDLE_ST;
            start_agu_reg <= 1'b0;
            fft_bank_sel  <= 1'b0;
            done          <= 1'b0;
            flush_counter <= 0;
        end else begin
            case (state)
                IDLE_ST: begin
                    done <= 1'b0;
                    if (start) begin
                        fft_bank_sel  <= 1'b1;
                        start_agu_reg <= 1'b1;
                        state         <= RUN_ST;
                    end
                end

                RUN_ST: begin
                    start_agu_reg <= 1'b0;
                    if (pipeline_stall_cnt == 1) begin
                        fft_bank_sel <= ~fft_bank_sel;
                    end
                    if (safe_done_fft) begin
                        state         <= FLUSH_ST;
                        // +AGU_PIPE_LATENCY: safe_done_fft fires on the AGU's
                        // raw (undelayed) state, 1 cycle before this same
                        // transaction's idx_a_r/k_r (and hence its write-back
                        // pipe entry) become valid -- see AGU_PIPE_LATENCY.
                        flush_counter <= TOTAL_LATENCY + AGU_PIPE_LATENCY;
                    end
                end

                FLUSH_ST: begin
                    if (flush_counter == 0) begin
                        fft_bank_sel <= RESULT_BANK;
                        done         <= 1'b1;
                        state        <= DONE_ST;
                    end else begin
                        flush_counter <= flush_counter - 1;
                    end
                end

                DONE_ST: begin
                    if (!start) begin
                        done  <= 1'b0;
                        state <= IDLE_ST;
                    end
                end
                default: state <= IDLE_ST;
            endcase
        end
    end
endmodule
"""


def gen_top(n: int) -> str:
    lg = log2i(n)
    res_bank = result_bank(n)
    aw = max(1, lg)              # width of the user-facing load/unload address
    pad = ADDR_WIDTH - aw
    pad_lit = f"{pad}'b{'0' * pad}"
    return f"""// =============================================================================
// FP32 Baseline FFT TOP - {n}-point PIPELINED CONFIGURATION
//
// Same interface shape as mixed_fft_{n}_top, with 64-bit complex FP32 load /
// unload words in place of the 16-bit mixed-precision words.  No format
// conversion on load: the baseline stores exactly what it is given.
// =============================================================================
`timescale 1ns/1ps

module fp32_fft_{n}_top (
    input  wire        clk,
    input  wire        rst,

    input  wire        start,
    output reg         done,

    input  wire              load_en,
    input  wire [{aw - 1}:0]  load_addr,
    input  wire [63:0]       load_data,

    input  wire              unload_en,
    input  wire [{aw - 1}:0]  unload_addr,
    output wire [63:0]       unload_data
);

    wire [{ADDR_WIDTH - 1}:0] load_addr_rev;

    bit_reverse #(
        .MAX_N({MAX_N}),
        .WIDTH({ADDR_WIDTH})
    ) br (
        .in  ({{{pad_lit}, load_addr}}),
        .N   (11'd{n}),
        .out (load_addr_rev)
    );

    reg bank_sel;
    wire core_done;
    wire [63:0] core_rd_data;

    fp32_fft_{n}_core #(
        .MAX_N     ({MAX_N}),
        .ADDR_WIDTH({ADDR_WIDTH})
    ) core (
        .clk          (clk),
        .rst          (rst),
        .start        (start),
        .done         (core_done),

        .ext_wr_en    (load_en),
        .ext_wr_addr  (load_addr_rev),
        .ext_wr_data  (load_data),

        .ext_reading  (unload_en),
        .ext_rd_addr  ({{{pad_lit}, unload_addr}}),
        .ext_rd_data  (core_rd_data),

        .ext_bank_sel (bank_sel)
    );

    assign unload_data = core_rd_data;

    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            done     <= 1'b0;
            bank_sel <= 1'b1;
        end else begin
            if (load_en && !start)
                bank_sel <= 1'b1;

            if (start) begin
                done     <= 1'b0;
                bank_sel <= 1'b1;
            end else if (core_done) begin
                done     <= 1'b1;
                bank_sel <= 1'b{res_bank};   // bank holding the result (log2 N = {lg})
            end else if (!start && done) begin
                done <= 1'b0;
            end
        end
    end
endmodule
"""


def main():
    ap = argparse.ArgumentParser(description="Generate FP32 baseline FFT cores.")
    ap.add_argument("--sizes", type=int, nargs="*", default=ALL_SIZES,
                    help="FFT sizes to generate (default: all powers of two 2..1024)")
    ap.add_argument("--outdir", type=str, default=DEFAULT_OUTDIR,
                    help="Output directory (default: fp32_baseline/generated_cores)")
    args = ap.parse_args()

    for n in args.sizes:
        if n & (n - 1) or n < 2 or n > MAX_N:
            raise SystemExit(f"{n} is not a supported power of two in [2, {MAX_N}]")

        d = os.path.join(args.outdir, f"fp32_fft_{n}")
        os.makedirs(d, exist_ok=True)

        core_path = os.path.join(d, f"fp32_fft_{n}_core.v")
        top_path = os.path.join(d, f"fp32_fft_{n}_top.v")

        with open(core_path, "w") as f:
            f.write(gen_core(n))
        with open(top_path, "w") as f:
            f.write(gen_top(n))

        print(f"  {core_path}")
        print(f"  {top_path}")

    print(f"\nGenerated {len(args.sizes)} FP32 baseline core/top pairs in {args.outdir}/")


if __name__ == "__main__":
    main()
