"""EasyEDA Pro project importer: V2 `.epro` (zip of JSON-array lines) and V3 `.epro2` (zip with a `.epru` log).

Both versions are normalised into the same intermediate form (symbols, devices, pages of components and
wires) and then connected by one routine. The rules below reproduced the PCB pad nets of real sample
projects (KiCad QA data) exactly:

- a placed pin's connection point is the symbol PIN x/y, rotated counter-clockwise, mirrored on X, then
  translated to the component; V2 uses a y-up axis, V3 a y-down axis;
- every polyline/LINE of one WIRE is one node; wires and pins connect where vertices coincide;
- a wire's name is its `NET` attribute; power flags (symbol type 18) and net ports (19) name their pin;
- equal names connect across all pages; Short symbols (22) merge their nets, alphabetically lowest name wins;
- unnamed nets are named like EasyEDA does: "$" + page + "N" + wire number.

References: https://prodocs.easyeda.com/en/format/index , KiCad eeschema/sch_io/easyedapro.
"""

import io
import json
import math
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from embedforge.eda.board import Board
from embedforge.eda.easyeda_std import _DSU

SYM_NORMAL, SYM_BLOCK, SYM_FLAG, SYM_PORT, SYM_FRAME, SYM_SHORT = 2, 17, 18, 19, 20, 22
Point = Tuple[float, float]


@dataclass
class _Symbol:
    type: int = SYM_NORMAL
    parts: Dict[str, List[Tuple[str, float, float]]] = field(default_factory=lambda: defaultdict(list))
    part_titles: Dict[str, str] = field(default_factory=dict)   # V3: PART title -> PART id
    pin_num: Dict[str, str] = field(default_factory=dict)
    pin_name: Dict[str, str] = field(default_factory=dict)
    root: Dict[str, str] = field(default_factory=dict)

    def pins_for(self, part: str) -> List[Tuple[str, float, float]]:
        if part in self.parts:
            return self.parts[part]
        if part in self.part_titles and self.part_titles[part] in self.parts:
            return self.parts[self.part_titles[part]]
        return next(iter(self.parts.values())) if len(self.parts) == 1 else []


@dataclass
class _Comp:
    id: str
    part: str
    x: float
    y: float
    rot: float
    mirror: bool


@dataclass
class _Page:
    number: int
    comps: List[_Comp] = field(default_factory=list)
    wires: Dict[str, List[Point]] = field(default_factory=lambda: defaultdict(list))
    attrs: Dict[str, Dict[str, Any]] = field(default_factory=lambda: defaultdict(dict))  # parentId -> {key: value}


@dataclass
class _Project:
    version: str
    symbols: Dict[str, _Symbol] = field(default_factory=dict)
    devices: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    footprints: Dict[str, str] = field(default_factory=dict)
    schematics: List[Tuple[str, List[_Page]]] = field(default_factory=list)


# ------------------------------------------------------------------ entry points
def is_pro_archive(names: List[str]) -> bool:
    has_v2 = any(n.endswith("project.json") for n in names) and any(n.endswith(".esch") for n in names)
    has_v3 = any(n.endswith(".epru") for n in names)
    return has_v2 or has_v3


def parse_epro(data: bytes, filename: str = "project.epro") -> Board:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if any(n.endswith(".epru") for n in names):
            proj = _load_v3(z, names)
        else:
            proj = _load_v2(z, names)
    label = "EasyEDA Pro project" + (" (V3)" if proj.version == "v3" else "")
    boards = [_connect(proj, title, pages, filename, label) for title, pages in proj.schematics]
    boards = [b for b in boards if b.parts]
    if not boards:
        b = Board(imported=[{"file": filename, "format": label}])
        b.warnings.append(f"{filename}: no schematic components found")
        return b
    boards.sort(key=lambda b: -len(b.parts))
    best = boards[0]
    if len(boards) > 1:
        best.warnings.append(f"{filename} has {len(boards)} schematics; imported '{best.name}' (most parts). Others: "
                             + ", ".join(b.name for b in boards[1:]))
    return best


# ------------------------------------------------------------------ V2 (.epro)
def _json_lines(text: str) -> List[List[Any]]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, list) and rec:
            out.append(rec)
    return out


