# sram_config_512x24.py

# --- Memory Dimensions ---
word_size = 24
num_words = 512

# --- Ports ---
# 1 Read/Write port is standard for compact SRAM
num_rw_ports = 2
num_r_ports = 0
num_w_ports = 0

# --- Technology Node ---
# OpenRAM includes FreePDK45 out of the box, which perfectly matches your 45nm targets
tech_name = "freepdk45"

# --- Output Settings ---
output_name = "sram_512x24_2rw"
output_path = "openram_outputs/"

# --- Run Settings ---
# Set this to True for a quick run. False does full characterization across all corners (takes forever).
nominal_corner_only = True