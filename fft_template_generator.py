"""
Mixed-Precision FFT Template Generator (Pipelined II=1 Edition)
===============================================================
Generates per-solution core + top Verilog files for a high-performance,
fully pipelined DIT radix-2 FFT.
"""

import os
import math

class FFTTemplateGenerator:
    def __init__(self, fft_size):
        self.fft_size          = fft_size
        self.num_stages        = int(math.log2(fft_size))
        self.addr_width        = 11
        self.butterflies_per_stage = fft_size // 2
        self.total_butterflies = self.butterflies_per_stage * self.num_stages
        self.chromosome_length = self.num_stages * 2
        self.MAX_N_HW          = 1024

        self.MEM_RD_LATENCY    = 1
        self.TWIDDLE_LATENCY   = 10
        self.BUTTERFLY_LATENCY = 0
        self.TOTAL_PIPE_LATENCY = self.TWIDDLE_LATENCY + self.BUTTERFLY_LATENCY + 1

    def get_chromosome_length(self):
        return self.chromosome_length

    def chromosome_to_config(self, chromosome):
        config = {
            'fft_size'  : self.fft_size,
            'num_stages': self.num_stages,
            'addr_width': self.addr_width,
            'stages'    : [],
            'MAX_N_HW'  : self.MAX_N_HW,
        }
        for stage in range(self.num_stages):
            idx       = stage * 2
            mult_prec = int(chromosome[idx])     if idx     < len(chromosome) else 0
            add_prec  = int(chromosome[idx + 1]) if idx + 1 < len(chromosome) else 0
            out_prec  = add_prec   

            config['stages'].append({
                'stage_num'       : stage,
                'mult_precision'  : mult_prec,
                'add_precision'   : add_prec,
                'output_precision': out_prec,
            })
        return config

    def analyze_chromosome_statistics(self, chromosome):
        """Analyze the composition of the chromosome for logging."""
        config = self.chromosome_to_config(chromosome)
        num_stages = len(config['stages'])
        
        fp8_mult = sum(1 for s in config['stages'] if s['mult_precision'] == 1)
        fp4_mult = num_stages - fp8_mult
        fp8_add  = sum(1 for s in config['stages'] if s['add_precision'] == 1)
        fp4_add  = num_stages - fp8_add
        
        return {
            "FP8 Multipliers": f"{fp8_mult} ({fp8_mult/num_stages*100:.1f}%)",
            "FP4 Multipliers": f"{fp4_mult} ({fp4_mult/num_stages*100:.1f}%)",
            "FP8 Adders": f"{fp8_add} ({fp8_add/num_stages*100:.1f}%)",
            "FP4 Adders": f"{fp4_add} ({fp4_add/num_stages*100:.1f}%)",
        }

    def generate_verilog(self, chromosome, output_file):
        config = self.chromosome_to_config(chromosome)
        out_dir = os.path.dirname(os.path.abspath(output_file))
        os.makedirs(out_dir, exist_ok=True)

        stem             = os.path.splitext(os.path.basename(output_file))[0]
        core_module_name = f"{stem}_core"
        top_module_name  = f"{stem}_top"

        core_code = self._generate_core(config, core_module_name)
        with open(output_file, 'w') as f:
            f.write(core_code)

        top_file = os.path.join(out_dir, f"{stem}_top.v")
        top_code = self._generate_top(config, core_module_name, top_module_name)
        with open(top_file, 'w') as f:
            f.write(top_code)

        return output_file, top_file

    def generate_complete_fft(self, chromosome, output_dir='./generated_designs'):
        """Convenience wrapper; returns top file path."""
        config = self.chromosome_to_config(chromosome)
        os.makedirs(output_dir, exist_ok=True)

        base             = f"mixed_fft_{self.fft_size}"
        core_module_name = f"{base}_core"
        top_module_name  = f"{base}_top"

        core_file = f"{output_dir}/{base}_core.v"
        top_file  = f"{output_dir}/{base}_top.v"

        with open(core_file, 'w') as f:
            f.write(self._generate_core(config, core_module_name))
        with open(top_file, 'w') as f:
            f.write(self._generate_top(config, core_module_name, top_module_name))

        return top_file

    def _generate_core(self, config, core_module_name):
        n    = config['fft_size']
        aw   = self.addr_width        
        ns   = config['num_stages']
        MAXn = config['MAX_N_HW']
        stages = config['stages']

        lparams_lines = []
        for s in stages:
            sn = s['stage_num']
            lparams_lines += [
                f"    localparam STAGE{sn}_MULT_PREC = {s['mult_precision']};",
                f"    localparam STAGE{sn}_ADD_PREC  = {s['add_precision']};",
                f"    localparam STAGE{sn}_OUT_PREC  = {s['output_precision']};",
            ]
        lparams = '\n'.join(lparams_lines)
        last_out_prec = stages[-1]['output_precision']

        rd_prec_cases = []
        for s in stages:
            sn       = s['stage_num']
            rd_prec  = f"STAGE{sn}_ADD_PREC" if sn == 0 else f"STAGE{sn-1}_OUT_PREC"
            rd_prec_cases.append(
                f"            4'd{sn}: begin\n"
                f"                cur_mult_prec = STAGE{sn}_MULT_PREC;\n"
                f"                cur_rd_prec   = {rd_prec};\n"
                f"            end"
            )

        wr_prec_cases = []
        for s in stages:
            sn       = s['stage_num']
            wr_prec_cases.append(
                f"            4'd{sn}: begin\n"
                f"                cur_wr_prec   = STAGE{sn}_OUT_PREC;\n"
                f"            end"
            )

        prec_mux = (
            "    always @(*) begin\n"
            "        if (ext_reading) begin\n"
            "            cur_mult_prec = 1'b0;\n"
            f"            cur_rd_prec   = 1'b{last_out_prec};\n"
            "        end else begin\n"
            "            case (current_stage_stable)\n"
            + '\n'.join(rd_prec_cases) + "\n"
            "                default: begin\n"
            "                    cur_mult_prec = 1'b0; cur_rd_prec = 1'b1;\n"
            "                end\n"
            "            endcase\n"
            "        end\n"
            "    end\n\n"
            "    always @(*) begin\n"
            "        case (current_stage_stable_delayed)\n"
            + '\n'.join(wr_prec_cases) + "\n"
            "            default: cur_wr_prec = 1'b0;\n"
            "        endcase\n"
            "    end"
        )

        bf_lines = [
            "    // SINGLE SHARED BUTTERFLY UNIT",
            "    reg bf_mult_prec, bf_add_prec;",
            "    always @(*) begin",
            "        case (current_stage_stable_delayed)",
        ]
        for s in stages:
            sn = s['stage_num']
            mp = s['mult_precision']
            ap = s['add_precision']
            bf_lines.append(f"            4'd{sn}: begin bf_mult_prec = 1'b{mp}; bf_add_prec = 1'b{ap}; end")
        
        bf_lines += [
            "            default: begin bf_mult_prec = 1'b0; bf_add_prec = 1'b0; end",
            "        endcase",
            "    end",
            "",
            "    wire [15:0] X_bf, Y_bf;",
            "    wire        bf_is_fp8;",
            "    butterfly_wrapper shared_bf (",
            "        .A            (A_24_aligned),",
            "        .B            (B_24_aligned),",
            "        .W            (twiddle),",
            "        .mult_prec    (bf_mult_prec),",
            "        .add_prec     (bf_add_prec),",
            "        .X            (X_bf),",
            "        .Y            (Y_bf),",
            "        .output_is_fp8(bf_is_fp8)",
            "    );"
        ]
        butterfly_block = '\n'.join(bf_lines)

        mem_expand = (
            "    wire [7:0]  rd_a_fp8_as_fp4, rd_b_fp8_as_fp4;\n"
            "    complex_fp8_to_fp4 dec_a (.complex_fp8(rd_data_a_16), .complex_fp4(rd_a_fp8_as_fp4));\n"
            "    complex_fp8_to_fp4 dec_b (.complex_fp8(rd_data_b_16), .complex_fp4(rd_b_fp8_as_fp4));\n"
            "\n"
            "    wire [15:0] rd_a_fp4_as_fp8, rd_b_fp4_as_fp8;\n"
            "    complex_fp4_to_fp8 enc_a (.complex_fp4(rd_data_a_16[7:0]), .complex_fp8(rd_a_fp4_as_fp8));\n"
            "    complex_fp4_to_fp8 enc_b (.complex_fp4(rd_data_b_16[7:0]), .complex_fp8(rd_b_fp4_as_fp8));\n"
            "\n"
            "    wire [23:0] mem_rd_a_24 = cur_rd_prec ? {rd_data_a_16, rd_a_fp8_as_fp4} : {rd_a_fp4_as_fp8, rd_data_a_16[7:0]};\n"
            "    wire [23:0] mem_rd_b_24 = cur_rd_prec ? {rd_data_b_16, rd_b_fp8_as_fp4} : {rd_b_fp4_as_fp8, rd_data_b_16[7:0]};\n"
            "\n"
            "    (* srl_style = \"srl\" *) reg [23:0] A_24_pipe [0:9];\n"
            "    (* srl_style = \"srl\" *) reg [23:0] B_24_pipe [0:9];\n"
            "    integer j;\n"
            "    always @(posedge clk) begin\n"
            "        A_24_pipe[0] <= mem_rd_a_24;\n"
            "        B_24_pipe[0] <= mem_rd_b_24;\n"
            "        for (j = 1; j < 10; j = j + 1) begin\n"
            "            A_24_pipe[j] <= A_24_pipe[j-1];\n"
            "            B_24_pipe[j] <= B_24_pipe[j-1];\n"
            "        end\n"
            "    end\n"
            "    wire [23:0] A_24_aligned = A_24_pipe[9];\n"
            "    wire [23:0] B_24_aligned = B_24_pipe[9];"
        )

        writeback_block = (
            "    wire [7:0]  X_fp4_packed, Y_fp4_packed;\n"
            "    wire [15:0] X_fp8_packed, Y_fp8_packed;\n"
            "\n"
            "    fp8_to_fp4_converter conv_xr (.fp8_in(X_bf[15:8]), .fp4_out(X_fp4_packed[7:4]));\n"
            "    fp8_to_fp4_converter conv_xi (.fp8_in(X_bf[7:0]),  .fp4_out(X_fp4_packed[3:0]));\n"
            "    fp8_to_fp4_converter conv_yr (.fp8_in(Y_bf[15:8]), .fp4_out(Y_fp4_packed[7:4]));\n"
            "    fp8_to_fp4_converter conv_yi (.fp8_in(Y_bf[7:0]),  .fp4_out(Y_fp4_packed[3:0]));\n"
            "\n"
            "    fp4_to_fp8_converter conv_xr8 (.fp4_in(X_bf[7:4]), .fp8_out(X_fp8_packed[15:8]));\n"
            "    fp4_to_fp8_converter conv_xi8 (.fp4_in(X_bf[3:0]), .fp8_out(X_fp8_packed[7:0]));\n"
            "    fp4_to_fp8_converter conv_yr8 (.fp4_in(Y_bf[7:4]), .fp8_out(Y_fp8_packed[15:8]));\n"
            "    fp4_to_fp8_converter conv_yi8 (.fp4_in(Y_bf[3:0]), .fp8_out(Y_fp8_packed[7:0]));\n"
            "\n"
            "    assign X_wr_24 = bf_is_fp8 ? {X_bf, X_fp4_packed} : {X_fp8_packed, X_bf[7:0]};\n"
            "    assign Y_wr_24 = bf_is_fp8 ? {Y_bf, Y_fp4_packed} : {Y_fp8_packed, Y_bf[7:0]};"
        )

        return f"""\
// =============================================================================
// Mixed-Precision FFT Core - {n}-point FULLY PIPELINED II=1 ARCHITECTURE
// Active-low asynchronous reset (negedge rst).
// =============================================================================
`timescale 1ns/1ps

module {core_module_name} #(
    parameter MAX_N      = {MAXn},
    parameter ADDR_WIDTH = {aw}
)(
    input  wire        clk,
    input  wire        rst,

    input  wire        start,
    output reg         done,

    input  wire                  ext_wr_en,
    input  wire [ADDR_WIDTH-1:0] ext_wr_addr,
    input  wire [23:0]           ext_wr_data,

    input  wire                  ext_reading,
    input  wire [ADDR_WIDTH-1:0] ext_rd_addr,
    output wire [15:0]           ext_rd_data,

    input  wire                  ext_bank_sel
);

{lparams}

    reg cur_mult_prec;
    reg cur_rd_prec;
    reg cur_wr_prec;

    reg  start_agu_reg;
    wire streaming_enable;
    wire [ADDR_WIDTH-1:0] idx_a, idx_b, k;
    wire done_stage, done_fft;
    wire [ADDR_WIDTH-1:0] curr_stage;

    reg [5:0] pipeline_stall_cnt;
    wire agu_stall = (pipeline_stall_cnt > 0);
    
    // GHOST STALL FIX: Prevents the AGU from double-triggering after a flush
    reg just_unstalled;
    always @(posedge clk or negedge rst) begin
        if (!rst) just_unstalled <= 1'b0;
        else if (agu_stall) just_unstalled <= 1'b1;
        else just_unstalled <= 1'b0;
    end

    wire safe_done_stage = done_stage && !just_unstalled;
    wire safe_done_fft   = done_fft && !just_unstalled;
    
    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            pipeline_stall_cnt <= 0;
        end else if (safe_done_stage && !safe_done_fft) begin
            pipeline_stall_cnt <= {self.TOTAL_PIPE_LATENCY + 1};
        end else if (pipeline_stall_cnt > 0) begin
            pipeline_stall_cnt <= pipeline_stall_cnt - 1;
        end
    end

    // STALL-ALIGNED STAGE TRACKER
    reg [3:0] current_stage_stable;
    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            current_stage_stable <= 0;
        end else if (start) begin
            current_stage_stable <= 0;
        end else if (pipeline_stall_cnt == 1) begin
            current_stage_stable <= current_stage_stable + 1;
        end
    end

    dit_fft_agu_streaming #(
        .MAX_N     (MAX_N),
        .ADDR_WIDTH(ADDR_WIDTH)
    ) agu (
        .clk          (clk),
        .reset        (rst),
        .stall        (agu_stall),
        .start        (start_agu_reg),
        .N            ({aw}'d{n}),
        .stream_en    (streaming_enable),
        .idx_a        (idx_a),
        .idx_b        (idx_b),
        .k            (k),
        .done_stage   (done_stage),
        .done_fft     (done_fft),
        .curr_stage   (curr_stage)
    );

    wire [15:0] twiddle_rom_out;
    localparam TWIDDLE_LATENCY = {self.TWIDDLE_LATENCY};

    // Instantiate your Static Unified ROM (1-cycle registered execution)
    twiddle_factor_unified #(
        .MAX_N(MAX_N),
        .ADDR_WIDTH(ADDR_WIDTH)
    ) twiddle_gen (
        .clk        (clk),       // FIXED: Connected the clock!
        .k          (k),
        .n          ({aw}'d{n}),
        .PRECISION  (cur_mult_prec),
        .twiddle_out(twiddle_rom_out)
    );

    // FIXED: Shift the pipeline to accommodate the ROM's 1-cycle delay
    (* srl_style = "srl" *) reg [15:0] twiddle_pipe [1:TWIDDLE_LATENCY]; 
    (* srl_style = "srl" *) reg        v_pipe       [0:TWIDDLE_LATENCY]; 
    integer t_idx;
    
    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            for (t_idx = 1; t_idx <= TWIDDLE_LATENCY; t_idx = t_idx + 1) begin
                twiddle_pipe[t_idx] <= 16'd0;
            end
            for (t_idx = 0; t_idx <= TWIDDLE_LATENCY; t_idx = t_idx + 1) begin
                v_pipe[t_idx]       <= 1'b0;
            end
        end else begin
            v_pipe[0] <= streaming_enable;
            
            // Stage 1 catches the ROM output and aligns perfectly with v_pipe[1]
            twiddle_pipe[1] <= twiddle_rom_out;
            v_pipe[1]       <= v_pipe[0];
            
            for (t_idx = 2; t_idx <= TWIDDLE_LATENCY; t_idx = t_idx + 1) begin
                twiddle_pipe[t_idx] <= twiddle_pipe[t_idx-1];
                v_pipe[t_idx]       <= v_pipe[t_idx-1];
            end
        end
    end
    
    // Tap off the END of the 11-stage pipeline, gating with valid_in
    wire [15:0] twiddle = v_pipe[TWIDDLE_LATENCY] ? twiddle_pipe[TWIDDLE_LATENCY] : 16'h0000;
    localparam TOTAL_LATENCY = {self.TOTAL_PIPE_LATENCY};

    (* srl_style = "srl" *) reg [TOTAL_LATENCY-1:0]  wr_en_pipe;
    (* srl_style = "srl" *) reg [ADDR_WIDTH-1:0]     wr_addr_a_pipe    [0:TOTAL_LATENCY-1];
    (* srl_style = "srl" *) reg [ADDR_WIDTH-1:0]     wr_addr_b_pipe    [0:TOTAL_LATENCY-1];
    (* srl_style = "srl" *) reg [3:0]                stable_stage_pipe [0:TOTAL_LATENCY-1];

    reg                      fft_bank_sel;

    integer i;
    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            wr_en_pipe <= 0;
            for (i = 0; i < TOTAL_LATENCY; i = i + 1) begin
                wr_addr_a_pipe[i]  <= 0;  
                wr_addr_b_pipe[i]  <= 0;
                stable_stage_pipe[i] <= 0;
            end
        end else begin
            wr_en_pipe <= {{wr_en_pipe[TOTAL_LATENCY-2:0], streaming_enable}};
            
            wr_addr_a_pipe[0]  <= idx_a;
            wr_addr_b_pipe[0]  <= idx_b;
            stable_stage_pipe[0] <= current_stage_stable;

            for (i = 1; i < TOTAL_LATENCY; i = i + 1) begin
                wr_addr_a_pipe[i]  <= wr_addr_a_pipe[i-1];
                wr_addr_b_pipe[i]  <= wr_addr_b_pipe[i-1];
                stable_stage_pipe[i] <= stable_stage_pipe[i-1];
            end
        end
    end

    wire                  mem_wr_en       = wr_en_pipe[TOTAL_LATENCY-1];
    wire [ADDR_WIDTH-1:0] mem_wr_addr_a   = wr_addr_a_pipe[TOTAL_LATENCY-1];
    wire [ADDR_WIDTH-1:0] mem_wr_addr_b   = wr_addr_b_pipe[TOTAL_LATENCY-1];
    
    // CRITICAL FIX: Eliminate the 11-cycle delay on the write bank!
    // Stalls guarantee writes finish before the next stage starts.
    wire                  mem_wr_bank     = fft_bank_sel;
    wire [3:0]            current_stage_stable_delayed = stable_stage_pipe[TOTAL_LATENCY-1];

{prec_mux}

    wire                  active_rd_bank = ext_reading ? ext_bank_sel : fft_bank_sel;
    wire [ADDR_WIDTH-1:0] mem_rd_addr_a  = ext_reading ? ext_rd_addr  : idx_a;
    wire [ADDR_WIDTH-1:0] mem_rd_addr_b  = ext_reading ? ext_rd_addr  : idx_b;

    wire [15:0] rd_data_a_16, rd_data_b_16;
    wire [23:0] X_wr_24, Y_wr_24;
    assign ext_rd_data = rd_data_a_16;

    mixed_dual_bank_memory_concurrent #(
        .n         ({n}),
        .ADDR_WIDTH(ADDR_WIDTH)
    ) mem (
        .clk          (clk),
        .rst          (rst),
        
        .bank_pingpong (active_rd_bank),
        .stage_mask    (11'h001),
        .rd_addr_a     (mem_rd_addr_a),
        .rd_addr_b     (mem_rd_addr_b),
        .rd_precision  (cur_rd_prec),
        .rd_data_a     (rd_data_a_16),
        .rd_data_b     (rd_data_b_16),
        
        .wr_en         (ext_wr_en ? 1'b1 : mem_wr_en),
        .wr_addr_a     (ext_wr_en ? ext_wr_addr : mem_wr_addr_a),
        .wr_addr_b     (ext_wr_en ? ext_wr_addr : mem_wr_addr_b),
        .wr_data_a     (ext_wr_en ? ext_wr_data : X_wr_24),
        .wr_data_b     (ext_wr_en ? ext_wr_data : Y_wr_24),

        .bank_pingpong_wr (ext_wr_en ? 1'b0 : mem_wr_bank),
        .stage_mask_wr    (11'h001)
    );

{mem_expand}
{butterfly_block}
{writeback_block}

    localparam IDLE_ST   = 2'd0,
               RUN_ST    = 2'd1,
               FLUSH_ST  = 2'd2,
               DONE_ST   = 2'd3;

    reg [1:0]  state;
    reg [5:0]  flush_counter;

    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            state         <= IDLE_ST;
            start_agu_reg <= 1'b0;
            fft_bank_sel  <= 1'b0;
            done          <= 1'b0;
            flush_counter <= 0;
        end else begin
            case (state)
                IDLE_ST: begin
                    done <= 1'b0;
                    if (start) begin
                        fft_bank_sel  <= 1'b1;
                        start_agu_reg <= 1'b1;
                        state         <= RUN_ST;
                    end
                end

                RUN_ST: begin
                    start_agu_reg <= 1'b0;
                    if (pipeline_stall_cnt == 1) begin
                        fft_bank_sel <= ~fft_bank_sel;
                    end
                    if (safe_done_fft) begin
                        state         <= FLUSH_ST;
                        flush_counter <= TOTAL_LATENCY;
                    end
                end

                FLUSH_ST: begin
                    if (flush_counter == 0) begin
                        fft_bank_sel <= 1'b{1 ^ (ns % 2)};
                        done         <= 1'b1;
                        state        <= DONE_ST;
                    end else begin
                        flush_counter <= flush_counter - 1;
                    end
                end

                DONE_ST: begin
                    if (!start) begin
                        done  <= 1'b0;
                        state <= IDLE_ST;
                    end
                end
                default: state <= IDLE_ST;
            endcase
        end
    end
endmodule
"""

    def _generate_top(self, config, core_module_name, top_module_name):
        n    = config['fft_size']
        aw   = self.addr_width
        ns   = config['num_stages']
        MAXn = config['MAX_N_HW']
        addr_bits = int(math.log2(n))
        pad_bits  = aw - addr_bits          
        
        if pad_bits > 0:
            br_in_expr = "{{ " + str(pad_bits) + "'b0, load_addr }}"
            unload_addr_expr = "{{ " + str(pad_bits) + "'b0, unload_addr }}"
        else:
            br_in_expr = "load_addr"
            unload_addr_expr = "unload_addr"
        addr_msb = addr_bits - 1  

        return f"""\
// =============================================================================
// Mixed-Precision FFT TOP - {n}-point PIPELINED CONFIGURATION
// =============================================================================
`timescale 1ns/1ps

module {top_module_name} (
    input  wire        clk,
    input  wire        rst,     

    input  wire        start,
    output reg         done,

    input  wire              load_en,
    input  wire [{addr_msb}:0]  load_addr,
    input  wire [15:0]       load_data,

    input  wire              unload_en,
    input  wire [{addr_msb}:0]  unload_addr,
    output wire [15:0]       unload_data
);

    wire [{aw-1}:0] load_addr_rev;

    bit_reverse #(
        .MAX_N({MAXn}),
        .WIDTH({aw})
    ) br (
        .in  ({br_in_expr}),
        .N   ({aw}'d{n}),
        .out (load_addr_rev)
    );

    reg bank_sel;
    wire core_done;
    wire [15:0] core_rd_data;

    wire [7:0] load_fp4;
    complex_fp8_to_fp4 load_fmt_conv (
        .complex_fp8 (load_data),
        .complex_fp4 (load_fp4)
    );
    wire [23:0] load_data_24 = {{load_data, load_fp4}};

    {core_module_name} #(
        .MAX_N     ({MAXn}),
        .ADDR_WIDTH({aw})
    ) core (
        .clk          (clk),
        .rst          (rst),
        .start        (start),
        .done         (core_done),

        .ext_wr_en    (load_en),
        .ext_wr_addr  (load_addr_rev),
        .ext_wr_data  (load_data_24),

        .ext_reading  (unload_en),
        .ext_rd_addr  ({unload_addr_expr}),
        .ext_rd_data  (core_rd_data),

        .ext_bank_sel (bank_sel)
    );

    assign unload_data = core_rd_data;

    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            done     <= 1'b0;
            bank_sel <= 1'b1;
        end else begin
            if (load_en && !start)
                bank_sel <= 1'b1;

            if (start) begin
                done     <= 1'b0;
                bank_sel <= 1'b1;
            end else if (core_done) begin
                done     <= 1'b1;
                bank_sel <= 1'b{1 ^ (ns % 2)};
            end else if (!start && done) begin
                done <= 1'b0;
            end
        end
    end
endmodule
"""