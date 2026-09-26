module ntt_mod_add_sub #(
    parameter Q = 15'd12289
)(
    input  [13:0] a,
    input  [13:0] b,
    input         sub,
    output [13:0] out
);
    wire [14:0] sum = a + b;
    wire [13:0] add_out = (sum >= Q) ? (sum - Q) : sum[13:0];
    
    wire [13:0] sub_out = (a >= b) ? (a - b) : (a + Q[13:0] - b);
    
    assign out = sub ? sub_out : add_out;
endmodule

module ntt_mod_mul #(
    parameter Q = 14'd12289
)(
    input [13:0] a,
    input [13:0] b,
    output [13:0] out
);
    // Step 1: Multiplying two 14 bit integers
    wire [27:0] product = a*b;
    
    // Step 2: Barrett Reduction
    // Quotient = floor(Z/Q)
    // Remainder = Z - (Q * quotient)
    // 1/Q needs to be written approximately as m/2^k such that m = floor(2^k/Q). Then Quotient = floor(Z*m/2^k)
    // k is such that it is larger than the maximum value of z. Here maximum value of product is 12289^2, so k can be 28. Then m = 21844
    wire [42:0] q = product*15'd21843;
    // Shift right by 28 bits
    wire [14:0] Quotient = q[42:28];
    wire [28:0] sub = {Quotient, 13'b0} + {Quotient, 12'b0} + Quotient;
    wire [14:0] r = product - sub;
    assign out = (r >= 15'd12289) ? (r - 15'd12289) : r[13:0];
endmodule

module ntt_butterfly_unit(
    input [13:0] A,
    input [13:0] B,
    input [13:0] W,
    output [13:0] X,
    output [13:0] Y
);
    wire [13:0] BW;
    ntt_mod_mul mult_inst(.a(B), .b(W), .out(BW));
    ntt_mod_add_sub add_inst(.a(A), .b(BW), .sub(1'b0), .out(X));
    ntt_mod_add_sub sub_inst(.a(A), .b(BW), .sub(1'b1), .out(Y));
endmodule

module ntt_mod_mul_pipelined #(
    parameter Q = 14'd12289
)(
    input clk,
    input resetn,
    input [13:0] a,
    input [13:0] b,
    output reg [13:0] out
);
    // Step 1: Multiplication of 14 bit integers
    reg [27:0] prod_step1;
    always @(posedge clk or negedge resetn) begin
        if(!resetn) prod_step1 <= 28'd0;
        else prod_step1 <= a*b;
    end

    // Step 2: Barrett Reduction
    // Step 2a: Multiplication with m/2^k
    // Step 2b: Shifting, subtraction, modulo check
    reg [42:0] prod_step2;
    reg [42:0] q_step2;
    always @(posedge clk or negedge resetn) begin
        if(!resetn) begin 
            prod_step2 <= 43'd0;
            q_step2  <= 43'd0;
        end
        else begin
            q_step2 <= prod_step1 * 15'd21843;
            prod_step2 <= prod_step1;
        end
    end
    wire [14:0] Quotient = q_step2[42:28];
    wire [28:0] sub = {Quotient, 13'b0} + {Quotient, 12'b0} + Quotient;
    wire [14:0] r = prod_step2 - sub;
    always @(posedge clk or negedge resetn) begin
        if(!resetn) out <= 14'd0;
        else out <= (r >= 15'd12289) ? (r-15'd12289) : r[13:0];
    end
endmodule

module ntt_butterfly_unit_pipelined #(
    parameter Q = 14'd12289
)(
    input clk,
    input resetn,
    input [13:0] A,
    input [13:0] B,
    input [13:0] W,
    output reg [13:0] X,
    output reg [13:0] Y
);
    wire [13:0] BW;
    ntt_mod_mul_pipelined mult_inst(
        .clk(clk),
        .resetn(resetn),
        .a(B),
        .b(W),
        .out(BW)
    );
    reg [13:0] A1, A2, A3;
    always @(posedge clk or negedge resetn) begin
        if(!resetn) begin
            A1 <= 14'd0;
            A2 <= 14'd0;
            A3 <= 14'd0;
        end
        else begin
            A1 <= A;
            A2 <= A1;
            A3 <= A2;
        end
    end
    wire [13:0] X_out, Y_out;
    ntt_mod_add_sub add_inst(.a(A3), .b(BW), .sub(1'b0), .out(X_out));
    ntt_mod_add_sub sub_inst(.a(A3), .b(BW), .sub(1'b1), .out(Y_out));
    always @(posedge clk or negedge resetn) begin
        if(!resetn) begin
            X <= 14'd0;
            Y <= 14'd0;
        end
        else begin
            X <= X_out;
            Y <= Y_out;
        end
    end

endmodule