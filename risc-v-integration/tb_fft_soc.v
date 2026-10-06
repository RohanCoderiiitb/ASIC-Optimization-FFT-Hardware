// =============================================================================
// tb_fft_soc.v
// =============================================================================
`timescale 1ns/1ps

module tb_fft_soc;

    reg clk = 0;
    reg resetn = 0;
    always #5 clk = ~clk;

    // Expanded to 32KB to hold 11 x 256 inputs and 11 x 256 outputs
    picorv32_fft_soc #(.MEM_WORDS(8192)) dut (
        .clk    (clk),
        .resetn (resetn)
    );

    localparam SAMPLES_BASE_WORD = 2048; 
    localparam RESULTS_BASE_WORD = 5120;
    localparam N = 256;
    localparam NUM_TESTS = 11;

    integer i;
    integer dump_file;

    initial begin
        // REMOVE "sim/" prefix since the execution context is already inside the sim folder
        $readmemh("firmware.hex", dut.mem);

        resetn = 0;
        repeat (10) @(posedge clk);
        resetn = 1;

        repeat (300000) @(posedge clk);

        $display("---- dumping results to results.hex ----");
        // REMOVE "sim/" prefix here as well
        dump_file = $fopen("results.hex", "w");
        for (i = 0; i < (NUM_TESTS * N); i = i + 1) begin
            $fwrite(dump_file, "%08h\n", dut.mem[RESULTS_BASE_WORD + i]);
        end
        $fclose(dump_file);
        
        $finish;
    end

    // Safety timeout
    initial begin
        #5000000;
        $display("TIMEOUT -- simulation did not finish");
        $finish;
    end

endmodule