def _load_v2(z: zipfile.ZipFile, names: List[str]) -> _Project:
    pj = next(n for n in names if n.endswith("project.json"))
    prefix = pj[: -len("project.json")]
    prj = json.loads(z.read(pj).decode("utf-8"))
    proj = _Project("v2")
    for uuid, dev in (prj.get("devices") or {}).items():
        proj.devices[uuid] = {**(dev.get("attributes") or {}), "_title": dev.get("title", "")}
    for uuid, fp in (prj.get("footprints") or {}).items():
        proj.footprints[uuid] = fp.get("title", "") if isinstance(fp, dict) else str(fp)
    for n in names:
        if n.startswith(prefix + "SYMBOL/") and n.endswith(".esym"):
            proj.symbols[n.rsplit("/", 1)[-1][:-5]] = _v2_symbol(_json_lines(z.read(n).decode("utf-8")))
    for suuid, sch in (prj.get("schematics") or {}).items():
        pages = []
        for sheet in sch.get("sheets") or []:
            path = f"{prefix}SHEET/{suuid}/{sheet.get('id')}.esch"
            if path in names:
                pages.append(_v2_page(int(sheet.get("id") or len(pages) + 1), _json_lines(z.read(path).decode("utf-8"))))
        if pages:
            proj.schematics.append((sch.get("name") or suuid, pages))
    return proj


def _v2_symbol(recs: List[List[Any]]) -> _Symbol:
    s = _Symbol()
    cur = ""
    for r in recs:
        if r[0] == "HEAD" and isinstance(r[1], dict):
            s.type = int(r[1].get("symbolType", SYM_NORMAL) or SYM_NORMAL)
        elif r[0] == "PART":
            cur = r[1]
        elif r[0] == "PIN" and len(r) > 5:
            s.parts[cur].append((r[1], float(r[4]), float(r[5])))
        elif r[0] == "ATTR" and len(r) > 4:
            _symbol_attr(s, r[2], r[3], r[4])
    return s


def _v2_page(number: int, recs: List[List[Any]]) -> _Page:
    page = _Page(number)
    for r in recs:
        if r[0] == "COMPONENT" and len(r) > 6:
            page.comps.append(_Comp(r[1], r[2] or "", float(r[3]), float(r[4]), float(r[5] or 0), bool(r[6])))
        elif r[0] == "WIRE" and len(r) > 2:
            for poly in r[2] or []:
                page.wires[r[1]] += [(float(poly[i]), float(poly[i + 1])) for i in range(0, len(poly) - 1, 2)]
        elif r[0] == "ATTR" and len(r) > 4:
            page.attrs[r[2]][r[3]] = r[4]
    return page


# ------------------------------------------------------------------ V3 (.epro2)
def _epru_docs(text: str) -> Dict[Tuple[str, str], Dict[Tuple[str, str], Tuple[Dict[str, Any], Any]]]:
    """(docType, uuid) -> {(rowType, rowId): (outer, inner)}, keeping the highest ticket and dropping tombstones."""
    docs: Dict[Tuple[str, str], Dict[Tuple[str, str], Tuple[Dict[str, Any], Any]]] = {}
    cur: Optional[Dict[Tuple[str, str], Tuple[Dict[str, Any], Any]]] = None
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line:
            continue
        if line.endswith("|"):
            line = line[:-1]
        i = line.find("||")
        if i < 0:
            continue
        try:
            outer = json.loads(line[:i])
            rest = line[i + 2:]
            inner = json.loads(rest) if rest.strip() else None
        except ValueError:
            continue
        if outer.get("type") == "DOCHEAD":
            if isinstance(inner, dict):
                cur = docs.setdefault((inner.get("docType", ""), inner.get("uuid", "")), {})
            continue
        if cur is None:
            continue
        key = (outer.get("type"), outer.get("id"))
        old = cur.get(key)
        if old is None or (outer.get("ticket") or 0) >= (old[0].get("ticket") or 0):
            cur[key] = (outer, inner)
    return {k: {rk: rv for rk, rv in rows.items() if rv[1] not in (None, "")} for k, rows in docs.items()}


def _rows(doc: Dict[Tuple[str, str], Tuple[Dict[str, Any], Any]], t: str) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    return [(o, i) for (tt, _), (o, i) in doc.items() if tt == t and isinstance(i, dict)]


def _meta(doc: Dict[Tuple[str, str], Tuple[Dict[str, Any], Any]]) -> Dict[str, Any]:
    rows = _rows(doc, "META")
    return rows[0][1] if rows else {}


