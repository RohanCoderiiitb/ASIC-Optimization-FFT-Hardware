create_constraint_mode -name CONSTRAINTS \
-sdc_files { /home/ms2025019_vaishnavi/counter_pd_flow/counter_synth.sdc }

# ---------------- Timing library ----------------
create_library_set -name FAST_LIB \
-timing { /home/ms2025019_vaishnavi/cadence/pdks/cadence_45nm/lib/fast_vdd1v0_basicCells.lib }

# ---------------- Delay corner (no RC corner) ----------------
create_delay_corner -name DELAY_CORNER \
-library_set FAST_LIB

# ---------------- Analysis view ----------------
create_analysis_view -name FAST_VIEW \
-delay_corner DELAY_CORNER \
-constraint_mode CONSTRAINTS

# ---------------- Set analysis view ----------------
set_analysis_view -setup {FAST_VIEW} -hold {FAST_VIEW}

