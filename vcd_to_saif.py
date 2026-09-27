"""
vcd_to_saif.py
==============
Minimal VCD -> SAIF converter, written specifically to feed OpenSTA's
`read_saif` from an Icarus Verilog (iverilog/vvp) simulation.

WHY THIS EXISTS
---------------
Icarus Verilog has no native SAIF dumper -- only `$dumpfile`/`$dumpvars`
(VCD). To switch the ASIC baseline power-extraction methodology from
`read_vcd` to `read_saif` WITHOUT changing the RTL, the testbench, the
workload, or the simulation itself, this module parses the exact same VCD
Icarus already writes and re-emits the identical toggle data (T0/T1/TX/TZ/TC
per bit) in SAIF's own grammar. Every number in the output SAIF is computed
directly from the VCD's own recorded value-change times: this is a lossless
container conversion of one simulation's activity, not a resimulation and
not a different workload.

FORMAT NOTES
------------
- SAIF's identifier token (SaifLex.ll: `ID [A-Za-z_][A-Za-z0-9_$\\[\\]\\.]*`)
  accepts `[`, `]`, `$`, `.` as part of a *bare* (unescaped) identifier, so
  per-bit names are emitted unescaped, e.g. `load_data[3]` -- matching how
  OpenSTA already matches per-bit net names from `read_vcd` on this same
  design (verified: both readers use the same net-name-based annotation
  path in OpenSTA).
- Only `$scope module ... $end` levels become SAIF `(INSTANCE ...)` blocks,
  mirroring real Verilog instance hierarchy. `$scope begin/fork/task/
  function` blocks (named procedural blocks -- e.g. `MEM_READ0` inside the
  SRAM behavioral model, or a testbench's `initial begin : STIM`) are
  simulation bookkeeping, not synthesizable design hierarchy, and are
  skipped entirely, along with anything declared inside them.
- Header layout (SAIFVERSION/DIRECTION/DIVIDER/TIMESCALE/DURATION/INSTANCE
  nesting/NET (T0)(T1)(TX)(TZ)(TB)(TC) fields) matches a real Vivado-xsim
  SAIF captured for this same FFT design, and OpenSTA's SaifParse.yy grammar.
- TIMESCALE and DURATION come directly from the VCD's own `$timescale` and
  its last `#<time>` marker -- nothing is guessed or rescaled.

USAGE
-----
    from vcd_to_saif import vcd_to_saif
    vcd_to_saif("/path/to/sim.vcd", "/path/to/out.saif")

STATUS: unit-tested against a real generated VCD for fp16_fft_2 (verified:
non-trivial, non-zero T0/T1/TC values, and OpenSTA's `read_saif` on the
result reports annotated pin activity in the same ballpark as `read_vcd` on
the source VCD for the same design). Not fuzz-tested against arbitrary VCDs
from other tools -- it targets exactly what iverilog/vvp writes.
"""

import re
import sys
from datetime import datetime

_BIT_BUCKET = {'0': 't0', '1': 't1', 'z': 'tz', 'Z': 'tz'}


def _bucket_for(ch):
    return _BIT_BUCKET.get(ch, 'tx')


class _ScopeNode:
    __slots__ = ("name", "children", "nets")

    def __init__(self, name):
        self.name = name
        self.children = {}
        self.nets = []  # list of (label, t0, t1, tx, tz, tc)

    def child(self, name):
        node = self.children.get(name)
        if node is None:
            node = _ScopeNode(name)
            self.children[name] = node
        return node


def _pad_value(value, width):
    """VCD bus-value padding rule: pad on the left with '0', unless the
    leftmost given character is x/z, in which case pad with that character."""
    if len(value) < width:
        pad_char = value[0] if value[0] in 'xXzZ' else '0'
        value = pad_char * (width - len(value)) + value
    elif len(value) > width:
        value = value[-width:]
    return value


