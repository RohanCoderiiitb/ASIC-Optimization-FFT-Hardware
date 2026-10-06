// =============================================================================
// picorv32_fft_soc.v
//
// Minimal SoC: PicoRV32 (PCPI enabled, custom-0 routed to fft_pcpi_wrapper) +
// a single unified byte-addressable memory for both instructions and data.
// This is a simulation-only integration target (Milestone 1+3+6 combined, kept
// deliberately minimal) -- not a Vivado-ready memory map, just enough to prove
// the PCPI handshake and FFT data path end-to-end in iverilog before investing
// in real IMEM/DMEM BRAM wrappers and a linker script.
// =============================================================================
`timescale 1ns/1ps

module picorv32_fft_soc #(
    parameter MEM_WORDS = 4096   // 16KB unified memory
)(
    input wire clk,
    input wire resetn
);

    // -------------------------------------------------------------------
    // PicoRV32 core <-> memory
    // -------------------------------------------------------------------
    wire        mem_valid;
    wire        mem_instr;
    reg         mem_ready;
    wire [31:0] mem_addr;
    wire [31:0] mem_wdata;
    wire [ 3:0] mem_wstrb;
    reg  [31:0] mem_rdata;

    // -------------------------------------------------------------------
    // PicoRV32 core <-> PCPI wrapper
    // -------------------------------------------------------------------
    wire        pcpi_valid;
    wire [31:0] pcpi_insn;
    wire [31:0] pcpi_rs1;
    wire [31:0] pcpi_rs2;
    wire        pcpi_wr;
    wire [31:0] pcpi_rd;
    wire        pcpi_wait;
    wire        pcpi_ready;

    picorv32 #(
        .ENABLE_PCPI    (1),
        .ENABLE_MUL     (0),
        .ENABLE_DIV     (0),
        .ENABLE_IRQ     (0),
        .BARREL_SHIFTER (1),
        .CATCH_ILLINSN  (1),
        .CATCH_MISALIGN (1),
        .PROGADDR_RESET (32'h0000_0000),
        .STACKADDR      (32'h0000_3FF0)   // top of the 16KB memory, word-aligned
    ) u_cpu (
        .clk        (clk),
        .resetn     (resetn),
        .trap       (),

        .mem_valid  (mem_valid),
        .mem_instr  (mem_instr),
        .mem_ready  (mem_ready),
        .mem_addr   (mem_addr),
        .mem_wdata  (mem_wdata),
        .mem_wstrb  (mem_wstrb),
        .mem_rdata  (mem_rdata),

        .mem_la_read  (),
        .mem_la_write (),
        .mem_la_addr  (),
        .mem_la_wdata (),
        .mem_la_wstrb (),

        .pcpi_valid (pcpi_valid),
        .pcpi_insn  (pcpi_insn),
        .pcpi_rs1   (pcpi_rs1),
        .pcpi_rs2   (pcpi_rs2),
        .pcpi_wr    (pcpi_wr),
        .pcpi_rd    (pcpi_rd),
        .pcpi_wait  (pcpi_wait),
        .pcpi_ready (pcpi_ready),

        .irq        (32'b0),
        .eoi        (),

        .trace_valid(),
        .trace_data ()
    );

    fft_pcpi_wrapper u_fft_wrapper (
        .clk        (clk),
        .rst_n      (resetn),
        .pcpi_valid (pcpi_valid),
        .pcpi_insn  (pcpi_insn),
        .pcpi_rs1   (pcpi_rs1),
        .pcpi_rs2   (pcpi_rs2),
        .pcpi_wr    (pcpi_wr),
        .pcpi_rd    (pcpi_rd),
        .pcpi_wait  (pcpi_wait),
        .pcpi_ready (pcpi_ready)
    );

    // -------------------------------------------------------------------
    // Unified memory, single-cycle, zero-wait-state.
    // -------------------------------------------------------------------
    reg [31:0] mem [0:MEM_WORDS-1];

    initial mem_ready = 1'b0;

    wire [$clog2(MEM_WORDS)-1:0] word_addr = mem_addr[$clog2(MEM_WORDS)+1:2];

    always @(posedge clk) begin
        mem_ready <= 1'b0;
        if (mem_valid && !mem_ready) begin
            mem_ready <= 1'b1;
            mem_rdata <= mem[word_addr];
            if (mem_wstrb[0]) mem[word_addr][ 7: 0] <= mem_wdata[ 7: 0];
            if (mem_wstrb[1]) mem[word_addr][15: 8] <= mem_wdata[15: 8];
            if (mem_wstrb[2]) mem[word_addr][23:16] <= mem_wdata[23:16];
            if (mem_wstrb[3]) mem[word_addr][31:24] <= mem_wdata[31:24];
        end
    end

    // Preload from a hex file (one 32-bit word per line) -- set via +firmware=
    // plusarg or defaults to firmware.hex in the sim run directory.
    integer i;
    initial begin
        for (i = 0; i < MEM_WORDS; i = i + 1)
            mem[i] = 32'h0000_0013; // NOP (addi x0,x0,0) filler
    end

endmodule
