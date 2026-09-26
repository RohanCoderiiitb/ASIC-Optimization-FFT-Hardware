// =============================================================================
// FP32 Baseline FFT TOP - 64-point PIPELINED CONFIGURATION
//
// Same interface shape as mixed_fft_64_top, with 64-bit complex FP32 load /
// unload words in place of the 16-bit mixed-precision words.  No format
// conversion on load: the baseline stores exactly what it is given.
// =============================================================================
`timescale 1ns/1ps

module fp32_fft_64_top (
    input  wire        clk,
    input  wire        rst,

    input  wire        start,
    output reg         done,

    input  wire              load_en,
    input  wire [5:0]  load_addr,
    input  wire [63:0]       load_data,

    input  wire              unload_en,
    input  wire [5:0]  unload_addr,
    output wire [63:0]       unload_data
);

    wire [10:0] load_addr_rev;

    bit_reverse #(
        .MAX_N(1024),
        .WIDTH(11)
    ) br (
        .in  ({5'b00000, load_addr}),
        .N   (11'd64),
        .out (load_addr_rev)
    );

    reg bank_sel;
    wire core_done;
    wire [63:0] core_rd_data;

    fp32_fft_64_core #(
        .MAX_N     (1024),
        .ADDR_WIDTH(11)
    ) core (
        .clk          (clk),
        .rst          (rst),
        .start        (start),
        .done         (core_done),

        .ext_wr_en    (load_en),
        .ext_wr_addr  (load_addr_rev),
        .ext_wr_data  (load_data),

        .ext_reading  (unload_en),
        .ext_rd_addr  ({5'b00000, unload_addr}),
        .ext_rd_data  (core_rd_data),

        .ext_bank_sel (bank_sel)
    );

    assign unload_data = core_rd_data;

    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            done     <= 1'b0;
            bank_sel <= 1'b1;
        end else begin
            if (load_en && !start)
                bank_sel <= 1'b1;

            if (start) begin
                done     <= 1'b0;
                bank_sel <= 1'b1;
            end else if (core_done) begin
                done     <= 1'b1;
                bank_sel <= 1'b1;   // bank holding the result (log2 N = 6)
            end else if (!start && done) begin
                done <= 1'b0;
            end
        end
    end
endmodule