class _IdState:
    """Per-VCD-identifier tracking: one bit-state slot per declared bit,
    shared across every (possibly aliased) target net that identifier feeds."""
    __slots__ = ("width", "targets", "prev_char", "prev_time",
                 "t0", "t1", "tx", "tz", "tc")

    def __init__(self, width):
        self.width = width
        self.targets = []  # (scope_path_tuple, base_name, msb, lsb_or_None)
        self.prev_char = ['x'] * width
        self.prev_time = [0] * width
        self.t0 = [0] * width
        self.t1 = [0] * width
        self.tx = [0] * width
        self.tz = [0] * width
        self.tc = [0] * width

    def apply(self, new_bits, time_now):
        for i in range(self.width):
            ch = new_bits[i]
            prev = self.prev_char[i]
            if ch == prev:
                continue
            elapsed = time_now - self.prev_time[i]
            getattr(self, _bucket_for(prev))[i] += elapsed
            self.prev_char[i] = ch
            self.prev_time[i] = time_now
            self.tc[i] += 1

    def flush(self, end_time):
        for i in range(self.width):
            elapsed = end_time - self.prev_time[i]
            if elapsed <= 0:
                continue
            bucket = _bucket_for(self.prev_char[i])
            getattr(self, bucket)[i] += elapsed
            self.prev_time[i] = end_time


_VAR_RE = re.compile(
    r'^\$var\s+(\S+)\s+(\d+)\s+(\S+)\s+(\S+)(?:\s+(\[[^\]]*\]))?\s+\$end\s*$')
_SCOPE_RE = re.compile(r'^\$scope\s+(\S+)\s+(\S+)\s+\$end\s*$')
_TIMESCALE_RE = re.compile(r'(\d+)\s*([a-zA-Z]+)')


def _parse_vcd(vcd_path):
    """Returns (root_scope_node, timescale_num, timescale_unit, duration)."""
    id_states = {}          # id code -> _IdState
    scope_stack = []        # list of (kind, node_or_None) ; node is None for
                             # non-module scopes we're skipping
    root = None
    timescale_num, timescale_unit = 1, "ps"
    current_time = 0

    with open(vcd_path, 'r', errors='replace') as fh:
        lines = fh.readlines()

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip('\n')
        i += 1
        if not line:
            continue

        if line.startswith('$timescale'):
            # Value may be on the same line or the next non-$end line.
            body = line[len('$timescale'):].strip()
            while '$end' not in body and i < n:
                body += ' ' + lines[i].strip()
                i += 1
            body = body.replace('$end', '').strip()
            m = _TIMESCALE_RE.search(body)
            if m:
                timescale_num, timescale_unit = int(m.group(1)), m.group(2)
            continue

        if line.startswith('$scope'):
            m = _SCOPE_RE.match(line)
            if not m:
                # Some writers split '$scope module foo' and '$end' across
                # lines; tolerate by joining.
                body = line
                while '$end' not in body and i < n:
                    body += ' ' + lines[i].strip()
                    i += 1
                m = _SCOPE_RE.match(body)
            kind, name = (m.group(1), m.group(2)) if m else ("module", "?")
            if kind == "module":
                parent = scope_stack[-1][1] if scope_stack and scope_stack[-1][1] is not None else root
                if parent is None and root is None:
                    root = _ScopeNode(name)
                    node = root
                elif parent is None:
                    # A second top-level module scope: shouldn't happen for
                    # our testbenches (single top), but stay defensive.
                    node = root
                else:
                    node = parent.child(name)
                scope_stack.append((kind, node))
            else:
                scope_stack.append((kind, None))
            continue

        if line.startswith('$upscope'):
            if scope_stack:
                scope_stack.pop()
            continue

        if line.startswith('$var'):
            m = _VAR_RE.match(line)
            if not m:
                body = line
                while '$end' not in body and i < n:
                    body += ' ' + lines[i].strip()
                    i += 1
                m = _VAR_RE.match(body)
            if not m:
                continue
            _vtype, width_s, vid, name, rng = m.groups()
            width = int(width_s)
            # Skip anything declared inside a non-module (begin/fork/task/
            # function) scope, or before any module scope has been seen.
            cur_kind_ok = (not scope_stack) or scope_stack[-1][1] is not None
            cur_node = scope_stack[-1][1] if scope_stack else root
            if not cur_kind_ok or cur_node is None:
                continue
            msb, lsb = None, None
            if rng:
                rm = re.match(r'\[(\d+):(\d+)\]', rng)
                if rm:
                    msb, lsb = int(rm.group(1)), int(rm.group(2))
                else:
                    rm2 = re.match(r'\[(\d+)\]', rng)
                    if rm2:
                        msb = lsb = int(rm2.group(1))
            st = id_states.get(vid)
            if st is None:
                st = _IdState(width)
                id_states[vid] = st
            st.targets.append((cur_node, name, msb, lsb))
            continue

        if line.startswith('$dumpvars'):
            continue

        if line.startswith('$enddefinitions'):
            continue

        if line.startswith('$end'):
            continue

        if line.startswith('$'):
            # $date, $version, $comment, etc. -- not needed.
            continue

        if line[0] == '#':
            try:
                current_time = int(line[1:].strip())
            except ValueError:
                pass
            continue

        # Value-change line.
        c0 = line[0]
        if c0 in 'bB':
            parts = line[1:].split(None, 1)
            if len(parts) != 2:
                continue
            value, vid = parts[0], parts[1].strip()
            st = id_states.get(vid)
            if st is None:
                continue
            bits = _pad_value(value, st.width)
            st.apply(bits, current_time)
        elif c0 in 'rR':
            continue  # real-valued signals: not a hardware net in this design
        elif c0 in '01xXzZ':
            vid = line[1:].strip()
            st = id_states.get(vid)
            if st is None:
                continue
            st.apply(_pad_value(c0.lower() if c0 in 'xXzZ' else c0, st.width),
                     current_time)
        # anything else: ignore silently (defensive; malformed/unknown line)

    for st in id_states.values():
        st.flush(current_time)
        for (node, name, msb, lsb) in st.targets:
            if st.width == 1 or msb is None:
                node.nets.append((name, st.t0[0], st.t1[0], st.tx[0], st.tz[0], st.tc[0]))
            else:
                descending = msb >= lsb
                for bit_pos in range(st.width):
                    bit_index = (msb - bit_pos) if descending else (lsb + bit_pos)
                    label = f"{name}[{bit_index}]"
                    node.nets.append((label, st.t0[bit_pos], st.t1[bit_pos],
                                      st.tx[bit_pos], st.tz[bit_pos], st.tc[bit_pos]))

    if root is None:
        raise ValueError(f"{vcd_path}: no top-level $scope module found")

    return root, timescale_num, timescale_unit, current_time


