"""EDA import: turn EasyEDA (Standard/Pro), Protel, PADS, KiCad netlists and BOMs into one Board.

Usage from the server: `detect(name, data)` to route an upload, then `rebuild_project_board(project)`
to merge every EDA file in the project into `board.json` + a quotable connectivity source.
"""

import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from embedforge.eda.board import Board
from embedforge.eda.bom import decode_text, looks_like_bom, parse_bom
from embedforge.eda.easyeda_std import is_easyeda_std, parse_easyeda_std
from embedforge.eda.netlists import parse_kicad, parse_pads, parse_protel

__all__ = ["Board", "detect", "parse", "FORMAT_LABELS", "EDA_EXTS", "EDA_DIR", "load_board", "rebuild_project_board", "BOARD_SOURCE_URL"]

EDA_EXTS = {".json", ".epro", ".epro2", ".zip", ".net", ".asc", ".csv", ".tsv", ".txt"}
BOARD_SOURCE_URL = "user://board"
FORMAT_LABELS = {
    "easyeda_std": "EasyEDA Standard schematic", "easyeda_pro": "EasyEDA Pro project",
    "protel": "Protel2 netlist", "pads": "PADS netlist", "kicad": "KiCad netlist", "bom": "BOM",
}
_NETLIST_KINDS = ("protel", "pads", "kicad")
_SCHEMATIC_KINDS = ("easyeda_std", "easyeda_pro")


def _zip_names(data: bytes) -> List[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return z.namelist()
    except zipfile.BadZipFile:
        return []


def detect(name: str, data: bytes) -> Optional[str]:
    """Identify an EDA export by content (extension is only a hint). Returns a key of FORMAT_LABELS or None."""
    n = name.lower()
    if data[:2] == b"PK":
        from embedforge.eda.easyeda_pro import is_pro_archive
        return "easyeda_pro" if is_pro_archive(_zip_names(data)) else None
    text = decode_text(data[:4_000_000])
    s = text.lstrip()
    if s.startswith("{"):
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        if is_easyeda_std(doc):
            return "easyeda_std"
    if re.match(r"^[*!]PADS", s, re.I):
        return "pads"
    if s.startswith("(export"):
        return "kicad"
    if n.endswith((".net", ".txt")) and re.search(r"^\[\s*$", text, re.M) and re.search(r"^\(\s*$", text, re.M):
        return "protel"
    if n.endswith((".csv", ".tsv", ".txt")) and looks_like_bom(text):
        return "bom"
    return None


def parse(kind: str, name: str, data: bytes) -> Optional[Board]:
    if kind == "easyeda_std":
        return parse_easyeda_std(json.loads(decode_text(data)), name)
    if kind == "easyeda_pro":
        from embedforge.eda.easyeda_pro import parse_epro
        return parse_epro(data, name)
    text = decode_text(data)
    if kind == "protel":
        return parse_protel(text, name)
    if kind == "pads":
        return parse_pads(text, name)
    if kind == "kicad":
        return parse_kicad(text, name)
    if kind == "bom":
        return parse_bom(text, name)
    return None


# ------------------------------------------------------------------ project integration
EDA_DIR = "schematics"  # uploads only; the agent's write policy excludes it, so it cannot plant a fake board


def _eda_files(root: Path) -> List[Tuple[Path, str]]:
    base = root / EDA_DIR
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in EDA_EXTS:
            continue
        if p.stat().st_size > 50_000_000:
            continue
        kind = detect(p.name, p.read_bytes())
        if kind:
            out.append((p, kind))
    return out


def build_board(root: Path, name: str = "board") -> Optional[Board]:
    files = _eda_files(root)
    if not files:
        return None
    order = {k: i for i, k in enumerate(_NETLIST_KINDS + _SCHEMATIC_KINDS + ("bom",))}
    files.sort(key=lambda f: order.get(f[1], 99))
    board = Board(name=name)
    for path, kind in files:
        rel = str(path.relative_to(root)).replace("\\", "/")
        try:
            part = parse(kind, rel, path.read_bytes())
        except Exception as e:  # a broken file must not hide the others
            board.warnings.append(f"{rel}: could not parse as {FORMAT_LABELS.get(kind, kind)} ({type(e).__name__}: {e})")
            continue
        if part is not None:
            board.merge(part)
    return board if board.parts else None


def load_board(root: Path) -> Optional[Board]:
    p = Path(root) / "board.json"
    if not p.exists():
        return None
    try:
        return Board.from_json(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None


def rebuild_project_board(root: Path, store: Any, name: str = "board") -> Optional[Dict[str, Any]]:
    """Merge all EDA files in the project, persist board.json and the quotable connectivity source."""
    root = Path(root)
    board = build_board(root, name)
    bp = root / "board.json"
    if board is None:
        if bp.exists():
            bp.unlink()
        return None
    bp.write_text(json.dumps(board.to_json(), indent=1), encoding="utf-8")
    src = store.add(BOARD_SOURCE_URL, f"Board connectivity ({name})", board.report(), kind="schematic", replace=True)
    return {**board.summary(), "source_id": src.id}
