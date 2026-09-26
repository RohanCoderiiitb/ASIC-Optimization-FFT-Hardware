// =============================================================================
// FP32 Baseline FFT Core - 128-point FULLY PIPELINED II=1 ARCHITECTURE
//
// IEEE 754 binary32 (E8M23) throughout.  Reference design for benchmarking
// against the NSGA-optimised mixed-precision FP4/FP8 core mixed_fft_128_core.
//
// Cycle-for-cycle identical control: same AGU, same TOTAL_LATENCY = 13,
// same 14-cycle inter-stage pipeline flush, same FSM.
// Active-low asynchronous reset (negedge rst).
//
// Memory word: [63:32] FP32 Real, [63:0] FP32 Imag
// =============================================================================
`timescale 1ns/1ps

module fp32_fft_128_core #(
    parameter MAX_N      = 1024,
    parameter ADDR_WIDTH = 11
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

    // Single-precision baseline: 7 stages, all FP32.
    localparam TOTAL_STAGES = 7;

    // Bank holding the final result.  Load writes bank-select 0 (sub-arrays
    // b1_*) and every stage writes the opposite bank from the one it reads,
    // so the result ends up in b1_* (select 1) after an even number of
    // stages and in b0_* (select 0) after an odd number.  fft_bank_sel is
    // parked on this value after the transform because it also decides
    // which arrays see the READ addresses during unload.
    localparam RESULT_BANK = 1'b0;

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
            pipeline_stall_cnt <= 14;
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
        .N            (11'd128),
        .stream_en    (streaming_enable),
        .idx_a        (idx_a),
        .idx_b        (idx_b),
        .k            (k),
        .done_stage   (done_stage),
        .done_fft     (done_fft),
        .curr_stage   (curr_stage)
    );

    // -------------------------------------------------------------------------
    // Twiddle ROM (combinational) + latency-matching pipeline
    // -------------------------------------------------------------------------
    wire [63:0] twiddle_comb;
    localparam TWIDDLE_LATENCY = 10;

    twiddle_factor_fp32 #(
        .MAX_N     (MAX_N),
        .ADDR_WIDTH(ADDR_WIDTH)
    ) twiddle_gen (
        .k          (k),
        .n          (11'd128),
        .twiddle_out(twiddle_comb)
    );

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
            v_pipe[0]       <= streaming_enable;

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
    localparam TOTAL_LATENCY = 13;

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
            wr_en_pipe <= {wr_en_pipe[TOTAL_LATENCY-2:0], streaming_enable};

            wr_addr_a_pipe[0] <= idx_a;
            wr_addr_b_pipe[0] <= idx_b;

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
    wire [ADDR_WIDTH-1:0] mem_rd_addr_a  = ext_reading ? ext_rd_addr  : idx_a;
    wire [ADDR_WIDTH-1:0] mem_rd_addr_b  = ext_reading ? ext_rd_addr  : idx_b;

    wire [63:0] rd_data_a_64, rd_data_b_64;
    wire [63:0] X_wr_64, Y_wr_64;
    assign ext_rd_data = rd_data_a_64;

    fp32_dual_bank_memory_concurrent #(
        .n         (128),
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
                        flush_counter <= TOTAL_LATENCY;
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