def _emit_saif(root, timescale_num, timescale_unit, duration, design_name):
    lines = []
    lines.append("(SAIFILE")
    lines.append('   (SAIFVERSION "2.0")')
    lines.append('   (DIRECTION "backward")')
    lines.append(f'   (DESIGN "{design_name}")')
    lines.append(f'   (DATE "{datetime.now().strftime("%a %b %d %H:%M:%S %Y")}")')
    lines.append('   (VENDOR "Icarus Verilog (vcd_to_saif.py conversion)")')
    lines.append('   (PROGRAM_NAME "iverilog/vvp + vcd_to_saif.py")')
    lines.append('   (VERSION "1.0")')
    lines.append('   (DIVIDER /)')
    lines.append(f'   (TIMESCALE  {timescale_num} {timescale_unit})')
    lines.append(f'   (DURATION  {duration})')

    def emit(node, indent):
        lines.append(f"{indent}(INSTANCE  {node.name}")
        if node.nets:
            lines.append(f"{indent}   (NET ")
            for (label, t0, t1, tx, tz, tc) in node.nets:
                lines.append(
                    f"{indent}      ({label} (T0 {t0}) (T1 {t1}) (TX {tx}) "
                    f"(TZ {tz}) (TB 0) (TC {tc}))")
            lines.append(f"{indent}   )")
        for child in node.children.values():
            emit(child, indent + "   ")
        lines.append(f"{indent})")

    emit(root, "   ")
    lines.append(")")
    return "\n".join(lines) + "\n"


def vcd_to_saif(vcd_path, saif_path, design_name=None):
    """Convert `vcd_path` (an Icarus-written VCD) into a SAIF file at
    `saif_path`. Returns (net_count, duration) for the caller to sanity-check
    (e.g. refuse to trust a SAIF with suspiciously few nets)."""
    root, ts_num, ts_unit, duration = _parse_vcd(vcd_path)

    def count_nets(node):
        return len(node.nets) + sum(count_nets(c) for c in node.children.values())

    net_count = count_nets(root)
    text = _emit_saif(root, ts_num, ts_unit, duration,
                      design_name or root.name)
    with open(saif_path, 'w') as f:
        f.write(text)
    return net_count, duration


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <in.vcd> <out.saif>", file=sys.stderr)
        raise SystemExit(1)
    n, dur = vcd_to_saif(sys.argv[1], sys.argv[2])
    print(f"wrote {sys.argv[2]}: {n} nets, duration={dur}")
