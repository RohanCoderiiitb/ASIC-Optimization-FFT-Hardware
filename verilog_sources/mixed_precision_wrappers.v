module butterfly_wrapper (
    input  [23:0] A, B,       
    input  [15:0] W,          
    input         mult_prec,  
    input         add_prec,   
    output [15:0] X, Y,    
    output        output_is_fp8
);
    wire [15:0] A_fp8 = A[23:8];
    wire [7:0]  A_fp4 = A[7:0];
    wire [15:0] B_fp8 = B[23:8];
    wire [7:0]  B_fp4 = B[7:0];

    wire [7:0] wb_prod_fp4;
    fp4_cmul cmul_fp4(
        .a(B_fp4[7:4]), .b(B_fp4[3:0]),
        .c(W[7:4]),     .d(W[3:0]),
        .out_real(wb_prod_fp4[7:4]), .out_imag(wb_prod_fp4[3:0])
    );

    wire [15:0] wb_prod_fp8;
    fp8_cmul cmul_fp8(
        .a(B_fp8[15:8]), .b(B_fp8[7:0]),
        .c(W[15:8]),     .d(W[7:0]),
        .out_real(wb_prod_fp8[15:8]), .out_imag(wb_prod_fp8[7:0])
    );

    wire [7:0]  wb_prod_fp8_as_fp4;
    complex_fp8_to_fp4 conv_wb84(.complex_fp8(wb_prod_fp8), .complex_fp4(wb_prod_fp8_as_fp4));

    wire [15:0] wb_prod_fp4_as_fp8;
    complex_fp4_to_fp8 conv_wb48(.complex_fp4(wb_prod_fp4), .complex_fp8(wb_prod_fp4_as_fp8));

    wire [7:0]  add_B_fp4 = mult_prec ? wb_prod_fp8_as_fp4 : wb_prod_fp4;
    wire [15:0] add_B_fp8 = mult_prec ? wb_prod_fp8 : wb_prod_fp4_as_fp8;

    wire [7:0] X_fp4, Y_fp4;
    fp4_complex_add_sub add_fp4(.a(A_fp4), .b(add_B_fp4), .sub(1'b0), .out(X_fp4));
    fp4_complex_add_sub sub_fp4(.a(A_fp4), .b(add_B_fp4), .sub(1'b1), .out(Y_fp4));

    wire [15:0] X_fp8, Y_fp8;
    fp8_complex_add_sub add_fp8(.a(A_fp8), .b(add_B_fp8), .sub(1'b0), .out(X_fp8));
    fp8_complex_add_sub sub_fp8(.a(A_fp8), .b(add_B_fp8), .sub(1'b1), .out(Y_fp8));

    assign X = add_prec ? X_fp8 : {8'h00, X_fp4};
    assign Y = add_prec ? Y_fp8 : {8'h00, Y_fp4};
    assign output_is_fp8 = add_prec;
endmodule

module cmul_wrapper #(
    parameter PRECISION = 0 
)(
    input  [7:0] a,
    input  [7:0] b,
    input  [7:0] c,
    input  [7:0] d,
    output [7:0] out_real,
    output [7:0] out_imag
);
    generate
        if (PRECISION == 0) begin : USE_FP4
            wire [3:0] r4, i4;
            fp4_cmul inst_fp4 (
                .a(a[3:0]),
                .b(b[3:0]),
                .c(c[3:0]),
                .d(d[3:0]),
                .out_real(r4),
                .out_imag(i4)
            );
            assign out_real = {4'b0000, r4};
            assign out_imag = {4'b0000, i4};
        end else begin : USE_FP8
            fp8_cmul inst_fp8 (
                .a(a),
                .b(b),
                .c(c),
                .d(d),
                .out_real(out_real),
                .out_imag(out_imag)
            );
        end
    endgenerate
endmodule