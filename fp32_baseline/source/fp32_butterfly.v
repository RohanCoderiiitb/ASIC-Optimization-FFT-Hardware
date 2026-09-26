// =============================================================================
// FP32 Radix-2 DIT Butterfly -- internally pipelined, 2-cycle latency
//
// Memory / datapath format: 64-bit complex FP32
//   [63:32] FP32 Real, [31:0] FP32 Imag
//
// Mirrors fp8_butterfly_generation_unit in verilog_sources/butterfly.v
// structurally (same X = A + W*B / Y = A - W*B), but is NOT purely
// combinational: at 45nm generic-cell synthesis, chaining a 24x24 multiply
// straight into two dependent FP32 adds (the complex-multiply combine, then
// the final complex add/sub) measures ~13.4ns end to end -- over a 10ns
// clock budget. The chain is split into three single-primitive pipeline
// stages (multiply-only -> combine-add-only -> final-add-only), each of
// which comfortably meets timing on its own:
//
//   cycle T   : 4 real multiplies (B x W)                     -> register
//   cycle T+1 : complex-multiply combine (ac-bd, ad+bc = W*B)  -> register
//   cycle T+2 : final complex add/sub, X = A + WB, Y = A - WB  (combinational)
//
// A is carried alongside in shift registers so it reaches the final add in
// lock-step with the now-2-cycle-delayed W*B product. Callers must delay
// TOTAL_LATENCY (the write-back address/enable pipeline depth) by these same
// 2 extra cycles -- see TOTAL_LATENCY in fp32_template_generator.py.
// =============================================================================

module fp32_butterfly_generation_unit(
    input         clk,
    input  [63:0] A,
    input  [63:0] B,
    input  [63:0] W,
    output [63:0] X,
    output [63:0] Y
);

    // ---- Stage 0 (combinational): 4 real multiplies, B x W ----
    wire [31:0] ac_w, bd_w, ad_w, bc_w;

    fp32_mul m1 (.a(B[63:32]), .b(W[63:32]), .out(ac_w));  // Br * Wr
    fp32_mul m2 (.a(B[31:0]),  .b(W[31:0]),  .out(bd_w));  // Bi * Wi
    fp32_mul m3 (.a(B[63:32]), .b(W[31:0]),  .out(ad_w));  // Br * Wi
    fp32_mul m4 (.a(B[31:0]),  .b(W[63:32]), .out(bc_w));  // Bi * Wr

    reg [31:0] ac_r, bd_r, ad_r, bc_r;
    reg [63:0] A_stage1;

    always @(posedge clk) begin
        ac_r <= ac_w;
        bd_r <= bd_w;
        ad_r <= ad_w;
        bc_r <= bc_w;
        A_stage1 <= A;
    end

    // ---- Stage 1 (combinational): complex-multiply combine, WB = ac-bd + j(ad+bc) ----
    wire [31:0] wb_real_w, wb_imag_w;

    fp32_add_sub cmul_real (.a(ac_r), .b(bd_r), .sub(1'b1), .out(wb_real_w));
    fp32_add_sub cmul_imag (.a(ad_r), .b(bc_r), .sub(1'b0), .out(wb_imag_w));

    reg [31:0] wb_real_r, wb_imag_r;
    reg [63:0] A_stage2;

    always @(posedge clk) begin
        wb_real_r <= wb_real_w;
        wb_imag_r <= wb_imag_w;
        A_stage2  <= A_stage1;
    end

    wire [63:0] wb_product = {wb_real_r, wb_imag_r};

    // ---- Stage 2 (combinational): final complex add / subtract ----
    fp32_complex_add_sub adder_inst (
        .a   (A_stage2),
        .b   (wb_product),
        .sub (1'b0),
        .out (X)
    );

    fp32_complex_add_sub sub_inst (
        .a   (A_stage2),
        .b   (wb_product),
        .sub (1'b1),
        .out (Y)
    );

endmodule


// -----------------------------------------------------------------------------
// Thin wrapper kept for structural parity with butterfly_wrapper in
// verilog_sources/mixed_precision_wrappers.v, so the FP32 core instantiates
// a like-named block.  No precision plumbing: the baseline is FP32 throughout.
// Passes `clk` through for the internal 2-cycle pipeline above.
// -----------------------------------------------------------------------------
module fp32_butterfly_wrapper (
    input         clk,
    input  [63:0] A, B,
    input  [63:0] W,
    output [63:0] X, Y
);
    fp32_butterfly_generation_unit bf (
        .clk (clk),
        .A (A),
        .B (B),
        .W (W),
        .X (X),
        .Y (Y)
    );
endmodule
