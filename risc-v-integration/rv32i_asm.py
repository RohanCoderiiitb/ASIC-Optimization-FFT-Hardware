#!/usr/bin/env python3
"""
rv32i_asm.py
============
A deliberately small two-pass assembler for a tiny RV32I subset, extended with
the 5 custom FFT instructions from encode_insn.py. This is NOT a replacement
for binutils/gas (that's Milestone 5) -- it exists purely so the SoC can be
exercised end-to-end in iverilog simulation right now, with the same R-type
field layout the real assembler will eventually emit for `.insn r 0x0B, ...`.

Supported mnemonics: addi, add, sub, lui, lw, sw, beq, bne, blt, bge, jal, j,
li (pseudo, expands to addi or lui+addi), nop, fftload, fftstore, fftstart,
fftwait, fftstatus.
"""

import sys
import re

REGS = {f"x{i}": i for i in range(32)}
REGS.update({
    "zero": 0, "ra": 1, "sp": 2, "gp": 3, "tp": 4,
    "t0": 5, "t1": 6, "t2": 7,
    "s0": 8, "fp": 8, "s1": 9,
    "a0": 10, "a1": 11, "a2": 12, "a3": 13, "a4": 14, "a5": 15, "a6": 16, "a7": 17,
    "s2": 18, "s3": 19, "s4": 20, "s5": 21, "s6": 22, "s7": 23, "s8": 24, "s9": 25,
    "s10": 26, "s11": 27,
    "t3": 28, "t4": 29, "t5": 30, "t6": 31,
})


def reg(name):
    return REGS[name.strip().rstrip(",")]


def r_type(funct7, rs2, rs1, funct3, rd, opcode):
    return ((funct7 & 0x7F) << 25) | ((rs2 & 0x1F) << 20) | ((rs1 & 0x1F) << 15) | \
           ((funct3 & 0x7) << 12) | ((rd & 0x1F) << 7) | (opcode & 0x7F)


def i_type(imm, rs1, funct3, rd, opcode):
    return ((imm & 0xFFF) << 20) | ((rs1 & 0x1F) << 15) | ((funct3 & 0x7) << 12) | \
           ((rd & 0x1F) << 7) | (opcode & 0x7F)


def s_type(imm, rs2, rs1, funct3, opcode):
    imm11_5 = (imm >> 5) & 0x7F
    imm4_0 = imm & 0x1F
    return (imm11_5 << 25) | ((rs2 & 0x1F) << 20) | ((rs1 & 0x1F) << 15) | \
           ((funct3 & 0x7) << 12) | (imm4_0 << 7) | (opcode & 0x7F)


def b_type(imm, rs2, rs1, funct3, opcode):
    imm12 = (imm >> 12) & 0x1
    imm10_5 = (imm >> 5) & 0x3F
    imm4_1 = (imm >> 1) & 0xF
    imm11 = (imm >> 11) & 0x1
    return (imm12 << 31) | (imm10_5 << 25) | ((rs2 & 0x1F) << 20) | ((rs1 & 0x1F) << 15) | \
           ((funct3 & 0x7) << 12) | (imm4_1 << 8) | (imm11 << 7) | (opcode & 0x7F)


def u_type(imm, rd, opcode):
    return ((imm & 0xFFFFF) << 12) | ((rd & 0x1F) << 7) | (opcode & 0x7F)


def j_type(imm, rd, opcode):
    imm20 = (imm >> 20) & 0x1
    imm10_1 = (imm >> 1) & 0x3FF
    imm11 = (imm >> 11) & 0x1
    imm19_12 = (imm >> 12) & 0xFF
    return (imm20 << 31) | (imm19_12 << 12) | (imm11 << 20) | (imm10_1 << 21) | \
           ((rd & 0x1F) << 7) | (opcode & 0x7F)


OPCODE_OP    = 0b0110011
OPCODE_OPIMM = 0b0010011
OPCODE_LOAD  = 0b0000011
OPCODE_STORE = 0b0100011
OPCODE_BRANCH = 0b1100011
OPCODE_LUI   = 0b0110111
OPCODE_JAL   = 0b1101111
OPCODE_CUSTOM0 = 0b0001011


