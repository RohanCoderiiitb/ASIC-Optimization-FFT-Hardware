// =============================================================================
// FP32 (IEEE 754 binary32, E8M23) Multiplier
//
// Format
//   FP32 E8M23 : [sign:1][exp:8][mant:23]  bias = 127
//
// Conventions matched to verilog_sources/multiplier.v:
//   * Round-to-Nearest-Even (RNE)
//   * Overflow SATURATES to the largest finite normal (no Inf / NaN encoding)
//   * Underflow flushes to zero -- except that a product just below the
//     smallest normal which IEEE rounding carries UP to the smallest normal
//     is kept (see the exp_norm == 0 path below)
// =============================================================================

// -----------------------------------------------------------------------------
// FP32 E8M23 scalar multiplier
// -----------------------------------------------------------------------------
module fp32_mul (
    input  [31:0] a,
    input  [31:0] b,
    output [31:0] out
);
    wire        sign_a = a[31],      sign_b = b[31];
    wire [7:0]  exp_a  = a[30:23],   exp_b  = b[30:23];
    wire [22:0] mant_a = a[22:0],    mant_b = b[22:0];

    wire sign_out = sign_a ^ sign_b;
    wire a_zero   = (exp_a == 8'd0) && (mant_a == 23'd0);
    wire b_zero   = (exp_b == 8'd0) && (mant_b == 23'd0);

    // Hidden bits; a subnormal shares the true exponent of exp == 1
    wire hidden_a = (exp_a != 8'd0);
    wire hidden_b = (exp_b != 8'd0);
    wire [7:0] eff_exp_a = hidden_a ? exp_a : 8'd1;
    wire [7:0] eff_exp_b = hidden_b ? exp_b : 8'd1;

    // Significands: {hidden, mant[22:0]}, 24 bits, value = sig / 2^23
    wire [23:0] sig_a = {hidden_a, mant_a};
    wire [23:0] sig_b = {hidden_b, mant_b};

    // Product: 48 bits.  With both hidden bits set the leading one sits at
    // bit 46 or bit 47, so bit 46 is the reference position:
    //   value = (prod / 2^46) * 2^(eff_exp_a + eff_exp_b - 254)
    //         = (sig  / 2^23) * 2^(exp - 127)   with exp = ea + eb - 127
    (* use_dsp = "yes" *) wire [47:0] prod = sig_a * sig_b;

    wire signed [10:0] exp_temp = $signed({3'b000, eff_exp_a}) +
                                  $signed({3'b000, eff_exp_b}) - 11'sd127;

    // -------------------------------------------------------------------------
    // Leading-one detection (handles subnormal operands generically)
    // -------------------------------------------------------------------------
    integer   idx;
    reg [5:0] msb_pos;
    reg       any_bit;
    always @(*) begin
        msb_pos = 6'd0;
        any_bit = 1'b0;
        for (idx = 47; idx >= 0; idx = idx - 1) begin
            if (!any_bit && prod[idx]) begin
                msb_pos = idx[5:0];
                any_bit = 1'b1;
            end
        end
    end

    // -------------------------------------------------------------------------
    // Normalise so the leading one sits at bit 46
    // -------------------------------------------------------------------------
    reg  [47:0]        prod_norm;
    reg signed [10:0]  exp_norm;
    reg [5:0]          sh_r, sh_l;

    always @(*) begin
        prod_norm = prod;
        exp_norm  = exp_temp;
        sh_r      = 6'd0;
        sh_l      = 6'd0;

        if (any_bit) begin
            if (msb_pos > 6'd46) begin
                sh_r      = msb_pos - 6'd46;
                // Sticky-preserving right shift
                prod_norm = (prod >> sh_r) |
                            {47'd0, |(prod & ((48'd1 << sh_r) - 48'd1))};
                exp_norm  = exp_temp + $signed({5'b00000, sh_r});
            end
            else if (msb_pos < 6'd46) begin
                sh_l      = 6'd46 - msb_pos;
                prod_norm = prod << sh_l;
                exp_norm  = exp_temp - $signed({5'b00000, sh_l});
            end
        end
    end

    // -------------------------------------------------------------------------
    // Round-to-Nearest-Even (normal precision)
    //   kept   : prod_norm[45:23]
    //   guard  : prod_norm[22]
    //   sticky : prod_norm[21:0]
    // -------------------------------------------------------------------------
    wire [22:0] raw_mant = prod_norm[45:23];
    wire        guard    = prod_norm[22];
    wire        sticky   = |prod_norm[21:0];
    wire        round_up = guard & (sticky | raw_mant[0]);

    wire [23:0] mant_rounded = {1'b0, raw_mant} + {23'd0, round_up};
    wire        mant_ovf     = mant_rounded[23];

    wire signed [10:0] exp_final = mant_ovf ? (exp_norm + 11'sd1) : exp_norm;
    wire [22:0]        mant_out  = mant_ovf ? 23'd0 : mant_rounded[22:0];

    // -------------------------------------------------------------------------
    // Boundary case exp_norm == 0: the value lies in [2^-127, 2^-126) and IEEE
    // rounds it at SUBNORMAL precision (one bit coarser than above).  It
    // either stays subnormal (-> flushed) or carries up to the smallest
    // normal 2^-126.  The subnormal fraction is prod_norm[46:24]; since bit 46
    // is the leading one, a carry-out happens only if [46:24] is all ones.
    // -------------------------------------------------------------------------
    wire sub_guard    = prod_norm[23];
    wire sub_sticky   = |prod_norm[22:0];
    wire sub_round_up = sub_guard & (sub_sticky | prod_norm[24]);
    wire sub_to_min   = (&prod_norm[46:24]) & sub_round_up;

    reg [31:0] output_reg;
    always @(*) begin
        if (a_zero || b_zero || !any_bit) begin
            output_reg = 32'h0000_0000;
        end else if (exp_norm == 11'sd0) begin
            // Rounds up to the smallest normal, or flushes to zero
            output_reg = sub_to_min ? {sign_out, 8'd1, 23'd0} : 32'h0000_0000;
        end else if (exp_final < 11'sd1) begin
            // Underflow -> flush to zero
            output_reg = 32'h0000_0000;
        end else if (exp_final > 11'sd254) begin
            // Overflow: saturate to the largest finite normal
            output_reg = {sign_out, 8'd254, 23'h7FFFFF};
        end else begin
            output_reg = {sign_out, exp_final[7:0], mant_out};
        end
    end

    assign out = output_reg;
endmodule


// -----------------------------------------------------------------------------
// FP32 complex multiplier
//   out_real = a*c - b*d
//   out_imag = a*d + b*c
// -----------------------------------------------------------------------------
module fp32_cmul (
    input  [31:0] a,
    input  [31:0] b,
    input  [31:0] c,
    input  [31:0] d,
    output [31:0] out_real,
    output [31:0] out_imag
);
    wire [31:0] ac, bd, ad, bc;

    fp32_mul m1 (.a(a), .b(c), .out(ac));
    fp32_mul m2 (.a(b), .b(d), .out(bd));
    fp32_mul m3 (.a(a), .b(d), .out(ad));
    fp32_mul m4 (.a(b), .b(c), .out(bc));

    fp32_add_sub s1 (.a(ac), .b(bd), .sub(1'b1), .out(out_real));  // ac - bd
    fp32_add_sub a1 (.a(ad), .b(bc), .sub(1'b0), .out(out_imag));  // ad + bc
endmodule
