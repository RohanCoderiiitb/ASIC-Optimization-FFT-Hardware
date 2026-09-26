// =============================================================================
// FP16 (IEEE 754 binary16, E5M10) Multiplier
//
// Format
//   FP16 E5M10 : [sign:1][exp:5][mant:10]  bias = 15
//
// Conventions matched to fp32_baseline/source/fp32_multiplier.v, scaled down
// to binary16:
//   * Round-to-Nearest-Even (RNE)
//   * Overflow SATURATES to the largest finite normal (no Inf / NaN encoding)
//   * Underflow flushes to zero -- except that a product just below the
//     smallest normal which IEEE rounding carries UP to the smallest normal
//     is kept (see the exp_norm == 0 path below)
// =============================================================================

// -----------------------------------------------------------------------------
// FP16 E5M10 scalar multiplier
// -----------------------------------------------------------------------------
module fp16_mul (
    input  [15:0] a,
    input  [15:0] b,
    output [15:0] out
);
    wire        sign_a = a[15],      sign_b = b[15];
    wire [4:0]  exp_a  = a[14:10],   exp_b  = b[14:10];
    wire [9:0]  mant_a = a[9:0],     mant_b = b[9:0];

    wire sign_out = sign_a ^ sign_b;
    wire a_zero   = (exp_a == 5'd0) && (mant_a == 10'd0);
    wire b_zero   = (exp_b == 5'd0) && (mant_b == 10'd0);

    // Hidden bits; a subnormal shares the true exponent of exp == 1
    wire hidden_a = (exp_a != 5'd0);
    wire hidden_b = (exp_b != 5'd0);
    wire [4:0] eff_exp_a = hidden_a ? exp_a : 5'd1;
    wire [4:0] eff_exp_b = hidden_b ? exp_b : 5'd1;

    // Significands: {hidden, mant[9:0]}, 11 bits, value = sig / 2^10
    wire [10:0] sig_a = {hidden_a, mant_a};
    wire [10:0] sig_b = {hidden_b, mant_b};

    // Product: 22 bits.  With both hidden bits set the leading one sits at
    // bit 20 or bit 21, so bit 20 is the reference position:
    //   value = (prod / 2^20) * 2^(eff_exp_a + eff_exp_b - 30)
    //         = (sig  / 2^10) * 2^(exp - 15)   with exp = ea + eb - 15
    (* use_dsp = "yes" *) wire [21:0] prod = sig_a * sig_b;

    wire signed [7:0] exp_temp = $signed({3'b000, eff_exp_a}) +
                                 $signed({3'b000, eff_exp_b}) - 8'sd15;

    // -------------------------------------------------------------------------
    // Leading-one detection (handles subnormal operands generically)
    // -------------------------------------------------------------------------
    integer   idx;
    reg [4:0] msb_pos;
    reg       any_bit;
    always @(*) begin
        msb_pos = 5'd0;
        any_bit = 1'b0;
        for (idx = 21; idx >= 0; idx = idx - 1) begin
            if (!any_bit && prod[idx]) begin
                msb_pos = idx[4:0];
                any_bit = 1'b1;
            end
        end
    end

    // -------------------------------------------------------------------------
    // Normalise so the leading one sits at bit 20
    // -------------------------------------------------------------------------
    reg  [21:0]       prod_norm;
    reg signed [7:0]  exp_norm;
    reg [4:0]         sh_r, sh_l;

    always @(*) begin
        prod_norm = prod;
        exp_norm  = exp_temp;
        sh_r      = 5'd0;
        sh_l      = 5'd0;

        if (any_bit) begin
            if (msb_pos > 5'd20) begin
                sh_r      = msb_pos - 5'd20;
                // Sticky-preserving right shift
                prod_norm = (prod >> sh_r) |
                            {21'd0, |(prod & ((22'd1 << sh_r) - 22'd1))};
                exp_norm  = exp_temp + $signed({3'b000, sh_r});
            end
            else if (msb_pos < 5'd20) begin
                sh_l      = 5'd20 - msb_pos;
                prod_norm = prod << sh_l;
                exp_norm  = exp_temp - $signed({3'b000, sh_l});
            end
        end
    end

    // -------------------------------------------------------------------------
    // Round-to-Nearest-Even (normal precision)
    //   kept   : prod_norm[19:10]
    //   guard  : prod_norm[9]
    //   sticky : prod_norm[8:0]
    // -------------------------------------------------------------------------
    wire [9:0] raw_mant = prod_norm[19:10];
    wire       guard    = prod_norm[9];
    wire       sticky   = |prod_norm[8:0];
    wire       round_up = guard & (sticky | raw_mant[0]);

    wire [10:0] mant_rounded = {1'b0, raw_mant} + {10'd0, round_up};
    wire        mant_ovf     = mant_rounded[10];

    wire signed [7:0] exp_final = mant_ovf ? (exp_norm + 8'sd1) : exp_norm;
    wire [9:0]        mant_out  = mant_ovf ? 10'd0 : mant_rounded[9:0];

    // -------------------------------------------------------------------------
    // Boundary case exp_norm == 0: the value lies in [2^-15, 2^-14) and IEEE
    // rounds it at SUBNORMAL precision (one bit coarser than above).  It
    // either stays subnormal (-> flushed) or carries up to the smallest
    // normal 2^-14. The subnormal fraction is prod_norm[20:11]; since bit 20
    // is the leading one, a carry-out happens only if [20:11] is all ones.
    // -------------------------------------------------------------------------
    wire sub_guard    = prod_norm[10];
    wire sub_sticky   = |prod_norm[9:0];
    wire sub_round_up = sub_guard & (sub_sticky | prod_norm[11]);
    wire sub_to_min   = (&prod_norm[20:11]) & sub_round_up;

    reg [15:0] output_reg;
    always @(*) begin
        if (a_zero || b_zero || !any_bit) begin
            output_reg = 16'h0000;
        end else if (exp_norm == 8'sd0) begin
            // Rounds up to the smallest normal, or flushes to zero
            output_reg = sub_to_min ? {sign_out, 5'd1, 10'd0} : 16'h0000;
        end else if (exp_final < 8'sd1) begin
            // Underflow -> flush to zero
            output_reg = 16'h0000;
        end else if (exp_final > 8'sd30) begin
            // Overflow: saturate to the largest finite normal
            output_reg = {sign_out, 5'd30, 10'h3FF};
        end else begin
            output_reg = {sign_out, exp_final[4:0], mant_out};
        end
    end

    assign out = output_reg;
endmodule


// -----------------------------------------------------------------------------
// FP16 complex multiplier
//   out_real = a*c - b*d
//   out_imag = a*d + b*c
// -----------------------------------------------------------------------------
module fp16_cmul (
    input  [15:0] a,
    input  [15:0] b,
    input  [15:0] c,
    input  [15:0] d,
    output [15:0] out_real,
    output [15:0] out_imag
);
    wire [15:0] ac, bd, ad, bc;

    fp16_mul m1 (.a(a), .b(c), .out(ac));
    fp16_mul m2 (.a(b), .b(d), .out(bd));
    fp16_mul m3 (.a(a), .b(d), .out(ad));
    fp16_mul m4 (.a(b), .b(c), .out(bc));

    fp16_add_sub s1 (.a(ac), .b(bd), .sub(1'b1), .out(out_real));  // ac - bd
    fp16_add_sub a1 (.a(ad), .b(bc), .sub(1'b0), .out(out_imag));  // ad + bc
endmodule
