set design_netlisttype verilog
set init_verilog { /home/ms2025019_vaishnavi/counter_pd_flow/hdl_synth.v }
set init_design_set_top 0
set init_lef_file {
    /home/ms2025019_vaishnavi/cadence/pdks/cadence_45nm/lef/gsclib045_tech.lef
    /home/ms2025019_vaishnavi/cadence/pdks/cadence_45nm/lef/gsclib045_macro.lef
}
set init_pwr_net {VDD}
set init_gnd_net {VSS}
set init_mmmc_file /home/ms2025019_vaishnavi/counter_pd_flow/mmmc.tcl
init_design

setDrawView fplan
floorPlan -site CoreSite -r 0.7 0.7 2.5 2.5 2.5 2.5

#floorPlan -site CoreSite -r 0.7 0.7 20 20 20 20

editPin \
    -side BOTTOM \
    -layer M3 \
    -fixedPin 1 \
    -spreadType RANGE \
    -start {4 0} \
    -end {50 0} \
    -spreadDirection CounterClockwise \
    -pin {count[0] count[1] count[2] count[3]}

editPin \
    -side TOP \
    -layer M3 \
    -fixedPin 1 \
    -spreadType RANGE \
    -start {4 100} \
    -end {50 100} \
    -spreadDirection Clockwise \
    -pin {count[4] count[5] count[6] count[7]}

editPin \
    -side LEFT \
    -layer M3 \
    -fixedPin 1 \
    -spreadType RANGE \
    -start {0 20} \
    -end {0 60} \
    -spreadDirection CounterClockwise \
    -pin {clk rst_n}

setAddRingMode \
    -stacked_via_top_layer M11 \
    -stacked_via_bottom_layer M1
    
addRing -nets {VDD VSS} -type core_rings -layer {top M11 bottom M11 left M10 right M10} -width {top 0.7 bottom 0.7 left 0.7 right 0.7} -spacing {top 0.2 bottom 0.2 left 0.2 right 0.2} -offset {top 0.5 bottom 0.5 left 0.5 right 0.5}

addStripe -nets {VDD VSS} -layer M10 -direction vertical -width 0.3 -spacing 0.4 -set_to_set_distance 5 -start_from left 

globalNetConnect VDD -type pgpin -pin VDD -override -verbose -netlistOverride
globalNetConnect VSS -type pgpin -pin VSS -override -verbose -netlistOverride


sroute -connect {blockPin padPin padRing corePin floatingStripe } -allowJogging true -allowLayerChange true -blockPin useLef -targetviaLayerRange {M1 M11}

setPlaceMode -place_global_timing_effort high -place_global_cong_effort high -place_global_reorder_scan true
placeDesign
setDrawView place

reportCongestion -hotspot       #after placement

#check_design -type place/cts/route              #maybe after

optDesign -preCTS
timeDesign -preCTS


set clkbuf_cells {}
# Open the .lib file for reading
set file [open "/home/ms2025019_vaishnavi/cadence/pdks/cadence_45nm/lib/fast_vdd1v0_basicCells.lib" r]

# Read the file line by line
while {[gets $file line] != -1} {
    # Check if "CLKBUF" is present in the line
    if {[string match "*CLKBUF*" $line]} {
        # Extract the text inside parentheses
        if {[regexp {^\s*cell\s*\(\s*([^\)]+)\s*\)} $line match cell_name]} {
            # Add cell_name to the list if not already present
            if {[lsearch -exact $clkbuf_cells $cell_name] == -1} {
                lappend clkbuf_cells $cell_name
            }
        }
    }
}

close $file

clock_opt_design

optDesign -postCTS
timeDesign -postCTS


routedesign -globalDetail

set fillercells [list FILL1 FILL2 FILL4 FILL8 FILL16 FILL32 FILL64]
setFillerMode -corePrefix counter_FILL -core ${fillercells}
addFiller -cell $fillercells -prefix counter_FILL -markFixed

verify_drc
verifyConnectivity

#verifyWellAntenna
#verify_antenna
verify_PG_short

#report_timing
#report_power

streamOut counter.gds -mapFile streamOut.map -libName DesignLib
