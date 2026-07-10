module q2_14_to_fp4_e2m1 (
    input  wire signed [15:0] fixed_in,
    output wire [3:0]         fp_out
);
    // 1. Extract Sign and Absolute Value
    wire sign = fixed_in[15];
    wire [15:0] abs_val = sign ? -fixed_in : fixed_in;

    // 2. Hardware Magnitude Thresholds (Matches Python float_to_fp4 logic)
    // 0.25 in Q2.14 = 16'h0400
    // 0.50 in Q2.14 = 16'h0800
    // 1.00 in Q2.14 = 16'h1000
    // 1.50 in Q2.14 = 16'h1800
    
    reg [1:0] exp;
    reg       mant;

    always @(*) begin
        if      (abs_val < 16'h1000) begin exp = 2'd0; mant = 1'b0; end // val < 0.25 -> 0.0
        else if (abs_val < 16'h3000) begin exp = 2'd0; mant = 1'b1; end // val < 0.75 -> 0.5
        else if (abs_val < 16'h5000) begin exp = 2'd1; mant = 1'b0; end // val < 1.25 -> 1.0
        else if (abs_val < 16'h7000) begin exp = 2'd1; mant = 1'b1; end // val < 1.75 -> 1.5
        else                         begin exp = 2'd2; mant = 1'b0; end // val >=1.75 -> 2.0
    end

    // 3. Pack into 4-bit E2M1 format
    assign fp_out = {sign, exp, mant};
    
endmodule

module q2_14_to_fp8_e4m3 (
    input  wire signed [15:0] fixed_in,
    output wire [7:0]         fp_out
);
    // 1. Extract Sign and Absolute Value
    wire sign = fixed_in[15];
    wire [15:0] abs_val = sign ? -fixed_in : fixed_in;

    // 2. Leading Zero Detector (LZD) to find the exponent
    // abs_val[14] is the 2^0 bit. abs_val[13] is 2^-1, etc.
    reg [3:0] lz;
    always @(*) begin
        if      (abs_val[15]) lz = 0;  // Edge case: exactly -2.0 (16'h8000)
        else if (abs_val[14]) lz = 1;  // Value >= 1.0
        else if (abs_val[13]) lz = 2;  // Value >= 0.5
        else if (abs_val[12]) lz = 3;  // Value >= 0.25
        else if (abs_val[11]) lz = 4;
        else if (abs_val[10]) lz = 5;
        else if (abs_val[9])  lz = 6;
        else if (abs_val[8])  lz = 7;
        else if (abs_val[7])  lz = 8;
        else                  lz = 9;  // Subnormals/Zero (Flush to zero)
    end

    // 3. Shift Data to Extract Mantissa and Round Bit
    wire [15:0] shifted = abs_val << lz;
    // The hidden '1' is now positioned at shifted[15].
    wire [2:0] raw_mant = shifted[14:12]; // Extract 3 mantissa bits
    wire       round_bit = shifted[11];   // Extract bit for rounding

    // 4. Calculate Final Biased Exponent (Bias = 7) and apply rounding
    reg [3:0] final_exp;
    reg [2:0] final_mant;

    always @(*) begin
        // If value is too small (val < 2^-6), flush to zero as per Python model
        if (abs_val == 0 || lz >= 8) begin
            final_exp  = 4'd0;
            final_mant = 3'd0;
        end else begin
            final_exp = 4'd8 - lz; // True Exp = 1 - lz. Biased = True Exp + 7.
            
            // Round-to-nearest logic
            if (round_bit) begin
                if (raw_mant == 3'b111) begin
                    final_mant = 3'b000;
                    final_exp  = final_exp + 1'b1; // Carry over pushes exponent up
                end else begin
                    final_mant = raw_mant + 1'b1;
                end
            end else begin
                final_mant = raw_mant;
            end
        end
    end

    // 5. Pack into 8-bit E4M3 format
    assign fp_out = {sign, final_exp, final_mant};

endmodule