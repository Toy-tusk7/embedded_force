"""EasyEDA Standard schematic source (.json) importer.

Format reference: https://docs.easyeda.com/en/DocumentFormat/2-EasyEDA-Schematic-File-Format/
Shapes are '~'-separated strings; a component ("LIB") holds its pins/texts after '#@$' separators and
pin sub-fields are split by '^^'. Child shapes are stored in absolute sheet coordinates (KiCad's importer
subtracts the LIB origin to build its symbols). Connectivity is geometric: wire vertices, pin end-points, net labels
("N") and net flags/power ports ("F") that share a coordinate (or touch a wire segment) are one net;
equally named labels/flags join nets across the whole design.
"""

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from embedforge.eda.board import Board

Point = Tuple[int, int]
TOL = 1  # coordinates are compared in 1/10 units


def _pt(x: str, y: str) -> Optional[Point]:
    try:
        return (round(float(x) * 10), round(float(y) * 10))
    except (TypeError, ValueError):
        return None


class _DSU:
    def __init__(self) -> None:
        self.p: Dict[Any, Any] = {}

    def find(self, a: Any) -> Any:
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    (px, py), (ax, ay), (bx, by) = p, a, b
    if not (min(ax, bx) - TOL <= px <= max(ax, bx) + TOL and min(ay, by) - TOL <= py <= max(ay, by) + TOL):
        return False
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    seg_len = max(abs(bx - ax), abs(by - ay), 1)
    return abs(cross) <= TOL * seg_len


def _attrs(s: str) -> Dict[str, str]:
    parts = s.split("`")
    return {parts[i]: parts[i + 1] for i in range(0, len(parts) - 1, 2)}


def _sheets(doc: Dict[str, Any]) -> Iterable[Tuple[str, Dict[str, Any]]]:
    """Yield (title, sheet) for a single sheet, a multi-sheet schematic or a project export."""
    def as_dict(x: Any) -> Dict[str, Any]:
        if isinstance(x, str):
            try:
                return json.loads(x)
            except ValueError:
                return {}
        return x if isinstance(x, dict) else {}

    if isinstance(doc.get("schematics"), list):
        for i, s in enumerate(doc["schematics"]):
            s = as_dict(s)
            yield s.get("title") or f"sheet{i + 1}", as_dict(s.get("dataStr")) or s
        return
    if "dataStr" in doc and not doc.get("shape"):
        inner = as_dict(doc["dataStr"])
        if inner:
            yield from _sheets(inner)
            return
    yield doc.get("title", "sheet1"), doc


def is_easyeda_std(doc: Any) -> bool:
    if not isinstance(doc, dict):
        return False
    if isinstance(doc.get("schematics"), list):
        return True
    head = doc.get("head")
    has_shapes = any(k.startswith("shape") for k in doc)
    return has_shapes and (isinstance(head, (str, dict)) or "canvas" in doc)


