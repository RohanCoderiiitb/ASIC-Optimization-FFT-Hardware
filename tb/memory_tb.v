// =============================================================================
// Testbench for Mixed-Precision Concurrent FFT Memory Subsystem
// =============================================================================
`timescale 1ns/1ps

module mixed_dual_bank_memory_concurrent_tb;

    parameter N = 16; // Using a smaller N=16 for clear tracking, scales identically to 1024
    parameter ADDR_WIDTH = 11;

    // Clock & Reset
    reg clk;
    reg rst;

    // Control Signals
    reg                  bank_pingpong;
    reg [ADDR_WIDTH-1:0] stage_mask;

    // Read Interface
    reg [ADDR_WIDTH-1:0] rd_addr_a;
    reg [ADDR_WIDTH-1:0] rd_addr_b;
    reg                  rd_precision;
    wire [15:0]          rd_data_a;
    wire [15:0]          rd_data_b;

    // Write Interface
    reg                  wr_en;
    reg [ADDR_WIDTH-1:0] wr_addr_a;
    reg [ADDR_WIDTH-1:0] wr_addr_b;
    reg [23:0]           wr_data_a;
    reg [23:0]           wr_data_b;

    // Instantiate UUT (Unit Under Test)
    mixed_dual_bank_memory_concurrent #(
        .n(N),
        .ADDR_WIDTH(ADDR_WIDTH)
    ) uut (
        .clk(clk),
        .rst(rst),
        .bank_pingpong(bank_pingpong),
        .stage_mask(stage_mask),
        .rd_addr_a(rd_addr_a),
        .rd_addr_b(rd_addr_b),
        .rd_precision(rd_precision),
        .rd_data_a(rd_data_a),
        .rd_data_b(rd_data_b),
        .wr_en(wr_en),
        .wr_addr_a(wr_addr_a),
        .wr_addr_b(wr_addr_b),
        .wr_data_a(wr_data_a),
        .wr_data_b(wr_data_b)
    );

    // Clock Generator (100MHz / 10ns period)
    always #5 clk = ~clk;

    // Verification Variables
    integer i;
    reg error_flag;

    initial begin
        // Initialize Inputs
        clk = 0;
        rst = 0;
        bank_pingpong = 0;
        stage_mask = 11'd1; // Emulating FFT Stage 0 (Bit 0 determines sub-banking)
        rd_addr_a = 0;
        rd_addr_b = 0;
        rd_precision = 1;   // Defaulting to FP8 verification 
        wr_en = 0;
        wr_addr_a = 0;
        wr_addr_b = 0;
        wr_data_a = 24'b0;
        wr_data_b = 24'b0;
        error_flag = 0;

        // Release Reset after 2 cycles
        #20;
        rst = 1;
        #10;

        $display("=================================================================");
        $display("STARTING CONCURRENT MEMORY SYSTEM VERIFICATION TESTBENCH");
        $display("=================================================================");

        // ---------------------------------------------------------------------
        // TEST CASE 1: Concurrent Write & Read to Opposite Sub-banks (Conflict-Free)
        // At Stage 0 (mask=1), elements 0 (even) and 1 (odd) map to separate sub-banks.
        // ---------------------------------------------------------------------
        $display("[TC1] Testing concurrent sub-bank write capabilities...");
        @(posedge clk);
        bank_pingpong = 1'b0; // Reading Bank 0 / Writing Bank 1
        stage_mask    = 11'h001;
        wr_en         = 1'b1;
        
        // Write dual paired indexes simultaneously 
        wr_addr_a     = 11'd0; // Targets Bank 1, Sub-bank 0 (Even)
        wr_data_a     = 24'hAA_BB_01; 
        
        wr_addr_b     = 11'd1; // Targets Bank 1, Sub-bank 1 (Odd)
        wr_data_b     = 24'hCC_DD_02;

        @(posedge clk);
        // Turn off write enable
        wr_en = 1'b0;
        
        // Now read back what we just wrote to Bank 1. 
        // We flip bank_pingpong to 1'b1 so execution reads from Bank 1.
        $display("[TC1] Verifying low-overhead 2-cycle pipelined read out...");
        bank_pingpong = 1'b1; 
        rd_precision  = 1'b1;  // Pulling FP8 slice [23:8]
        rd_addr_a     = 11'd0;
        rd_addr_b     = 11'd1;

        // BRAM Read Latency Step 1
        @(posedge clk);
        // BRAM Read Latency Step 2 (Data reaches output registers)
        @(posedge clk);
        #1; // Minor delta delay to catch stabilized lines

        if (rd_data_a === 16'hAABB && rd_data_b === 16'hCCDD) begin
            $display("TC1 PASSED: Successfully wrote and read concurrent sub-banks simultaneously!");
        end else begin
            $display("TC1 FAILED: Expected A: AABB (Got %h), B: CCDD (Got %h)", rd_data_a, rd_data_b);
            error_flag = 1;
        end

        // ---------------------------------------------------------------------
        // TEST CASE 2: Precision Slicing (FP4 Mode Zero-Extension Validation)
        // ---------------------------------------------------------------------
        $display("[TC2] Testing sub-word precision mapping down-converter...");
        @(posedge clk);
        bank_pingpong = 1'b0; // Back to writing Bank 1
        wr_en         = 1'b1;
        wr_addr_a     = 11'd4; // Even
        wr_data_a     = 24'hFF_FF_E5; // E5 is in the lower FP4 slot
        wr_addr_b     = 11'd5; // Odd
        wr_data_b     = 24'hFF_FF_F6; // F6 is in the lower FP4 slot

        @(posedge clk);
        wr_en = 1'b0;
        
        // Read back using FP4 mode (rd_precision = 0)
        bank_pingpong = 1'b1;
        rd_precision  = 1'b0; // FP4 configuration extraction
        rd_addr_a     = 11'd4;
        rd_addr_b     = 11'd5;

        @ (posedge clk); // Latency 1
        @ (posedge clk); // Latency 2
        #1;

        if (rd_data_a === 16'h00E5 && rd_data_b === 16'h00F6) begin
            $display("TC2 PASSED: FP4 Sub-word extraction and Zero-extension works perfectly!");
        end else begin
            $display("TC2 FAILED: Expected A: 00E5 (Got %h), B: 00F6 (Got %h)", rd_data_a, rd_data_b);
            error_flag = 1;
        end

        // ---------------------------------------------------------------------
        // TEST CASE 3: Higher Stage Addressing Matrix Validation
        // For Stage 2, mask = 4 (binary 0100). Bit 2 becomes the sub-bank router.
        // Addresses 0 (0000) and 4 (0100) have different Bit 2 values!
        // ---------------------------------------------------------------------
        $display("[TC3] Testing complex address packing at a higher FFT stage (Stage 2)...");
        @(posedge clk);
        bank_pingpong = 1'b0;
        stage_mask    = 11'd4; // Stage 2 mask
        wr_en         = 1'b1;
        wr_addr_a     = 11'd0; // Bit 2 is 0 -> Sub-bank 0
        wr_data_a     = 24'h11_22_33;
        wr_addr_b     = 11'd4; // Bit 2 is 1 -> Sub-bank 1
        wr_data_b     = 24'h44_55_66;

        @(posedge clk);
        wr_en = 1'b0;

        bank_pingpong = 1'b1;
        rd_precision  = 1'b1;
        rd_addr_a     = 11'd0;
        rd_addr_b     = 11'd4;

        @(posedge clk);
        @(posedge clk);
        #1;

        if (rd_data_a === 16'h1122 && rd_data_b === 16'h4455) begin
            $display("TC3 PASSED: Stage mask address compression works flawlessly at higher stages!");
        end else begin
            $display("TC3 FAILED: Address compression routing error at higher stage indices.");
            error_flag = 1;
        end

        // Final Summary
        $display("=================================================================");
        if (error_flag == 0) begin
            $display("SUCCESS: ALL MEMORY SUB-SYSTEM TESTS PASSED UNIT TESTS!");
        end else begin
            $display("ERROR: MEMORY SUB-SYSTEM FAILED SIMULATION LOG ANALYSIS.");
        end
        $display("=================================================================");
        $finish;
    end

endmodule