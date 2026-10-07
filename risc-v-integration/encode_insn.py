#!/usr/bin/env python3
"""
encode_insn.py
==============
Hand-encoder for the 5 custom-0 FFT instructions, mirroring exactly what
`.insn r 0x0B, funct3, 0, rd, rs1, rs2` would produce with GNU `as`.

This exists so the SoC can be verified end-to-end in simulation *before*
Milestone 5 (binutils/gas opcode tables + GCC builtins) is built. Every
instruction word produced here is bit-for-bit what the real assembler will
emit once `fft_intrinsics.h` (sw/fft_intrinsics.h) is used with a patched
toolchain -- the same R-type field layout is used by both.

R-type encoding: [31:25 funct7][24:20 rs2][19:15 rs1][14:12 funct3][11:7 rd][6:0 opcode]
opcode = 0001011 (CUSTOM-0) for all seven instructions, funct7 = 0 for all of
them except FFTLOADHI/FFTSTOREHI (funct7 = 1).

FFTLOADHI/FFTSTOREHI (64-bit/FP32 samples only -- see
soc_wrapper_gen.generate_hilo64_wrapper) deliberately reuse FFTLOAD's/
FFTSTORE's own funct3 (001/010) rather than new funct3 codes, distinguishing
"low word" (funct7=0) from "high word" (funct7=1) instead. This is NOT
just an encoding-space choice: tb_fft_soc_perf.v's `c0_load`/`c0_store`
wires decode purely on funct3 (001/010), with no knowledge of a hilo64
protocol -- new funct3 codes would make it silently under-count (only the
"low" half of each transfer), which starves its own finished/EXPECTED
threshold of completions it can't ever see. Same funct3 as the existing
ops means every real transfer is counted once per instruction, same as the
single-register tracks, with no changes to that shared file.
"""

OPCODE_CUSTOM0 = 0b0001011

F3_FFTLOAD    = 0b001
F3_FFTSTORE   = 0b010
F3_FFTSTART   = 0b011
F3_FFTWAIT    = 0b100
F3_FFTSTATUS  = 0b101


def r_type(funct7, rs2, rs1, funct3, rd, opcode):
    return ((funct7 & 0x7F) << 25) | ((rs2 & 0x1F) << 20) | ((rs1 & 0x1F) << 15) | \
           ((funct3 & 0x7) << 12) | ((rd & 0x1F) << 7) | (opcode & 0x7F)


def fftload(rd, rs1, rs2):
    """FFTLOAD rd, rs1, rs2  -- rs1=packed sample, rs2=index. rd conventionally x0."""
    return r_type(0, rs2, rs1, F3_FFTLOAD, rd, OPCODE_CUSTOM0)


def fftstore(rd, rs1):
    """FFTSTORE rd, rs1      -- rs1=index, rd<-packed result."""
    return r_type(0, 0, rs1, F3_FFTSTORE, rd, OPCODE_CUSTOM0)


def fftstart():
    """FFTSTART              -- no operands."""
    return r_type(0, 0, 0, F3_FFTSTART, 0, OPCODE_CUSTOM0)


def fftwait(rd, rs1):
    """FFTWAIT rd, rs1       -- rs1=timeout cycles (0=forever), rd<-1 done/0 timeout."""
    return r_type(0, 0, rs1, F3_FFTWAIT, rd, OPCODE_CUSTOM0)


def fftstatus(rd):
    """FFTSTATUS rd          -- rd<-{31'b0,status_done}."""
    return r_type(0, 0, 0, F3_FFTSTATUS, rd, OPCODE_CUSTOM0)


def fftloadhi(rd, rs1, rs2):
    """FFTLOADHI rd, rs1, rs2 -- 64-bit samples only (FP32): same funct3 as
    FFTLOAD, funct7=1 marks it as the HIGH word. rs1=high word (imag),
    rs2=index (must match the preceding FFTLOAD's rs2). Combines with the
    low word FFTLOAD already staged and pulses load_en once. rd
    conventionally x0."""
    return r_type(1, rs2, rs1, F3_FFTLOAD, rd, OPCODE_CUSTOM0)


def fftstorehi(rd, rs1):
    """FFTSTOREHI rd, rs1    -- 64-bit samples only (FP32): same funct3 as
    FFTSTORE, funct7=1 marks it as the HIGH word. rs1=index (must match the
    preceding FFTSTORE's rs1), rd<-high word (imag) of the same result
    FFTSTORE already returned the low word for."""
    return r_type(1, 0, rs1, F3_FFTSTORE, rd, OPCODE_CUSTOM0)


if __name__ == "__main__":
    # Self-check against the funct3 table in fft_pcpi_wrapper.v
    tests = {
        "FFTLOAD":   fftload(0, 10, 11),
        "FFTSTORE":  fftstore(12, 11),
        "FFTSTART":  fftstart(),
        "FFTWAIT":   fftwait(13, 0),
        "FFTSTATUS": fftstatus(14),
    }
    for name, word in tests.items():
        opcode = word & 0x7F
        funct3 = (word >> 12) & 0x7
        print(f"{name:10s} = 0x{word:08x}  opcode={opcode:07b} funct3={funct3:03b}")
