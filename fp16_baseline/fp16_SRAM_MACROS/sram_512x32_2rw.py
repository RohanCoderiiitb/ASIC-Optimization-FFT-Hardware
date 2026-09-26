# sram_512x32_2rw.py
#
# OpenRAM config for the FP16 baseline FFT memory (fp16_dual_bank_memory_concurrent).
#
# Memory architecture being replaced (N = 1024):
#   4 arrays  : b0_sub0, b0_sub1, b1_sub0, b1_sub1   -> instantiate this macro 4x
#   depth     : n/2 = 512 words each (9-bit compressed address)
#   width     : 32 bits  ([31:16] FP16 real, [15:0] FP16 imag)
#   ports     : true dual-port, each port reads AND writes -> 2 RW ports
#   read      : synchronous, 1-cycle latency

# --- Memory Dimensions ---
word_size = 32
num_words = 512

# --- Ports ---
# Two Read/Write ports to match the TDP inference (port A / port B)
num_rw_ports = 2
num_r_ports = 0
num_w_ports = 0

# --- Technology Node ---
# FreePDK45, same as the mixed-precision sram_512x24_2rw macro
tech_name = "freepdk45"

# --- Output Settings ---
# .v, .lib, .lef, .gds, .sp, .html, .log all land in this directory
output_name = "sram_512x32_2rw"
output_path = "openram_outputs/fp16_SRAM_MACROS/"

# --- Run Settings ---
# Set this to True for a quick run. False does full characterization across all corners (takes forever).
nominal_corner_only = True
