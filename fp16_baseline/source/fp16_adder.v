// =============================================================================
// FP16 (IEEE 754 binary16, E5M10) Adder / Subtractor
//
// Format
//   FP16 E5M10 : [sign:1][exp:5][mant:10]  bias = 15
//
// Baseline reference design for benchmarking against the mixed-precision
// FP4/FP8 cores and the FP32 baseline. Behavioural conventions are the same
// as fp32_baseline/source/fp32_adder.v, scaled down to binary16:
//   * Round-to-Nearest-Even (RNE)
//   * Overflow SATURATES to the largest finite normal (no Inf / NaN encoding)
//   * Subnormal inputs and subnormal results are supported
// =============================================================================

// -----------------------------------------------------------------------------
// FP16 scalar add / subtract
//   sub = 0 : out = a + b
//   sub = 1 : out = a - b
// -----------------------------------------------------------------------------
module fp16_add_sub(
    input  [15:0] a,
    input  [15:0] b,
    input         sub,
    output [15:0] out
);
    wire        sign_a = a[15];
    wire [4:0]  exp_a  = a[14:10];
    wire [9:0]  mant_a = a[9:0];
    wire        sign_b = b[15];
    wire [4:0]  exp_b  = b[14:10];
    wire [9:0]  mant_b = b[9:0];

    wire sign_b_eff = sub ? ~sign_b : sign_b;

    // Magnitude ordering (exponent first, then mantissa)
    wire a_larger = (exp_a > exp_b) || ((exp_a == exp_b) && (mant_a >= mant_b));

    wire        sign_l = a_larger ? sign_a     : sign_b_eff;
    wire [4:0]  exp_l  = a_larger ? exp_a      : exp_b;
    wire [9:0]  mant_l = a_larger ? mant_a     : mant_b;
    wire        sign_s = a_larger ? sign_b_eff : sign_a;
    wire [4:0]  exp_s  = a_larger ? exp_b      : exp_a;
    wire [9:0]  mant_s = a_larger ? mant_b     : mant_a;

    // Hidden bits; a subnormal (exp == 0) has the SAME true exponent as exp == 1
    wire hidden_l = (exp_l != 5'd0);
    wire hidden_s = (exp_s != 5'd0);
    wire [4:0] eff_exp_l = hidden_l ? exp_l : 5'd1;
    wire [4:0] eff_exp_s = hidden_s ? exp_s : 5'd1;
    wire [4:0] exp_diff  = eff_exp_l - eff_exp_s;

    // Significand layout, 15 bits:
    //   [14]    carry headroom
    //   [13]    hidden bit          <-- reference position for eff_exp
    //   [12:3]  mantissa (10 bits)
    //   [2:0]   guard / round / sticky
    // value = (sig / 2^13) * 2^(eff_exp - 15)
    wire [14:0] sig_l           = {1'b0, hidden_l, mant_l, 3'b000};
    wire [14:0] sig_s_unaligned = {1'b0, hidden_s, mant_s, 3'b000};

    // Alignment shift with sticky-bit preservation
    wire [14:0] shift_mask   = (15'd1 << exp_diff) - 15'd1;
    wire        sticky_lost  = (exp_diff >= 5'd15) ? (|sig_s_unaligned)
                                                   : (|(sig_s_unaligned & shift_mask));
    wire [14:0] sig_s_shift  = (exp_diff >= 5'd15) ? 15'd0
                                                   : (sig_s_unaligned >> exp_diff);
    wire [14:0] sig_s        = sig_s_shift | {14'd0, sticky_lost};

    wire do_sub = (sign_l != sign_s);

    wire [15:0] sig_result_raw = do_sub ? ({1'b0, sig_l} - {1'b0, sig_s})
                                        : ({1'b0, sig_l} + {1'b0, sig_s});

    // -------------------------------------------------------------------------
    // Leading-one detection (priority encoder over the 16-bit result)
    // -------------------------------------------------------------------------
    integer   idx;
    reg [3:0] msb_pos;
    reg       any_bit;
    always @(*) begin
        msb_pos = 4'd0;
        any_bit = 1'b0;
        for (idx = 15; idx >= 0; idx = idx - 1) begin
            if (!any_bit && sig_result_raw[idx]) begin
                msb_pos = idx[3:0];
                any_bit = 1'b1;
            end
        end
    end

    // -------------------------------------------------------------------------
    // Normalisation: bring the leading one back to bit 13
    // -------------------------------------------------------------------------
    reg  [15:0] sig_norm;
    reg  [5:0]  exp_res;       // 6 bits so the +1 carry-out is visible

    reg [3:0] sh_r, sh_l;
    always @(*) begin
        sig_norm      = sig_result_raw;
        exp_res       = {1'b0, eff_exp_l};
        sh_r          = 4'd0;
        sh_l          = 4'd0;

        if (any_bit) begin
            if (msb_pos > 4'd13) begin
                // Carry out of the hidden bit: shift right, sticky-preserving
                sh_r     = msb_pos - 4'd13;
                sig_norm = (sig_result_raw >> sh_r) |
                           {15'd0, |(sig_result_raw & ((16'd1 << sh_r) - 16'd1))};
                exp_res  = {1'b0, eff_exp_l} + {2'd0, sh_r};
            end
            else if (msb_pos < 4'd13) begin
                sh_l = 4'd13 - msb_pos;
                if ({1'b0, eff_exp_l} > {2'd0, sh_l}) begin
                    // Still normal after left-normalisation
                    sig_norm = sig_result_raw << sh_l;
                    exp_res  = {1'b0, eff_exp_l} - {2'd0, sh_l};
                end else begin
                    // Underflows into the subnormal range
                    sig_norm = sig_result_raw << (eff_exp_l - 5'd1);
                    exp_res  = 6'd0;
                end
            end
            // msb_pos == 13 : already normalised, defaults hold
        end
    end

    // -------------------------------------------------------------------------
    // Round-to-Nearest-Even
    //   kept   : sig_norm[12:3]
    //   guard  : sig_norm[2]
    //   sticky : sig_norm[1:0]
    // -------------------------------------------------------------------------
    wire [9:0] raw_mant = sig_norm[12:3];
    wire       guard    = sig_norm[2];
    wire       sticky   = |sig_norm[1:0];
    wire       round_up = guard & (sticky | raw_mant[0]);

    wire [10:0] mant_rounded = {1'b0, raw_mant} + {10'd0, round_up};
    wire        mant_ovf     = mant_rounded[10];

    // On subnormal results a mantissa carry-out promotes the value to exp == 1
    wire [5:0] exp_final  = mant_ovf ? (exp_res + 6'd1) : exp_res;
    wire [9:0] mant_final = mant_ovf ? 10'd0 : mant_rounded[9:0];

    reg [15:0] result;
    always @(*) begin
        if (!any_bit) begin
            // Exact cancellation -> +0
            result = 16'h0000;
        end
        else if (exp_final >= 6'd31) begin
            // Saturate to the largest finite normal (+/- 65504)
            result = {sign_l, 5'd30, 10'h3FF};
        end
        else if ((exp_final == 6'd0) && (mant_final == 10'd0)) begin
            result = 16'h0000;
        end
        else begin
            result = {sign_l, exp_final[4:0], mant_final};
        end
    end

    assign out = result;
endmodule


// -----------------------------------------------------------------------------
// FP16 complex add / subtract
//   Packing: {real[15:0], imag[15:0]}
// -----------------------------------------------------------------------------
module fp16_complex_add_sub(
    input  [31:0] a,
    input  [31:0] b,
    input         sub,
    output [31:0] out
);
    wire [15:0] a_real = a[31:16];
    wire [15:0] a_imag = a[15:0];
    wire [15:0] b_real = b[31:16];
    wire [15:0] b_imag = b[15:0];
    wire [15:0] out_real, out_imag;

    fp16_add_sub adder_real (.a(a_real), .b(b_real), .sub(sub), .out(out_real));
    fp16_add_sub adder_imag (.a(a_imag), .b(b_imag), .sub(sub), .out(out_imag));

    assign out = {out_real, out_imag};
endmodule