def _load_v3(z: zipfile.ZipFile, names: List[str]) -> _Project:
    epru = next(n for n in names if n.endswith(".epru"))
    docs = _epru_docs(z.read(epru).decode("utf-8", errors="replace"))
    proj = _Project("v3")
    sch_titles: Dict[str, str] = {}
    pages_by_sch: Dict[str, List[_Page]] = defaultdict(list)
    for (dtype, uuid), doc in docs.items():
        meta = _meta(doc)
        if dtype == "SYMBOL":
            s = _Symbol(type=int(meta.get("docType", SYM_NORMAL) or SYM_NORMAL))
            for o, i in _rows(doc, "PART"):
                if i.get("title"):
                    s.part_titles[i["title"]] = o.get("id")
            for o, i in _rows(doc, "PIN"):
                s.parts[i.get("partId") or ""].append((o.get("id"), float(i.get("x") or 0), float(i.get("y") or 0)))
            for o, i in _rows(doc, "ATTR"):
                _symbol_attr(s, i.get("parentId"), i.get("key"), i.get("value"))
            proj.symbols[uuid] = s
        elif dtype == "DEVICE":
            proj.devices[uuid] = {**(meta.get("attributes") or {}), "_title": meta.get("title", "")}
        elif dtype == "FOOTPRINT":
            proj.footprints[uuid] = meta.get("title", "")
        elif dtype == "SCH":
            sch_titles[uuid] = meta.get("title", "")
        elif dtype == "SCH_PAGE":
            page = _Page(int(meta.get("zIndex") or len(pages_by_sch[meta.get("schematic", "")]) + 1))
            for o, i in _rows(doc, "COMPONENT"):
                page.comps.append(_Comp(o.get("id"), i.get("partId") or "", float(i.get("x") or 0), float(i.get("y") or 0),
                                        float(i.get("rotation") or 0), bool(i.get("isMirror"))))
            for o, i in _rows(doc, "WIRE"):
                page.wires.setdefault(o.get("id"), [])
            for o, i in _rows(doc, "LINE"):
                g = i.get("lineGroup")
                if g:
                    page.wires[g] += [(float(i.get("startX") or 0), float(i.get("startY") or 0)),
                                      (float(i.get("endX") or 0), float(i.get("endY") or 0))]
            for o, i in _rows(doc, "ATTR"):
                page.attrs[i.get("parentId")][i.get("key")] = i.get("value")
            pages_by_sch[meta.get("schematic", "")].append(page)
    for suuid, pages in pages_by_sch.items():
        proj.schematics.append((sch_titles.get(suuid) or suuid or "schematic", sorted(pages, key=lambda p: p.number)))
    return proj


# ------------------------------------------------------------------ shared
def _symbol_attr(s: _Symbol, parent: Any, key: Any, value: Any) -> None:
    if value is None:
        return
    if key in ("NUMBER", "Pin Number"):
        s.pin_num[parent] = str(value)
    elif key in ("NAME", "Pin Name"):
        s.pin_name[parent] = str(value)
    elif parent in ("", None):
        s.root[key] = str(value)


def _xf(px: float, py: float, c: _Comp, y_down: bool) -> Point:
    a = math.radians(c.rot or 0)
    cos, sin = math.cos(a), math.sin(a)
    if y_down:
        x, y = px * cos + py * sin, -px * sin + py * cos
    else:
        x, y = px * cos - py * sin, px * sin + py * cos
    if c.mirror:
        x = -x
    return (round(x + c.x, 3), round(y + c.y, 3))


def _resolve(value: Any, *scopes: Dict[str, Any]) -> str:
    """Resolve '={Attr}' / '={@Attr}' variables against the given attribute scopes."""
    v = "" if value is None else str(value)
    m = re.fullmatch(r"=\{@?([^}]+)\}", v.strip())
    if m:
        for sc in scopes:
            if sc.get(m.group(1)) not in (None, ""):
                return str(sc[m.group(1)])
        return ""
    return v


def _first(*vals: Any) -> str:
    for v in vals:
        if v not in (None, ""):
            return str(v)
    return ""


