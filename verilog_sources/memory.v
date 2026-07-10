// =============================================================================
// Mixed-Precision Concurrent FFT Memory Subsystem
// PERFECT TRUE DUAL-PORT (TDP) MEMORY INFERENCE (1-CYCLE READ)
// USING OPENRAM MACROS (sram_512x24_2rw)
// =============================================================================
`timescale 1ns/1ps

module mixed_dual_bank_memory_concurrent #(
    parameter n          = 1024,
    parameter ADDR_WIDTH = 11
)(
    input  wire                  clk,
    input  wire                  rst,

    input  wire                  bank_pingpong,
    input  wire [ADDR_WIDTH-1:0] stage_mask,

    input  wire [ADDR_WIDTH-1:0] rd_addr_a,
    input  wire [ADDR_WIDTH-1:0] rd_addr_b,
    input  wire                  rd_precision,
    output wire [15:0]           rd_data_a,
    output wire [15:0]           rd_data_b,

    input  wire                  wr_en,
    input  wire [ADDR_WIDTH-1:0] wr_addr_a,
    input  wire [ADDR_WIDTH-1:0] wr_addr_b,
    input  wire [23:0]           wr_data_a,
    input  wire [23:0]           wr_data_b,

    input  wire                  bank_pingpong_wr,
    input  wire [ADDR_WIDTH-1:0] stage_mask_wr
);

    // Safe sizing logic
    localparam MEM_AW = (n <= 2) ? 1 : $clog2(n/2);

    // Fallback Resolution Logic Matrix
    wire is_wr_bank_floating = (bank_pingpong_wr === 1'bz || bank_pingpong_wr === 1'bx);
    wire actual_wr_bank      = is_wr_bank_floating ? bank_pingpong : bank_pingpong_wr;

    wire is_wr_mask_floating = (^stage_mask_wr === 1'bx);
    wire [ADDR_WIDTH-1:0] actual_wr_mask = is_wr_mask_floating ? stage_mask : stage_mask_wr;

    // Sub-bank routing selection
    wire read_sub_sel_a  = |(rd_addr_a & stage_mask);
    wire read_sub_sel_b  = |(rd_addr_b & stage_mask);
    wire write_sub_sel_a = |(wr_addr_a & actual_wr_mask);
    wire write_sub_sel_b = |(wr_addr_b & actual_wr_mask);

    // Address compression formulas
    wire [ADDR_WIDTH-1:0] rd_lower_mask = stage_mask - 1'b1;
    wire [ADDR_WIDTH-1:0] rd_upper_mask = ~rd_lower_mask;
    wire [ADDR_WIDTH-2:0] c_rd_addr_a = (rd_addr_a & rd_lower_mask) | ((rd_addr_a & (rd_upper_mask << 1)) >> 1);
    wire [ADDR_WIDTH-2:0] c_rd_addr_b = (rd_addr_b & rd_lower_mask) | ((rd_addr_b & (rd_upper_mask << 1)) >> 1);

    wire [ADDR_WIDTH-1:0] wr_lower_mask = actual_wr_mask - 1'b1;
    wire [ADDR_WIDTH-1:0] wr_upper_mask = ~wr_lower_mask;
    wire [ADDR_WIDTH-2:0] c_wr_addr_a = (wr_addr_a & wr_lower_mask) | ((wr_addr_a & (wr_upper_mask << 1)) >> 1);
    wire [ADDR_WIDTH-2:0] c_wr_addr_b = (wr_addr_b & wr_lower_mask) | ((wr_addr_b & (wr_upper_mask << 1)) >> 1);

    // =========================================================================
    // ADDRESS & WRITE-ENABLE MUXING
    // =========================================================================
    
    // CRITICAL FIX: Prevent Port B from writing the exact same address as Port A simultaneously.
    // Protects OpenRAM from memory corruption during the external data load phase.
    wire avoid_double_write = (wr_addr_a == wr_addr_b);

    // Bank 0, Sub-Bank 0
    wire [ADDR_WIDTH-2:0] b0_sub0_addr_a = (actual_wr_bank == 1'b1) ? c_wr_addr_a : c_rd_addr_a;
    wire                  b0_sub0_we_a   = wr_en & (actual_wr_bank == 1'b1) & (!write_sub_sel_a);
    wire [ADDR_WIDTH-2:0] b0_sub0_addr_b = (actual_wr_bank == 1'b1) ? c_wr_addr_b : c_rd_addr_b;
    wire                  b0_sub0_we_b   = wr_en & (actual_wr_bank == 1'b1) & (!write_sub_sel_b) & (!avoid_double_write);

    wire [MEM_AW-1:0] safe_b0_sub0_addr_a = (n <= 2) ? 1'b0 : b0_sub0_addr_a[MEM_AW-1:0];
    wire [MEM_AW-1:0] safe_b0_sub0_addr_b = (n <= 2) ? 1'b0 : b0_sub0_addr_b[MEM_AW-1:0];

    // Bank 0, Sub-Bank 1
    wire [ADDR_WIDTH-2:0] b0_sub1_addr_a = (actual_wr_bank == 1'b1) ? c_wr_addr_a : c_rd_addr_a;
    wire                  b0_sub1_we_a   = wr_en & (actual_wr_bank == 1'b1) & (write_sub_sel_a);
    wire [ADDR_WIDTH-2:0] b0_sub1_addr_b = (actual_wr_bank == 1'b1) ? c_wr_addr_b : c_rd_addr_b;
    wire                  b0_sub1_we_b   = wr_en & (actual_wr_bank == 1'b1) & (write_sub_sel_b) & (!avoid_double_write);

    wire [MEM_AW-1:0] safe_b0_sub1_addr_a = (n <= 2) ? 1'b0 : b0_sub1_addr_a[MEM_AW-1:0];
    wire [MEM_AW-1:0] safe_b0_sub1_addr_b = (n <= 2) ? 1'b0 : b0_sub1_addr_b[MEM_AW-1:0];

    // Bank 1, Sub-Bank 0 
    wire [ADDR_WIDTH-2:0] b1_sub0_addr_a = (actual_wr_bank == 1'b0) ? c_wr_addr_a : c_rd_addr_a;
    wire                  b1_sub0_we_a   = wr_en & (actual_wr_bank == 1'b0) & (!write_sub_sel_a);
    wire [ADDR_WIDTH-2:0] b1_sub0_addr_b = (actual_wr_bank == 1'b0) ? c_wr_addr_b : c_rd_addr_b;
    wire                  b1_sub0_we_b   = wr_en & (actual_wr_bank == 1'b0) & (!write_sub_sel_b) & (!avoid_double_write);

    wire [MEM_AW-1:0] safe_b1_sub0_addr_a = (n <= 2) ? 1'b0 : b1_sub0_addr_a[MEM_AW-1:0];
    wire [MEM_AW-1:0] safe_b1_sub0_addr_b = (n <= 2) ? 1'b0 : b1_sub0_addr_b[MEM_AW-1:0];

    // Bank 1, Sub-Bank 1
    wire [ADDR_WIDTH-2:0] b1_sub1_addr_a = (actual_wr_bank == 1'b0) ? c_wr_addr_a : c_rd_addr_a;
    wire                  b1_sub1_we_a   = wr_en & (actual_wr_bank == 1'b0) & (write_sub_sel_a);
    wire [ADDR_WIDTH-2:0] b1_sub1_addr_b = (actual_wr_bank == 1'b0) ? c_wr_addr_b : c_rd_addr_b;
    wire                  b1_sub1_we_b   = wr_en & (actual_wr_bank == 1'b0) & (write_sub_sel_b) & (!avoid_double_write);

    wire [MEM_AW-1:0] safe_b1_sub1_addr_a = (n <= 2) ? 1'b0 : b1_sub1_addr_a[MEM_AW-1:0];
    wire [MEM_AW-1:0] safe_b1_sub1_addr_b = (n <= 2) ? 1'b0 : b1_sub1_addr_b[MEM_AW-1:0];

    // Output reading wires
    wire [23:0] r_b0_sub0_a, r_b0_sub1_a, r_b1_sub0_a, r_b1_sub1_a;
    wire [23:0] r_b0_sub0_b, r_b0_sub1_b, r_b1_sub0_b, r_b1_sub1_b;

    // =========================================================================
    // OPENRAM MACRO INSTANTIATIONS
    // =========================================================================

    // Explicit 9-bit wires for safe zero-padding. 
    wire [8:0] final_b0_sub0_addr_a = safe_b0_sub0_addr_a;
    wire [8:0] final_b0_sub0_addr_b = safe_b0_sub0_addr_b;
    wire [8:0] final_b0_sub1_addr_a = safe_b0_sub1_addr_a;
    wire [8:0] final_b0_sub1_addr_b = safe_b0_sub1_addr_b;
    wire [8:0] final_b1_sub0_addr_a = safe_b1_sub0_addr_a;
    wire [8:0] final_b1_sub0_addr_b = safe_b1_sub0_addr_b;
    wire [8:0] final_b1_sub1_addr_a = safe_b1_sub1_addr_a;
    wire [8:0] final_b1_sub1_addr_b = safe_b1_sub1_addr_b;

    sram_512x24_2rw b0_sub0_ram (
    `ifdef USE_POWER_PINS
        .vdd(1'b1), .gnd(1'b0),
    `endif
        .clk0(clk), .csb0(1'b0), .web0(~b0_sub0_we_a), .addr0(final_b0_sub0_addr_a), .din0(wr_data_a), .dout0(r_b0_sub0_a),
        .clk1(clk), .csb1(1'b0), .web1(~b0_sub0_we_b), .addr1(final_b0_sub0_addr_b), .din1(wr_data_b), .dout1(r_b0_sub0_b)
    );

    sram_512x24_2rw b0_sub1_ram (
    `ifdef USE_POWER_PINS
        .vdd(1'b1), .gnd(1'b0),
    `endif
        .clk0(clk), .csb0(1'b0), .web0(~b0_sub1_we_a), .addr0(final_b0_sub1_addr_a), .din0(wr_data_a), .dout0(r_b0_sub1_a),
        .clk1(clk), .csb1(1'b0), .web1(~b0_sub1_we_b), .addr1(final_b0_sub1_addr_b), .din1(wr_data_b), .dout1(r_b0_sub1_b)
    );

    sram_512x24_2rw b1_sub0_ram (
    `ifdef USE_POWER_PINS
        .vdd(1'b1), .gnd(1'b0),
    `endif
        .clk0(clk), .csb0(1'b0), .web0(~b1_sub0_we_a), .addr0(final_b1_sub0_addr_a), .din0(wr_data_a), .dout0(r_b1_sub0_a),
        .clk1(clk), .csb1(1'b0), .web1(~b1_sub0_we_b), .addr1(final_b1_sub0_addr_b), .din1(wr_data_b), .dout1(r_b1_sub0_b)
    );

    sram_512x24_2rw b1_sub1_ram (
    `ifdef USE_POWER_PINS
        .vdd(1'b1), .gnd(1'b0),
    `endif
        .clk0(clk), .csb0(1'b0), .web0(~b1_sub1_we_a), .addr0(final_b1_sub1_addr_a), .din0(wr_data_a), .dout0(r_b1_sub1_a),
        .clk1(clk), .csb1(1'b0), .web1(~b1_sub1_we_b), .addr1(final_b1_sub1_addr_b), .din1(wr_data_b), .dout1(r_b1_sub1_b)
    );

    // =========================================================================
    // PERFECT 1-CYCLE PIPELINE ALIGNMENT
    // =========================================================================

    reg pipe_bank_pingpong;
    reg pipe_read_sub_sel_a;
    reg pipe_read_sub_sel_b;
    reg rd_prec_d1;

    always @(posedge clk) begin
        pipe_bank_pingpong  <= bank_pingpong;
        pipe_read_sub_sel_a <= read_sub_sel_a;
        pipe_read_sub_sel_b <= read_sub_sel_b;
        rd_prec_d1          <= rd_precision;
    end

    // By leaving this purely combinational, it utilizes the OpenRAM's negedge 
    // update to provide valid data perfectly in sync for the downstream posedge setup.
    wire [23:0] rd_full_a = (pipe_bank_pingpong == 1'b0) ?
                            (pipe_read_sub_sel_a ? r_b0_sub1_a : r_b0_sub0_a) :
                            (pipe_read_sub_sel_a ? r_b1_sub1_a : r_b1_sub0_a);
                            
    wire [23:0] rd_full_b = (pipe_bank_pingpong == 1'b0) ? 
                            (pipe_read_sub_sel_b ? r_b0_sub1_b : r_b0_sub0_b) :
                            (pipe_read_sub_sel_b ? r_b1_sub1_b : r_b1_sub0_b);

    assign rd_data_a = rd_prec_d1 ? rd_full_a[23:8] : {8'h00, rd_full_a[7:0]};
    assign rd_data_b = rd_prec_d1 ? rd_full_b[23:8] : {8'h00, rd_full_b[7:0]};

endmodule