def parse_easyeda_std(doc: Dict[str, Any], filename: str = "schematic.json") -> Board:
    b = Board(imported=[{"file": filename, "format": "EasyEDA Standard schematic"}])
    dsu = _DSU()
    pins: List[Tuple[Point, str, str, str]] = []       # (point, ref, number, name)
    labels: List[Tuple[Point, str]] = []                # (point, net name)
    wires: List[List[Point]] = []
    anon = 0

    for title, sheet in _sheets(doc):
        shapes: List[str] = []
        for k, v in sheet.items():
            if k.startswith("shape") and isinstance(v, list):
                shapes += [s for s in v if isinstance(s, str)]
        sheet_tag = title
        for s in shapes:
            kind = s.split("~", 1)[0]
            if kind == "W":
                f = s.split("~")
                nums = f[1].split() if len(f) > 1 else []
                pts = [p for p in (_pt(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)) if p]
                pts = [(sheet_tag, p) for p in pts]
                if pts:
                    wires.append(pts)  # type: ignore[arg-type]
            elif kind == "N":
                f = s.split("~")
                p = _pt(f[1], f[2]) if len(f) > 5 else None
                if p and f[5].strip():
                    labels.append(((sheet_tag, p), f[5].strip()))  # type: ignore[arg-type]
            elif kind == "F":  # net flag / power port ("O" is a no-connect marker and carries no net)
                seg = s.split("^^")
                f0 = seg[0].split("~")
                p = _pt(f0[2], f0[3]) if len(f0) > 3 else None
                if p is None and len(seg) > 1 and "~" in seg[1]:
                    p = _pt(*seg[1].split("~")[:2])
                name = seg[2].split("~")[0].strip() if len(seg) > 2 else ""
                if p and name:
                    labels.append(((sheet_tag, p), name))  # type: ignore[arg-type]
            elif kind == "LIB":
                anon += 1
                _parse_lib(s, sheet_tag, b, pins, anon)

    # union all vertices of each wire, then anything touching a wire segment
    for w in wires:
        for p in w[1:]:
            dsu.union(w[0], p)
    anchors = [p for p, *_ in pins] + [p for p, _ in labels] + [w[0] for w in wires] + [w[-1] for w in wires]
    for w in wires:
        for a, c in zip(w, w[1:]):
            if a[0] != c[0]:
                continue
            for p in anchors:
                if p[0] == a[0] and _on_segment(p[1], a[1], c[1]):
                    dsu.union(p, a)
    for p, name in labels:
        dsu.union(p, ("label", name))  # same name joins nets globally

    groups: Dict[Any, Dict[str, Any]] = {}
    for p, ref, num, pname in pins:
        g = groups.setdefault(dsu.find(p), {"pins": [], "names": set()})
        g["pins"].append((ref, num, pname))
    for p, name in labels:
        groups.setdefault(dsu.find(p), {"pins": [], "names": set()})["names"].add(name)

    for g in groups.values():
        if not g["pins"]:
            continue
        names = sorted(g["names"])
        if len(names) > 1:
            b.warnings.append(f"Pins joined under several net names {names}; using '{names[0]}'")
        if names:
            net = names[0]
        elif len(g["pins"]) >= 2:
            ref, num, _ = sorted(g["pins"])[0]
            net = f"NET_{ref}_{num}"
        else:
            continue
        for ref, num, pname in g["pins"]:
            b.connect(net, ref, num, pname)
    if not b.parts:
        b.warnings.append(f"{filename}: no components found — is this a schematic (not a PCB/symbol) source?")
    return b


def _parse_lib(s: str, sheet: str, b: Board, pins: List[Tuple[Any, str, str, str]], idx: int) -> None:
    chunks = s.split("#@$")
    head = chunks[0].split("~")
    attrs = _attrs(head[3]) if len(head) > 3 else {}
    ref = ""
    name = ""
    lib_pins: List[Tuple[Point, str, str]] = []
    for c in chunks[1:]:
        kind = c.split("~", 1)[0]
        if kind == "T":
            f = c.split("~")
            if len(f) > 12 and f[1] == "P":
                ref = f[12].strip()
            elif len(f) > 12 and f[1] == "N":
                name = f[12].strip()
        elif kind == "P":
            seg = c.split("^^")
            f0 = seg[0].split("~")
            p = _pt(f0[4], f0[5]) if len(f0) > 5 else None
            if p is None and len(seg) > 1:
                p = _pt(*seg[1].split("~")[:2])
            pname = seg[3].split("~")[4].strip() if len(seg) > 3 and len(seg[3].split("~")) > 4 else ""
            # the 4th field is the pin number EasyEDA uses for netlists (KiCad's importer reads the same field);
            # the visible number text in segment 4 is only a fallback
            pnum = f0[3].strip() if len(f0) > 3 else ""
            if not pnum and len(seg) > 4 and len(seg[4].split("~")) > 4:
                pnum = seg[4].split("~")[4].strip()
            if p and pnum:
                lib_pins.append((p, pnum, pname))
    ref = ref or attrs.get("pre", "").replace("?", str(idx)) or f"X{idx}"
    if ref.endswith("?"):
        ref = ref[:-1] + str(idx)
    part = b.part(ref)
    part.value = name or attrs.get("name", "") or attrs.get("Value", "")
    part.footprint = attrs.get("package", "")
    part.mpn = attrs.get("BOM_Manufacturer Part", "") or attrs.get("Manufacturer Part", "")
    part.manufacturer = attrs.get("BOM_Manufacturer", "") or attrs.get("Manufacturer", "")
    part.lcsc = attrs.get("BOM_Supplier Part", "") or attrs.get("Supplier Part", "") or attrs.get("LCSC Part", "")
    for p, pnum, pname in lib_pins:
        part.pin(pnum).name = pname
        pins.append(((sheet, p), ref, pnum, pname))
