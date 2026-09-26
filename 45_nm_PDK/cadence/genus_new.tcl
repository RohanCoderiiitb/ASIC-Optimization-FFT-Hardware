# 1. Setup and Library 
# (Replace with the path to your lab's specific standard cell .lib file)
set_db library /home/digital-1/SRIP2026/cadence/cadence_45nm/lib/fast_vdd1v0_basicCells.lib
# 2. Read RTL (Only the NTT ALU file)
read_hdl -sv /home/digital-1/SRIP2026/cadence/ntt_alu.v

# 3. Elaborate the top module 
# (Targeting the pipelined version to see maximum frequency potential)
elaborate ntt_butterfly_unit_pipelined

# 4. Apply Constraints
# Targeting a highly aggressive 2.0ns (500MHz) clock to stress test the pipeline
create_clock -name clk -period 2.0 [get_ports clk]

# Constrain I/O to simulate realistic delays from adjacent modules (e.g., AGU/Memory)
set_input_delay  0.5 -clock clk [all_inputs]
set_output_delay 0.5 -clock clk [all_outputs]

# 5. Synthesize
syn_generic
syn_map
syn_opt

# 6. Generate Reports
report_timing > timing_ntt.rpt
report_area   > area_ntt.rpt
report_power  > power_ntt.rpt

# 7. Export Netlist (Optional)
write_hdl > ntt_butterfly_pipelined_netlist.v
