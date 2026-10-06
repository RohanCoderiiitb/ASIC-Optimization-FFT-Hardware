// =============================================================================
// tb_fft_soc_perf.v
//
// Cycle-accurate throughput instrumentation for the PicoRV32 + FFT SoC.
//
// WHY A SEPARATE TESTBENCH
//   tb_fft_soc.v runs a fixed `repeat (300000)` budget and dumps results. It
//   counts nothing, so there is no SoC-level throughput number anywhere in the
//   project. This testbench measures one, and it does so WITHOUT touching
//   fft_pcpi_wrapper.v (which soc_evaluator.py regenerates on every run and
//   would overwrite) or any of the RTL.
//
// WHAT IT OBSERVES
//   Only the SoC's own top-level PCPI wires, hierarchically. Nothing inside the
//   wrapper, so a change to the wrapper's internals cannot silently break the
//   measurement:
//
//     dut.pcpi_valid  dut.pcpi_insn  dut.pcpi_ready  dut.pcpi_wait
//
//   A custom-0 instruction is pcpi_valid with opcode 0x0B. funct3 selects the
//   operation, per encode_insn.py:
//     001 FFTLOAD   010 FFTSTORE   011 FFTSTART   100 FFTWAIT   101 FFTSTATUS
//
// THE THREE NUMBERS IT SEPARATES
//   total          every cycle from reset release to the last result stored.
//                  Divided by the frame count this is end-to-end application
//                  throughput: the number a systems reviewer asks for, and the
//                  lowest, because it includes the CPU's own load/store loops.
//
//   accelerator    cycles in which a custom-0 instruction occupies the CPU.
//                  This is the cost of the extension itself, PCPI handshake
//                  included: a load costs 2 cycles of CPU time, a store 4.
//
//   wait           cycles inside FFTWAIT, i.e. the CPU stalled on the datapath.
//                  This should land on the standalone ExecCycles figure for the
//                  same transform size (1123 at N=256), which makes it a
//                  cross-check between the SoC and the core-level tables rather
//                  than a new unverified number.
//
//   The difference (accelerator - wait) is what the ISA extension costs to move
//   data across PCPI, separated from what the FFT costs to compute.
//
// TERMINATION
//   Ends when the expected number of results have been stored, not on a fixed
//   cycle budget. A store completes on (custom-0, funct3=010, pcpi_ready), so
//   the count is taken from the PCPI handshake and needs no firmware change and
//   no cooperation from the wrapper. The safety timeout still exists and reports
//   the partial counts rather than dying silently.
//
// OUTPUT
//   A `PERF <key> <value>` line per metric on stdout, which
//   measure_soc_throughput.py parses, plus results.hex exactly as the original
//   testbench writes it so the existing SQNR checking still works.
//
// Overridable at compile time:
//   -DFFT_N=256  -DNUM_TESTS=11  -DCLOCK_NS=10.0
//   -DSAMPLES_BASE_WORD=2048  -DRESULTS_BASE_WORD=5120  -DMEM_WORDS=8192
//   -DFIRMWARE_HEX=\"firmware.hex\"  -DRESULTS_HEX=\"results.hex\"
//
// STATUS: the counting logic is driven only by the PCPI handshake, so it is
//   independent of the wrapper's state encoding. Verify the `wait` column
//   against the standalone ExecCycles before quoting any of it: if they
//   disagree, the FFTWAIT path is not stalling the way this assumes and the
//   split is wrong even if the total is right.
// =============================================================================
`timescale 1ns/1ps

`ifndef FFT_N
  `define FFT_N 256
`endif
`ifndef NUM_TESTS
  `define NUM_TESTS 11
`endif
`ifndef CLOCK_NS
  `define CLOCK_NS 10.0
`endif
`ifndef SAMPLES_BASE_WORD
  `define SAMPLES_BASE_WORD 2048
`endif
`ifndef RESULTS_BASE_WORD
  `define RESULTS_BASE_WORD 5120
`endif
`ifndef MEM_WORDS
  `define MEM_WORDS 8192
`endif
`ifndef FIRMWARE_HEX
  `define FIRMWARE_HEX "firmware.hex"
`endif
`ifndef RESULTS_HEX
  `define RESULTS_HEX "results.hex"
`endif
`ifndef TIMEOUT_CYCLES
  `define TIMEOUT_CYCLES 2000000
`endif

