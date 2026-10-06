# fft_batch_test.asm
#
# Memory map (32KB / 8192 words total):
#   0x0000 (Word 0)    : program instructions
#   0x0800 (Word 512)  : DIAGNOSTIC: fftwait timeout counter (1 word)
#   0x2000 (Word 2048) : input samples,  11 x 256 words
#   0x5000 (Word 5120) : output results, 11 x 256 words
#
# DIAGNOSTIC BUILD: counts how many of the 11 tests hit the fftwait
# timeout instead of seeing status_done go high. The original firmware
# never checked fftwait's return value (a0), so a systematically-too-short
# wait would silently read back partially-computed FFT data every time
# and nobody would know. This build makes that visible without changing
# any of the existing load/store/results behavior.

_start:
    li   s11, 11             # Number of test signals
    li   s10, 0              # Current test index
    li   s1, 8192            # SAMPLES_BASE address (2048 * 4)
    li   s2, 20480           # RESULTS_BASE address (5120 * 4)
    li   s4, 2048             # TIMEOUT_COUNTER address (512 * 4)
    li   t2, 0
    sw   t2, 0(s4)            # clear timeout counter

outer_loop:
    bge  s10, s11, halt      # If test index >= 11, we are done

    # 1. Load 256 samples into accelerator
    li   s0, 0               # i = 0
    li   t1, 256
loop_load:
    lw   t0, 0(s1)
    fftload x0, t0, s0
    addi s1, s1, 4
    addi s0, s0, 1
    blt  s0, t1, loop_load

    # 2. Fire FFT and wait
    fftstart
    li   a1, 8000            # generous timeout
    fftwait a0, a1
    bne  a0, x0, no_timeout   # a0==1 -> status_done seen in time, skip
    lw   t2, 0(s4)
    addi t2, t2, 1
    sw   t2, 0(s4)            # a0==0 -> timed out, bump the counter
no_timeout:

    # 3. Store 256 results to memory
    li   s0, 0
loop_store:
    fftstore t0, s0
    sw   t0, 0(s2)
    addi s2, s2, 4
    addi s0, s0, 1
    blt  s0, t1, loop_store

    # Increment test counter and repeat
    addi s10, s10, 1
    j    outer_loop

halt:
    j halt