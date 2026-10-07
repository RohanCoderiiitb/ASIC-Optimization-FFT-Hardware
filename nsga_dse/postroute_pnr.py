"""
postroute_pnr.py
=================
OpenROAD place-and-route driver shared by the FP16 and FP32 baselines, used
by run_fp16_postroute.py / run_fp32_postroute.py to extract POST-ROUTE PPA
(area, critical-path delay/slack, SAIF-measured power, Energy/FFT) as a
companion to the existing PRE-ROUTE flow in run_fp16_synthesis.py /
run_fp32_synthesis.py. The pre-route reports are untouched; post-route goes
to its own report file (fp16_postroute_ppa_report.txt / fp32_...).

STAGE REACHED: floorplan -> macro placement -> PDN -> global placement ->
detailed placement -> clock tree synthesis -> global route -> parasitics
estimated from the global-route topology -> post-route timing + SAIF power.

NOT reached: full DRC-clean detailed routing (TritonRoute). On this PDK
(GSCLIB045) a fixed ~2.3% of standard-cell instances (scattered across many
cell types, independent of PDN presence, routing-layer range, placement
density, and router random seed -- all tested) fail TritonRoute's pin-access
stage with "No access point", most likely because their Metal1 pin shapes
sit at the layer's minimum width while the process's real via enclosure
rule needs extra room on each side that these specific shapes don't have,
and TritonRoute isn't extending a stub to create a legal landing for them.
Routing around the resulting hard error (Tcl `catch`) technically lets the
script continue, but contaminates timing: paths through the unrouted pins
show multi-nanosecond artificial delays, and the reported worst-case
critical path then almost always lands on one of them -- i.e. the resulting
number would be actively misleading, not just imprecise. Global-route-based
parasitics do not have this failure mode (every net gets a uniform,
topology-based RC estimate), so that is the checkpoint this module reports
as "post-route". This is a real, substantial accuracy jump over pre-route
(actual floorplan/macro placement, actual clock tree, actual approximate
routing topology and parasitics) -- just not full signoff.

PDK FIXES APPLIED (once, to copies -- the original PDK files under
45_nm_PDK/ and fp16_baseline/fp32_baseline/*_SRAM_MACROS/ are never modified):
  1. Tech LEF: six device-level VIARULE blocks (M1_PO, M1_NWELL, M1_PSUB,
     M1_NIMP, M1_PIMP, M1_DIFF -- transistor-to-Metal1 contacts, not
     inter-metal routing vias) reference layers (Oxide, Poly) that
     TritonRoute's LEF parser rejects outright ("Unknown layer Oxide for
     viarule M1_DIFF"). Stripped, along with their USEVIARULE references;
     the ten inter-metal viarules (M2_M1 ... M11_M10) are untouched.
  2. SRAM macro LEF: pin/obstruction shapes are on layers named lowercase
     "metal3"/"metal4", while the tech LEF defines "Metal3"/"Metal4"
     (capitalized) -- a case mismatch that silently drops those shapes
     ("undefined layer (metal4) referenced"). Renamed to match.
  3. SRAM macro LEF: several pin RECT coordinates (e.g. 31.1175) are not a
     multiple of the tech LEF's 0.005 um manufacturing grid, which
     TritonRoute rejects ("offgrid pin shape"). All RECT coordinates are
     snapped to the nearest 0.005 um grid point (sub-nanometer correction,
     not a shape change).
  4. A net named "zero_" (materialized by OpenSTA's linker for the
     constant-tied SRAM control ports, e.g. `.addr0(9'h000)`) is classified
     GROUND signal type by odb, apparently because the tie-low cell's LEF
     output pin is marked USE GROUND on this PDK. TritonRoute refuses to
     route GROUND/POWER-type nets as ordinary signals ("move to special
     nets"). It is an ordinary constant-valued logic net, not a power rail,
     so it (and any other non-VDD/VSS net misclassified the same way) is
     forced back to SIGNAL type via the odb API before routing.

TIMING REPAIR: the flow previously went straight from placement/CTS/global-route
to report_checks with no resizer intervention, so any net left with a
too-weak driver for its fanout/length (a small-drive cell picked by Yosys'
synthesis-time mapping, which knows nothing about physical fanout) reported
its full, unbuffered RC delay -- multi-nanosecond single-net delays on an
otherwise unremarkable path, dwarfing the 10ns target. `repair_timing -setup`
(once after CTS on placement-based parasitics, then repeated after global
routing on the final estimate) lets the resizer buffer/upsize/clone cells
along the worst setup paths only -- exactly the standard-cell driver strength
fix this design needs, done in well under a second per call.

The post-route repair_timing calls are deliberately repeated (four calls, not
one): each call only reports/fixes violations against its current view of
parasitics, and fixing one path's driver strength shifts load onto its
neighbors enough to expose a handful of new, smaller violations the previous
call couldn't have seen yet. On the largest/deepest-logic case actually
hitting this (fp32 N=1024, ~90 logic levels of combinational depth on its
worst path), four successive calls converged as 240 -> 252 -> 67 -> 1 -> 0
violating endpoints, WNS -0.532ns -> -0.023ns -> +0.002ns -- i.e. the repeats
are what closes it, not any single call's effort setting. Smaller N converge
in one call; the extra calls are then a no-op (found 0 violations, <1s).

`repair_design` (the blanket max-cap/max-slew DRV sweep OpenROAD-flow-scripts
normally also runs after placement) was tried first and rejected: this
netlist has ~1000 nets nominally DRV-violating (most never on a timing-
critical path), and fixing all of them unconditionally inserted 30-45
thousand extra buffers -- tripling the standard-cell instance count. With
that many new cells the negotiation-based detailed-placement legalizer
reproducibly got stuck at ~72% of all movable cells permanently "illegal"
(same count regardless of global-placement density, free-area headroom, or
an extra global_placement re-pass), a legalizer scaling limit rather than a
capacity problem. `repair_timing -setup` alone only ever touches the cells
on paths actually failing the 10ns constraint -- on the worst-case N tested
(fp16 N=32, originally -4.50ns) it closed all 62 violating endpoints to
positive slack by upsizing just 2 gates, no buffers needed, in 0.36s.

Both fp16 and fp32 use an identical memory architecture: 4 instances of the
SRAM macro, always named core.mem.b0_sub0_ram / b0_sub1_ram / b1_sub0_ram /
b1_sub1_ram regardless of N (verified against N=2 and N=1024 netlists) --
matching the pre-route finding that area is ~constant across N because it is
dominated by this fixed-size memory. One floorplan size (per baseline, big
enough for the largest macro layout with margin) is therefore reused for
every N; post-route area is consequently expected to come out ~identical
across N within a baseline -- that is the fixed floorplan choice, not a bug.
"""

