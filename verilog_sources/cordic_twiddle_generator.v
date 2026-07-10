// =============================================================================
// Pipelined CORDIC Twiddle Generator 
// Featuring High-Precision Phase Scaling & Exact Cardinal Bypass
// =============================================================================
`timescale 1ns / 1ps

module cordic_twiddle_generator #(
    parameter LATENCY = 10,       
    parameter MAX_N = 1024,
    parameter ADDR_WIDTH = 11     
)(
    input  wire                  clk,
    input  wire                  rst,
    input  wire [ADDR_WIDTH-1:0] k,      
    input  wire [ADDR_WIDTH-1:0] n,          
    input  wire                  valid_in,   
    input  wire                  PRECISION,  
    output wire  [15:0]          twiddle_out
);

    reg [ADDR_WIDTH-1:0] scaled_k;
    always @(*) begin
        case (n)
            1024: scaled_k = k;
            512:  scaled_k = {k, 1'b0};       
            256:  scaled_k = {k, 2'b00};      
            128:  scaled_k = {k, 3'b000};
            64:   scaled_k = {k, 4'b0000};    
            32:   scaled_k = {k, 5'b00000};
            16:   scaled_k = {k, 6'b000000};  
            8:    scaled_k = {k, 7'b0000000};
            4:    scaled_k = {k, 8'b00000000};
            2:    scaled_k = {k, 9'b000000000};
            default: scaled_k = 11'd0;
        endcase
    end

    localparam W = 16;
    localparam signed [W-1:0] CORDIC_GAIN = 16'h26DD;

    wire signed [W-1:0] atan_table [0:9];
    assign atan_table[0] = 16'h3243; 
    assign atan_table[1] = 16'h1DAC; 
    assign atan_table[2] = 16'h0FAD;
    assign atan_table[3] = 16'h07F5; 
    assign atan_table[4] = 16'h03FE; 
    assign atan_table[5] = 16'h01FF; 
    assign atan_table[6] = 16'h00FF; 
    assign atan_table[7] = 16'h007F;
    assign atan_table[8] = 16'h003F; 
    assign atan_table[9] = 16'h001F; 

    reg signed [W-1:0] k_signed;
    reg signed [W-1:0] x_start;
    reg signed [W-1:0] y_start;
    reg signed [W-1:0] z_start;

    always @(*) begin
        k_signed = {5'b0, scaled_k};
        
        // Use 3217 >>> 5 to approximate 100.53 without integer drift
        if (scaled_k < 256) begin
            x_start = CORDIC_GAIN;
            y_start = 16'd0;
            z_start = -((k_signed * 32'sd3217) >>> 5); 
        end else if (scaled_k < 768) begin
            x_start = -CORDIC_GAIN;
            y_start = 16'd0;
            z_start = -(((k_signed - 16'sd512) * 32'sd3217) >>> 5);
        end else begin
            x_start = CORDIC_GAIN;
            y_start = 16'd0;
            z_start = -(((k_signed - 16'sd1024) * 32'sd3217) >>> 5);
        end
    end
    
    reg signed [W-1:0] x_pipe [0:LATENCY];
    reg signed [W-1:0] y_pipe [0:LATENCY];
    reg signed [W-1:0] z_pipe [0:LATENCY];
    (* srl_style = "srl" *) reg v_pipe [0:LATENCY]; 
    (* srl_style = "srl" *) reg p_pipe [0:LATENCY]; 
    (* srl_style = "srl" *) reg [ADDR_WIDTH-1:0] scaled_k_pipe [0:LATENCY];

    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            x_pipe[0] <= 0;
            y_pipe[0] <= 0;
            z_pipe[0] <= 0;
            v_pipe[0] <= 0;
            p_pipe[0] <= 0;
            scaled_k_pipe[0] <= 0;
        end else begin
            x_pipe[0] <= x_start; 
            y_pipe[0] <= y_start;
            z_pipe[0] <= z_start; 
            v_pipe[0] <= valid_in;
            p_pipe[0] <= PRECISION; 
            scaled_k_pipe[0] <= scaled_k;
        end
    end

    genvar i;
    generate
        for (i = 0; i < LATENCY; i = i + 1) begin : cordic_stages
            wire d = z_pipe[i][W-1];

            wire signed [W-1:0] x_shifted = x_pipe[i] >>> i;
            wire signed [W-1:0] y_shifted = y_pipe[i] >>> i;

            wire signed [W-1:0] next_x = d ? (x_pipe[i] + y_shifted) : (x_pipe[i] - y_shifted);
            wire signed [W-1:0] next_y = d ? (y_pipe[i] - x_shifted) : (y_pipe[i] + x_shifted);
            wire signed [W-1:0] next_z = d ? (z_pipe[i] + atan_table[i]) : (z_pipe[i] - atan_table[i]);

            always @(posedge clk or negedge rst) begin
                if (!rst) begin
                    x_pipe[i+1] <= 0;
                    y_pipe[i+1] <= 0;
                    z_pipe[i+1] <= 0;
                    v_pipe[i+1] <= 0;
                    p_pipe[i+1] <= 0;
                    scaled_k_pipe[i+1] <= 0;
                end else begin
                    x_pipe[i+1] <= next_x;
                    y_pipe[i+1] <= next_y;
                    z_pipe[i+1] <= next_z;
                    v_pipe[i+1] <= v_pipe[i];
                    p_pipe[i+1] <= p_pipe[i];
                    scaled_k_pipe[i+1] <= scaled_k_pipe[i];
                end
            end
        end
    endgenerate

    wire signed [15:0] final_cos_cordic = x_pipe[LATENCY];
    wire signed [15:0] final_sin_cordic = y_pipe[LATENCY];

    // EXACT CARDINAL BYPASS (Restores impulse cancellation)
    wire signed [15:0] final_cos = 
        (scaled_k_pipe[LATENCY] == 11'd0)   ?  16'h4000 : //  1.0
        (scaled_k_pipe[LATENCY] == 11'd256) ?  16'h0000 : //  0.0
        (scaled_k_pipe[LATENCY] == 11'd512) ? -16'sd16384 : // -1.0
        (scaled_k_pipe[LATENCY] == 11'd768) ?  16'h0000 : //  0.0
        final_cos_cordic;

    wire signed [15:0] final_sin = 
        (scaled_k_pipe[LATENCY] == 11'd0)   ?  16'h0000 : //  0.0
        (scaled_k_pipe[LATENCY] == 11'd256) ? -16'sd16384 : // -1.0
        (scaled_k_pipe[LATENCY] == 11'd512) ?  16'h0000 : //  0.0
        (scaled_k_pipe[LATENCY] == 11'd768) ?  16'h4000 : //  1.0
        final_sin_cordic;

    wire [7:0] cos_fp8, sin_fp8;
    wire [3:0] cos_fp4, sin_fp4;

    q2_14_to_fp8_e4m3 conv_cos_8 (.fixed_in(final_cos), .fp_out(cos_fp8));
    q2_14_to_fp8_e4m3 conv_sin_8 (.fixed_in(final_sin), .fp_out(sin_fp8));
    q2_14_to_fp4_e2m1 conv_cos_4 (.fixed_in(final_cos), .fp_out(cos_fp4));
    q2_14_to_fp4_e2m1 conv_sin_4 (.fixed_in(final_sin), .fp_out(sin_fp4));

    assign twiddle_out = (v_pipe[LATENCY]) ?
                         ((p_pipe[LATENCY] == 1'b1) ? {cos_fp8, sin_fp8} : {8'h00, cos_fp4, sin_fp4}) : 
                         16'h0000;

endmodule