// =============================================================================
// FP32 Radix-2 DIT Butterfly -- internally pipelined, 4-cycle latency
//
// Memory / datapath format: 64-bit complex FP32
//   [63:32] FP32 Real, [31:0] FP32 Imag
//
// Mirrors fp8_butterfly_generation_unit in verilog_sources/butterfly.v
// structurally (same X = A + W*B / Y = A - W*B), but is NOT purely
// combinational: at 45nm generic-cell synthesis, chaining a 24x24 multiply
// straight into two dependent FP32 adds (the complex-multiply combine, then
// the final complex add/sub) measures ~13.4ns end to end -- over a 10ns
// clock budget. The chain is split into pipeline stages, each of which
// comfortably meets timing on its own:
//
//   cycle T   : 4 real multiplies (B x W)                       -> register
//   cycle T+1 : complex-multiply combine (ac-bd, ad+bc = W*B)    -> register
//   cycle T+2 : final complex add/sub, align + significand add   -> register
//               (inside fp32_complex_add_sub_pipe)
//   cycle T+3 : final complex add/sub, normalize + round         -> register
//
// A is carried alongside in shift registers so it reaches the final add in
// lock-step with the now-2-cycle-delayed W*B product. Callers must delay
// TOTAL_LATENCY (the write-back address/enable pipeline depth) by this same
// pipeline depth -- see TOTAL_LATENCY in fp32_template_generator.py.
//
// Two things changed here under real post-route parasitics (OpenROAD
// floorplan + macro placement + global route, not the pre-route
// zero/estimated-parasitic view), neither visible pre-route:
//   1. cycle T+2 used to drive X/Y straight out combinationally from a
//      single fp32_complex_add_sub feeding directly into the SRAM write
//      port -- once the SRAM macros are floorplanned that pin is physically
//      far away, and the combined path measured 31.16ns (fft_2, worse at
//      larger N) against the 10ns budget. Registering X/Y (what is now
//      cycle T+3's register) split that into two independent paths that
//      each budget separately.
//   2. even after that split, the final complex add/sub ALONE (one
//      fp32_complex_add_sub, register-to-register) still measured over
//      budget: a full FP32 add (exponent compare, mantissa align, add,
//      leading-one detect, normalize shift, round) is a genuinely deep
//      combinational block on its own post-route. fp32_complex_add_sub_pipe
//      (see fp32_adder.v) splits it at its standard, natural boundary --
//      align+add, then normalize+round -- with a register in between, which
//      is what makes this a 4-cycle butterfly instead of 3. The OTHER
//      fp32_add_sub call site (the complex-multiply combine, cycle T+1)
//      already met timing as a single combinational block and is
//      unchanged.
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

    // ---- Stage 2+3 (2-cycle internally pipelined): final complex add / subtract ----
    wire [63:0] X_w, Y_w;

    fp32_complex_add_sub_pipe adder_inst (
        .clk (clk),
        .a   (A_stage2),
        .b   (wb_product),
        .sub (1'b0),
        .out (X_w)
    );

    fp32_complex_add_sub_pipe sub_inst (
        .clk (clk),
        .a   (A_stage2),
        .b   (wb_product),
        .sub (1'b1),
        .out (Y_w)
    );

    // ---- Stage 4 (register): pipeline the final add/sub result ----
    reg [63:0] X_r, Y_r;

    always @(posedge clk) begin
        X_r <= X_w;
        Y_r <= Y_w;
    end

    assign X = X_r;
    assign Y = Y_r;

endmodule


// -----------------------------------------------------------------------------
// Thin wrapper kept for structural parity with butterfly_wrapper in
// verilog_sources/mixed_precision_wrappers.v, so the FP32 core instantiates
// a like-named block.  No precision plumbing: the baseline is FP32 throughout.
// Passes `clk` through for the internal 4-cycle pipeline above.
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
