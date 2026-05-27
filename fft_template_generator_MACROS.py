"""
Mixed-Precision FFT Template Generator  —  VARIANT A: RAM MACRO
================================================================
Identical to the baseline generator EXCEPT the mixed_memory_unified
instantiation is replaced with a direct SRAM macro call.

The macro is assumed to be named SRAM1024x24 from the 45nm lib.
If your library uses a different name, change MACRO_NAME below.

Key change vs baseline:
  - memory.v is no longer needed in the source list.
  - The core instantiates SRAM1024x24 (black box) directly.
  - Genus treats the macro as opaque → zero FF elaboration overhead.
  - Two macros are used (ping-pong banks), just like the RTL banks.

Interface of SRAM1024x24 (standard 45nm single-port SRAM):
  CK   — clock
  CSN  — chip select (active-low)
  WEN  — write enable (active-low)
  A    — address [9:0]
  D    — data in  [23:0]
  Q    — data out [23:0]  (registered, 1-cycle latency)

Adjust port names to match your actual library macro.
"""

import os
import math


# ── Change this to match the actual macro name in your 45nm lib ──────────────
MACRO_NAME   = "SRAM1024x24"
MACRO_DEPTH  = 1024          # hard depth of the macro
MACRO_WIDTH  = 24            # hard width of the macro
MACRO_ABITS  = 10            # address bits log2(MACRO_DEPTH)


