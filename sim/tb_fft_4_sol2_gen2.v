// Auto-generated testbench for fft_4_sol2_gen2_top
// Memory lives in the TB, but TOP/CORE controls everything (load/unload/arbitration).
`timescale 1ns/1ps

module tb_fft_4_sol2_gen2;

    reg        clk;
    reg        rst;
    reg        start;
    wire       done;

    reg        load_en;
    reg  [1:0]  load_addr;
    reg  [15:0] load_data;

    reg        unload_en;
    reg  [1:0]  unload_addr;
    wire [15:0] unload_data;

    // ── External RAM Interface (Driven by TOP) ─────────────────────────────
    wire                  mem_bank_sel;
    wire [11-1:0]       mem_rd_addr;
    wire                  mem_rd_prec;
    reg  [15:0]           mem_rd_data;
    wire                  mem_wr_en;
    wire [11-1:0]       mem_wr_addr;
    wire [23:0]           mem_wr_data;

    integer i, ti, out_file;
    integer cycle_count;        // Cycles for the current FFT run
    integer total_cycles;       // Accumulated cycles across all test vectors
    integer load_cycles;        // Cycles for the current load phase
    integer unload_cycles_cnt;  // Cycles for the current unload phase

    // Test vector storage: fp8 packed {{real[7:0], imag[7:0]}}
    reg [15:0] tv [43:0];

    // DUT
    fft_4_sol2_gen2_top dut (
        .clk        (clk),
        .rst        (rst),
        .start      (start),
        .done       (done),
        .load_en    (load_en),
        .load_addr  (load_addr),
        .load_data  (load_data),
        .unload_en  (unload_en),
        .unload_addr(unload_addr),
        .unload_data(unload_data),
        // External RAM
        .mem_bank_sel (mem_bank_sel),
        .mem_rd_addr  (mem_rd_addr),
        .mem_rd_prec  (mem_rd_prec),
        .mem_rd_data  (mem_rd_data),
        .mem_wr_en    (mem_wr_en),
        .mem_wr_addr  (mem_wr_addr),
        .mem_wr_data  (mem_wr_data)
    );

    // ── Ping-Pong Memory Arrays (1024 deep) ────────────────────────────────
    reg [23:0] bank0 [0:1023];
    reg [23:0] bank1 [0:1023];

    // Read Logic (2-cycle latency)
    reg [23:0] active_rd_word_reg;
    reg        mem_rd_prec_reg;
    always @(posedge clk) begin
        // Stage 1: Synchronous read
        if (mem_bank_sel) 
            active_rd_word_reg <= bank1[mem_rd_addr];
        else              
            active_rd_word_reg <= bank0[mem_rd_addr];
            
        mem_rd_prec_reg <= mem_rd_prec;
    end
    always @(posedge clk) begin
        // Stage 2: Precision formatting
        if (mem_rd_prec_reg) 
            mem_rd_data <= active_rd_word_reg[23:8];
        else             
            mem_rd_data <= {8'h00, active_rd_word_reg[7:0]};
    end

    // Write Logic 
    // Data is written to the OPPOSITE bank of what is being read
    always @(posedge clk) begin
        if (mem_wr_en) begin
            if (mem_bank_sel) 
                bank0[mem_wr_addr] <= mem_wr_data;
            else              
                bank1[mem_wr_addr] <= mem_wr_data;
        end
    end

    // 100 MHz clock
    initial clk = 0;
    always  #5 clk = ~clk;

    // Watchdog
    initial begin
        #51040;
        $display("WATCHDOG TIMEOUT for fft_4_sol2_gen2");
        $finish;
    end

    initial begin : STIM
        integer wait_cnt;

        // Pre-load test vectors
        tv[0] = 16'h3600;
        tv[1] = 16'h0000;
        tv[2] = 16'h0000;
        tv[3] = 16'h0000;
        tv[4] = 16'h3600;
        tv[5] = 16'h0000;
        tv[6] = 16'hb600;
        tv[7] = 16'h8000;
        tv[8] = 16'h3600;
        tv[9] = 16'h0036;
        tv[10] = 16'hb600;
        tv[11] = 16'h80b6;
        tv[12] = 16'h3600;
        tv[13] = 16'h3232;
        tv[14] = 16'hb600;
        tv[15] = 16'h3232;
        tv[16] = 16'h3600;
        tv[17] = 16'h0036;
        tv[18] = 16'hb600;
        tv[19] = 16'h80b6;
        tv[20] = 16'h3600;
        tv[21] = 16'h3600;
        tv[22] = 16'hb600;
        tv[23] = 16'hb600;
        tv[24] = 16'h0000;
        tv[25] = 16'h1f00;
        tv[26] = 16'h3600;
        tv[27] = 16'h1f00;
        tv[28] = 16'h0000;
        tv[29] = 16'h0036;
        tv[30] = 16'hb600;
        tv[31] = 16'h0000;
        tv[32] = 16'h3600;
        tv[33] = 16'h0035;
        tv[34] = 16'hb600;
        tv[35] = 16'h80b5;
        tv[36] = 16'h3600;
        tv[37] = 16'h3600;
        tv[38] = 16'h3600;
        tv[39] = 16'h3600;
        tv[40] = 16'h1c00;
        tv[41] = 16'h0036;
        tv[42] = 16'hb600;
        tv[43] = 16'h809c;

        // Open output file
        out_file = $fopen("/home/rohan/Documents/ASIC-Optimization-FFT-Hardware/sim/fft_4_sol2_gen2_output.txt", "w");

        // Initialise signals
        rst             = 0;
        start           = 0;
        load_en         = 0;
        load_addr       = 0;
        load_data       = 0;
        unload_en       = 0;
        unload_addr     = 0;
        total_cycles    = 0;

        // Hold reset for 8 cycles then release
        repeat(8) @(posedge clk);
        rst = 1;
        repeat(4) @(posedge clk);

        $display("\n================================================================");
        $display("  Clock-Cycle Report  --  fft_4_sol2_gen2  (FFT-4)");
        $display("================================================================");
        $display("  %-4s  %-10s  %-12s  %-13s  %-12s",
                 "Test", "Load cyc", "Compute cyc", "Unload cyc", "Total cyc");
        $display("----------------------------------------------------------------");

        // Run each test vector
        for (ti = 0; ti < 11; ti = ti + 1) begin

            // --- Load phase ---
            @(posedge clk);
            load_cycles = 0;
            load_en = 1;
            for (i = 0; i < 4; i = i + 1) begin
                load_addr = i[1:0];
                load_data = tv[ti*4 + i];
                @(posedge clk);
                load_cycles = load_cycles + 1;
            end
            load_en = 0;

            @(posedge clk);
            load_cycles = load_cycles + 1;

            // --- Run FFT (count compute cycles: start pulse → done asserted) ---
            cycle_count = 0;
            start = 1;
            @(posedge clk);
            start = 0;
            cycle_count = cycle_count + 1;

            // Wait for done — count every clock edge
            wait_cnt = 0;
            while (!done && wait_cnt < 1284) begin
                @(posedge clk);
                cycle_count = cycle_count + 1;
                wait_cnt    = wait_cnt    + 1;
            end
            if (!done)
                $display("WARN: done never asserted for test %0d, design fft_4_sol2_gen2", ti);

            @(posedge clk);

            // --- Unload phase ---
            // Memory has 2-cycle read latency.
            // For each sample: assert address, wait 3 posedge clk, sample data.
            unload_cycles_cnt = 0;
            unload_en = 1;
            for (i = 0; i < 4; i = i + 1) begin
                unload_addr = i[1:0];
                @(posedge clk); unload_cycles_cnt = unload_cycles_cnt + 1;
                @(posedge clk); unload_cycles_cnt = unload_cycles_cnt + 1;
                @(posedge clk); unload_cycles_cnt = unload_cycles_cnt + 1;
                $fwrite(out_file, "%04h\n", unload_data);
            end
            unload_en = 0;

            @(posedge clk);
            @(posedge clk);

            // --- Per-test cycle report ---
            $display("  %-4d  %-10d  %-12d  %-13d  %-12d",
                     ti,
                     load_cycles,
                     cycle_count,
                     unload_cycles_cnt,
                     load_cycles + cycle_count + unload_cycles_cnt);
            total_cycles = total_cycles + load_cycles + cycle_count + unload_cycles_cnt;

        end // for ti

        $display("----------------------------------------------------------------");
        $display("  Total cycles (all %0d tests)  : %0d", 11, total_cycles);
        $display("  Avg   cycles per FFT compute  : %0d", total_cycles / 11);
        $display("================================================================\n");

        $fclose(out_file);
        $display("Simulation complete for fft_4_sol2_gen2. Results in /home/rohan/Documents/ASIC-Optimization-FFT-Hardware/sim/fft_4_sol2_gen2_output.txt");
        $finish;
    end

endmodule
