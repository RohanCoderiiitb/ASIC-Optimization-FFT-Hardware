/*
 * fft_app.c
 *
 * Demonstrates the intended C-level usage of the FFT extension once a real
 * riscv32-unknown-elf-gcc is on hand. This mirrors exactly the instruction
 * sequence already verified in simulation via sw/fft_impulse_test.asm --
 * same load loop, same start/wait, same store loop.
 *
 * Not yet compiled/linked against real hardware (no riscv32-unknown-elf-gcc
 * in this environment) -- treat as the Milestone 7 target source, validated
 * at the instruction-encoding level today.
 */
#include <stdint.h>
#include "fft_intrinsics.h"

#define FFT_N 256

void fft_run(const uint16_t *samples_in, uint16_t *results_out) {
    for (uint32_t i = 0; i < FFT_N; i++) {
        fft_load(samples_in[i], (uint8_t)i);
    }

    fft_start();

    uint32_t ok = fft_wait(8000); /* generous timeout, see roadmap latency model */
    if (!ok) {
        /* timed out -- in a real application, signal an error here */
        return;
    }

    for (uint32_t i = 0; i < FFT_N; i++) {
        results_out[i] = fft_store((uint8_t)i);
    }
}

int main(void) {
    static uint16_t samples[FFT_N];
    static uint16_t results[FFT_N];

    samples[0] = 0x3800; /* impulse test vector, matches sim/fft_impulse_test.asm */
    for (uint32_t i = 1; i < FFT_N; i++) samples[i] = 0x0000;

    fft_run(samples, results);

    /* expected: results[k] == 0x3800 for all k */
    for (;;) { /* halt */ }
}