module tb_fft_soc_perf;

    localparam integer N          = `FFT_N;
    localparam integer NUM_TESTS  = `NUM_TESTS;
    localparam integer EXPECTED   = N * NUM_TESTS;

    reg clk = 0;
    reg resetn = 0;
    always #(`CLOCK_NS / 2.0) clk = ~clk;

    picorv32_fft_soc #(.MEM_WORDS(`MEM_WORDS)) dut (
        .clk    (clk),
        .resetn (resetn)
    );

    // -------------------------------------------------------------------------
    // PCPI decode, from the SoC's top-level wires only.
    // -------------------------------------------------------------------------
    wire        p_valid = dut.pcpi_valid;
    wire [31:0] p_insn  = dut.pcpi_insn;
    wire        p_ready = dut.pcpi_ready;
    wire        p_wait  = dut.pcpi_wait;

    wire        is_c0 = p_valid && (p_insn[6:0] == 7'b0001011);
    wire [2:0]  f3    = p_insn[14:12];

    wire c0_load   = is_c0 && (f3 == 3'b001);
    wire c0_store  = is_c0 && (f3 == 3'b010);
    wire c0_start  = is_c0 && (f3 == 3'b011);
    wire c0_wait   = is_c0 && (f3 == 3'b100);
    wire c0_status = is_c0 && (f3 == 3'b101);

    // -------------------------------------------------------------------------
    // Counters. All 64-bit: a ten-size sweep at N=1024 overflows 32 bits.
    // -------------------------------------------------------------------------
    reg [63:0] cyc_total     = 0;
    reg [63:0] cyc_accel     = 0;
    reg [63:0] cyc_load      = 0;
    reg [63:0] cyc_store     = 0;
    reg [63:0] cyc_start     = 0;
    reg [63:0] cyc_wait      = 0;
    reg [63:0] cyc_status    = 0;
    reg [63:0] cyc_pcpi_wait = 0;   // cycles the wrapper held the CPU stalled

    reg [63:0] n_load  = 0;         // completed operations, by handshake
    reg [63:0] n_store = 0;
    reg [63:0] n_start = 0;

    reg [63:0] first_load_cycle = 0;
    reg        seen_first_load  = 0;
    reg [63:0] frame_start_cyc  = 0;
    reg [63:0] frame_cyc_sum    = 0;   // sum over frames of last_store - first_load
    reg [63:0] frames_done      = 0;
    reg        counting         = 0;
    reg        finished         = 0;

    always @(posedge clk) begin
        if (!resetn) begin
            counting <= 1;
        end else if (counting && !finished) begin
            cyc_total <= cyc_total + 1;

            if (is_c0) cyc_accel <= cyc_accel + 1;
            if (c0_load)   cyc_load   <= cyc_load   + 1;
            if (c0_store)  cyc_store  <= cyc_store  + 1;
            if (c0_start)  cyc_start  <= cyc_start  + 1;
            if (c0_wait)   cyc_wait   <= cyc_wait   + 1;
            if (c0_status) cyc_status <= cyc_status + 1;
            if (p_wait)    cyc_pcpi_wait <= cyc_pcpi_wait + 1;

            // A custom-0 operation retires on the cycle the wrapper asserts
            // pcpi_ready, so these are completed-operation counts rather than
            // cycle counts.
            if (c0_load && p_ready) begin
                n_load <= n_load + 1;
                if (!seen_first_load) begin
                    seen_first_load  <= 1;
                    first_load_cycle <= cyc_total;
                    frame_start_cyc  <= cyc_total;
                end
            end
            if (c0_start && p_ready) n_start <= n_start + 1;
            if (c0_store && p_ready) begin
                n_store <= n_store + 1;
                // Last store of a frame closes that frame's window.
                if (((n_store + 1) % N) == 0) begin
                    frame_cyc_sum   <= frame_cyc_sum + (cyc_total - frame_start_cyc);
                    frames_done     <= frames_done + 1;
                    frame_start_cyc <= cyc_total;   // next frame starts here
                end
                if ((n_store + 1) >= EXPECTED) finished <= 1;
            end
        end
    end

    // -------------------------------------------------------------------------
    // Reporting
    // -------------------------------------------------------------------------
    integer dump_file;
    integer i;

    task dump_results;
        begin
            dump_file = $fopen(`RESULTS_HEX, "w");
            for (i = 0; i < EXPECTED; i = i + 1)
                $fwrite(dump_file, "%08h\n", dut.mem[`RESULTS_BASE_WORD + i]);
            $fclose(dump_file);
        end
    endtask

    task emit_perf;
        input integer complete;
        begin
            $display("PERF fft_n %0d", N);
            $display("PERF num_tests %0d", NUM_TESTS);
            $display("PERF clock_ns %f", `CLOCK_NS);
            $display("PERF complete %0d", complete);
            $display("PERF cycles_total %0d", cyc_total);
            $display("PERF cycles_accelerator %0d", cyc_accel);
            $display("PERF cycles_load %0d", cyc_load);
            $display("PERF cycles_store %0d", cyc_store);
            $display("PERF cycles_start %0d", cyc_start);
            $display("PERF cycles_wait %0d", cyc_wait);
            $display("PERF cycles_status %0d", cyc_status);
            $display("PERF cycles_pcpi_stall %0d", cyc_pcpi_wait);
            $display("PERF ops_load %0d", n_load);
            $display("PERF ops_store %0d", n_store);
            $display("PERF ops_start %0d", n_start);
            $display("PERF frames_done %0d", frames_done);
            $display("PERF frame_cycles_sum %0d", frame_cyc_sum);
            $display("PERF first_load_cycle %0d", first_load_cycle);
        end
    endtask

    initial begin
        $readmemh(`FIRMWARE_HEX, dut.mem);
        resetn = 0;
        repeat (10) @(posedge clk);
        resetn = 1;

        wait (finished === 1'b1);
        repeat (4) @(posedge clk);
        $display("---- %0d results stored; dumping to %s ----", n_store, `RESULTS_HEX);
        dump_results;
        emit_perf(1);
        $finish;
    end

    // Safety net. Reports the partial counts rather than dying silently, so a
    // short firmware timeout shows up as frames_done < num_tests instead of as
    // an unexplained absence of output.
    initial begin
        repeat (`TIMEOUT_CYCLES) @(posedge clk);
        if (!finished) begin
            $display("---- TIMEOUT after %0d cycles: %0d of %0d results stored ----",
                     `TIMEOUT_CYCLES, n_store, EXPECTED);
            dump_results;
            emit_perf(0);
            $finish;
        end
    end

endmodule