class FFTTemplateGenerator:
    def __init__(self, fft_size):
        self.fft_size              = fft_size
        self.num_stages            = int(math.log2(fft_size))
        self.addr_width            = int(math.log2(fft_size)) + 1  # FIX: was fixed 11
        self.butterflies_per_stage = fft_size // 2
        self.total_butterflies     = self.butterflies_per_stage * self.num_stages
        self.chromosome_length     = self.num_stages * 2
        self.MAX_N_HW              = 1024   # unchanged — hardware supports 2..1024

        print(f"FFTTemplateGenerator (RAM MACRO) FFT-{fft_size}:")
        print(f"  Stages            : {self.num_stages}")
        print(f"  Butterflies/stage : {self.butterflies_per_stage}")
        print(f"  Chromosome length : {self.chromosome_length}")

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
            config['stages'].append({
                'stage_num'       : stage,
                'mult_precision'  : mult_prec,
                'add_precision'   : add_prec,
                'output_precision': add_prec,
            })
        return config

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
        top_file  = os.path.join(out_dir, f"{stem}_top.v")
        top_code  = self._generate_top(config, core_module_name, top_module_name)
        with open(top_file, 'w') as f:
            f.write(top_code)
        return output_file, top_file

    def generate_complete_fft(self, chromosome, output_dir='./generated_designs'):
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
        print(f"✓ Generated: {core_file}")
        print(f"✓ Generated: {top_file}")
        return top_file

    def analyze_chromosome_statistics(self, chromosome):
        config   = self.chromosome_to_config(chromosome)
        fp8_mult = sum(s['mult_precision'] for s in config['stages'])
        fp8_add  = sum(s['add_precision']  for s in config['stages'])
        return {
            'fp8_mult': fp8_mult, 'fp4_mult': self.num_stages - fp8_mult,
            'fp8_add' : fp8_add,  'fp4_add' : self.num_stages - fp8_add,
        }

    # ==========================================================================
    # Core generator — RAM MACRO variant
    # ==========================================================================
    def _generate_core(self, config, core_module_name):
        n      = config['fft_size']
        aw     = self.addr_width
        ns     = config['num_stages']
        MAXn   = config['MAX_N_HW']
        stages = config['stages']

        # ---- localparams ----
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

        # ---- precision mux ----
        prec_cases = []
        for s in stages:
            sn      = s['stage_num']
            rd_prec = "1'b1" if sn == 0 else f"STAGE{sn-1}_OUT_PREC"
            prec_cases.append(
                f"            10'd{sn}: begin\n"
                f"                cur_mult_prec = STAGE{sn}_MULT_PREC;\n"
                f"                cur_add_prec  = STAGE{sn}_ADD_PREC;\n"
                f"                cur_rd_prec   = {rd_prec};\n"
                f"                cur_wr_prec   = STAGE{sn}_OUT_PREC;\n"
                f"            end"
            )
        prec_mux = (
            "    always @(*) begin\n"
            "        if (ext_reading) begin\n"
            f"            cur_mult_prec = 1'b0;\n"
            f"            cur_add_prec  = 1'b0;\n"
            f"            cur_rd_prec   = 1'b{last_out_prec};\n"
            f"            cur_wr_prec   = 1'b0;\n"
            "        end else begin\n"
            "            case (curr_stage)\n"
            + '\n'.join(prec_cases) + "\n"
            "                default: begin\n"
            "                    cur_mult_prec = 1'b0; cur_add_prec = 1'b0;\n"
            "                    cur_rd_prec = 1'b1; cur_wr_prec = 1'b0;\n"
            "                end\n"
            "            endcase\n"
            "        end\n"
            "    end"
        )

        # ---- butterfly instances ----
        bf_lines = ["    // Per-stage butterfly wrappers"]
        for s in stages:
            sn = s['stage_num']
            bf_lines += [f"    wire [15:0] X_st{sn}, Y_st{sn};",
                         f"    wire        fp8_out_st{sn};"]
        bf_lines.append("")
        for s in stages:
            sn = s['stage_num']
            bf_lines += [
                f"    butterfly_wrapper #(",
                f"        .MULT_PRECISION({s['mult_precision']}),",
                f"        .ADD_PRECISION ({s['add_precision']})",
                f"    ) bf_st{sn} (",
                f"        .A            (A_24),",
                f"        .B            (B_24),",
                f"        .W            (twiddle),",
                f"        .X            (X_st{sn}),",
                f"        .Y            (Y_st{sn}),",
                f"        .output_is_fp8(fp8_out_st{sn})",
                f"    );", "",
            ]
        bf_lines += [
            "    reg [15:0] X_bf, Y_bf;",
            "    reg        bf_is_fp8;",
            "    always @(*) begin",
            "        case (curr_stage)",
        ]
        for s in stages:
            sn = s['stage_num']
            bf_lines.append(
                f"            10'd{sn}: begin X_bf = X_st{sn}; Y_bf = Y_st{sn}; bf_is_fp8 = fp8_out_st{sn}; end"
            )
        bf_lines += [
            "            default: begin X_bf = 16'h0; Y_bf = 16'h0; bf_is_fp8 = 1'b0; end",
            "        endcase", "    end",
        ]
        butterfly_block = '\n'.join(bf_lines)

        # ---- RAM MACRO instantiation (replaces mixed_memory_unified) ----
        # Two instances: bank0 and bank1 (ping-pong).
        # bank_sel=0 → read bank0 / write bank1.
        # bank_sel=1 → read bank1 / write bank0.
        # CSN (active-low chip select) always asserted.
        # WEN per bank: active-low, only when writing to that bank.
        #
        # The macro address is always MACRO_ABITS=10 bits regardless of
        # runtime N.  For N<1024 only entries 0..N-1 are ever used;
        # the rest are unused but allocated (black-box, no synthesis cost).
        ram_block = f"""\
    // =========================================================================
    // Ping-pong SRAM macro instances  ({MACRO_NAME})
    // bank_sel=0 : read bank0, write bank1
    // bank_sel=1 : read bank1, write bank0
    // =========================================================================

    // Bank 0
    wire [{MACRO_ABITS-1}:0] bank0_addr;
    wire [23:0]  bank0_din, bank0_dout;
    wire         bank0_wen;   // active-low write enable

    // Bank 1
    wire [{MACRO_ABITS-1}:0] bank1_addr;
    wire [23:0]  bank1_din, bank1_dout;
    wire         bank1_wen;

    // Address / data mux per bank
    //   Read bank  → rd address + no write (WEN=1)
    //   Write bank → wr address + write data (WEN=0 when wr_en_int)
    assign bank0_addr = active_bank_sel ? mem_wr_addr[{MACRO_ABITS-1}:0]
                                        : mem_rd_addr[{MACRO_ABITS-1}:0];
    assign bank0_din  = mem_wr_data;
    assign bank0_wen  = active_bank_sel ? ~mem_wr_en : 1'b1;

    assign bank1_addr = active_bank_sel ? mem_rd_addr[{MACRO_ABITS-1}:0]
                                        : mem_wr_addr[{MACRO_ABITS-1}:0];
    assign bank1_din  = mem_wr_data;
    assign bank1_wen  = active_bank_sel ? 1'b1 : ~mem_wr_en;

    {MACRO_NAME} sram_bank0 (
        .CK  (clk),
        .CSN (1'b0),          // always selected
        .WEN (bank0_wen),
        .A   (bank0_addr),
        .D   (bank0_din),
        .Q   (bank0_dout)
    );

    {MACRO_NAME} sram_bank1 (
        .CK  (clk),
        .CSN (1'b0),
        .WEN (bank1_wen),
        .A   (bank1_addr),
        .D   (bank1_din),
        .Q   (bank1_dout)
    );

    // Select read data from the correct bank (1-cycle macro latency)
    // Use a registered mux so timing is clean across the macro output.
    reg         bank_sel_d;
    always @(posedge clk) bank_sel_d <= active_bank_sel;

    wire [23:0] raw_rd_24 = bank_sel_d ? bank1_dout : bank0_dout;

    // Precision slice — registered output register with reset
    reg [15:0] rd_data_16_r;
    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            rd_data_16_r <= 16'h0000;
        end else begin
            if (cur_rd_prec)
                rd_data_16_r <= raw_rd_24[23:8];          // FP8
            else
                rd_data_16_r <= {{8'h00, raw_rd_24[7:0]}}; // FP4 zero-padded
        end
    end
    wire [15:0] rd_data_16 = rd_data_16_r;"""

        # ---- mem expand (same as baseline) ----
        mem_expand = (
            "    wire [7:0]  rd_fp8_as_fp4;\n"
            "    complex_fp8_to_fp4 rd_conv_down (\n"
            "        .complex_fp8(rd_data_16),\n"
            "        .complex_fp4(rd_fp8_as_fp4)\n"
            "    );\n"
            "    wire [15:0] rd_fp4_as_fp8;\n"
            "    complex_fp4_to_fp8 rd_conv_up (\n"
            "        .complex_fp4(rd_data_16[7:0]),\n"
            "        .complex_fp8(rd_fp4_as_fp8)\n"
            "    );\n"
            "    wire [23:0] mem_rd_24;\n"
            "    assign mem_rd_24 = cur_rd_prec\n"
            "                       ? {rd_data_16,    rd_fp8_as_fp4}\n"
            "                       : {rd_fp4_as_fp8, rd_data_16[7:0]};\n"
            "    reg [23:0] A_24, B_24;"
        )

        # ---- write-back (same as baseline) ----
        writeback_block = (
            "    wire [7:0]  X_fp4_packed, Y_fp4_packed;\n"
            "    wire [15:0] X_fp8_packed, Y_fp8_packed;\n"
            "    fp8_to_fp4_converter conv_xr (.fp8_in(X_reg[15:8]), .fp4_out(X_fp4_packed[7:4]));\n"
            "    fp8_to_fp4_converter conv_xi (.fp8_in(X_reg[7:0]),  .fp4_out(X_fp4_packed[3:0]));\n"
            "    fp8_to_fp4_converter conv_yr (.fp8_in(Y_reg[15:8]), .fp4_out(Y_fp4_packed[7:4]));\n"
            "    fp8_to_fp4_converter conv_yi (.fp8_in(Y_reg[7:0]),  .fp4_out(Y_fp4_packed[3:0]));\n"
            "    fp4_to_fp8_converter conv_xr8 (.fp4_in(X_reg[7:4]), .fp8_out(X_fp8_packed[15:8]));\n"
            "    fp4_to_fp8_converter conv_xi8 (.fp4_in(X_reg[3:0]), .fp8_out(X_fp8_packed[7:0]));\n"
            "    fp4_to_fp8_converter conv_yr8 (.fp4_in(Y_reg[7:4]), .fp8_out(Y_fp8_packed[15:8]));\n"
            "    fp4_to_fp8_converter conv_yi8 (.fp4_in(Y_reg[3:0]), .fp8_out(Y_fp8_packed[7:0]));\n"
            "    assign X_wr_24 = out_was_fp8 ? {X_reg,       X_fp4_packed}\n"
            "                                 : {X_fp8_packed, X_reg[7:0]};\n"
            "    assign Y_wr_24 = out_was_fp8 ? {Y_reg,       Y_fp4_packed}\n"
            "                                 : {Y_fp8_packed, Y_reg[7:0]};"
        )

        return f"""\
// =============================================================================
// Mixed-Precision FFT Core – {n}-point   [VARIANT A: RAM MACRO]
// Auto-generated by FFTTemplateGenerator (macro variant)
// Memory: two {MACRO_NAME} instances (ping-pong).
// Genus sees them as black boxes → zero FF elaboration for storage.
// =============================================================================
`timescale 1ns/1ps

// Black-box declaration so iverilog/Genus knows the port list.
// In Genus, the actual lib cell replaces this automatically.
// In simulation, replace with a behavioural model (see sram_behav.v).
(* black_box *)
module {MACRO_NAME} (
    input  wire              CK,
    input  wire              CSN,
    input  wire              WEN,
    input  wire [{MACRO_ABITS-1}:0] A,
    input  wire [23:0]       D,
    output reg  [23:0]       Q
);
`ifndef SYNTHESIS
    // Behavioural model for simulation only — ignored by Genus
    reg [23:0] mem [0:{MACRO_DEPTH-1}];
    always @(posedge CK) begin
        if (!CSN) begin
            if (!WEN) mem[A] <= D;
            Q <= mem[A];
        end
    end
`endif
endmodule

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

    localparam IDLE=4'd0, START_AGU=4'd1, WAIT_AGU_START=4'd2,
               READ_A=4'd3, READ_B=4'd4, WAIT_A=4'd5, WAIT_B=4'd6,
               COMPUTE=4'd7, WRITE_X=4'd8, WRITE_Y=4'd9,
               WAIT_AGU=4'd10, EVAL_AGU=4'd11, DONE_STATE=4'd12;
    reg [3:0] state;

{lparams}

    reg cur_mult_prec, cur_add_prec, cur_rd_prec, cur_wr_prec;

    // AGU
    reg  start_agu_reg, next_step_reg;
    wire [ADDR_WIDTH-1:0] idx_a, idx_b, k, curr_stage;
    wire done_stage, done_fft;

    dit_fft_agu_variable #(
        .MAX_N({MAXn}), .ADDR_WIDTH(ADDR_WIDTH)
    ) agu (
        .clk(clk), .reset(rst), .start(start_agu_reg),
        .N({aw}'d{n}), .next_step(next_step_reg),
        .idx_a(idx_a), .idx_b(idx_b), .k(k),
        .done_stage(done_stage), .done_fft(done_fft),
        .curr_stage(curr_stage), .twiddle_output()
    );

{prec_mux}

    // Twiddle ROM
    wire [15:0] twiddle;
    twiddle_factor_unified #(
        .MAX_N({MAXn}), .ADDR_WIDTH(ADDR_WIDTH)
    ) twiddle_rom (
        .clk(clk), .k(k), .n({aw}'d{n}),
        .PRECISION(cur_mult_prec), .twiddle_out(twiddle)
    );

    // Memory control signals
    reg  fft_bank_sel;
    wire active_bank_sel = (state == IDLE || ext_reading) ? ext_bank_sel : fft_bank_sel;

    wire [ADDR_WIDTH-1:0] mem_rd_addr = ext_reading   ? ext_rd_addr  :
                                        (state==READ_A)? idx_a :
                                        (state==READ_B)? idx_b : {{aw{{1'b0}}}};

    wire mem_wr_en = ext_wr_en ? 1'b1 :
                     (state==WRITE_X || state==WRITE_Y) ? 1'b1 : 1'b0;

    wire [ADDR_WIDTH-1:0] mem_wr_addr = ext_wr_en    ? ext_wr_addr :
                                        (state==WRITE_X)? idx_a :
                                        (state==WRITE_Y)? idx_b : {{aw{{1'b0}}}};

    wire [23:0] X_wr_24, Y_wr_24;
    wire [23:0] mem_wr_data = ext_wr_en    ? ext_wr_data :
                              (state==WRITE_X) ? X_wr_24 :
                              (state==WRITE_Y) ? Y_wr_24 : 24'd0;

    // =========================================================================
    // SRAM macro instances (ping-pong)
    // =========================================================================
{ram_block}

    assign ext_rd_data = rd_data_16;

    // Expand 16-bit read to 24-bit butterfly input
{mem_expand}

    // Butterfly instances
{butterfly_block}

    // Write-back packing
    reg [15:0] X_reg, Y_reg;
    reg        out_was_fp8;

{writeback_block}

    // FSM
    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            state <= IDLE; start_agu_reg <= 0; next_step_reg <= 0;
            fft_bank_sel <= 0; done <= 0;
            A_24 <= 0; B_24 <= 0; X_reg <= 0; Y_reg <= 0; out_was_fp8 <= 0;
        end else begin
            case (state)
                IDLE:           begin done<=0; if(start) begin fft_bank_sel<=0; state<=START_AGU; end end
                START_AGU:      begin start_agu_reg<=1; state<=WAIT_AGU_START; end
                WAIT_AGU_START: begin start_agu_reg<=0; state<=READ_A; end
                READ_A:         state<=READ_B;
                READ_B:         state<=WAIT_A;
                WAIT_A:         begin A_24<=mem_rd_24; state<=WAIT_B; end
                WAIT_B:         begin B_24<=mem_rd_24; state<=COMPUTE; end
                COMPUTE:        begin X_reg<=X_bf; Y_reg<=Y_bf; out_was_fp8<=bf_is_fp8; state<=WRITE_X; end
                WRITE_X:        state<=WRITE_Y;
                WRITE_Y:        begin next_step_reg<=1; state<=WAIT_AGU; end
                WAIT_AGU:       begin next_step_reg<=0; state<=EVAL_AGU; end
                EVAL_AGU: begin
                    if (done_fft)       begin fft_bank_sel<=1'b{ns%2}; state<=DONE_STATE; end
                    else if (done_stage) begin fft_bank_sel<=~fft_bank_sel; state<=READ_A; end
                    else                 state<=READ_A;
                end
                DONE_STATE: begin done<=1; if(!start) state<=IDLE; end
                default: state<=IDLE;
            endcase
        end
    end

endmodule
"""

    def _generate_top(self, config, core_module_name, top_module_name):
        n      = config['fft_size']
        aw     = self.addr_width
        ns     = config['num_stages']
        MAXn   = config['MAX_N_HW']
        addr_bits = int(math.log2(n))
        pad_bits  = aw - addr_bits
        br_in_expr       = f"{{{{ {pad_bits}'b0, load_addr }}}}" if pad_bits > 0 else "load_addr"
        unload_addr_expr = f"{{{{ {pad_bits}'b0, unload_addr }}}}" if pad_bits > 0 else "unload_addr"
        addr_msb = addr_bits - 1

        return f"""\
// =============================================================================
// Mixed-Precision FFT TOP – {n}-point  [VARIANT A: RAM MACRO]
// =============================================================================
`timescale 1ns/1ps

module {top_module_name} (
    input  wire        clk, rst, start,
    output reg         done,
    input  wire              load_en,
    input  wire [{addr_msb}:0]  load_addr,
    input  wire [15:0]       load_data,
    input  wire              unload_en,
    input  wire [{addr_msb}:0]  unload_addr,
    output wire [15:0]       unload_data
);
    wire [{aw-1}:0] load_addr_rev;
    bit_reverse #(.MAX_N({MAXn}), .WIDTH({aw})) br (
        .in({br_in_expr}), .N({aw}'d{n}), .out(load_addr_rev)
    );

    reg bank_sel;
    wire core_done;
    wire [15:0] core_rd_data;

    {core_module_name} #(.MAX_N({MAXn}), .ADDR_WIDTH({aw})) core (
        .clk(clk), .rst(rst), .start(start), .done(core_done),
        .ext_wr_en(load_en), .ext_wr_addr(load_addr_rev),
        .ext_wr_data({{load_data, 8'h00}}),
        .ext_reading(unload_en), .ext_rd_addr({unload_addr_expr}),
        .ext_rd_data(core_rd_data), .ext_bank_sel(bank_sel)
    );
    assign unload_data = core_rd_data;

    always @(posedge clk or negedge rst) begin
        if (!rst) begin done<=0; bank_sel<=1; end
        else begin
            if (load_en && !start) bank_sel<=1;
            if (start)             begin done<=0; bank_sel<=1; end
            else if (core_done)    begin done<=1; bank_sel<=1'b{ns%2}; end
            else if (!start&&done)  done<=0;
        end
    end
endmodule
"""


# =============================================================================
# Smoke-test
# =============================================================================
if __name__ == "__main__":
    os.makedirs("./generated_designs", exist_ok=True)
    for fft_sz in [8, 16, 32]:
        gen   = FFTTemplateGenerator(fft_size=fft_sz)
        ns    = gen.num_stages
        chrom = []
        for s in range(ns):
            chrom += [s % 2, (s + 1) % 2]
        print(f"\n--- FFT-{fft_sz}  chromosome: {chrom} ---")
        core_f, top_f = gen.generate_verilog(
            chrom, f"./generated_designs/macro_fft_{fft_sz}_test.v"
        )
        print(f"  Core : {core_f}")
        print(f"  Top  : {top_f}")