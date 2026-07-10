// =============================================================================
// FP4 E2M1 and FP8 E4M3 Multipliers
//
// Formats
//   FP4  E2M1 : [sign:1][exp:2][mant:1]  bias = 1
//   FP8  E4M3 : [sign:1][exp:4][mant:3]  bias = 7
//
// Rounding: Round-to-Nearest-Even (RNE) in both multipliers.
//
// Changes vs original
// -------------------
// fp8_mul
//   The 5x5-bit significand product is 8 bits wide (stored in 11-bit wire).
//   After optional normalisation shift, 3 mantissa bits are kept from
//   prod_norm[5:3].  The original code discarded prod_norm[2:0] without
//   rounding (pure truncation).
//   Fix: extract guard = prod_norm[2], sticky = |prod_norm[1:0], apply RNE.
//
// fp4_mul
//   The 2x2-bit significand product is 4 bits wide (stored in 5-bit wire).
//   After optional normalisation shift, 1 mantissa bit is kept from
//   prod_norm[1].  The original code discarded prod_norm[0] without
//   rounding.
//   Fix: guard = prod_norm[0].  With only one discarded bit there are no
//   further sticky bits, so the RNE tie-break reduces to:
//     round_up = guard AND kept_bit   (round up on tie only if kept bit is 1)
//
// Saturation logic
//   The original saturation condition mixed signed exp_norm with an unsigned
//   literal in the overflow comparison, which was safe only because the
//   underflow check (exp_norm < 0) runs first in the if-else chain.
//   For clarity, exp_norm comparisons now use explicit signed literals
//   ($signed) and the overflow and saturation checks are written in terms of
//   a full-width signed compare so tool-specific elaboration warnings are
//   avoided.
// =============================================================================


