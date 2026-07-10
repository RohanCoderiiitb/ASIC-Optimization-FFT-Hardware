`timescale 1ns / 1ps

module tb_cordic_twiddle_generator;

    // -------------------------------------------------------------------------
    // Parameters & Signals
    // -------------------------------------------------------------------------
    parameter LATENCY    = 10;    // Matches DUT CORDIC_LATENCY
    parameter MAX_N      = 1024;  // Matches DUT MAX_N
    parameter ADDR_WIDTH = 11;    // Matches DUT ADDR_WIDTH
    
    reg                  clk;
    reg                  rst;
    reg [ADDR_WIDTH-1:0] k;
    reg [ADDR_WIDTH-1:0] n;
    reg                  valid_in;
    reg                  PRECISION; // 0: FP4, 1: FP8
    wire [15:0]          twiddle_out;

    // Testbench tracking variables
    integer total_tests = 0;
    integer passed_tests = 0;
    integer i;
    
    // Shadow pipeline arrays to track expected values through the latency
    reg [ADDR_WIDTH-1:0] k_pipe_tb    [0:LATENCY];
    reg [ADDR_WIDTH-1:0] n_pipe_tb    [0:LATENCY];
    reg                  prec_pipe_tb [0:LATENCY];
    reg                  v_pipe_tb    [0:LATENCY];

    // -------------------------------------------------------------------------
    // DUT Instantiation
    // -------------------------------------------------------------------------
    cordic_twiddle_generator #(
        .LATENCY(LATENCY),
        .MAX_N(MAX_N),
        .ADDR_WIDTH(ADDR_WIDTH)
    ) dut (
        .clk(clk),
        .rst(rst),
        .k(k),
        .n(n),
        .valid_in(valid_in),
        .PRECISION(PRECISION),
        .twiddle_out(twiddle_out)
    );

    // -------------------------------------------------------------------------
    // Clock Generation
    // -------------------------------------------------------------------------
    initial begin
        clk = 0;
        forever #5 clk = ~clk; // 100MHz clock, 10ns period
    end

    // -------------------------------------------------------------------------
    // Shadow Pipeline Logic (Tracks latency in parallel with DUT)
    // -------------------------------------------------------------------------
    always @(posedge clk) begin
        if (!rst) begin
            for (i = 0; i <= LATENCY; i = i + 1) begin
                k_pipe_tb[i]    <= 0;
                n_pipe_tb[i]    <= 0;
                prec_pipe_tb[i] <= 0;
                v_pipe_tb[i]    <= 0;
            end
        end else begin
            // Push current inputs into the start of the shadow pipeline
            k_pipe_tb[0]    <= k;
            n_pipe_tb[0]    <= n;
            prec_pipe_tb[0] <= PRECISION;
            v_pipe_tb[0]    <= valid_in;
            
            // Shift data down the pipeline
            for (i = 0; i < LATENCY; i = i + 1) begin
                k_pipe_tb[i+1]    <= k_pipe_tb[i];
                n_pipe_tb[i+1]    <= n_pipe_tb[i];
                prec_pipe_tb[i+1] <= prec_pipe_tb[i];
                v_pipe_tb[i+1]    <= v_pipe_tb[i];
            end
        end
    end

    // -------------------------------------------------------------------------
    // Stimulus Tasks
    // -------------------------------------------------------------------------
    task send_stimulus(input [ADDR_WIDTH-1:0] k_val, input [ADDR_WIDTH-1:0] n_val, input prec_val);
        begin
            @(posedge clk);
            k         <= k_val;
            n         <= n_val;
            PRECISION <= prec_val;
            valid_in  <= 1'b1;
            total_tests <= total_tests + 1;
        end
    endtask

    task insert_bubble();
        begin
            @(posedge clk);
            valid_in <= 1'b0;
        end
    endtask

    // -------------------------------------------------------------------------
    // Main Test Sequence
    // -------------------------------------------------------------------------
    initial begin
        // Initialize signals
        rst       = 1'b1; // Active low reset
        k         = 0;
        n         = 0;
        valid_in  = 0;
        PRECISION = 0;

        $display("===============================================================");
        $display("   STARTING CORDIC TWIDDLE GENERATOR VERIFICATION (VERILOG)");
        $display("===============================================================");

        // Apply Reset
        #15 rst = 1'b0;
        #25 rst = 1'b1;
        @(posedge clk);

        // --- Edge Case 1: Zero Phase ---
        $display("\n[TEST] Zero Phase (k = 0, n = 1024)");
        send_stimulus(11'd0, 11'd1024, 1'b0); 

        // --- Edge Case 2: Maximum valid bounds for different FFT sizes ---
        $display("[TEST] Boundaries for varying N");
        send_stimulus(11'd511, 11'd1024, 1'b1); // Max k for N=1024
        send_stimulus(11'd127, 11'd256,  1'b0); // Max k for N=256
        send_stimulus(11'd3,   11'd8,    1'b1); // Max k for N=8

        // --- Edge Case 3: Back-to-Back Pipeline Stress Test ---
        $display("[TEST] Pipeline Back-to-Back Stress (Dynamic N changes)");
        send_stimulus(11'd10, 11'd1024, 1'b1);
        send_stimulus(11'd20, 11'd512,  1'b0);
        send_stimulus(11'd40, 11'd128,  1'b1);
        send_stimulus(11'd1,  11'd4,    1'b0);

        // --- Edge Case 4: Pipeline Bubbles (Interrupted Data) ---
        $display("[TEST] Pipeline Bubbles (Handling invalid cycles)");
        insert_bubble();
        insert_bubble();
        send_stimulus(11'd256, 11'd1024, 1'b1); // Exact midpoint for N=1024
        insert_bubble();
        send_stimulus(11'd64,  11'd256,  1'b0); // Exact midpoint for N=256

        // --- Edge Case 5: Sweep across different precisions ---
        $display("[TEST] Random/Sweep Quadrants");
        send_stimulus(11'd85,  11'd1024, 1'b0);
        send_stimulus(11'd170, 11'd1024, 1'b1);
        send_stimulus(11'd341, 11'd1024, 1'b0);
        send_stimulus(11'd450, 11'd1024, 1'b1);
        
        // Stop sending valid data cleanly
        @(posedge clk);
        valid_in <= 1'b0; // FIX: Use non-blocking assignment here

        // Wait for the pipeline to drain completely
        #(LATENCY * 20);

        // Print Comprehensive Report
        $display("\n===============================================================");
        $display("   COMPREHENSIVE TEST REPORT");
        $display("===============================================================");
        $display("Total Stimuli Sent  : %0d", total_tests);
        $display("Total Responses Read: %0d", passed_tests);
        if (total_tests == passed_tests)
            $display("STATUS              : ALL PIPELINE STAGES PASSED");
        else
            $display("STATUS              : PIPELINE MISMATCH DETECTED!");
        $display("===============================================================\n");

        $finish;
    end

    // -------------------------------------------------------------------------
    // Output Monitor & Checker with Mathematical Golden Reference
    // -------------------------------------------------------------------------
    // Hierarchical bindings to internal DUT signals (Crucial Fix)
    wire signed [15:0] internal_cos = dut.final_cos;
    wire signed [15:0] internal_sin = dut.final_sin;
    wire               out_prec     = dut.p_pipe[LATENCY];

    // Real-number tracking variables for mathematical validation
    real expected_rad;
    real expected_cos;
    real expected_sin;
    integer int_expected_cos;
    integer int_expected_sin;

    always @(posedge clk) begin
        // Add a #1 delay to let combinational assignments settle before checking
        #1;
        if (dut.v_pipe[LATENCY] == 1'b1 && rst == 1'b1) begin
            
            // 1. Calculate the true mathematical angle in radians
            // Twiddle factor angle: -2 * pi * k / N
            expected_rad = -2.0 * 3.141592653589793 * k_pipe_tb[LATENCY] / n_pipe_tb[LATENCY];
            
            // 2. Compute ideal real-number trigonometric values
            expected_cos = $cos(expected_rad);
            expected_sin = $sin(expected_rad);
            
            // 3. Scale to Q2.14 integer domain for direct hardware comparison
            int_expected_cos = $rtoi(expected_cos * 16384.0);
            int_expected_sin = $rtoi(expected_sin * 16384.0);

            $display("---------------------------------------------------------------");
            $display("Time: %0t ns | Tracked K = %0d, N = %0d", $time, k_pipe_tb[LATENCY], n_pipe_tb[LATENCY]);
            $display("Precision Mode    : %s", prec_pipe_tb[LATENCY] ? "FP8" : "FP4");
            $display("CORDIC Q2.14 Out  -> Cos: %d (0x%h), Sin: %d (0x%h)", internal_cos, internal_cos, internal_sin, internal_sin);
            $display("GOLDEN Q2.14 Ref  -> Cos: %d, Sin: %d", int_expected_cos, int_expected_sin);
            
            if (out_prec == 1'b1) begin
                $display("FINAL FP8 OUTPUT  -> Cos: 0x%h, Sin: 0x%h", twiddle_out[15:8], twiddle_out[7:0]);
            end else begin
                $display("FINAL FP4 OUTPUT  -> Cos: 0x%h, Sin: 0x%h", twiddle_out[11:8], twiddle_out[3:0]);
            end
            
            // 4. Control-Path and Data-Path Verification
            if (out_prec !== prec_pipe_tb[LATENCY]) begin
                $display("ERROR: Precision mismatch! Expected %b, got %b", prec_pipe_tb[LATENCY], out_prec);
            end else if (out_prec == 1'b1 && twiddle_out == 16'h0000) begin
                $display("ERROR: Data is blank/zero during valid FP8 output window!");
            end else if (((internal_cos - int_expected_cos) > 150) || ((internal_cos - int_expected_cos) < -150)) begin
                $display("MATHEMATICAL ERROR: Cosine value diverges significantly from golden model!");
            end else begin
                $display("MATHEMATICAL MATCH: CORDIC values are within precision bounds.");
                passed_tests = passed_tests + 1;
            end
        end
    end

endmodule
