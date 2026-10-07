"""Netlist parsers: Protel2/Altium (.net), PADS ASCII (.asc/.net) and KiCad (.net s-expression).

EasyEDA Standard exports Protel/Altium and PADS; EasyEDA Pro exports Protel2 and PADS. These give
reliable connectivity (designator + pin number per net); KiCad netlists also carry pin names.
"""

import re
from typing import Any, List, Tuple

from embedforge.eda.board import Board


def _split_node(node: str, sep: str) -> Tuple[str, str]:
    ref, _, pin = node.strip().rpartition(sep)
    return ref, pin


_PIN_TYPES = {"passive", "input", "output", "bidirectional", "bidirection", "bi", "power", "powerinput", "poweroutput",
              "opencollector", "openemitter", "hiz", "tristate", "unspecified", "undefined", "in", "out", "io", "nc"}


def _protel_component(block: List[str], b: Board, parttypes: dict) -> None:
    """Classic layout is positional (REF, FOOTPRINT, VALUE, ...); EasyEDA Pro's 'PROTEL NETLIST 2.0' is KEY/VALUE pairs."""
    if block and block[0].upper() == "DESIGNATOR":
        kv = {}
        for k, v in zip(block[0::2], block[1::2]):
            kv.setdefault(k.strip(), v.strip())
        ref = kv.get("DESIGNATOR") or kv.get("Designator")
        if not ref:
            return
        part = b.part(ref)
        parttype = kv.get("PARTTYPE", "")
        parttypes[ref] = parttype
        part.footprint = kv.get("FOOTPRINT", "")
        part.value = kv.get("Value") or kv.get("Name") or parttype
        part.mpn = kv.get("Manufacturer Part", "")
        part.manufacturer = kv.get("Manufacturer", "")
        part.lcsc = kv.get("Supplier Part", "")
    elif block and block[0]:
        part = b.part(block[0])
        part.footprint = block[1] if len(block) > 1 else ""
        part.value = block[2] if len(block) > 2 else ""
        parttypes[block[0]] = part.value


def _protel_node(node: str, parttypes: dict) -> Tuple[str, str, str]:
    """'U1-3' (classic) or 'U1-3 RP2354B_C39843328-GPIO6 Passive' (EasyEDA Pro: REF-PIN PARTTYPE-PINNAME TYPE)."""
    node = node.strip()
    first, _, rest = node.partition(" ")
    ref, pin = _split_node(first, "-")
    rest = rest.strip()
    if rest:
        toks = rest.split()
        if len(toks) > 1 and toks[-1].lower() in _PIN_TYPES:
            rest = rest[: rest.rfind(toks[-1])].strip()
    name = ""
    if rest:
        pt = parttypes.get(ref, "")
        name = rest[len(pt) + 1:] if pt and rest.startswith(pt + "-") else rest.rpartition("-")[2]
    return ref, pin, (name if name != pin else "")


def parse_protel(text: str, filename: str = "netlist.net") -> Board:
    """Protel2 / Altium netlist:  [ component ]  and  ( NETNAME  REF-PIN ... ), incl. EasyEDA Pro's key/value variant."""
    b = Board(imported=[{"file": filename, "format": "Protel2 netlist"}])
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
    parttypes: dict = {}
    nets: List[List[str]] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln in ("[", "("):
            close = "]" if ln == "[" else ")"
            block = []
            i += 1
            while i < len(lines) and lines[i] != close:
                block.append(lines[i])
                i += 1
            if ln == "[":
                _protel_component(block, b, parttypes)
            elif block:
                nets.append(block)
        i += 1
    for block in nets:  # after all components, so PARTTYPEs are known for splitting pin names
        net = block[0]
        for node in block[1:]:
            if node and "-" in node.split(" ", 1)[0]:
                ref, pin, name = _protel_node(node, parttypes)
                if ref and pin:
                    b.connect(net, ref, pin, name)
    if not b.parts:
        b.warnings.append(f"{filename}: no components or nets found in Protel netlist")
    return b


