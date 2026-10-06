/*
 * fft_intrinsics.h
 *
 * C wrappers around the 5 custom FFT instructions, using GNU `as`'s generic
 * `.insn r` directive. This works with a STOCK, unmodified riscv32-unknown-elf
 * toolchain -- no binutils/gas source patch is required for functional use.
 *
 * Patching riscv-opc.c/tc-riscv.c (Milestone 5 in the original roadmap) only
 * becomes worth doing if you want `objdump -d` to print "fftload" instead of
 * "custom-0" / raw hex, or want GCC to pattern-match these in normal C code
 * instead of via intrinsics. Functionally, `.insn r` already produces the
 * exact same instruction words as encode_insn.py / rv32i_asm.py verified in
 * simulation -- so this header is usable today, the moment a real toolchain
 * is on hand, with zero hardware-side changes.
 *
 * R-type layout used by all 5: .insn r opcode, funct3, funct7, rd, rs1, rs2
 *   opcode = 0x0B (CUSTOM-0), funct7 = 0 for all of them.
 */
#ifndef FFT_INTRINSICS_H
#define FFT_INTRINSICS_H

#include <stdint.h>

/* FFTLOAD rd, rs1, rs2 : rs1 = packed sample {real[7:0],imag[7:0]}, rs2 = index */
static inline void fft_load(uint16_t packed_sample, uint8_t index) {
    register uint32_t a asm("a0") = packed_sample;
    register uint32_t b asm("a1") = index;
    asm volatile (".insn r 0x0B, 1, 0, x0, %0, %1" :: "r"(a), "r"(b));
}

/* FFTSTORE rd, rs1 : rs1 = index, rd <- packed result */
static inline uint16_t fft_store(uint8_t index) {
    register uint32_t a asm("a0") = index;
    register uint32_t result asm("a0");
    asm volatile (".insn r 0x0B, 2, 0, %0, %1, x0" : "=r"(result) : "r"(a));
    return (uint16_t)result;
}

/* FFTSTART : no operands, pulses start and clears sticky done */
static inline void fft_start(void) {
    asm volatile (".insn r 0x0B, 3, 0, x0, x0, x0");
}

/* FFTWAIT rd, rs1 : rs1 = timeout cycles (0 = forever), rd = 1 done / 0 timeout */
static inline uint32_t fft_wait(uint32_t timeout_cycles) {
    register uint32_t a asm("a0") = timeout_cycles;
    register uint32_t result asm("a0");
    asm volatile (".insn r 0x0B, 4, 0, %0, %1, x0" : "=r"(result) : "r"(a));
    return result;
}

/* FFTSTATUS rd : rd <- {31'b0, status_done}, non-blocking */
static inline uint32_t fft_status(void) {
    register uint32_t result asm("a0");
    asm volatile (".insn r 0x0B, 5, 0, %0, x0, x0" : "=r"(result));
    return result;
}

#endif /* FFT_INTRINSICS_H */
