// =============================================================================
// butterfly_wrapper_gated.v
//
// Gated replacement for `butterfly_wrapper` in mixed_precision_wrappers.v.
//
// HOW TO ADOPT IT
//   This file declares a SEPARATE module name (`butterfly_wrapper_gated`) so it
//   can sit in verilog_sources/ without colliding with the existing
//   `butterfly_wrapper` in mixed_precision_wrappers.v - vivado_synthesis.tcl
//   globs every *.v in that directory, and two modules of the same name is a
//   hard synthesis error. Until you change the instantiation it is simply an
//   unused module and costs nothing.
//
//   To switch over, change ONE line in fft_template_generator.py, in the
//   emitted core (currently "butterfly_wrapper shared_bf ("):
//       butterfly_wrapper        shared_bf (  ->  butterfly_wrapper_gated shared_bf (
//   and, if you set PIPELINE_OPERANDS=1, connect .clk(clk) and bump
//   TOTAL_LATENCY from 11 to 12.
//
// WHAT CHANGES
//   Functionally IDENTICAL to the original (same ports, same selected output).
//   The only difference is OPERAND ISOLATION: the inputs of every datapath that
//   the current (mult_prec, add_prec) pair does not select are forced to zero,
//   so the unselected multiplier / adder / converter cone stops toggling.
//
// WHY
//   The original wrapper drives BOTH the FP4 and the FP8 datapath every cycle
//   and muxes the result. Area is therefore constant by construction (correct,
//   and defensible - see the paper narrative), but DYNAMIC POWER is also
//   constant, because every gate in both paths switches on every input change
//   regardless of the chromosome. With this version, dynamic power becomes a
//   genuine, measurable function of the per-stage precision schedule:
//   an FP4 stage leaves the FP8 significand multipliers quiescent, and vice
//   versa. That is the number the NSGA-II power objective should be optimising.
//
// IMPORTANT
//   - Zero is a valid encoding in both formats (E2M1 / E4M3 all-zero = +0),
//     and both fp4_mul and fp8_mul short-circuit on a_zero/b_zero, so gated
//     operands hold the whole downstream cone at a stable 0. No X, no
//     spurious saturation.
//   - Gating is combinational (AND with a replicated enable). It adds one
//     LUT-level of logic per operand bus. Expect a SMALL area increase
//     (tens of LUTs), which is the honest cost of the isolation and should be
//     reported as such.
//   - For a larger power delta, register the gated operands (see the
//     PIPELINE_OPERANDS option below): registered isolation stops the glitch
//     power that a combinational gate still lets through, and it also breaks
//     the mult->add critical path, which is what actually blocks any claim
//     above ~30 MHz on Artix-7.
//
// STATUS: NOT SIMULATED, NOT SYNTHESISED. This is untested RTL written against
//   the module interfaces in verilog_sources/. Before it goes near the paper:
//     1. run your existing testbench and diff X/Y against the original wrapper
//        for all four (mult_prec, add_prec) combinations - it must be
//        bit-identical;
//     2. re-run synthesis and record the area delta;
//     3. re-run power with a real SAIF (vectorless power will still be flat).
// =============================================================================

