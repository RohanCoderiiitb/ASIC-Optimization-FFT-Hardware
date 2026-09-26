// =============================================================================
// FP32 (IEEE 754 binary32, E8M23) Adder / Subtractor
//
// Format
//   FP32 E8M23 : [sign:1][exp:8][mant:23]  bias = 127
//
// Baseline reference design for benchmarking against the mixed-precision
// FP4/FP8 cores.  Behavioural conventions are deliberately matched to
// verilog_sources/adder.v so the comparison is apples-to-apples:
//   * Round-to-Nearest-Even (RNE)
//   * Overflow SATURATES to the largest finite normal (no Inf / NaN encoding)
//   * Subnormal inputs and subnormal results are supported
// =============================================================================

// -----------------------------------------------------------------------------
// FP32 scalar add / subtract
//   sub = 0 : out = a + b
//   sub = 1 : out = a - b
// -----------------------------------------------------------------------------
module fp32_add_sub(
    input  [31:0] a,
    input  [31:0] b,
    input         sub,
    output [31:0] out
);
    wire        sign_a = a[31];
    wire [7:0]  exp_a  = a[30:23];
    wire [22:0] mant_a = a[22:0];
    wire        sign_b = b[31];
    wire [7:0]  exp_b  = b[30:23];
    wire [22:0] mant_b = b[22:0];

    wire sign_b_eff = sub ? ~sign_b : sign_b;

    // Magnitude ordering (exponent first, then mantissa)
    wire a_larger = (exp_a > exp_b) || ((exp_a == exp_b) && (mant_a >= mant_b));

    wire        sign_l = a_larger ? sign_a     : sign_b_eff;
    wire [7:0]  exp_l  = a_larger ? exp_a      : exp_b;
    wire [22:0] mant_l = a_larger ? mant_a     : mant_b;
    wire        sign_s = a_larger ? sign_b_eff : sign_a;
    wire [7:0]  exp_s  = a_larger ? exp_b      : exp_a;
    wire [22:0] mant_s = a_larger ? mant_b     : mant_a;

    // Hidden bits; a subnormal (exp == 0) has the SAME true exponent as exp == 1
    wire hidden_l = (exp_l != 8'd0);
    wire hidden_s = (exp_s != 8'd0);
    wire [7:0] eff_exp_l = hidden_l ? exp_l : 8'd1;
    wire [7:0] eff_exp_s = hidden_s ? exp_s : 8'd1;
    wire [7:0] exp_diff  = eff_exp_l - eff_exp_s;

    // Significand layout, 28 bits:
    //   [27]    carry headroom
    //   [26]    hidden bit          <-- reference position for eff_exp
    //   [25:3]  mantissa (23 bits)
    //   [2:0]   guard / round / sticky
    // value = (sig / 2^26) * 2^(eff_exp - 127)
    wire [27:0] sig_l           = {1'b0, hidden_l, mant_l, 3'b000};
    wire [27:0] sig_s_unaligned = {1'b0, hidden_s, mant_s, 3'b000};

    // Alignment shift with sticky-bit preservation
    wire [27:0] shift_mask   = (28'd1 << exp_diff) - 28'd1;
    wire        sticky_lost  = (exp_diff >= 8'd28) ? (|sig_s_unaligned)
                                                   : (|(sig_s_unaligned & shift_mask));
    wire [27:0] sig_s_shift  = (exp_diff >= 8'd28) ? 28'd0
                                                   : (sig_s_unaligned >> exp_diff);
    wire [27:0] sig_s        = sig_s_shift | {27'd0, sticky_lost};

    wire do_sub = (sign_l != sign_s);

    wire [28:0] sig_result_raw = do_sub ? ({1'b0, sig_l} - {1'b0, sig_s})
                                        : ({1'b0, sig_l} + {1'b0, sig_s});

    // -------------------------------------------------------------------------
    // Leading-one detection (priority encoder over the 29-bit result)
    // -------------------------------------------------------------------------
    integer   idx;
    reg [4:0] msb_pos;
    reg       any_bit;
    always @(*) begin
        msb_pos = 5'd0;
        any_bit = 1'b0;
        for (idx = 28; idx >= 0; idx = idx - 1) begin
            if (!any_bit && sig_result_raw[idx]) begin
                msb_pos = idx[4:0];
                any_bit = 1'b1;
            end
        end
    end

    // -------------------------------------------------------------------------
    // Normalisation: bring the leading one back to bit 26
    // -------------------------------------------------------------------------
    reg  [28:0] sig_norm;
    reg  [8:0]  exp_res;       // 9 bits so the +1 carry-out is visible

    reg [4:0] sh_r, sh_l;
    always @(*) begin
        sig_norm      = sig_result_raw;
        exp_res       = {1'b0, eff_exp_l};
        sh_r          = 5'd0;
        sh_l          = 5'd0;

        if (any_bit) begin
            if (msb_pos > 5'd26) begin
                // Carry out of the hidden bit: shift right, sticky-preserving
                sh_r     = msb_pos - 5'd26;
                sig_norm = (sig_result_raw >> sh_r) |
                           {28'd0, |(sig_result_raw & ((29'd1 << sh_r) - 29'd1))};
                exp_res  = {1'b0, eff_exp_l} + {4'd0, sh_r};
            end
            else if (msb_pos < 5'd26) begin
                sh_l = 5'd26 - msb_pos;
                if ({1'b0, eff_exp_l} > {4'd0, sh_l}) begin
                    // Still normal after left-normalisation
                    sig_norm = sig_result_raw << sh_l;
                    exp_res  = {1'b0, eff_exp_l} - {4'd0, sh_l};
                end else begin
                    // Underflows into the subnormal range
                    sig_norm = sig_result_raw << (eff_exp_l - 8'd1);
                    exp_res  = 9'd0;
                end
            end
            // msb_pos == 26 : already normalised, defaults hold
        end
    end

    // -------------------------------------------------------------------------
    // Round-to-Nearest-Even
    //   kept   : sig_norm[25:3]
    //   guard  : sig_norm[2]
    //   sticky : sig_norm[1:0]
    // -------------------------------------------------------------------------
    wire [22:0] raw_mant = sig_norm[25:3];
    wire        guard    = sig_norm[2];
    wire        sticky   = |sig_norm[1:0];
    wire        round_up = guard & (sticky | raw_mant[0]);

    wire [23:0] mant_rounded = {1'b0, raw_mant} + {23'd0, round_up};
    wire        mant_ovf     = mant_rounded[23];

    // On subnormal results a mantissa carry-out promotes the value to exp == 1
    wire [8:0]  exp_final  = mant_ovf ? (exp_res + 9'd1) : exp_res;
    wire [22:0] mant_final = mant_ovf ? 23'd0 : mant_rounded[22:0];

    reg [31:0] result;
    always @(*) begin
        if (!any_bit) begin
            // Exact cancellation -> +0
            result = 32'h0000_0000;
        end
        else if (exp_final >= 9'd255) begin
            // Saturate to the largest finite normal (+/- 3.4028235e38)
            result = {sign_l, 8'd254, 23'h7FFFFF};
        end
        else if ((exp_final == 9'd0) && (mant_final == 23'd0)) begin
            result = 32'h0000_0000;
        end
        else begin
            result = {sign_l, exp_final[7:0], mant_final};
        end
    end

    assign out = result;
endmodule


// -----------------------------------------------------------------------------
// FP32 complex add / subtract
//   Packing: {real[31:0], imag[31:0]}
// -----------------------------------------------------------------------------
module fp32_complex_add_sub(
    input  [63:0] a,
    input  [63:0] b,
    input         sub,
    output [63:0] out
);
    wire [31:0] a_real = a[63:32];
    wire [31:0] a_imag = a[31:0];
    wire [31:0] b_real = b[63:32];
    wire [31:0] b_imag = b[31:0];
    wire [31:0] out_real, out_imag;

    fp32_add_sub adder_real (.a(a_real), .b(b_real), .sub(sub), .out(out_real));
    fp32_add_sub adder_imag (.a(a_imag), .b(b_imag), .sub(sub), .out(out_imag));

    assign out = {out_real, out_imag};
endmodule