import math
import os
import re
import subprocess
import textwrap


def _fix_tech_lef(src_path, dst_path):
    """Strip the six device-level (Oxide/Poly-referencing) VIARULE blocks
    that TritonRoute's LEF parser rejects. See module docstring, fix #1."""
    content = open(src_path).read()
    for name in ("M1_PO", "M1_NWELL", "M1_PSUB", "M1_NIMP", "M1_PIMP", "M1_DIFF"):
        content = re.sub(rf"VIARULE {name} GENERATE.*?END {name}\n", "", content, flags=re.DOTALL)
        content = re.sub(rf"^\s*USEVIARULE {name} ;\n", "", content, flags=re.MULTILINE)
    with open(dst_path, "w") as f:
        f.write(content)


def _fix_sram_lef(src_path, dst_path, grid=0.005):
    """Fix layer-name case (metal3/4 -> Metal3/4) and snap RECT coordinates
    to the manufacturing grid. See module docstring, fixes #2-#3."""
    content = open(src_path).read()
    content = content.replace("metal3", "Metal3").replace("metal4", "Metal4")

    rect_re = re.compile(r"(RECT\s+)([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)\s*;")

    def snap(v):
        return f"{round(float(v) / grid) * grid:.3f}"

    def repl(m):
        coords = [snap(m.group(i)) for i in (2, 3, 4, 5)]
        return f"{m.group(1)}{coords[0]} {coords[1]} {coords[2]} {coords[3]} ;"

    with open(dst_path, "w") as f:
        f.write(rect_re.sub(repl, content))


