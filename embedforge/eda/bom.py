"""BOM parser (EasyEDA Standard/Pro, JLCPCB, KiCad, generic CSV/TSV) -> part numbers per designator."""

import csv
import io
import re
from typing import Dict, List, Optional

from embedforge.eda.board import Board

_ALIASES = {
    "ref": ("designator", "designators", "reference", "references", "ref", "refs", "refdes", "part reference"),
    "value": ("name", "value", "comment", "description", "device"),
    "footprint": ("footprint", "package", "pcb footprint", "jlcpcb footprint"),
    "mpn": ("manufacturer part", "manufacturer part number", "mpn", "mfr part", "mfr. part #", "mfr.part #", "part number", "bom_manufacturer part"),
    "manufacturer": ("manufacturer", "mfr", "mfr.", "bom_manufacturer"),
    "lcsc": ("supplier part", "lcsc", "lcsc part", "lcsc part #", "lcsc part number", "jlcpcb part #", "bom_supplier part"),
}


def decode_text(data: bytes) -> str:
    """EasyEDA Standard exports its BOM as UTF-16 (tab-separated) despite the .csv extension."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", errors="replace")
    if len(data) > 4 and data[1:2] == b"\x00" and data[3:4] == b"\x00":
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8", errors="replace")


def _columns(header: List[str]) -> Dict[str, int]:
    norm = [re.sub(r"\s+", " ", h.strip().strip('"').lower()) for h in header]
    cols: Dict[str, int] = {}
    for key, names in _ALIASES.items():
        for i, h in enumerate(norm):
            if h in names and key not in cols and i not in cols.values():
                cols[key] = i
    return cols


def looks_like_bom(text: str) -> bool:
    head = text.lstrip("﻿").split("\n", 1)[0].lower()
    return any(k in head for k in ("designator", "reference", "refdes")) and any(
        k in head for k in ("footprint", "package", "manufacturer", "value", "name", "comment", "quantity", "qty"))


def parse_bom(text: str, filename: str = "bom.csv") -> Optional[Board]:
    text = text.lstrip("﻿")
    first = text.split("\n", 1)[0]
    delim = "\t" if first.count("\t") >= max(1, first.count(",")) else ";" if first.count(";") > first.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows:
        return None
    cols = _columns(rows[0])
    if "ref" not in cols:
        return None
    b = Board(imported=[{"file": filename, "format": "BOM"}])
    for row in rows[1:]:
        if len(row) <= cols["ref"]:
            continue
        refs = [r for r in re.split(r"[,\s;]+", row[cols["ref"]].strip()) if r]
        for ref in refs:
            part = b.part(ref)
            for key in ("value", "footprint", "mpn", "manufacturer", "lcsc"):
                if key in cols and cols[key] < len(row):
                    setattr(part, key, row[cols[key]].strip())
    return b if b.parts else None
