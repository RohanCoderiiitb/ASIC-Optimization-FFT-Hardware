#!/usr/bin/env python3
import os
import subprocess
import sys
import glob
import numpy as np
import shutil
import re
import math

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR) 

sys.path.append(SCRIPT_DIR)
sys.path.append(ROOT_DIR)

from rv32i_asm import assemble
from performance_evaluator import PerformanceEvaluator, SIGNAL_LABELS

def generate_bulletproof_wrapper():
    """Injects the mechanically flawless PCPI wrapper."""
    wrapper_code = """`timescale 1ns/1ps
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
"""
    wrapper_path = os.path.abspath("./risc-v-integration/fft_pcpi_wrapper.v")
    os.makedirs(os.path.dirname(wrapper_path), exist_ok=True)
    with open(wrapper_path, "w") as f:
        f.write(wrapper_code)

def build_firmware_hex(pe, asm_file, out_hex):
    with open(asm_file, 'r') as f:
        lines = f.readlines()
    words = assemble(lines)
    firmware = ["00000013"] * 8192 
    for i, w in enumerate(words):
        firmware[i] = f"{w:08x}"
    base_idx = 2048
    for ti, vec in enumerate(pe.test_vectors):
        for si, sample in enumerate(vec):
            re_fp8 = pe.float_to_fp8_e4m3(sample.real) & 0xFF
            im_fp8 = pe.float_to_fp8_e4m3(sample.imag) & 0xFF
            packed_word = (re_fp8 << 8) | im_fp8
            firmware[base_idx + (ti * 256) + si] = f"{packed_word:08x}"
    os.makedirs(os.path.dirname(out_hex), exist_ok=True)
    with open(out_hex, 'w') as f:
        f.write("\n".join(firmware) + "\n")

