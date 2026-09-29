            read_lef /home/ihs24/Desktop/ASIC-Optimization-FFT-Hardware/_mixed_pnr_fixed_lef/tech_fixed.lef
            read_lef /home/ihs24/Desktop/ASIC-Optimization-FFT-Hardware/45_nm_PDK/cadence/cadence_45nm/lef/gsclib045_macro.lef
            read_lef /home/ihs24/Desktop/ASIC-Optimization-FFT-Hardware/_mixed_pnr_fixed_lef/sram_512x24_2rw_fixed.lef
            read_liberty /home/ihs24/Desktop/ASIC-Optimization-FFT-Hardware/45_nm_PDK/cadence/cadence_45nm/lib/fast_vdd1v0_basicCells.lib
            read_liberty /home/ihs24/Desktop/ASIC-Optimization-FFT-Hardware/openram_outputs/sram_512x24_2rw_TT_1p0V_25C.lib
            read_verilog /home/ihs24/Desktop/ASIC-Optimization-FFT-Hardware/mixed_postroute_work/mixed_fft_1024/mixed_fft_1024_netlist.v
            link_design mixed_fft_1024_top

            # See postroute_pnr.py module docstring, fix #4.
            set blk [ord::get_db_block]
            foreach net [$blk getNets] {
              set nm [$net getName]
              if {($nm != "VDD") && ($nm != "VSS") && \
                  ([$net getSigType] == "GROUND" || [$net getSigType] == "POWER")} {
                $net setSigType "SIGNAL"
              }
            }

            create_clock -name clk -period 10.0 [get_ports clk]
            set_input_delay  [expr {10.0} / 4.0] -clock clk [all_inputs] -add_delay
            set_output_delay [expr {10.0} / 4.0] -clock clk [all_outputs]
            set_false_path -from [get_ports rst]

            initialize_floorplan -die_area {0.0 0.0 512.01 619.01} \
                                 -core_area {10.0 10.0 502.01 609.01} -site CoreSite
            make_tracks

            place_macro -macro_name core.mem.b0_sub0_ram -location {30.000 30.000} -orientation R0
place_macro -macro_name core.mem.b0_sub1_ram -location {256.005 30.000} -orientation R0
place_macro -macro_name core.mem.b1_sub0_ram -location {30.000 309.505} -orientation R0
place_macro -macro_name core.mem.b1_sub1_ram -location {256.005 309.505} -orientation R0

            place_pins -hor_layers Metal5 -ver_layers Metal6

            add_global_connection -net VDD -pin_pattern "^VDD$" -power
            add_global_connection -net VSS -pin_pattern "^VSS$" -ground
            add_global_connection -net VDD -pin_pattern "^vdd$" -power
            add_global_connection -net VSS -pin_pattern "^gnd$" -ground
            global_connect

            set_voltage_domain -name CORE -power VDD -ground VSS
            define_pdn_grid -name grid -voltage_domains CORE
            add_pdn_stripe -grid grid -layer Metal1 -width 0.12 -followpins
            add_pdn_stripe -grid grid -layer Metal4 -width 1.0 -pitch 40 -offset 5
            add_pdn_stripe -grid grid -layer Metal5 -width 1.0 -pitch 40 -offset 5
            add_pdn_connect -grid grid -layers {Metal1 Metal4}
            add_pdn_connect -grid grid -layers {Metal4 Metal5}
            add_pdn_ring -grid grid -layers {Metal8 Metal9} -widths 2.0 -spacings 2.0 -core_offsets 4.0
            add_pdn_connect -grid grid -layers {Metal5 Metal8}
            pdngen

            global_placement -density 0.6
            detailed_placement
            estimate_parasitics -placement
            clock_tree_synthesis -root_buf BUFX4 -buf_list BUFX4
            set_propagated_clock [all_clocks]
            detailed_placement
            estimate_parasitics -placement

            set_routing_layers -signal Metal2-Metal9 -clock Metal2-Metal9
            global_route -guide_file /home/ihs24/Desktop/ASIC-Optimization-FFT-Hardware/mixed_postroute_work/mixed_fft_1024/mixed_fft_1024.guide
            estimate_parasitics -global_routing

            report_checks -path_delay max -format full_clock_expanded > /home/ihs24/Desktop/ASIC-Optimization-FFT-Hardware/mixed_postroute_work/mixed_fft_1024/mixed_fft_1024_postroute_timing.rpt

            read_saif -scope tb_mixed_fft_1024/dut /home/ihs24/Desktop/ASIC-Optimization-FFT-Hardware/mixed_postroute_work/mixed_fft_1024/mixed_fft_1024.saif
            report_activity_annotation > /home/ihs24/Desktop/ASIC-Optimization-FFT-Hardware/mixed_postroute_work/mixed_fft_1024/mixed_fft_1024_postroute_activity_annotation.rpt
            report_power > /home/ihs24/Desktop/ASIC-Optimization-FFT-Hardware/mixed_postroute_work/mixed_fft_1024/mixed_fft_1024_postroute_power.rpt

            set core_w [expr {502.01 - 10.0}]
            set core_h [expr {609.01 - 10.0}]
            set fh [open /home/ihs24/Desktop/ASIC-Optimization-FFT-Hardware/mixed_postroute_work/mixed_fft_1024/mixed_fft_1024_postroute_area.rpt w]
            puts $fh "core_area_um2 [expr {$core_w * $core_h}]"
            close $fh

            exit