def parse_pads(text: str, filename: str = "netlist.asc") -> Board:
    """PADS ASCII netlist: *PART* (REF DECAL or REF VALUE@DECAL) and *NET* / *SIGNAL* NAME + REF.PIN nodes."""
    b = Board(imported=[{"file": filename, "format": "PADS netlist"}])
    section = ""
    net = ""
    for raw in text.replace("\r", "").split("\n"):
        ln = raw.strip()
        if not ln or ln.startswith("!") and section == "":
            continue
        up = ln.upper()
        if up.startswith("*PART"):
            section = "part"
            continue
        if up.startswith("*NET*"):
            section = "net"
            continue
        if up.startswith("*SIGNAL*"):
            section = "net"
            net = ln.split(None, 1)[1].strip() if len(ln.split(None, 1)) > 1 else ""
            continue
        if up.startswith("*END*"):
            break
        if up.startswith("*"):
            section = "other"
            continue
        if section == "part":
            toks = ln.split()
            if toks:
                part = b.part(toks[0])
                if len(toks) > 1:
                    decal = toks[1]
                    if "@" in decal:
                        part.value, part.footprint = decal.split("@", 1)
                    else:
                        part.footprint = decal
        elif section == "net" and net:
            for node in ln.split():
                ref, pin = _split_node(node, ".")
                if ref and pin:
                    b.connect(net, ref, pin)
    if not b.parts:
        b.warnings.append(f"{filename}: no components or nets found in PADS netlist")
    return b


# ------------------------------------------------------------------ KiCad s-expression
def _sexpr(text: str) -> Any:
    tokens = re.findall(r'\(|\)|"(?:\\.|[^"\\])*"|[^\s()"]+', text)
    stack: List[List[Any]] = [[]]
    for t in tokens:
        if t == "(":
            stack.append([])
        elif t == ")":
            if len(stack) > 1:
                done = stack.pop()
                stack[-1].append(done)
        else:
            stack[-1].append(t[1:-1].replace('\\"', '"') if t.startswith('"') else t)
    return stack[0][0] if stack[0] else []


def _field(node: List[Any], key: str) -> str:
    for x in node[1:]:
        if isinstance(x, list) and x and x[0] == key:
            return str(x[1]) if len(x) > 1 else ""
    return ""


def parse_kicad(text: str, filename: str = "netlist.net") -> Board:
    """KiCad netlist (export (components (comp ...)) (nets (net (name ..) (node (ref ..) (pin ..) (pinfunction ..)))))."""
    b = Board(imported=[{"file": filename, "format": "KiCad netlist"}])
    root = _sexpr(text)
    for sec in root[1:] if isinstance(root, list) else []:
        if not isinstance(sec, list) or not sec:
            continue
        if sec[0] == "components":
            for comp in sec[1:]:
                if isinstance(comp, list) and comp and comp[0] == "comp":
                    part = b.part(_field(comp, "ref"))
                    part.value = _field(comp, "value")
                    part.footprint = _field(comp, "footprint").split(":")[-1]
                    for x in comp[1:]:
                        if isinstance(x, list) and x and x[0] == "fields":
                            for f in x[1:]:
                                if isinstance(f, list) and len(f) >= 3 and f[0] == "field":
                                    key = _field(f, "name").lower() if isinstance(f[1], list) else str(f[1]).lower()
                                    val = str(f[-1]) if not isinstance(f[-1], list) else ""
                                    if key in ("mpn", "manufacturer part", "manufacturer_part_number", "mfr. part #"):
                                        part.mpn = val
                                    elif key in ("lcsc", "lcsc part", "supplier part"):
                                        part.lcsc = val
        elif sec[0] == "nets":
            for net in sec[1:]:
                if isinstance(net, list) and net and net[0] == "net":
                    name = _field(net, "name").lstrip("/")
                    for node in net[1:]:
                        if isinstance(node, list) and node and node[0] == "node":
                            b.connect(name, _field(node, "ref"), _field(node, "pin"), _field(node, "pinfunction"))
    if not b.parts:
        b.warnings.append(f"{filename}: no components or nets found in KiCad netlist")
    return b
