`timescale 1ns/1ps
module fft_pcpi_wrapper (
    input clk, input rst_n, input pcpi_valid, input [31:0] pcpi_insn,
    input [31:0] pcpi_rs1, input [31:0] pcpi_rs2,
    output reg pcpi_wr, output reg [31:0] pcpi_rd, output reg pcpi_wait, output reg pcpi_ready
);
    wire is_custom0 = pcpi_valid && (pcpi_insn[6:0] == 7'b0001011);
    wire [2:0] f3 = pcpi_insn[14:12];

    reg [2:0] state;

    // 1-cycle pulses for load and start
    wire fft_load_en = is_custom0 && (f3 == 3'b001) && (state == 0) && !pcpi_ready;
    wire fft_start   = is_custom0 && (f3 == 3'b011) && (state == 0) && !pcpi_ready;
    
    // Multi-cycle hold for SRAM pipeline reads
    wire fft_unload_en = is_custom0 && (f3 == 3'b010) && !pcpi_ready;

    wire fft_done;
    wire [15:0] fft_unload_data;

    // --- DIAGNOSTIC TRACE (safe to remove once root cause is found) ---
    integer dbg_load_seq;
    integer dbg_store_seq;
    initial begin
        dbg_load_seq = 0;
        dbg_store_seq = 0;
    end
    always @(posedge clk) begin
        if (fft_load_en) begin
            $display("LOADEV %0d %0d %04h", dbg_load_seq, pcpi_rs2[7:0], pcpi_rs1[15:0]);
            dbg_load_seq = dbg_load_seq + 1;
        end
    end
    always @(posedge clk) begin
        if (is_custom0 && (f3 == 3'b010) && !pcpi_ready && (state == 3'd4)) begin
            $display("STOREV %0d %0d %04h", dbg_store_seq, pcpi_rs1[7:0], fft_unload_data);
            dbg_store_seq = dbg_store_seq + 1;
        end
    end
    // --- END DIAGNOSTIC TRACE ---

    mixed_fft_256_top u_fft (
        .clk(clk), .rst(rst_n), .start(fft_start), .done(fft_done),
        .load_en(fft_load_en), .load_addr(pcpi_rs2[7:0]), .load_data(pcpi_rs1[15:0]),
        .unload_en(fft_unload_en), .unload_addr(pcpi_rs1[7:0]), .unload_data(fft_unload_data)
    );

    reg status_done;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) status_done <= 0;
        else if (fft_start) status_done <= 0;
        else if (fft_done) status_done <= 1;
    end

    reg [31:0] timeout;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= 0; pcpi_ready <= 0; pcpi_wr <= 0; pcpi_rd <= 0; pcpi_wait <= 0;
        end else begin
            pcpi_ready <= 0; pcpi_wr <= 0; pcpi_wait <= 0;
            if (is_custom0 && !pcpi_ready) begin
                pcpi_wait <= 1;
                if (f3 == 3'b001 || f3 == 3'b010 || f3 == 3'b011) begin
                    if (state == 4) begin
                        pcpi_ready <= 1; pcpi_wait <= 0; state <= 0;
                        if (f3 == 3'b010) begin
                            pcpi_wr <= 1; pcpi_rd <= {16'b0, fft_unload_data};
                        end
                    end else begin
                        state <= state + 1;
                    end
                end else if (f3 == 3'b100) begin
                    if (state == 0) begin
                        timeout <= pcpi_rs1; state <= 1;
                    end else begin
                        if (status_done) begin
                            pcpi_ready <= 1; pcpi_wr <= 1; pcpi_rd <= 1; pcpi_wait <= 0; state <= 0;
                        end else if (timeout == 1) begin
                            pcpi_ready <= 1; pcpi_wr <= 1; pcpi_rd <= 0; pcpi_wait <= 0; state <= 0;
                        end else timeout <= timeout - 1;
                    end
                end else if (f3 == 3'b101) begin
                    pcpi_ready <= 1; pcpi_wr <= 1; pcpi_rd <= {31'b0, status_done}; pcpi_wait <= 0;
                end
            end
        end
    end
endmodule