`timescale 1ns/1ps

module butterfly_wrapper_gated #(
    // 0 = combinational operand gating (bit-identical, zero added latency)
    // 1 = registered operand gating   (adds 1 cycle, much larger power delta,
    //                                  breaks the mult->add critical path)
    parameter PIPELINE_OPERANDS = 0
)(
    input          clk,          // NEW: only used when PIPELINE_OPERANDS = 1
    input  [23:0]  A, B,
    input  [15:0]  W,
    input          mult_prec,    // 1 = FP8 multiply, 0 = FP4 multiply
    input          add_prec,     // 1 = FP8 add/sub,  0 = FP4 add/sub
    output [15:0]  X, Y,
    output         output_is_fp8
);

    // -------------------------------------------------------------------------
    // Unpacked views of the 24-bit unified memory word
    //   [23:8] = FP8 {real, imag}   [7:0] = FP4 {real, imag}
    // -------------------------------------------------------------------------
    wire [15:0] A_fp8_raw = A[23:8];
    wire [7:0]  A_fp4_raw = A[7:0];
    wire [15:0] B_fp8_raw = B[23:8];
    wire [7:0]  B_fp4_raw = B[7:0];

    // -------------------------------------------------------------------------
    // Enables. Exactly one multiplier and one adder path is live per cycle.
    // The two format converters are live only on the two mixed combinations.
    // -------------------------------------------------------------------------
    wire en_mul_fp4  = ~mult_prec;
    wire en_mul_fp8  =  mult_prec;
    wire en_add_fp4  = ~add_prec;
    wire en_add_fp8  =  add_prec;
    wire en_cvt_8to4 =  mult_prec & ~add_prec;   // FP8 product -> FP4 adder
    wire en_cvt_4to8 = ~mult_prec &  add_prec;   // FP4 product -> FP8 adder

    // -------------------------------------------------------------------------
    // Operand isolation on the multiplier inputs.
    // -------------------------------------------------------------------------
    wire [7:0]  B_fp4_g = B_fp4_raw & {8{en_mul_fp4}};
    wire [7:0]  W_fp4_g = W[7:0]    & {8{en_mul_fp4}};
    wire [15:0] B_fp8_g = B_fp8_raw & {16{en_mul_fp8}};
    wire [15:0] W_fp8_g = W          & {16{en_mul_fp8}};

    // FP4 complex multiply: B * W
    wire [7:0] wb_prod_fp4;
    fp4_cmul cmul_fp4 (
        .a(B_fp4_g[7:4]), .b(B_fp4_g[3:0]),
        .c(W_fp4_g[7:4]), .d(W_fp4_g[3:0]),
        .out_real(wb_prod_fp4[7:4]), .out_imag(wb_prod_fp4[3:0])
    );

    // FP8 complex multiply: B * W
    wire [15:0] wb_prod_fp8;
    fp8_cmul cmul_fp8 (
        .a(B_fp8_g[15:8]), .b(B_fp8_g[7:0]),
        .c(W_fp8_g[15:8]), .d(W_fp8_g[7:0]),
        .out_real(wb_prod_fp8[15:8]), .out_imag(wb_prod_fp8[7:0])
    );

    // -------------------------------------------------------------------------
    // Operand isolation on the format converters.
    // -------------------------------------------------------------------------
    wire [15:0] cvt84_in = wb_prod_fp8 & {16{en_cvt_8to4}};
    wire [7:0]  cvt48_in = wb_prod_fp4 & {8{en_cvt_4to8}};

    wire [7:0]  wb_prod_fp8_as_fp4;
    complex_fp8_to_fp4 conv_wb84 (.complex_fp8(cvt84_in), .complex_fp4(wb_prod_fp8_as_fp4));

    wire [15:0] wb_prod_fp4_as_fp8;
    complex_fp4_to_fp8 conv_wb48 (.complex_fp4(cvt48_in), .complex_fp8(wb_prod_fp4_as_fp8));

    // -------------------------------------------------------------------------
    // Adder operand selection, then isolation.
    // (Same mux as the original; the gating is applied after it.)
    // -------------------------------------------------------------------------
    wire [7:0]  add_B_fp4_sel = mult_prec ? wb_prod_fp8_as_fp4 : wb_prod_fp4;
    wire [15:0] add_B_fp8_sel = mult_prec ? wb_prod_fp8        : wb_prod_fp4_as_fp8;

    wire [7:0]  add_A_fp4_c = A_fp4_raw     & {8{en_add_fp4}};
    wire [7:0]  add_B_fp4_c = add_B_fp4_sel & {8{en_add_fp4}};
    wire [15:0] add_A_fp8_c = A_fp8_raw     & {16{en_add_fp8}};
    wire [15:0] add_B_fp8_c = add_B_fp8_sel & {16{en_add_fp8}};

    // -------------------------------------------------------------------------
    // Optional registered isolation stage.
    //   PIPELINE_OPERANDS = 0 -> pure wires, bit-identical to the original.
    //   PIPELINE_OPERANDS = 1 -> +1 cycle of latency; the FSM stage-decode
    //                            pipeline in the generated core must be
    //                            lengthened by one to match (TOTAL_LATENCY).
    // -------------------------------------------------------------------------
    wire [7:0]  add_A_fp4, add_B_fp4;
    wire [15:0] add_A_fp8, add_B_fp8;
    wire        add_prec_q;

    generate
    if (PIPELINE_OPERANDS == 0) begin : G_COMB
        assign add_A_fp4  = add_A_fp4_c;
        assign add_B_fp4  = add_B_fp4_c;
        assign add_A_fp8  = add_A_fp8_c;
        assign add_B_fp8  = add_B_fp8_c;
        assign add_prec_q = add_prec;
    end else begin : G_REG
        reg [7:0]  a4_q, b4_q;
        reg [15:0] a8_q, b8_q;
        reg        ap_q;
        always @(posedge clk) begin
            a4_q <= add_A_fp4_c;
            b4_q <= add_B_fp4_c;
            a8_q <= add_A_fp8_c;
            b8_q <= add_B_fp8_c;
            ap_q <= add_prec;
        end
        assign add_A_fp4  = a4_q;
        assign add_B_fp4  = b4_q;
        assign add_A_fp8  = a8_q;
        assign add_B_fp8  = b8_q;
        assign add_prec_q = ap_q;
    end
    endgenerate

    // -------------------------------------------------------------------------
    // FP4 and FP8 add/sub pairs
    // -------------------------------------------------------------------------
    wire [7:0] X_fp4, Y_fp4;
    fp4_complex_add_sub add_fp4 (.a(add_A_fp4), .b(add_B_fp4), .sub(1'b0), .out(X_fp4));
    fp4_complex_add_sub sub_fp4 (.a(add_A_fp4), .b(add_B_fp4), .sub(1'b1), .out(Y_fp4));

    wire [15:0] X_fp8, Y_fp8;
    fp8_complex_add_sub add_fp8 (.a(add_A_fp8), .b(add_B_fp8), .sub(1'b0), .out(X_fp8));
    fp8_complex_add_sub sub_fp8 (.a(add_A_fp8), .b(add_B_fp8), .sub(1'b1), .out(Y_fp8));

    // -------------------------------------------------------------------------
    // Output selection (unchanged)
    // -------------------------------------------------------------------------
    assign X = add_prec_q ? X_fp8 : {8'h00, X_fp4};
    assign Y = add_prec_q ? Y_fp8 : {8'h00, Y_fp4};
    assign output_is_fp8 = add_prec_q;

endmodule
