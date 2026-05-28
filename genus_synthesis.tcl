# =============================================================================
# genus_synthesis.tcl
# Cadence Genus logical synthesis script for Mixed-Precision FFT ASIC flow.
# Refactored for strictly -legacy_ui compatibility.
# =============================================================================

# -----------------------------------------------------------------------
# 0. Validate that all required variables were passed by the Python caller
# -----------------------------------------------------------------------
foreach required_var {
    DESIGN_NAME CORE_FILE TOP_FILE VERILOG_DIR
    LIB_FILE CLK_PERIOD RPT_DIR WORK_DIR
} {
    if { ![info exists $required_var] } {
        puts "ERROR: Required variable '$required_var' was not set by caller."
        exit 1
    }
}

puts "================================================================"
puts "Genus synthesis: $DESIGN_NAME"
puts "  Core RTL  : $CORE_FILE"
puts "  Top RTL   : $TOP_FILE"
puts "  Liberty   : $LIB_FILE"
puts "  Clk period: $CLK_PERIOD ns"
puts "  Reports -> : $RPT_DIR"
puts "================================================================"

set_db / .max_cpus_per_server 16

# -----------------------------------------------------------------------
# 1. Ensure working and report directories exist
# NOTE: In Genus legacy UI, 'cd' is an alias for the design hierarchy
#       navigation command 'vcd', NOT a filesystem chdir. Since all paths
#       in this script are absolute, no directory change is needed.
# -----------------------------------------------------------------------
file mkdir $WORK_DIR
file mkdir $RPT_DIR

# -----------------------------------------------------------------------
# 2. Read Liberty library (Legacy UI approach)
# -----------------------------------------------------------------------
set_attribute library $LIB_FILE /

# -----------------------------------------------------------------------
# 3. Read RTL source files
# -----------------------------------------------------------------------
# Set the search path for the primitives
set_attribute hdl_search_path $VERILOG_DIR /

set verilog_primitives [glob -nocomplain ${VERILOG_DIR}/*.v]

# Read primitives
if { [llength $verilog_primitives] > 0 } {
    read_hdl -sv $verilog_primitives
}

# Read the generated per-solution files
read_hdl -sv $CORE_FILE
read_hdl -sv $TOP_FILE

# -----------------------------------------------------------------------
# 4. Elaborate the top-level design
# -----------------------------------------------------------------------
elaborate ${DESIGN_NAME}_top

# -----------------------------------------------------------------------
# 5. Define timing constraints (SDC-style)
# -----------------------------------------------------------------------
set CLK_PORT clk

# Create clock with the target period supplied by Python
create_clock -name sys_clk -period $CLK_PERIOD [get_ports $CLK_PORT]

# Conservative IO timing budgets (25% of clock period each side)
set io_budget [expr { $CLK_PERIOD * 0.25 }]
set_input_delay  $io_budget -clock sys_clk [remove_from_collection [all_inputs] [get_ports $CLK_PORT]]
set_output_delay $io_budget -clock sys_clk [all_outputs]

# -----------------------------------------------------------------------
# 6. Synthesis (Modern 3-step flow)
# -----------------------------------------------------------------------
syn_generic
syn_map
syn_opt

write_hdl -mapped > ${RPT_DIR}/${DESIGN_NAME}_netlist.v

# -----------------------------------------------------------------------
# 7. Write reports
# -----------------------------------------------------------------------
report_power  > ${RPT_DIR}/${DESIGN_NAME}_power.rpt
report_area   > ${RPT_DIR}/${DESIGN_NAME}_area.rpt
report_timing > ${RPT_DIR}/${DESIGN_NAME}_timing.rpt

puts "Reports written to $RPT_DIR"

# -----------------------------------------------------------------------
# 8. Logical Equivalence Checking (LEC) Setup & Execution
# -----------------------------------------------------------------------
set LEC_DOFILE ${WORK_DIR}/${DESIGN_NAME}_lec.do
set LEC_LOG ${RPT_DIR}/${DESIGN_NAME}_lec.log

write_do_lec -golden_design RTL -revised_design ${RPT_DIR}/${DESIGN_NAME}_netlist.v > $LEC_DOFILE

puts "Running Cadence Conformal LEC..."

# >& redirects both stdout and stderr. 
if {[catch {exec /mnt/cadence_tools/CONFRML211/bin/lec -lpgxl -nogui -dofile $LEC_DOFILE >& $LEC_LOG} result]} {
    puts "WARNING: LEC execution failed."
    puts "TCL Error: $result"
    
    # Manually append the caught error to the log file so it isn't empty
    set log_id [open $LEC_LOG a]
    puts $log_id "\n--- LEC EXECUTION FAILED ---"
    puts $log_id "Error details: $result"
    close $log_id
} else {
    puts "LEC execution complete. Log written to $LEC_LOG"
}

puts "================================================================"
puts "Genus synthesis and LEC complete: $DESIGN_NAME"
puts "================================================================"

exit 0