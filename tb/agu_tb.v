// =============================================================================
// Testbench for Multi-Length Pipelined Streaming AGU Verification Suite
// =============================================================================
`timescale 1ns/1ps

module dit_fft_agu_streaming_multi_tb;

    parameter MAX_N = 1024;
    parameter ADDR_WIDTH = 11;

    // UUT Inputs
    reg clk;
    reg reset;
    reg start;
    reg [ADDR_WIDTH-1:0] N;

    // UUT Outputs
    wire                  stream_en;
    wire [ADDR_WIDTH-1:0] idx_a;
    wire [ADDR_WIDTH-1:0] idx_b;
    wire [ADDR_WIDTH-1:0] k;
    wire                  done_stage;
    wire                  done_fft;
    wire [ADDR_WIDTH-1:0] curr_stage;

    // Instantiate the Unit Under Test (UUT)
    dit_fft_agu_streaming #(
        .MAX_N(MAX_N),
        .ADDR_WIDTH(ADDR_WIDTH)
    ) uut (
        .clk(clk),
        .reset(reset),
        .start(start),
        .N(N),
        .stream_en(stream_en),
        .idx_a(idx_a),
        .idx_b(idx_b),
        .k(k),
        .done_stage(done_stage),
        .done_fft(done_fft),
        .curr_stage(curr_stage)
    );

    // Clock Generator (100 MHz / 10ns period)
    always #5 clk = ~clk;

    // Dynamic Test Bench Environment Control Channels
    reg [ADDR_WIDTH-1:0] current_test_n;
    reg [ADDR_WIDTH-1:0] current_bf_per_stage;
    integer              cycle_count;
    integer              expected_total_cycles;
    reg                  error_detected;
    reg                  test_running;

    // =========================================================================
    // MAIN VERIFICATION SUITE AUTOMATION LOOP
    // =========================================================================
    initial begin
        // Initialize Core System Pins
        clk            = 0;
        reset          = 0;
        start          = 0;
        N              = 0;
        error_detected = 0;
        test_running   = 0;

        // Hold Reset 
        #15;
        reset = 1;
        #25;

        $display("=================================================================");
        $display("STARTING PIPELINED AGU MULTI-LENGTH PERFORMANCE SUITE");
        $display("=================================================================");

        // Run sequential check passes across varying spectrum widths
        // Arguments: run_fft_test(N_size, log2_N_stages);
        run_fft_test(8,    3);   // 4 bflies/stage * 3 stages = 12 cycles
        run_fft_test(16,   4);   // 8 bflies/stage * 4 stages = 32 cycles
        run_fft_test(64,   6);   // 32 bflies/stage * 6 stages = 192 cycles
        run_fft_test(1024, 10);  // 512 bflies/stage * 10 stages = 5,120 cycles

        // Global Evaluation Reporting
        $display("\n=================================================================");
        if (!error_detected) begin
            $display("SUCCESS: ALL REGRESSION TEST FFT CONFIGURATIONS PASSED!");
        end else begin
            $display("ERROR: LOG PARSING REVEALED FAULTS DURING STREAM FLOWS.");
        end
        $display("=================================================================");
        $finish;
    end

    // =========================================================================
    // ENCAPSULATION TASK: RE-USABLE RUN ENGINE
    // =========================================================================
    task run_fft_test;
        input [ADDR_WIDTH-1:0] test_size;
        input [ADDR_WIDTH-1:0] num_stages;
        begin
            $display("\n[RUNNING TEST] Configuring AGU for N = %0d (%0d Stages)", test_size, num_stages);
            $display("Cycle | Stage | Port A | Port B | Twiddle (k) | DoneStage | DoneFFT");
            $display("-----------------------------------------------------------------");

            // Load tracking profiles into context registers
            current_test_n      = test_size;
            current_bf_per_stage = test_size >> 1;
            cycle_count         = 0;
            expected_total_cycles = (test_size >> 1) * num_stages;
            
            N            = test_size;
            test_running = 1;

            @(posedge clk);
            start = 1'b1; // Trigger AGU engine initialization
            @(posedge clk);
            start = 1'b0;

            // Stall execution context thread until combinational hardware flags resolve
            @(posedge done_fft);
            @(posedge clk); // Allow pipeline flush step
            test_running = 0;
            #10;
        end
    endtask

    // =========================================================================
    // INLINE STREAM INSPECTION ENGINE (Evaluates on every active cycle)
    // =========================================================================
    always @(posedge clk) begin
        if (stream_en && test_running) begin
            cycle_count = cycle_count + 1;
            
            // Smart Filter: Always print logs for small tests. 
            // For large tests (like N=1024), only print the first 4 cycles, last 4 cycles, and stage boundaries.
            if (current_test_n <= 16 || cycle_count <= 4 || cycle_count >= expected_total_cycles - 3 || done_stage) begin
                $display("%5d | %5d | %6d | %6d | %11d | %9b | %7b", 
                         cycle_count, curr_stage, idx_a, idx_b, k, done_stage, done_fft);
            end else if (cycle_count == 5) begin
                $display("... [Streaming Cycles 5 to %0d Compressed] ...", expected_total_cycles - 4);
            end

            // Assert 1: Core Address Collisions Check
            if (idx_a == idx_b) begin
                $display("  [CRITICAL ERROR] Cycle %0d: Bus collision detected! idx_a == idx_b (%0d)", cycle_count, idx_a);
                error_detected = 1;
            end

            // Assert 2: Stage Transition Boundary Alignment Check
            if (done_stage && (cycle_count % current_bf_per_stage != 0)) begin
                $display("  [CRITICAL ERROR] Cycle %0d: done_stage flag split sequence out of alignment bounds!", cycle_count);
                error_detected = 1;
            end

            // Assert 3: FFT Termination Alignment Check
            if (done_fft && (cycle_count != expected_total_cycles)) begin
                $display("  [CRITICAL ERROR] Cycle %0d: done_fft fired prematurely before complete matrix scan completed!", cycle_count);
                error_detected = 1;
            end
        end
    end

endmodule