def prepare_fixed_lefs(tech_lef_src, sram_lef_src, work_dir):
    """Idempotent: writes (or reuses) the corrected LEF copies for this
    baseline's PDK files under work_dir. Returns (tech_lef, sram_lef)."""
    os.makedirs(work_dir, exist_ok=True)
    tech_fixed = os.path.join(work_dir, "tech_fixed.lef")
    sram_fixed = os.path.join(work_dir, os.path.basename(sram_lef_src).replace(".lef", "_fixed.lef"))
    if not os.path.isfile(tech_fixed):
        _fix_tech_lef(tech_lef_src, tech_fixed)
    if not os.path.isfile(sram_fixed):
        _fix_sram_lef(sram_lef_src, sram_fixed)
    return tech_fixed, sram_fixed


def macro_grid_locations(macro_w, macro_h, gap=30.0, margin=30.0):
    """2x2 grid of (x, y) lower-left placement coordinates for 4 identical
    macros, plus the (die_w, die_h) floorplan size they imply, with `margin`
    border and `gap` between macros. All four of fp16/fp32's SRAM instances
    are the same macro type/size, so this one layout covers both."""
    locs = [
        (margin, margin),
        (margin + macro_w + gap, margin),
        (margin, margin + macro_h + gap),
        (margin + macro_w + gap, margin + macro_h + gap),
    ]
    die_w = 2 * macro_w + 2 * gap + 2 * margin
    die_h = 2 * macro_h + 2 * gap + 2 * margin
    return locs, die_w, die_h


