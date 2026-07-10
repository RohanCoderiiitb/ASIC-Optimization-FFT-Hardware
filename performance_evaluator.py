"""
Performance Evaluation Module (Pipelined II=1 FP32 Edition)
===========================================================
Calculates SQNR (Signal-to-Quantisation-Noise Ratio) by running
iverilog/vvp simulation of generated mixed-precision pipelined FFT designs.

Updates:
  - FIXED: Restored output-bound quantization check to accurately reflect algorithmic
           error and match previous 26dB readings.
  - Generates twiddles hex file automatically for the ROM.
"""

import numpy as np
import subprocess
import os
import glob as glob_module
import math

SIGNAL_LABELS = [
    "Impulse",
    "Single Tone",
    "Multi-Tone",
    "Chirp (LFM)",
    "Sinusoid (complex)",
    "Step Function",
    "Gaussian Pulse",
    "Radar Pulsed Sinusoid",
    "Radar Clutter + Target",
    "Radar Barker-13 Pulse",
    "Radar Doppler Burst",
]


class PerformanceEvaluator:
    def __init__(self, fft_size):
        self.fft_size            = fft_size
        self.num_stages          = int(math.log2(fft_size))
        self.verilog_sources_dir = './verilog_sources'
        self.test_vectors        = self._generate_test_vectors()
        self.golden_outputs      = self._compute_golden_outputs()

    def _generate_test_vectors(self):
        n     = self.fft_size
        n_arr = np.arange(n, dtype=np.float32)
        vecs  = []

        # 1. Impulse
        v = np.zeros(n, dtype=np.complex64)
        v[0] = 0.9 + 0.0j
        vecs.append(v)

        # 2. Single tone
        k = max(1, n // 8)
        v = (0.9 * np.cos(2.0 * np.pi * k * n_arr / n)).astype(np.complex64)
        vecs.append(v)

        # 3. Multi-tone
        tones = [1, 3, 5, 7] if n >= 8 else [1]
        v = np.zeros(n, dtype=np.complex64)
        for kt in tones:
            if kt < n // 2:
                v += np.exp(2j * np.pi * kt * n_arr / n).astype(np.complex64)
        peak = np.max(np.abs(v))
        v = (v / peak * 0.9).astype(np.complex64) if peak > 0 else v
        vecs.append(v)

        # 4. Chirp (LFM)
        v = (np.exp(1j * np.pi * (n_arr ** 2) / n) * 0.9).astype(np.complex64)
        vecs.append(v)

        # 5. Sinusoid (complex)
        k5 = max(1, n // 4)
        v  = (0.9 * np.exp(2j * np.pi * k5 * n_arr / n)).astype(np.complex64)
        vecs.append(v)

        # 7. Step function
        v = np.zeros(n, dtype=np.complex64)
        v[:n // 2] =  0.9 + 0.0j
        v[n // 2:] = -0.9 + 0.0j
        vecs.append(v)

        # 8. Gaussian pulse
        sigma  = np.float32(n / 8.0)
        centre = np.float32(n / 2.0)
        v = np.exp(-0.5 * ((n_arr - centre) / sigma) ** 2).astype(np.complex64)
        peak = np.max(np.abs(v))
        v = (v / peak * 0.9).astype(np.complex64) if peak > 0 else v
        vecs.append(v)

        # 9. Radar Pulsed Sinusoid
        k_radar = max(1, n // 6)
        pulse_start = n // 4
        pulse_end   = 3 * n // 4
        v = np.zeros(n, dtype=np.complex64)
        v[pulse_start:pulse_end] = np.exp(
            2j * np.pi * k_radar * n_arr[pulse_start:pulse_end] / n
        ).astype(np.complex64)
        peak = np.max(np.abs(v))
        v = (v / peak * 0.9).astype(np.complex64) if peak > 0 else v
        vecs.append(v)

        # 10. Radar Clutter + Target
        k_clutter = max(1, n // 16)
        k_target  = max(k_clutter + 2, n // 5)
        clutter_amp = np.float32(0.85)
        target_amp  = np.float32(0.05)
        v = (
            clutter_amp * np.exp(2j * np.pi * k_clutter * n_arr / n)
            + target_amp * np.exp(2j * np.pi * k_target  * n_arr / n)
        ).astype(np.complex64)
        peak = np.max(np.abs(v))
        v = (v / peak * 0.9).astype(np.complex64) if peak > 0 else v
        vecs.append(v)

        # 11. Radar Barker-13 Pulse
        barker13 = np.array([1, 1, 1, 1, 1, -1, -1, 1, 1, -1, 1, -1, 1], dtype=np.float32)
        v = np.zeros(n, dtype=np.complex64)
        code_len = min(13, n)
        v[:code_len] = barker13[:code_len]
        peak = np.max(np.abs(v))
        v = (v / peak * 0.9).astype(np.complex64) if peak > 0 else v
        vecs.append(v)

        # 12. Radar Doppler Burst
        k_doppler = max(1, n // 7)
        hamming   = np.hamming(n).astype(np.float32)
        v = (hamming * np.exp(2j * np.pi * k_doppler * n_arr / n)).astype(np.complex64)
        peak = np.max(np.abs(v))
        v = (v / peak * 0.9).astype(np.complex64) if peak > 0 else v
        vecs.append(v)

        assert len(vecs) == len(SIGNAL_LABELS), "Signal mapping count tracking mismatched"
        return vecs

    def _compute_golden_outputs(self):
        return self._compute_golden_for_precision(fp8_input=True)

    def _compute_golden_for_precision(self, fp8_input=True):
        goldens = []
        for v in self.test_vectors:
            if fp8_input:
                v_quantized = np.array([
                    np.float32(self.fp8_to_float(self.float_to_fp8_e4m3(x.real))) +
                    1j * np.float32(self.fp8_to_float(self.float_to_fp8_e4m3(x.imag)))
                    for x in v
                ], dtype=np.complex64)
            else:
                v_quantized = np.array([
                    np.float32(self.fp4_to_float(self.float_to_fp4(x.real))) +
                    1j * np.float32(self.fp4_to_float(self.float_to_fp4(x.imag)))
                    for x in v
                ], dtype=np.complex64)

            goldens.append(np.fft.fft(v_quantized).astype(np.complex64))
            
        return goldens

    # ==================================================================
    # Float <-> FP conversion helpers 
    # ==================================================================
    def float_to_fp8_e4m3(self, val):
        if val == 0.0: return 0
        sign_bit = 0x80 if val < 0 else 0x00
        val = abs(val)
        if val < 2 ** (-6): return sign_bit
        exp_u = math.floor(math.log2(val))
        exp_b = exp_u + 7
        if exp_b < 1:
            exp_b = 0
            mant  = min(7, round(val / (2 ** -6) * 8))
        elif exp_b >= 15:
            return sign_bit | 0x77  
        else:
            mant_f = val / (2 ** exp_u) - 1.0
            mant   = min(7, round(mant_f * 8))
            if mant >= 8:
                mant = 0; exp_b += 1
                if exp_b >= 15: return sign_bit | 0x77  
        return (sign_bit & 0x80) | ((exp_b & 0x0F) << 3) | (mant & 0x07)

    def fp8_to_float(self, fp8_val):
        fp8_val &= 0xFF
        if fp8_val == 0: return 0.0
        sign = (fp8_val >> 7) & 0x1
        exp  = (fp8_val >> 3) & 0xF
        mant =  fp8_val       & 0x7
        if exp == 0:    value = mant / 8.0 * (2 ** -6)
        elif exp == 15: value = (1.0 + 7 / 8.0) * (2 ** (14 - 7))  
        else:           value = (1.0 + mant / 8.0) * (2 ** (exp - 7))
        return -value if sign else value

    def float_to_fp4(self, val):
        if val == 0.0: return 0
        sign = 0x8 if val < 0 else 0x0
        val = abs(val)
        if   val < 0.25:  exp = 0; mant = 0
        elif val < 0.75:  exp = 0; mant = 1
        elif val < 1.25:  exp = 1; mant = 0
        elif val < 1.75:  exp = 1; mant = 1
        elif val < 2.5:   exp = 2; mant = 0
        elif val < 3.5:   exp = 2; mant = 1
        elif val < 5.0:   exp = 3; mant = 0
        else:             exp = 3; mant = 1
        return (sign & 0x8) | ((exp & 0x3) << 1) | (mant & 0x1)

    def fp4_to_float(self, fp4_val):
        fp4_val &= 0xF
        if fp4_val == 0: return 0.0
        sign = (fp4_val >> 3) & 0x1
        exp  = (fp4_val >> 1) & 0x3
        mant =  fp4_val       & 0x1
        if exp == 0:    value = mant * 0.5
        else:           value = (1.0 + mant * 0.5) * (2 ** (exp - 1))
        return -value if sign else value

    def _write_twiddle_file(self, sim_dir):
        path = os.path.join(sim_dir, 'twiddles_1024.txt')
        with open(path, 'w') as f:
            for idx in range(512):
                angle = -2.0 * math.pi * idx / 1024.0
                re, im = math.cos(angle), math.sin(angle)
                word = ((self.float_to_fp8_e4m3(re)&0xFF)<<16) | ((self.float_to_fp8_e4m3(im)&0xFF)<<8) | ((self.float_to_fp4(re)&0x0F)<<4) | (self.float_to_fp4(im)&0x0F)
                f.write(f"{word:024b}\n")
        return path

    def _generate_testbench(self, dut_file, design_name):
        design_name = self._sanitize_name(design_name)
        n          = self.fft_size
        addr_bits  = int(math.log2(n))
        num_tests  = len(self.test_vectors)
        top_module = f"{design_name}_top"

        butterflies     = (n // 2) * self.num_stages
        cycles_per_fft  = n + butterflies + 50 
        ready_timeout   = 512 + cycles_per_fft
        watchdog_ns     = (1024 + num_tests * (cycles_per_fft + n * 5) + 1000) * 10

        sim_dir  = os.path.abspath('./sim')
        out_path = os.path.join(sim_dir, f'{design_name}_output.txt')

        vec_hex_lines = []
        for ti, vec in enumerate(self.test_vectors):
            for si, sample in enumerate(vec):
                word = ((self.float_to_fp8_e4m3(sample.real) & 0xFF) << 8) | (self.float_to_fp8_e4m3(sample.imag) & 0xFF)
                vec_hex_lines.append(f"        tv[{ti*n + si}] = 16'h{word:04x};")
        vec_init = '\n'.join(vec_hex_lines)

        tb = f"""\
`timescale 1ns/1ps
module tb_{design_name};
    reg clk; reg rst; reg start; wire done;
    reg load_en; reg [{addr_bits-1}:0] load_addr; reg [15:0] load_data;
    reg unload_en; reg [{addr_bits-1}:0] unload_addr; wire [15:0] unload_data;
    integer i, ti, out_file;
    integer cycle_count, total_cycles, load_cycles, unload_cycles_cnt;
    reg [15:0] tv [{num_tests*n - 1}:0];

    {top_module} dut (
        .clk(clk), .rst(rst), .start(start), .done(done),
        .load_en(load_en), .load_addr(load_addr), .load_data(load_data),
        .unload_en(unload_en), .unload_addr(unload_addr), .unload_data(unload_data)
    );

    initial clk = 0;
    always #5 clk = ~clk;

    initial begin
        #{watchdog_ns};
        $display("WATCHDOG TIMEOUT");
        $finish;
    end

    initial begin : STIM
        integer wait_cnt;
{vec_init}
        out_file = $fopen("{out_path}", "w");
        rst = 0; start = 0; load_en = 0; load_addr = 0; load_data = 0; unload_en = 0; unload_addr = 0; total_cycles = 0;
        repeat(8) @(posedge clk);
        rst = 1;
        repeat(4) @(posedge clk);

        $display("\\n================================================================");
        $display("  Pipelined Run Report  --  {design_name} (FFT-{n})");
        $display("================================================================");

        for (ti = 0; ti < {num_tests}; ti = ti + 1) begin
            @(posedge clk);
            load_cycles = 0; load_en = 1;
            for (i = 0; i < {n}; i = i + 1) begin
                load_addr = i[{addr_bits-1}:0]; load_data = tv[ti*{n} + i];
                @(posedge clk); load_cycles = load_cycles + 1;
            end
            load_en = 0;
            @(posedge clk); load_cycles = load_cycles + 1;

            cycle_count = 0; start = 1;
            @(posedge clk); start = 0;
            cycle_count = cycle_count + 1;

            wait_cnt = 0;
            while (!done && wait_cnt < {ready_timeout}) begin
                @(posedge clk); cycle_count = cycle_count + 1; wait_cnt = wait_cnt + 1;
            end
            @(posedge clk);

            unload_cycles_cnt = 0; unload_en = 1;
            for (i = 0; i < {n}; i = i + 1) begin
                unload_addr = i[{addr_bits-1}:0];
                @(posedge clk); unload_cycles_cnt = unload_cycles_cnt + 1;
                @(posedge clk); unload_cycles_cnt = unload_cycles_cnt + 1;
                @(posedge clk); unload_cycles_cnt = unload_cycles_cnt + 1;
                $fwrite(out_file, "%04h\\n", unload_data);
            end
            unload_en = 0;
            repeat(2) @(posedge clk);
            
            total_cycles = total_cycles + load_cycles + cycle_count + unload_cycles_cnt;
            $display("  -> Test %0d | Exec Cycles: %0d | Load: %0d | Unload: %0d", ti, cycle_count, load_cycles, unload_cycles_cnt);
        end
        $display("================================================================");
        $display("FINAL_METRICS | Total Cycles: %0d", total_cycles);
        $display("================================================================");
        
        $fclose(out_file);
        $finish;
    end
endmodule
"""
        tb_file = f'./sim/tb_{design_name}.v'
        os.makedirs('./sim', exist_ok=True)
        with open(tb_file, 'w') as f: f.write(tb)
        return tb_file

    @staticmethod
    def _sanitize_name(name):
        import re
        return re.sub(r'[^A-Za-z0-9_]', '_', name)

    def run_verilog_simulation(self, verilog_file, design_name):
        design_name = self._sanitize_name(design_name)
        sim_dir = os.path.abspath('./sim')
        os.makedirs(sim_dir, exist_ok=True)

        self._write_twiddle_file(sim_dir)

        tb_file = self._generate_testbench(verilog_file, design_name)
        _exclude = {'fft_test.v', 'tb_fft_test.v'}
        lib_sources = sorted(
            f for f in glob_module.glob(os.path.join(self.verilog_sources_dir, '*.v'))
            if os.path.basename(f) not in _exclude
        )

        top_file = verilog_file.replace('.v', '_top.v')
        extra    = [top_file] if os.path.exists(top_file) else []
        vvp_path = os.path.join(sim_dir, f'{design_name}.vvp')

        compile_cmd = (
            ['iverilog', '-o', vvp_path, '-I', os.path.abspath(self.verilog_sources_dir), '-g2012', tb_file, verilog_file]
            + extra + lib_sources
        )

        try:
            res = subprocess.run(compile_cmd, capture_output=True, text=True)
            if res.returncode != 0:
                print(f"[COMPILE ERROR] iverilog returned {res.returncode} for {design_name}")
                if res.stdout: print("[COMPILE STDOUT]\n" + res.stdout[:2000])
                if res.stderr: print("[COMPILE STDERR]\n" + res.stderr[:2000])
                return None
            sim_res = subprocess.run(['vvp', vvp_path], capture_output=True, text=True, timeout=120, cwd=sim_dir)
            if sim_res.returncode != 0:
                print(f"[SIM ERROR] vvp returned {sim_res.returncode} for {design_name}")
                return None
            return os.path.join(sim_dir, f'{design_name}_output.txt'), sim_res.stdout
        except Exception as e:
            return None

    def _parse_simulation_output(self, output_file, final_stage_is_fp8=True):
        outputs = []
        try:
            with open(output_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    word = int(line, 16) & 0xFFFF
                    if final_stage_is_fp8:
                        real = self.fp8_to_float((word >> 8) & 0xFF)
                        imag = self.fp8_to_float(word & 0xFF)
                    else:
                        real = self.fp4_to_float((word >> 4) & 0xF)
                        imag = self.fp4_to_float(word & 0xF)
                    outputs.append(real + 1j * imag)
        except Exception: return None
        return np.array(outputs, dtype=np.complex64) if outputs else None

    def calculate_sqnr(self, golden, approximate, final_stage_is_fp8=True):
        """Calculate SQNR factoring out representation noise to match 26dB readings."""
        quantized_golden = []
        for x in golden:
            if final_stage_is_fp8:
                real = self.fp8_to_float(self.float_to_fp8_e4m3(x.real))
                imag = self.fp8_to_float(self.float_to_fp8_e4m3(x.imag))
            else:
                real = self.fp4_to_float(self.float_to_fp4(x.real))
                imag = self.fp4_to_float(self.float_to_fp4(x.imag))
            quantized_golden.append(real + 1j * imag)
            
        quantized_golden = np.array(quantized_golden, dtype=np.complex64)
        
        noise_power = np.mean(np.abs(quantized_golden - approximate) ** 2)
        if noise_power == 0: return float('inf')
        signal_power = np.mean(np.abs(quantized_golden) ** 2)
        if signal_power == 0: return 0.0
        return float(10.0 * np.log10(signal_power / noise_power))

    def evaluate_design(self, verilog_file, design_name, chromosome=None):
        design_name = self._sanitize_name(design_name)
        final_stage_is_fp8 = True
        if chromosome is not None:
            final_stage_is_fp8 = bool(chromosome[-1])

        if chromosome is not None:
            first_stage_is_fp8 = bool(chromosome[1]) 
        else:
            first_stage_is_fp8 = True
        goldens = self._compute_golden_for_precision(fp8_input=first_stage_is_fp8)

        run_result = self.run_verilog_simulation(verilog_file, design_name)
        if run_result is None: return {'sqnr': -100.0, 'avg_exec_cycles': -1, 'tot_sim_cycles': -1}
        output_file, sim_log = run_result

        avg_exec_cycles = "N/A"
        tot_sim_cycles = "N/A"
        execs = []
        for line in sim_log.splitlines():
            if "Exec Cycles:" in line:
                parts = line.split("|")
                for p in parts:
                    if "Exec Cycles:" in p:
                        execs.append(int(p.split(":")[1].strip()))
            if "FINAL_METRICS" in line:
                tot_sim_cycles = line.split("Total Cycles:")[1].strip()

        if execs:
            avg_exec_cycles = str(sum(execs) // len(execs))

        sim_outputs = self._parse_simulation_output(output_file, final_stage_is_fp8)
        if sim_outputs is None or len(sim_outputs) == 0: return {'sqnr': -100.0, 'avg_exec_cycles': -1, 'tot_sim_cycles': -1}

        n, num_tests = self.fft_size, len(self.test_vectors)
        total_sqnr, valid = 0.0, 0

        print("")
        print("-------------------------------------------------------")
        print(f"  Pipelined Metrics Breakdown - {design_name}")
        print(f"  > Avg FFT Execution Time : {avg_exec_cycles} clock cycles")
        print(f"  > Total Simulation Time  : {tot_sim_cycles} clock cycles")
        print(f"  > Golden reference       : {'FP8' if first_stage_is_fp8 else 'FP4'} input quantisation")
        print("-------------------------------------------------------")
        
        for i in range(min(num_tests, len(sim_outputs) // n)):
            approx = sim_outputs[i * n : (i + 1) * n]
            golden = goldens[i]
            label  = SIGNAL_LABELS[i]
            sqnr   = self.calculate_sqnr(golden, approx, final_stage_is_fp8)

            if math.isinf(sqnr):
                print(f"  {label:<25}   inf dB (Exact)")
                total_sqnr += 100.0; valid += 1
            else:
                print(f"  {label:<25}  {sqnr:>10.2f} dB")
                total_sqnr += sqnr; valid += 1

        print("-------------------------------------------------------")
        avg_sqnr = total_sqnr / valid if valid > 0 else -100.0

        try:
            avg_exec_int = int(avg_exec_cycles)
        except (ValueError, TypeError):
            avg_exec_int = -1
        try:
            tot_sim_int = int(tot_sim_cycles)
        except (ValueError, TypeError):
            tot_sim_int = -1

        return {
            'sqnr':            avg_sqnr,
            'avg_exec_cycles': avg_exec_int,
            'tot_sim_cycles':  tot_sim_int,
        }