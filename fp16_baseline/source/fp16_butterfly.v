// =============================================================================
// FP16 Radix-2 DIT Butterfly -- internally pipelined, 4-cycle latency
//
// Memory / datapath format: 32-bit complex FP16
//   [31:16] FP16 Real, [15:0] FP16 Imag
//
// Mirrors fp32_baseline/source/fp32_butterfly.v structurally (same
// X = A + W*B / Y = A - W*B, same 4-cycle internal pipeline split), scaled
// down to binary16. The FP16 multiply/add primitives are individually
// smaller and faster than their FP32 counterparts, but this baseline keeps
// the identical pipeline depth as the FP32 baseline for architectural
// parity across the precision sweep (same TOTAL_LATENCY / STALL_CYCLES in
// fp16_template_generator.py) -- see fp32_butterfly.v's header for the full
// timing rationale that motivated the split.
//
//   cycle T   : 4 real multiplies (B x W)                       -> register
//   cycle T+1 : complex-multiply combine (ac-bd, ad+bc = W*B)    -> register
//   cycle T+2 : final complex add/sub, align + significand add   -> register
//               (inside fp16_complex_add_sub_pipe)
//   cycle T+3 : final complex add/sub, normalize + round         -> register
//
// A is carried alongside in shift registers so it reaches the final add in
// lock-step with the now-2-cycle-delayed W*B product.
//
// Two things changed here under real post-route parasitics (OpenROAD
// floorplan + macro placement + global route, not the pre-route
// zero/estimated-parasitic view), neither visible pre-route:
//   1. cycle T+2 used to drive X/Y straight out combinationally from a
//      single fp16_complex_add_sub feeding directly into the SRAM write
//      port -- once the SRAM macros are floorplanned that pin is physically
//      far away, and the combined path measured 17.46ns against the 10ns
//      budget. Registering X/Y (what is now cycle T+3's register) split
//      that into two independent paths that each budget separately.
//   2. even after that split, the final complex add/sub ALONE (one
//      fp16_complex_add_sub, register-to-register) still measured 11.45ns:
//      a full FP16 add (exponent compare, mantissa align, add, leading-one
//      detect, normalize shift, round) is a genuinely deep combinational
//      block on its own post-route. fp16_complex_add_sub_pipe (see
//      fp16_adder.v) splits it at its standard, natural boundary --
//      align+add, then normalize+round -- with a register in between, which
//      is what makes this a 4-cycle butterfly instead of 3. The OTHER
//      fp16_add_sub call site (the complex-multiply combine, cycle T+1)
//      already met timing as a single combinational block and is
//      unchanged.
// =============================================================================

module fp16_butterfly_generation_unit(
    input         clk,
    input  [31:0] A,
    input  [31:0] B,
    input  [31:0] W,
    output [31:0] X,
    output [31:0] Y
);

    // ---- Stage 0 (combinational): 4 real multiplies, B x W ----
    wire [15:0] ac_w, bd_w, ad_w, bc_w;

    fp16_mul m1 (.a(B[31:16]), .b(W[31:16]), .out(ac_w));  // Br * Wr
    fp16_mul m2 (.a(B[15:0]),  .b(W[15:0]),  .out(bd_w));  // Bi * Wi
    fp16_mul m3 (.a(B[31:16]), .b(W[15:0]),  .out(ad_w));  // Br * Wi
    fp16_mul m4 (.a(B[15:0]),  .b(W[31:16]), .out(bc_w));  // Bi * Wr

    reg [15:0] ac_r, bd_r, ad_r, bc_r;
    reg [31:0] A_stage1;

    always @(posedge clk) begin
        ac_r <= ac_w;
        bd_r <= bd_w;
        ad_r <= ad_w;
        bc_r <= bc_w;
        A_stage1 <= A;
    end

    // ---- Stage 1 (combinational): complex-multiply combine, WB = ac-bd + j(ad+bc) ----
    wire [15:0] wb_real_w, wb_imag_w;

    fp16_add_sub cmul_real (.a(ac_r), .b(bd_r), .sub(1'b1), .out(wb_real_w));
    fp16_add_sub cmul_imag (.a(ad_r), .b(bc_r), .sub(1'b0), .out(wb_imag_w));

    reg [15:0] wb_real_r, wb_imag_r;
    reg [31:0] A_stage2;

    always @(posedge clk) begin
        wb_real_r <= wb_real_w;
        wb_imag_r <= wb_imag_w;
        A_stage2  <= A_stage1;
    end

    wire [31:0] wb_product = {wb_real_r, wb_imag_r};

    // ---- Stage 2+3 (2-cycle internally pipelined): final complex add / subtract ----
    wire [31:0] X_w, Y_w;

    fp16_complex_add_sub_pipe adder_inst (
        .clk (clk),
        .a   (A_stage2),
        .b   (wb_product),
        .sub (1'b0),
        .out (X_w)
    );

    fp16_complex_add_sub_pipe sub_inst (
        .clk (clk),
        .a   (A_stage2),
        .b   (wb_product),
        .sub (1'b1),
        .out (Y_w)
    );

    // ---- Stage 4 (register): pipeline the final add/sub result ----
    reg [31:0] X_r, Y_r;

    always @(posedge clk) begin
        X_r <= X_w;
        Y_r <= Y_w;
    end

    assign X = X_r;
    assign Y = Y_r;

endmodule


// -----------------------------------------------------------------------------
// Thin wrapper kept for structural parity with butterfly_wrapper in
// verilog_sources/mixed_precision_wrappers.v, so the FP16 core instantiates
// a like-named block.  No precision plumbing: the baseline is FP16 throughout.
// Passes `clk` through for the internal 4-cycle pipeline above.
// -----------------------------------------------------------------------------
module fp16_butterfly_wrapper (
    input         clk,
    input  [31:0] A, B,
    input  [31:0] W,
    output [31:0] X, Y
);
    fp16_butterfly_generation_unit bf (
        .clk (clk),
        .A (A),
        .B (B),
        .W (W),
        .X (X),
        .Y (Y)
    );
endmodule