// -----------------------------------------------------------------------------
// FP4 E2M1 scalar multiplier
// -----------------------------------------------------------------------------
module fp4_mul (
    input  [3:0] a,
    input  [3:0] b,
    output [3:0] out
);
    wire        sign_a = a[3],            sign_b = b[3];
    wire [1:0]  exp_a  = a[2:1],          exp_b  = b[2:1];
    wire        mant_a = a[0],            mant_b = b[0];

    wire sign_out = sign_a ^ sign_b;
    wire a_zero   = (exp_a == 2'b00) && (mant_a == 1'b0);
    wire b_zero   = (exp_b == 2'b00) && (mant_b == 1'b0);

    // Unbiased exponent: true_exp = (exp_a - 1) + (exp_b - 1) = exp_a + exp_b - 2
    // We keep one extra bias subtraction to align with the product normalisation,
    // so exp_temp = exp_a + exp_b - 1  (bias subtracted once; the second is handled
    // by the hidden-bit product position).
    wire [2:0]          exp_sum  = {1'b0, exp_a} + {1'b0, exp_b};
    wire signed [3:0]   exp_temp = $signed({1'b0, exp_sum}) - 4'sd1;

    // Significands: {0, hidden, mant}  (2-bit magnitude)
    wire hidden_a = (exp_a != 2'b00);
    wire hidden_b = (exp_b != 2'b00);
    wire [2:0] sig_a = {1'b0, hidden_a, mant_a};  // max value = 3 (0b011)
    wire [2:0] sig_b = {1'b0, hidden_b, mant_b};

    // Product: max 3*3 = 9, fits in 4 bits; stored in 5 bits for headroom
    (* use_dsp = "yes" *) wire [4:0] prod = sig_a * sig_b;

    // Normalise: if bit 3 is set the product has the form 1x.y (two integer bits)
    // and needs a right-shift of 1 to bring it to 1.y form.
    wire             need_norm = prod[3];
    wire [4:0]       prod_norm = need_norm ? (prod >> 1) : prod;
    wire signed [3:0] exp_norm = need_norm ? (exp_temp + 4'sd1) : exp_temp;

    // RNE rounding
    //   kept bit : prod_norm[1]  (the single FP4 mantissa bit)
    //   guard    : prod_norm[0]  (first dropped bit)
    //   sticky   : 0             (no further bits exist after a 2-bit x 2-bit product)
    //   RNE rule : round_up = guard AND kept   (round up on tie only when kept=1)
    wire guard     = prod_norm[0];
    wire kept      = prod_norm[1];
    wire round_up  = guard & kept;

    // Apply rounding and detect mantissa overflow
    wire mant_rounded = kept ^ round_up;   // 1-bit addition: 0+0=0, 0+1=1, 1+0=1, 1+1=0(+carry)
    wire mant_ovf     = kept & round_up;   // carry: only when 1+1

    wire signed [3:0] exp_final = mant_ovf ? (exp_norm + 4'sd1) : exp_norm;
    wire              mant_out  = mant_ovf ? 1'b0 : mant_rounded;

    // Assemble result
    reg [3:0] output_reg;
    always @(*) begin
        if (a_zero || b_zero || exp_final < 4'sd0) begin
            // Zero inputs or underflow
            output_reg = 4'b0000;
        end else if (exp_final > 4'sd3) begin
            // Overflow: saturate to max FP4 = {sign, 2'b11, 1'b1} = ±6.0
            output_reg = {sign_out, 2'b11, 1'b1};
        end else begin
            output_reg = {sign_out, exp_final[1:0], mant_out};
        end
    end

    assign out = output_reg;
endmodule


// -----------------------------------------------------------------------------
// FP4 E2M1 complex multiplier  (unchanged structure, uses fixed fp4_mul)
// out_real = a*c - b*d
// out_imag = a*d + b*c
// -----------------------------------------------------------------------------
module fp4_cmul (
    input  [3:0] a,
    input  [3:0] b,
    input  [3:0] c,
    input  [3:0] d,
    output [3:0] out_real,
    output [3:0] out_imag
);
    wire [3:0] ac, bd, ad, bc;
    fp4_mul m1 (.a(a), .b(c), .out(ac));
    fp4_mul m2 (.a(b), .b(d), .out(bd));
    fp4_mul m3 (.a(a), .b(d), .out(ad));
    fp4_mul m4 (.a(b), .b(c), .out(bc));

    fp4_add_sub s1 (.a(ac), .b(bd), .sub(1'b1), .out(out_real));  // ac - bd
    fp4_add_sub a1 (.a(ad), .b(bc), .sub(1'b0), .out(out_imag));  // ad + bc
endmodule


// -----------------------------------------------------------------------------
// FP8 E4M3 scalar multiplier
// -----------------------------------------------------------------------------
module fp8_mul (
    input  [7:0] a,
    input  [7:0] b,
    output [7:0] out
);
    wire        sign_a = a[7],            sign_b = b[7];
    wire [3:0]  exp_a  = a[6:3],          exp_b  = b[6:3];
    wire [2:0]  mant_a = a[2:0],          mant_b = b[2:0];

    wire sign_out = sign_a ^ sign_b;
    wire a_zero   = (exp_a == 4'b0000) && (mant_a == 3'b000);
    wire b_zero   = (exp_b == 4'b0000) && (mant_b == 3'b000);

    // Unbiased sum: bias7 + bias7 = 14, subtract 13 to leave one bias for output.
    // exp_temp = exp_a + exp_b - 7
    wire [4:0]          exp_sum  = {1'b0, exp_a} + {1'b0, exp_b};
    wire signed [5:0]   exp_temp = $signed({1'b0, exp_sum}) - 6'sd7;

    // Significands: {0, hidden, mant[2:0]}  (4-bit magnitude, 5-bit vector)
    wire hidden_a = (exp_a != 4'b0000);
    wire hidden_b = (exp_b != 4'b0000);
    wire [4:0] sig_a = {1'b0, hidden_a, mant_a};  // max 15
    wire [4:0] sig_b = {1'b0, hidden_b, mant_b};  // max 15

    // Product: max 15*15 = 225, needs 8 bits; stored in 11 bits for headroom
    (* use_dsp = "yes" *) wire [10:0] prod = sig_a * sig_b;

    // Normalise: bit 7 set means the product has the form 1xxx.yyyy (7 fractional bits
    // from the 4-bit x 4-bit significand), needing a right-shift of 1.
    wire             need_norm = prod[7];
    wire [10:0]      prod_norm = need_norm ? (prod >> 1) : prod;
    wire signed [5:0] exp_norm = need_norm ? (exp_temp + 6'sd1) : exp_temp;

    // RNE rounding
    //   After normalisation, the 11-bit prod_norm has the layout:
    //     [10:8] = zero-padding (never set for 5-bit inputs)
    //     [7]    = hidden 1 (normalisation ensures this)
    //     [6:4]  = would-be bit 6..4 (not used)
    //     [5:3]  = 3 mantissa bits to keep  <-- WAIT, let's be precise.
    //
    //   For need_norm=0: prod in [0,127].  sig values go up to 15*15=225, so
    //     if need_norm=0 then prod < 128 (bit 7 = 0).  Bits [6:0] are the product.
    //     The hidden 1 is at bit 6.  Mantissa bits: [5:3]. Guard: [2]. Sticky: |[1:0].
    //
    //   For need_norm=1: prod in [128,225].  prod_norm = prod>>1, so prod_norm in [64,112].
    //     Bit 6 of prod_norm is the hidden 1.  Mantissa bits: [5:3]. Guard: [2]. Sticky: |[1:0].
    //
    //   In both cases after normalisation the mantissa is at prod_norm[5:3].
    wire [2:0] raw_mant = prod_norm[5:3];
    wire       guard    = prod_norm[2];
    wire       sticky   = |prod_norm[1:0];

    // RNE: round up when guard=1 AND (sticky=1 OR lsb_of_kept_mant=1)
    wire round_up = guard & (sticky | raw_mant[0]);

    // Apply rounding; detect mantissa overflow (111 + 1 = 1000)
    wire [3:0] mant_rounded = {1'b0, raw_mant} + {{3{1'b0}}, round_up};
    wire       mant_ovf     = mant_rounded[3];   // carry out of 3-bit field

    wire signed [5:0] exp_final = mant_ovf ? (exp_norm + 6'sd1) : exp_norm;
    wire [2:0]        mant_out  = mant_ovf ? 3'b000 : mant_rounded[2:0];

    // Assemble result
    reg [7:0] output_reg;
    always @(*) begin
        if (a_zero || b_zero || exp_final < 6'sd0) begin
            // Zero inputs or underflow
            output_reg = 8'h00;
        end else if (exp_final > 6'sd14) begin
            // Overflow: saturate to max FP8 E4M3 = {sign, 4'b1111, 3'b111} = ±240.0
            // (E4M3 reserves exp=15 for NaN/Inf; max normal has exp=14)
            output_reg = {sign_out, 4'b1111, 3'b111};
        end else begin
            output_reg = {sign_out, exp_final[3:0], mant_out};
        end
    end

    assign out = output_reg;
endmodule


// -----------------------------------------------------------------------------
// FP8 E4M3 complex multiplier  (unchanged structure, uses fixed fp8_mul)
// out_real = a*c - b*d
// out_imag = a*d + b*c
// -----------------------------------------------------------------------------
module fp8_cmul (
    input  [7:0] a,
    input  [7:0] b,
    input  [7:0] c,
    input  [7:0] d,
    output [7:0] out_real,
    output [7:0] out_imag
);
    wire [7:0] ac, bd, ad, bc;
    fp8_mul m1 (.a(a), .b(c), .out(ac));
    fp8_mul m2 (.a(b), .b(d), .out(bd));
    fp8_mul m3 (.a(a), .b(d), .out(ad));
    fp8_mul m4 (.a(b), .b(c), .out(bc));

    fp8_add_sub s1 (.a(ac), .b(bd), .sub(1'b1), .out(out_real));  // ac - bd
    fp8_add_sub a1 (.a(ad), .b(bc), .sub(1'b0), .out(out_imag));  // ad + bc
endmodule