def run_soc_evaluation():
    n = 256
    num_tests = 11
    sim_dir = os.path.abspath("./sim")
    asm_path = os.path.abspath("./risc-v-integration/fft_batch_test.asm")
    
    if os.path.exists(sim_dir):
        shutil.rmtree(sim_dir, ignore_errors=True)
    os.makedirs(sim_dir)
    
    firmware_hex_path = os.path.join(sim_dir, "firmware.hex")
    results_hex_path = os.path.join(sim_dir, "results.hex")
    
    print("\n[1] Generating Test Vectors & Firmware...")
    pe = PerformanceEvaluator(n)
    build_firmware_hex(pe, asm_path, firmware_hex_path)
    generate_bulletproof_wrapper()
    
    print("[2] Hardcoding Absolute Twiddle Paths and Validating Parser format...")
    base_twiddle = pe._write_twiddle_file(sim_dir)
    abs_twiddle_path = base_twiddle.replace('\\', '/')
    
    # SILENT FAILURE FIX: Detect if file is Hex or Binary to enforce the correct readmem command
    with open(base_twiddle, 'r') as f:
        twiddle_content = f.read()
    # If the file contains letters A-F or numbers 2-9, it's hex, not binary
    is_hex = any(c in '23456789abcdefABCDEF' for c in twiddle_content)
    readmem_cmd = '$readmemh' if is_hex else '$readmemb'
    
    print(f"    -> Detected Twiddle format: {'HEX' if is_hex else 'BINARY'} (Applying {readmem_cmd})")
    
    # Overwrite all readmem commands to match the detected file format perfectly
    verilog_files = glob.glob('../verilog_sources/*.v') + glob.glob('../generated_cores/fft_256_sol_28/*.v')
    for f in verilog_files:
        try:
            with open(f, 'r') as file: content = file.read()
            if 'twiddles_1024.txt' in content:
                content = re.sub(r'\$readmem[bh]\s*\(\s*"[^"]*"', f'{readmem_cmd}("{abs_twiddle_path}"', content)
                with open(f, 'w') as file: file.write(content)
        except Exception: pass

    if not os.path.exists("./picorv32.v"):
        subprocess.run(["curl", "-s", "-o", "./picorv32.v", "https://raw.githubusercontent.com/YosysHQ/picorv32/master/picorv32.v"], check=True)
    
    print("[3] Compiling SoC via Icarus Verilog...")
    compile_cmd = [
        'iverilog', '-o', os.path.join(sim_dir, 'fft_soc.vvp'), 
        '-I', '../verilog_sources', '-I', '../generated_cores/fft_256_sol_28', '-g2012',
        '../risc-v-integration/tb_fft_soc.v', '../risc-v-integration/picorv32_fft_soc.v',  
        '../risc-v-integration/fft_pcpi_wrapper.v', '../generated_cores/fft_256_sol_28/mixed_fft_256_top.v', 
        '../picorv32.v'
    ]
    lib_sources = glob.glob('./verilog_sources/*.v') + glob.glob('./generated_cores/fft_256_sol_28/*.v')
    for f in lib_sources:
        if 'test' not in os.path.basename(f) and 'mixed_fft_256_top.v' not in os.path.basename(f):
            compile_cmd.append(os.path.join('..', f))
            
    res = subprocess.run(compile_cmd, capture_output=True, text=True, cwd=sim_dir)
    if res.returncode != 0:
        print("\n[!] COMPILATION FAILED:\n", res.stderr)
        return

    print("[4] Running Simulation Engine...")
    sim_res = subprocess.run(['vvp', 'fft_soc.vvp'], capture_output=True, text=True, cwd=sim_dir)
    
    if sim_res.returncode != 0 or not os.path.exists(results_hex_path):
        print(f"\n[!] SIMULATION FAILED OR CRASHED:\n{sim_res.stderr}\n{sim_res.stdout}")
        return

    with open(os.path.join(sim_dir, "debug_trace.log"), "w") as f:
        f.write(sim_res.stdout)

    print("[4b] Analyzing LOAD/STORE event trace...")
    load_re = re.compile(r'^LOADEV (\d+) (\d+) ([0-9a-fA-F]+)$')
    store_re = re.compile(r'^STOREV (\d+) (\d+) ([0-9a-fA-F]+)$')
    loads = []   # (seq, addr, data)
    stores = []
    for line in sim_res.stdout.splitlines():
        m = load_re.match(line)
        if m:
            loads.append((int(m.group(1)), int(m.group(2)), int(m.group(3), 16)))
            continue
        m = store_re.match(line)
        if m:
            stores.append((int(m.group(1)), int(m.group(2)), int(m.group(3), 16)))

    expected_events = num_tests * n
    print(f"    LOAD events captured:  {len(loads)} (expected {expected_events})")
    print(f"    STORE events captured: {len(stores)} (expected {expected_events})")

    # Build expected (addr, data) stream exactly as build_firmware_hex did
    expected_loads = []
    for vec in pe.test_vectors:
        for si, sample in enumerate(vec):
            re_fp8 = pe.float_to_fp8_e4m3(sample.real) & 0xFF
            im_fp8 = pe.float_to_fp8_e4m3(sample.imag) & 0xFF
            expected_loads.append((si, (re_fp8 << 8) | im_fp8))

    load_mismatch = None
    for idx, (seq, addr, data) in enumerate(loads):
        exp_addr, exp_data = expected_loads[idx] if idx < len(expected_loads) else (None, None)
        if addr != exp_addr or data != exp_data:
            load_mismatch = (idx, seq, addr, data, exp_addr, exp_data)
            break

    if load_mismatch:
        idx, seq, addr, data, exp_addr, exp_data = load_mismatch
        print(f"    [!] LOAD mismatch at event #{idx} (dbg_seq={seq}): "
              f"got addr={addr} data={data:04x}, expected addr={exp_addr} data={exp_data:04x}")
    elif loads:
        print("    LOAD stream matches expected samples/addresses exactly, in order.")

    store_addr_mismatch = None
    for idx, (seq, addr, data) in enumerate(stores):
        exp_addr = idx % n
        if addr != exp_addr:
            store_addr_mismatch = (idx, seq, addr, exp_addr)
            break

    if store_addr_mismatch:
        idx, seq, addr, exp_addr = store_addr_mismatch
        print(f"    [!] STORE address mismatch at event #{idx} (dbg_seq={seq}): "
              f"got addr={addr}, expected addr={exp_addr}")
    elif stores:
        print("    STORE address sequence is 0..255 in order for every test, as expected.")

    print()

    print("[5] Engaging SQNR Math Validation...")
    goldens = pe._compute_golden_outputs()

    with open(results_hex_path, 'r') as f:
        all_lines = [line.strip() for line in f if line.strip()]

    expected_words = num_tests * n
    result_lines = all_lines[:expected_words]
    timeout_count = None
    if len(all_lines) > expected_words:
        timeout_count = int(all_lines[expected_words], 16)

    raw_hw_outputs = []
    for line in result_lines:
        word = int(line, 16)
        raw_hw_outputs.append(pe.fp8_to_float((word >> 8) & 0xFF) + 1j * pe.fp8_to_float(word & 0xFF))

    raw_hw_outputs = np.array(raw_hw_outputs, dtype=np.complex64)

    if timeout_count is not None:
        print(f"\n    [DIAGNOSTIC] fftwait timeouts hit: {timeout_count} / {num_tests} tests")
        if timeout_count > 0:
            print("    -> The CPU read back FFT results before the core finished computing")
            print("       for at least one test. This is very likely the cause of the low")
            print("       SQNR: raise the fftwait timeout in fft_batch_test.asm (and the")
            print("       `repeat(300000)` cycle budget in tb_fft_soc.v proportionally) and")
            print("       re-run to confirm SQNR recovers.")
    else:
        print("\n    [DIAGNOSTIC] No timeout-counter line found in results.hex")
        print("    -> make sure you're using the patched tb_fft_soc.v")

    total_sqnr = 0.0
    print("\n=======================================================")
    print("  SoC Firmware Executed Metrics Breakdown - FFT-256")
    print("=======================================================")
    for i in range(num_tests):
        approx = raw_hw_outputs[i * n : (i + 1) * n]
        sqnr = pe.calculate_sqnr(goldens[i], approx, final_stage_is_fp8=True)
        # Match performance_evaluator.py's evaluate_design(): an exact
        # (zero-noise-power) match reports as literal inf dB, which would
        # blow up the average. performance_evaluator.py ceilings this case
        # to 100.0 dB before summing, so we mirror that here to keep the
        # SoC-path average comparable to the golden performance_evaluator
        # average for the same design.
        if math.isinf(sqnr):
            print(f"  {SIGNAL_LABELS[i]:<25}   inf dB (Exact)")
            total_sqnr += 100.0
        else:
            print(f"  {SIGNAL_LABELS[i]:<25}  {sqnr:>10.2f} dB")
            total_sqnr += sqnr
    print("-------------------------------------------------------")
    print(f"  Average System SQNR:        {total_sqnr/num_tests:>10.2f} dB")
    print("=======================================================")

if __name__ == "__main__":
    run_soc_evaluation()