def _connect(proj: _Project, title: str, pages: List[_Page], filename: str, label: str) -> Board:
    b = Board(name=title, imported=[{"file": filename, "format": label}])
    y_down = proj.version == "v3"
    dsu = _DSU()
    names: Dict[Any, set] = defaultdict(set)
    wire_num: Dict[Any, Tuple[int, int]] = {}
    pin_nodes: Dict[Any, Tuple[str, str, str]] = {}   # node -> (ref, number, name)
    shorted: List[Any] = []
    missing_symbols = 0

    for page in pages:
        at_point: Dict[Point, List[Any]] = defaultdict(list)
        for wid, pts in page.wires.items():
            node = ("w", page.number, wid)
            dsu.find(node)
            m = re.search(r"\d+", str(wid))
            wire_num[node] = (page.number, int(m.group()) if m else 0)
            net = page.attrs.get(wid, {}).get("NET")
            if net:
                names[node].add(str(net))
            for p in pts:
                at_point[(round(p[0], 3), round(p[1], 3))].append(node)

        for c in page.comps:
            a = page.attrs.get(c.id, {})
            dev = proj.devices.get(a.get("Device") or "", {})
            sym = proj.symbols.get(a.get("Symbol") or dev.get("Symbol") or "")
            if sym is None:
                if a.get("Designator"):
                    missing_symbols += 1
                continue
            if sym.type in (SYM_FRAME, SYM_BLOCK):
                continue
            pins = sym.pins_for(c.part)
            nodes = []
            ref = _resolve(a.get("Designator"), dev) if sym.type == SYM_NORMAL else ""
            for pid, px, py in pins:
                node = ("p", page.number, c.id, pid)
                dsu.find(node)
                nodes.append(node)
                at_point[_xf(px, py, c, y_down)].append(node)
                if sym.type == SYM_FLAG:
                    nm = _first(a.get("Global Net Name"), a.get("Name"), dev.get("Global Net Name"), dev.get("Name"), sym.root.get("Global Net Name"))
                    if nm:
                        names[node].add(_resolve(nm, dev))
                elif sym.type == SYM_PORT:
                    if a.get("Name"):
                        names[node].add(_resolve(a.get("Name"), dev))
                elif ref:
                    ov = page.attrs.get(c.id + pid) or page.attrs.get(f"{c.id}-{pid}") or {}
                    if str(ov.get("NO_CONNECT", "")).lower() == "yes":
                        continue
                    num = _first(ov.get("NUMBER"), ov.get("Pin Number"), sym.pin_num.get(pid), pid)
                    nm = _first(ov.get("NAME"), ov.get("Pin Name"), sym.pin_name.get(pid))
                    pin_nodes[node] = (ref, num, nm)
            if sym.type == SYM_SHORT and nodes:
                for n in nodes[1:]:
                    dsu.union(nodes[0], n)
                shorted.append(nodes[0])
            if ref:
                part = b.part(ref)
                fp = proj.footprints.get(a.get("Footprint") or dev.get("Footprint") or "", "")
                part.value = part.value or _resolve(_first(a.get("Value"), a.get("Name"), dev.get("Value"), dev.get("Name"), dev.get("_title")), dev)
                part.footprint = part.footprint or fp
                part.mpn = part.mpn or _resolve(_first(a.get("Manufacturer Part"), dev.get("Manufacturer Part")), dev)
                part.manufacturer = part.manufacturer or _resolve(_first(a.get("Manufacturer"), dev.get("Manufacturer")), dev)
                part.lcsc = part.lcsc or _resolve(_first(a.get("Supplier Part"), dev.get("Supplier Part"), dev.get("LCSC Part Name")), dev)

        for nodes_here in at_point.values():
            for n in nodes_here[1:]:
                dsu.union(nodes_here[0], n)

    for node, ns in names.items():
        for nm in ns:
            dsu.union(node, ("name", nm))

    groups: Dict[Any, Dict[str, Any]] = defaultdict(lambda: {"pins": [], "names": set(), "wires": [], "short": False})
    for node, ns in names.items():
        groups[dsu.find(node)]["names"] |= ns
    for node, num in wire_num.items():
        groups[dsu.find(node)]["wires"].append(num)
    for node, info in pin_nodes.items():
        groups[dsu.find(node)]["pins"].append(info)
    for node in shorted:
        groups[dsu.find(node)]["short"] = True

    for g in groups.values():
        if not g["pins"]:
            continue
        if g["names"]:
            ordered = sorted(g["names"])
            net = ordered[0]
            if len(ordered) > 1 and not g["short"]:
                b.warnings.append(f"Pins joined under several net names {ordered}; using '{net}'")
        elif len(g["pins"]) >= 2 and g["wires"]:
            page_no, num = min(g["wires"])
            net = f"${page_no}N{num}"
        elif len(g["pins"]) >= 2:
            ref, num, _ = sorted(g["pins"])[0]
            net = f"NET_{ref}_{num}"
        else:
            net = ""  # a pin touching nothing else has no net (matches EasyEDA's PCB)
        for ref, num, nm in g["pins"]:
            pin = b.part(ref).pin(num)
            if nm and not pin.name:
                pin.name = nm
            if net:
                pin.net = net
    if missing_symbols:
        b.warnings.append(f"{missing_symbols} component(s) reference symbols missing from the archive")
    return b