class PostRoutePnR:
    def __init__(self, design_prefix, std_lib, tech_lef, cell_lef, sram_lef,
                 sram_liberty, macro_module, macro_instances, macro_w, macro_h,
                 clock_period, fixed_lef_dir, openroad_path="openroad",
                 sta_path="sta", timeout=1800, min_annotated_pins=20,
                 setup_margin=0.0):
        self.design_prefix = design_prefix          # "fp16_fft" / "fp32_fft"
        self.std_lib = os.path.abspath(std_lib)
        self.cell_lef = os.path.abspath(cell_lef)
        self.sram_liberty = os.path.abspath(sram_liberty)
        self.macro_module = macro_module            # e.g. sram_512x32_2rw
        self.macro_instances = macro_instances       # 4 instance names
        self.clock_period = clock_period
        self.openroad_path = openroad_path
        self.sta_path = sta_path
        self.timeout = timeout
        self.min_annotated_pins = min_annotated_pins
        # Extra setup margin (ns) `repair_timing -setup` targets on top of the
        # bare 0-slack requirement, so the design closes with real margin
        # instead of landing exactly on "slack (MET) 0.00". 0.0 keeps the
        # original bare-closure behaviour.
        self.setup_margin = setup_margin

        # `fixed_lef_dir` is caller-owned scratch space (e.g. this baseline's
        # own synth/ directory) -- deliberately NOT inside the vendored PDK
        # tree (45_nm_PDK/), so generated/corrected LEF copies never mix with
        # reference PDK data.
        self.tech_lef, self.sram_lef = prepare_fixed_lefs(
            os.path.abspath(tech_lef), os.path.abspath(sram_lef),
            os.path.abspath(fixed_lef_dir))

        # gap/margin larger than the function's own defaults: closing setup
        # timing needs repair_design/repair_timing to buffer the many
        # long/high-fanout nets synthesis (which has no placement info) left
        # under-driven -- see module docstring's TIMING REPAIR section. That
        # buffering roughly triples the standard-cell instance count, and
        # those cells only have the area *outside* the 4 fixed SRAM macros to
        # legalize into; at the tight default gap/margin the legalizer ran
        # for 1170s+ on N=32 alone and still didn't converge (free area
        # saturated at >90% utilization). This wider grid keeps the same
        # macro layout but gives the post-buffering cell count enough free
        # area to legalize in seconds instead of tens of minutes.
        locs, die_w, die_h = macro_grid_locations(macro_w, macro_h, gap=100.0, margin=50.0)
        self.macro_locations = locs
        # 10 um extra border between core and die edge for I/O pins.
        self.core_area = (10.0, 10.0, die_w - 10.0, die_h - 10.0)
        self.die_area = (0.0, 0.0, die_w, die_h)

    # ------------------------------------------------------------------
    def _tcl(self, n, netlist_v, top_module, saif_file, work_dir):
        design_name = f"{self.design_prefix}_{n}"
        dx0, dy0, dx1, dy1 = self.die_area
        cx0, cy0, cx1, cy1 = self.core_area

        macro_place_cmds = "\n".join(
            f"place_macro -macro_name {inst} -location {{{x:.3f} {y:.3f}}} -orientation R0"
            for inst, (x, y) in zip(self.macro_instances, self.macro_locations)
        )

        timing_rpt = os.path.join(work_dir, f"{design_name}_postroute_timing.rpt")
        power_rpt = os.path.join(work_dir, f"{design_name}_postroute_power.rpt")
        annot_rpt = os.path.join(work_dir, f"{design_name}_postroute_activity_annotation.rpt")
        area_rpt = os.path.join(work_dir, f"{design_name}_postroute_area.rpt")
        route_guide = os.path.join(work_dir, f"{design_name}.guide")

        return textwrap.dedent(f"""\
            read_lef {self.tech_lef}
            read_lef {self.cell_lef}
            read_lef {self.sram_lef}
            read_liberty {self.std_lib}
            read_liberty {self.sram_liberty}
            read_verilog {netlist_v}
            link_design {top_module}

            # See postroute_pnr.py module docstring, fix #4.
            set blk [ord::get_db_block]
            foreach net [$blk getNets] {{
              set nm [$net getName]
              if {{($nm != "VDD") && ($nm != "VSS") && \\
                  ([$net getSigType] == "GROUND" || [$net getSigType] == "POWER")}} {{
                $net setSigType "SIGNAL"
              }}
            }}

            set_wire_rc -signal -layer Metal4
            set_wire_rc -clock -layer Metal6

            create_clock -name clk -period {self.clock_period} [get_ports clk]
            set_input_delay  [expr {{{self.clock_period}}} / 4.0] -clock clk [all_inputs] -add_delay
            set_output_delay [expr {{{self.clock_period}}} / 4.0] -clock clk [all_outputs]
            set_false_path -from [get_ports rst]

            initialize_floorplan -die_area {{{dx0} {dy0} {dx1} {dy1}}} \\
                                 -core_area {{{cx0} {cy0} {cx1} {cy1}}} -site CoreSite
            make_tracks

            {macro_place_cmds}

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
            add_pdn_connect -grid grid -layers {{Metal1 Metal4}}
            add_pdn_connect -grid grid -layers {{Metal4 Metal5}}
            add_pdn_ring -grid grid -layers {{Metal8 Metal9}} -widths 2.0 -spacings 2.0 -core_offsets 4.0
            add_pdn_connect -grid grid -layers {{Metal5 Metal8}}
            pdngen

            global_placement -density 0.6
            detailed_placement
            estimate_parasitics -placement
            clock_tree_synthesis -root_buf BUFX4 -buf_list BUFX4
            set_propagated_clock [all_clocks]
            detailed_placement
            estimate_parasitics -placement
            repair_timing -setup -setup_margin {self.setup_margin}

            set_routing_layers -signal Metal2-Metal9 -clock Metal2-Metal9
            global_route -guide_file {route_guide}
            estimate_parasitics -global_routing
            repair_timing -setup -setup_margin {self.setup_margin}
            repair_timing -setup -setup_margin {self.setup_margin} -repair_tns 100
            repair_timing -setup -setup_margin {self.setup_margin} -repair_tns 100
            repair_timing -setup -setup_margin {self.setup_margin} -repair_tns 100

            report_checks -path_delay max -format full_clock_expanded > {timing_rpt}

            read_saif -scope tb_{design_name}/dut {saif_file}
            report_activity_annotation > {annot_rpt}
            report_power > {power_rpt}

            set core_w [expr {{{cx1} - {cx0}}}]
            set core_h [expr {{{cy1} - {cy0}}}]
            set fh [open {area_rpt} w]
            puts $fh "core_area_um2 [expr {{$core_w * $core_h}}]"
            close $fh

            exit
        """)

    # ------------------------------------------------------------------
    _ANNOT_RE = re.compile(r"^\s*saif\s+(\d+)", re.MULTILINE)
    _UNANNOT_RE = re.compile(r"^\s*unannotated\s+(\d+)", re.MULTILINE)

    def run(self, n, netlist_v, top_module, saif_file, work_dir):
        """Returns a results dict. Never raises for a routing/tool failure --
        callers check `ok`."""
        os.makedirs(work_dir, exist_ok=True)
        design_name = f"{self.design_prefix}_{n}"
        script_path = os.path.join(work_dir, f"{design_name}_postroute.tcl")
        log_path = os.path.join(work_dir, f"{design_name}_postroute.log")

        tcl = self._tcl(n, netlist_v, top_module, saif_file, work_dir)
        with open(script_path, "w") as f:
            f.write(tcl)

        fail = {
            "area_um2": None, "crit_delay_ns": None, "slack_ns": None,
            "power_mw": None, "power_source": "PNR_FAILED",
            "annotated_pins": 0, "total_pins": 0, "ok": False,
        }
        try:
            result = subprocess.run(
                [self.openroad_path, "-no_init", "-exit", script_path],
                capture_output=True, text=True, timeout=self.timeout, cwd=work_dir)
        except subprocess.TimeoutExpired:
            with open(log_path, "w") as f:
                f.write("TIMEOUT\n")
            return fail
        with open(log_path, "w") as f:
            f.write(result.stdout)
            f.write(result.stderr)
        if result.returncode != 0:
            return fail

        area_rpt = os.path.join(work_dir, f"{design_name}_postroute_area.rpt")
        timing_rpt = os.path.join(work_dir, f"{design_name}_postroute_timing.rpt")
        power_rpt = os.path.join(work_dir, f"{design_name}_postroute_power.rpt")
        annot_rpt = os.path.join(work_dir, f"{design_name}_postroute_activity_annotation.rpt")

        area_um2 = self._parse_area(area_rpt)
        crit_delay, slack_ns = self._parse_timing(timing_rpt)
        power_mw = self._parse_power(power_rpt)
        annotated, total = self._parse_annotation(annot_rpt)

        power_ok = power_mw is not None and annotated >= self.min_annotated_pins
        ok = area_um2 is not None and crit_delay is not None and power_ok

        return {
            "area_um2": area_um2,
            "crit_delay_ns": crit_delay if crit_delay is not None else 200.0,
            "slack_ns": slack_ns if slack_ns is not None else -1.0,
            "power_mw": power_mw if power_ok else None,
            "power_source": "saif_measured" if power_ok else "SAIF_ANNOTATION_FAILED",
            "annotated_pins": annotated,
            "total_pins": total,
            "ok": ok,
        }

    @staticmethod
    def _parse_area(path):
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("core_area_um2"):
                        return float(line.split()[1])
        except Exception:
            return None
        return None

    @staticmethod
    def _parse_timing(path):
        if not os.path.exists(path):
            return None, None
        slack_vals, arr_vals = [], []
        try:
            with open(path) as f:
                for line in f:
                    ll = line.lower()
                    if "slack" in ll:
                        m = re.search(r"([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)", line)
                        if m:
                            slack_vals.append(float(m.group(1)))
                    elif "data arrival time" in ll:
                        m = re.search(r"([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)", line)
                        if m:
                            arr_vals.append(float(m.group(1)))
        except Exception:
            return None, None
        if arr_vals:
            crit_delay = max(arr_vals)
            slack = min(slack_vals) if slack_vals else None
            return crit_delay, slack
        return None, None

    @staticmethod
    def _parse_power(path):
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                content = f.read()
            pat = re.compile(
                r"^\s*Total\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)",
                re.MULTILINE)
            m = pat.search(content)
            if m:
                return float(m.group(4)) * 1000.0
        except Exception:
            return None
        return None

    def _parse_annotation(self, path):
        if not os.path.exists(path):
            return 0, 0
        try:
            with open(path) as f:
                content = f.read()
            am = self._ANNOT_RE.search(content)
            um = self._UNANNOT_RE.search(content)
            annotated = int(am.group(1)) if am else 0
            unannotated = int(um.group(1)) if um else 0
            return annotated, annotated + unannotated
        except Exception:
            return 0, 0