def assemble(lines):
    """Two-pass assemble. Returns list of 32-bit words."""
    # Pass 1: strip comments/blank lines, record label addresses
    instrs = []
    labels = {}
    addr = 0
    for raw in lines:
        line = raw.split("#")[0].strip()
        if not line:
            continue
        if line.endswith(":"):
            labels[line[:-1]] = addr
            continue
        instrs.append((addr, line))
        # li / la may expand to 2 words; pre-scan to size them
        mnem = line.split()[0]
        op_count = 2 if mnem in ("li", "la") else 1
        # we don't know yet if li needs 1 or 2 words without knowing the imm
        # is small; conservatively check now (imm already known, label refs only
        # used by li for absolute addr which we resolve in pass 2 -- assume 2)
        if mnem in ("li",):
            imm_str = line.split(",", 1)[1].strip()
            imm = int(imm_str, 0)
            op_count = 1 if -2048 <= imm <= 2047 else 2
        addr += 4 * op_count

    # Pass 2: encode
    words = []
    pc = 0
    for orig_addr, line in instrs:
        parts = re.split(r"[,\s]+", line.strip())
        mnem = parts[0]
        args = parts[1:]

        def resolve(tok):
            return labels[tok] if tok in labels else int(tok, 0)

        if mnem == "addi":
            rd_, rs1_, imm = reg(args[0]), reg(args[1]), int(args[2], 0)
            words.append(i_type(imm, rs1_, 0b000, rd_, OPCODE_OPIMM)); pc += 4
        elif mnem == "add":
            rd_, rs1_, rs2_ = reg(args[0]), reg(args[1]), reg(args[2])
            words.append(r_type(0, rs2_, rs1_, 0b000, rd_, OPCODE_OP)); pc += 4
        elif mnem == "sub":
            rd_, rs1_, rs2_ = reg(args[0]), reg(args[1]), reg(args[2])
            words.append(r_type(0b0100000, rs2_, rs1_, 0b000, rd_, OPCODE_OP)); pc += 4
        elif mnem == "lui":
            rd_, imm = reg(args[0]), int(args[1], 0)
            words.append(u_type(imm, rd_, OPCODE_LUI)); pc += 4
        elif mnem == "lw":
            rd_ = reg(args[0])
            m = re.match(r"(-?\d+)\((\w+)\)", args[1])
            imm, rs1_ = int(m.group(1)), reg(m.group(2))
            words.append(i_type(imm, rs1_, 0b010, rd_, OPCODE_LOAD)); pc += 4
        elif mnem == "sw":
            rs2_ = reg(args[0])
            m = re.match(r"(-?\d+)\((\w+)\)", args[1])
            imm, rs1_ = int(m.group(1)), reg(m.group(2))
            words.append(s_type(imm, rs2_, rs1_, 0b010, OPCODE_STORE)); pc += 4
        elif mnem in ("beq", "bne", "blt", "bge"):
            rs1_, rs2_, tgt = reg(args[0]), reg(args[1]), resolve(args[2])
            f3 = {"beq": 0b000, "bne": 0b001, "blt": 0b100, "bge": 0b101}[mnem]
            words.append(b_type(tgt - pc, rs2_, rs1_, f3, OPCODE_BRANCH)); pc += 4
        elif mnem == "jal":
            rd_, tgt = reg(args[0]), resolve(args[1])
            words.append(j_type(tgt - pc, rd_, OPCODE_JAL)); pc += 4
        elif mnem == "j":
            tgt = resolve(args[0])
            words.append(j_type(tgt - pc, 0, OPCODE_JAL)); pc += 4
        elif mnem == "li":
            rd_ = reg(args[0])
            imm = int(args[1], 0)
            if -2048 <= imm <= 2047:
                words.append(i_type(imm, 0, 0b000, rd_, OPCODE_OPIMM)); pc += 4
            else:
                upper = (imm + 0x800) >> 12
                lower = imm - (upper << 12)
                words.append(u_type(upper, rd_, OPCODE_LUI)); pc += 4
                words.append(i_type(lower, rd_, 0b000, rd_, OPCODE_OPIMM)); pc += 4
        elif mnem == "nop":
            words.append(i_type(0, 0, 0b000, 0, OPCODE_OPIMM)); pc += 4
        elif mnem == "fftload":
            rd_, rs1_, rs2_ = reg(args[0]), reg(args[1]), reg(args[2])
            words.append(r_type(0, rs2_, rs1_, 0b001, rd_, OPCODE_CUSTOM0)); pc += 4
        elif mnem == "fftstore":
            rd_, rs1_ = reg(args[0]), reg(args[1])
            words.append(r_type(0, 0, rs1_, 0b010, rd_, OPCODE_CUSTOM0)); pc += 4
        elif mnem == "fftstart":
            words.append(r_type(0, 0, 0, 0b011, 0, OPCODE_CUSTOM0)); pc += 4
        elif mnem == "fftwait":
            rd_, rs1_ = reg(args[0]), reg(args[1])
            words.append(r_type(0, 0, rs1_, 0b100, rd_, OPCODE_CUSTOM0)); pc += 4
        elif mnem == "fftstatus":
            rd_ = reg(args[0])
            words.append(r_type(0, 0, 0, 0b101, rd_, OPCODE_CUSTOM0)); pc += 4
        else:
            raise ValueError(f"unsupported mnemonic: {mnem}")

    return words


def main():
    src_path = sys.argv[1]
    out_path = sys.argv[2]
    with open(src_path) as f:
        lines = f.readlines()
    words = assemble(lines)
    with open(out_path, "w") as f:
        for w in words:
            f.write(f"{w:08x}\n")
    print(f"assembled {len(words)} words -> {out_path}")


if __name__ == "__main__":
    main()
