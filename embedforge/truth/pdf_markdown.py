"""Datasheet PDF -> Markdown for the datasheet analyzer.

pymupdf4llm turns the PDF into Markdown with headings and tables (tables are taken as HTML, whose row/col spans are then
expanded into plain Markdown rows, so every register row states its own size and sign). Two things it cannot do by itself:

* Encrypted datasheets (Bosch PDFs are AES-encrypted with an empty user password): PyMuPDF opens them directly.
* Equations: Word exports math in a Math font (Cambria Math) whose ToUnicode table is often broken, so the text layer
  says '𝑃𝑃𝑃𝑃𝑃𝑃_𝑇𝑇1' for 'PAR_T1', and the layout model drops the region. The glyph ids are still right: they are decoded
  through the font's own character map (the embedded font, another embedded subset, or the installed system font, whose
  MATH table also names the big parentheses and brackets), and the 2-D layout is turned back into a linear formula:
  numerator over denominator on baselines above and below the equation's '=' line become '(a) / (b)', smaller raised
  runs become '^' exponents:
      PAR_P1 = (NVM_PAR_P1 - 2^14) / 2^20
  Reconstructed formulas are inserted after the text they follow on the page, marked as reconstructed. Prose that
  carries inline math is rewritten from the decoded characters of its layout box ('T_conv = total conversion time').
"""

from __future__ import annotations

import html
import os
import re
import struct
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MAX_LAYOUT_PAGES = 150   # the layout model is slow (about 0.7 s/page); longer manuals use the plain Markdown path

_MATH_ALNUM = (0x1D400, 0x1D7FF)
_DASHES = dict.fromkeys(map(ord, "\u2212\u2010\u2011\u2012\u2013\u2014"), "-")


# ------------------------------------------------------------------ HTML tables -> Markdown


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: List[List[Tuple[str, int, int]]] = []
        self._cell: Optional[List[str]] = None
        self._span = (1, 1)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            self._cell = []
            self._span = (max(1, int(a.get("rowspan") or 1)), max(1, int(a.get("colspan") or 1)))
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            if not self.rows:
                self.rows.append([])
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            self.rows[-1].append((text, *self._span))
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_table_to_markdown(table_html: str) -> str:
    """One HTML table -> a Markdown table; a cell spanning rows/columns is repeated in each, so rows stand alone."""
    p = _TableParser()
    p.feed(table_html)
    grid: Dict[Tuple[int, int], str] = {}
    width = 0
    for r, row in enumerate(p.rows):
        c = 0
        for text, rs, cs in row:
            while (r, c) in grid:
                c += 1
            for dr in range(rs):
                for dc in range(cs):
                    grid[(r + dr, c + dc)] = text
            c += cs
            width = max(width, c)
    nrows = max((r for r, _ in grid), default=-1) + 1
    if not nrows or not width:
        return ""

    def cell(s: str) -> str:
        return s.replace("|", "\\|")
    lines = []
    for r in range(nrows):
        lines.append("| " + " | ".join(cell(grid.get((r, c), "")) for c in range(width)) + " |")
        if r == 0:
            lines.append("|" + "---|" * width)
    return "\n".join(lines)


def html_tables_to_markdown(md: str) -> str:
    return re.sub(r"<table>.*?</table>", lambda m: "\n" + html_table_to_markdown(m.group(0)) + "\n", md, flags=re.S)


# ------------------------------------------------------------------ math glyph decoding


def _ttc_face(data: bytes, index: int) -> bytes:
    """One face of a TrueType collection as a standalone font buffer: the face's table directory moved to offset 0
    (table offsets in a TTC are absolute, so the tables stay valid)."""
    if data[:4] != b"ttcf":
        return data
    num = struct.unpack(">I", data[8:12])[0]
    offs = struct.unpack(f">{num}I", data[12:12 + 4 * num])
    o = offs[index]
    ntab = struct.unpack(">H", data[o + 4:o + 6])[0]
    directory = data[o:o + 12 + 16 * ntab]
    return directory + data[len(directory):]


def _font_dirs() -> List[Path]:
    dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts", Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
            Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts", Path("/Library/Fonts"),
            Path("/System/Library/Fonts"), Path.home() / "Library/Fonts"]
    return [d for d in dirs if d.is_dir()]


def _family(base: str) -> str:
    return re.sub(r"[^a-z0-9]", "", base.split("+", 1)[-1].lower())


_SYSTEM_MAPS: Dict[str, Optional[Dict[int, int]]] = {}


def _reverse_cmap(font) -> Dict[int, int]:
    rev: Dict[int, int] = {}
    for cp in font.valid_codepoints():
        g = font.has_glyph(cp)
        if g:
            rev.setdefault(g, cp)
    return rev


def _math_variants(face: bytes, rev: Dict[int, int]) -> Dict[int, int]:
    """gid -> codepoint for the size variants and assembly parts of stretchy glyphs (big parentheses, brackets,
    radicals), read from the font's OpenType MATH table: they have no cmap entry of their own."""
    def u16(o: int) -> int:
        return struct.unpack(">H", face[o:o + 2])[0]

    def coverage(o: int) -> List[int]:
        if u16(o) == 1:
            return [u16(o + 4 + 2 * i) for i in range(u16(o + 2))]
        out: List[int] = []
        for i in range(u16(o + 2)):
            start, end = u16(o + 4 + 6 * i), u16(o + 6 + 6 * i)
            out.extend(range(start, end + 1))
        return out

    out: Dict[int, int] = {}
    try:
        math = next((struct.unpack(">I", face[12 + 16 * i + 8:12 + 16 * i + 12])[0] for i in range(u16(4))
                     if face[12 + 16 * i:12 + 16 * i + 4] == b"MATH"), None)
        if math is None:
            return out
        mv = math + u16(math + 8)
        for cov_off, count_at, first in ((u16(mv + 2), mv + 6, mv + 10), (u16(mv + 4), mv + 8, mv + 10 + 2 * u16(mv + 6))):
            if not cov_off:
                continue
            for i, base in enumerate(coverage(mv + cov_off)[:u16(count_at)]):
                cp = rev.get(base)
                if cp is None:
                    continue
                con = mv + u16(first + 2 * i)
                for k in range(u16(con + 2)):
                    out.setdefault(u16(con + 4 + 4 * k), cp)
                if u16(con):   # a glyph assembly: its parts (top, extender, bottom) stand for the same character
                    asm = con + u16(con)
                    for k in range(u16(asm + 4)):
                        out.setdefault(u16(asm + 6 + 10 * k), cp)
    except (struct.error, IndexError):
        pass
    return out


def _system_map(family: str) -> Optional[Dict[int, int]]:
    """gid -> codepoint of an installed font with this family name (e.g. Cambria Math inside cambria.ttc)."""
    if family in _SYSTEM_MAPS:
        return _SYSTEM_MAPS[family]
    import pymupdf
    found = None
    stem = family[:5]
    for d in _font_dirs():
        for f in sorted(d.rglob("*")):
            if f.suffix.lower() not in (".ttf", ".ttc", ".otf") or stem not in f.name.lower().replace(" ", ""):
                continue
            try:
                data = f.read_bytes()
                faces = struct.unpack(">I", data[8:12])[0] if data[:4] == b"ttcf" else 1
                for i in range(faces):
                    face = _ttc_face(data, i)
                    font = pymupdf.Font(fontbuffer=face)
                    if _family(font.name.replace(" Regular", "")) == family:
                        found = _reverse_cmap(font)
                        for g, cp in _math_variants(face, found).items():
                            found.setdefault(g, cp)
                        break
            except Exception:
                continue
            if found:
                break
        if found:
            break
    _SYSTEM_MAPS[family] = found
    return found


class GlyphDecoder:
    """Per document: for each Math font, gid -> codepoint from its own cmap, another embedded subset of the same family,
    or the installed font."""

    def __init__(self, doc):
        self.doc = doc
        self.maps: Dict[str, Optional[Dict[int, int]]] = {}
        self._embedded: Dict[str, Dict[int, int]] = {}
        self.blocks: Dict[str, Dict[str, int]] = {}

    def _embedded_map(self, family: str) -> Dict[int, int]:
        if family not in self._embedded:
            rev: Dict[int, int] = {}
            import pymupdf
            seen = set()
            for pno in range(min(len(self.doc), 400)):
                for xref, _ext, _typ, base, *_ in self.doc.get_page_fonts(pno):
                    if xref in seen or _family(base) != family:
                        continue
                    seen.add(xref)
                    try:
                        buf = self.doc.extract_font(xref)[3]
                        if buf:
                            for g, cp in _reverse_cmap(pymupdf.Font(fontbuffer=buf)).items():
                                rev.setdefault(g, cp)
                    except Exception:
                        continue
            self._embedded[family] = rev
        return self._embedded[family]

    def map_for(self, font_name: str) -> Optional[Dict[int, int]]:
        fam = _family(font_name)
        if fam not in self.maps:
            # the installed font keeps the original glyph numbering that Identity-H subsets use; another embedded subset
            # may have been renumbered, so it only fills gaps
            m = dict(_system_map(fam) or {})
            for g, cp in self._embedded_map(fam).items():
                m.setdefault(g, cp)
            self.maps[fam] = m or None
        return self.maps[fam]

    # Letter blocks of glyph variants that have no cmap entry (script-size letters in Cambria Math are 26 consecutive
    # glyphs). Their position is learnt by vote over the document: every unmapped glyph proposes 'gid - index of the
    # letter the text layer reports'; the text layer is right often enough (first letters of words) that the true block
    # start wins, while its wrong reports scatter.
    def letter(self, font_name: str, gid: int) -> Optional[str]:
        fam = _family(font_name)
        if fam not in self.blocks:
            self._learn_blocks(fam)
        for case, base in self.blocks.get(fam, {}).items():
            if 0 <= gid - base < 26:
                return chr(ord("a" if case == "lower" else "A") + gid - base)
        return None

    def _learn_blocks(self, fam: str) -> None:
        from collections import Counter
        m = self.maps.get(fam) or {}
        votes = {"lower": Counter(), "upper": Counter()}
        for pno in range(min(len(self.doc), 400)):
            try:
                spans = self.doc[pno].get_texttrace()
            except Exception:
                continue
            for span in spans:
                if _family(span.get("font", "")) != fam:
                    continue
                for u, g, *_ in span.get("chars") or []:
                    if g == -1 or g in m:
                        continue
                    c = _norm(chr(u))[:1]
                    if c.isascii() and c.isalpha():
                        votes["lower" if c.islower() else "upper"][g - (ord(c.lower()) - ord("a"))] += 1
        found = {}
        for case, cnt in votes.items():
            if cnt:
                base, n = cnt.most_common(1)[0]
                if n >= 3 and n >= 0.2 * sum(cnt.values()):
                    found[case] = base
        self.blocks[fam] = found


# ------------------------------------------------------------------ formula layout


@dataclass
class Run:
    text: str
    x0: float
    x1: float
    base: float   # baseline y
    size: float
    raw: str = ""   # what the PDF's text layer says for it (garbled for broken math fonts)


_OPS = {"⁡": "", "⁢": "*", "×": "*", "⋅": "*", "∙": "*", "·": "*", "∗": "*", "÷": "/"}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_DASHES)
    for a, b in _OPS.items():
        s = s.replace(a, b)
    return s


def _is_math_text(s: str) -> bool:
    return any(_MATH_ALNUM[0] <= ord(c) <= _MATH_ALNUM[1] for c in s)


def page_math_glyphs(page, decoder: GlyphDecoder) -> List[Run]:
    """The page's math-font glyphs, each decoded through the font's real character map; glyph variants with no map
    entry are decoded from their letter block, anchored on the first letter of the word, which the text layer reports
    correctly even when the rest is garbled."""
    glyphs: List[Run] = []
    for span in page.get_texttrace():
        font = span.get("font", "")
        chars = span.get("chars") or []
        if "math" not in font.lower() and not _is_math_text("".join(chr(c[0]) for c in chars)):
            continue
        m = decoder.map_for(font) or {}
        size = span["size"]
        word: List[Tuple[Run, int]] = []
        for u, g, origin, bbox in chars:
            if g == -1 and word:
                word[-1][0].raw += chr(u)   # extra code points of one glyph's ToUnicode entry
                continue
            word.append((Run("", bbox[0], bbox[2], origin[1], size, chr(u)), g))
        # the text layer is broken when different glyphs report the same letter (PAR -> 𝑃𝑃𝑃) or one glyph reports a
        # letter twice (c -> 𝑐𝑐)
        seen: Dict[str, int] = {}
        broken = any(len(r.raw) >= 2 and r.raw[0].isalpha() and r.raw == r.raw[0] * len(r.raw) for r, _ in word)
        for r, g in word:
            c = r.raw[:1]
            if c.isalpha() and seen.setdefault(c, g) != g:
                broken = True
        local: Dict[str, int] = {}   # too rare to vote on (e.g. one capital subscript): anchor on the word's first letter
        for r, g in word:
            if g in m:
                r.text = chr(m[g])
            elif not broken:
                r.text = r.raw.replace("�", "")   # a working text layer: trust it
            else:
                r.text = decoder.letter(font, g) or ""
                hint = _norm(r.raw[:1])
                if not r.text and hint.isascii() and hint.isalpha():
                    case = "lower" if hint.islower() else "upper"
                    base = local.setdefault(case, g - (ord(hint.lower()) - ord("a")))
                    if 0 <= g - base < 26:
                        r.text = chr(ord("a" if case == "lower" else "A") + g - base)
                r.text = r.text or "?"
        glyphs.extend(r for r, _ in word)
    return glyphs


def page_math_runs(page, decoder: GlyphDecoder) -> List[Run]:
    """The page's math text as runs: decoded glyphs on the same baseline and size, close together (a word gap becomes
    a space)."""
    glyphs = page_math_glyphs(page, decoder)

    def occupied(a: float, b: float, base: float, size: float) -> bool:
        """Another glyph (an index, a stacked fraction) sits in the gap between two glyphs of a line."""
        return any(a - 0.5 <= h.x0 and h.x1 <= b + 0.5 and abs(h.base - base) > 0.5 and abs(h.base - base) < size
                   for h in glyphs)
    runs: List[Run] = []
    for gl in sorted(glyphs, key=lambda r: (round(r.base, 0), r.x0)):
        cur = runs[-1] if runs else None
        gap = gl.x0 - cur.x1 if cur else 0.0
        if (cur and abs(gl.base - cur.base) < 0.5 and abs(gl.size - cur.size) < 0.5 and -0.5 < gap < 0.9 * gl.size
                and not (gap > 0.5 and occupied(cur.x1, gl.x0, gl.base, gl.size))):
            sep = " " if gl.x0 - cur.x1 > 0.22 * gl.size else ""
            cur.text += sep + gl.text
            cur.raw += gl.raw
            cur.x1 = max(cur.x1, gl.x1)
        else:
            runs.append(Run(gl.text, gl.x0, gl.x1, gl.base, gl.size, gl.raw))
    for r in runs:
        r.text = _norm(r.text)
    return [r for r in runs if r.text.strip()]


def _atom(s: str) -> str:
    s = s.strip()
    simple = re.fullmatch(r"[\w.]+(\^\(?-?[\w.]+\)?)?", s) or (s.startswith("(") and s.endswith(")") and s.count("(") == 1)
    return s if simple else f"({s})"


def _attach_scripts(runs: List[Run]) -> List[Run]:
    """Smaller runs right after a run become its exponent (raised) or index (lowered): 2^(osr_p), T_conv."""
    if not runs:
        return []
    big = max(r.size for r in runs)
    full = [Run(r.text, r.x0, r.x1, r.base, r.size, r.raw) for r in runs if r.size >= 0.85 * big]
    small = sorted((r for r in runs if r.size < 0.85 * big), key=lambda r: (round(r.base), r.x0))
    groups: List[Run] = []
    for s in small:   # adjacent small runs on one baseline form one script
        g = groups[-1] if groups else None
        if g and abs(g.base - s.base) < 0.5 and 0 <= s.x0 - g.x1 < 0.6 * s.size:
            g.text += (" " if s.x0 - g.x1 > 0.25 * s.size else "") + s.text
            g.x1 = s.x1
        else:
            groups.append(Run(s.text, s.x0, s.x1, s.base, s.size, s.raw))
    for s in groups:
        base = min((f for f in full if -0.5 * f.size <= s.x0 - f.x1 <= 0.6 * f.size and abs(f.base - s.base) < f.size),
                   key=lambda f: abs(s.x0 - f.x1), default=None)
        t = s.text.strip()
        if base is None:
            full.append(Run(t, s.x0, s.x1, s.base, big, s.raw))
            continue
        piece = t if re.fullmatch(r"-?[\w.]+", t) else f"({t})"
        base.text += ("^" if s.base < base.base - 0.1 * base.size else "_") + piece
        base.x1 = max(base.x1, s.x1)
    return full


def formulas_from_runs(runs: List[Run]) -> List[Tuple[float, float, str]]:
    """(top y, x0, linear formula) for each equation: a line with '=' and what is stacked above and below it."""
    full = sorted(_attach_scripts(runs), key=lambda r: r.base)
    bands: List[List[Run]] = []
    for r in full:
        if bands and r.base - max(x.base for x in bands[-1]) <= 1.2 * r.size:
            bands[-1].append(r)
        else:
            bands.append([r])
    out: List[Tuple[float, float, str]] = []
    for band in bands:
        band.sort(key=lambda r: r.x0)
        eqs: List[List[Run]] = []
        for r in band:   # equations side by side: split at wide horizontal gaps
            if eqs and r.x0 - max(x.x1 for x in eqs[-1]) <= 2.5 * r.size:
                eqs[-1].append(r)
            else:
                eqs.append([r])
        for eq in eqs:
            for part in _split_equations(eq):
                out.append((min(r.base - r.size for r in part), min(r.x0 for r in part), _linearize(part)))
    return out


def _split_equations(eq: List[Run]) -> List[List[Run]]:
    """Several equations close together on one row: cut at the widest gap before each further '='."""
    eqs_signs = sorted((r for r in eq if "=" in r.text), key=lambda r: r.x0)
    if len(eqs_signs) < 2:
        return [eq]
    items = sorted(eq, key=lambda r: r.x0)
    cuts = []
    for prev, cur in zip(eqs_signs, eqs_signs[1:]):
        best, at = -1.0, None
        reach = 0.0
        for a, b in zip(items, items[1:]):
            reach = max(reach, a.x1)
            if prev.x1 <= b.x0 <= cur.x0 and b.x0 - reach > best:
                best, at = b.x0 - reach, b.x0
        if at is not None:
            cuts.append(at)
    parts: List[List[Run]] = []
    edges = [float("-inf")] + cuts + [float("inf")]
    for lo, hi in zip(edges, edges[1:]):
        part = [r for r in eq if lo <= r.x0 < hi]
        if part:
            parts.append(part)
    return parts


def _clusters(runs: List[Run], size: float, gap: float = 1.0) -> List[List[Run]]:
    out: List[List[Run]] = []
    for r in sorted(runs, key=lambda r: r.x0):
        if out and r.x0 - max(x.x1 for x in out[-1]) <= gap * size:
            out[-1].append(r)
        else:
            out.append([r])
    return out


def _linearize(eq: List[Run]) -> str:
    size = max(r.size for r in eq)
    with_eq = [r for r in eq if "=" in r.text]
    if with_eq:
        main = with_eq[0].base
    else:
        main = max({round(r.base, 1) for r in eq}, key=lambda b: sum(len(r.text) for r in eq if abs(r.base - b) < 0.3 * size))
    mains = [r for r in eq if abs(r.base - main) < 0.3 * size]
    above = [r for r in eq if r.base < main - 0.3 * size]
    below = [r for r in eq if r.base > main + 0.3 * size]
    tokens: List[Tuple[float, str]] = [(r.x0, r.text) for r in mains]
    used = set()
    below_groups = _clusters(below, size, gap=0.4)   # touching pieces only: two fractions' denominators stay apart
    for num in _clusters(above, size):   # a fraction: numerator above, denominator below the same horizontal range
        x0, x1 = min(r.x0 for r in num), max(r.x1 for r in num)
        # the whole denominator, also when it is wider than the numerator: 1 over 'T_conv [μs] * 10^6'
        den = [r for grp in below_groups if any(r.x1 > x0 - 0.5 * size and r.x0 < x1 + 0.5 * size for r in grp)
               for r in grp if id(r) not in used]
        used |= {id(r) for r in den}
        n = " ".join(r.text for r in sorted(num, key=lambda r: r.x0))
        if den:
            d = " ".join(r.text for r in sorted(den, key=lambda r: r.x0))
            tokens.append((x0, f"{_atom(n)} / {_atom(d)}"))
        else:
            tokens.append((x0, n))
    for b in below:
        if id(b) not in used:
            tokens.append((b.x0, b.text))
    text = " ".join(t for _, t in sorted(tokens))
    text = re.sub(r"\s*=\s*", " = ", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"(?<=[\w)])\s*([+\-*])\s*(?=[\w(])", r" \1 ", text)
    return re.sub(r"\s+", " ", text).strip()


def page_formulas(page, decoder: GlyphDecoder) -> Tuple[List[Tuple[float, float, str]], Dict[str, str]]:
    """(equations on the page, {garbled text-layer string: decoded text} for inline math in the prose)."""
    runs = page_math_runs(page, decoder)
    fixes = {r.raw: r.text for r in runs if r.raw and r.raw != r.text and len(r.raw) >= 2}
    eqs = [f for f in formulas_from_runs(runs) if "=" in f[2] and len(f[2]) >= 5]
    return eqs, fixes


Char = Tuple[str, float, float, Tuple[float, float, float, float]]   # text, size, baseline y, bbox


def page_lines(page, decoder: GlyphDecoder) -> List[List[Char]]:
    """The page's text lines, character by character, with every math-font glyph replaced by its decoded character
    (matched by position: texttrace knows the glyph ids, rawdict the line structure)."""
    at: Dict[Tuple[float, float], str] = {}
    for gl in page_math_glyphs(page, decoder):
        at.setdefault((round(gl.x0, 1), round(gl.base, 1)), _norm(gl.text))
    lines: List[List[Char]] = []
    for b in page.get_text("rawdict").get("blocks", []):
        for ln in b.get("lines", []):
            chars: List[Char] = []
            used = set()
            for sp in ln.get("spans", []):
                for ch in sp.get("chars", []):
                    key = (round(ch["bbox"][0], 1), round(ch["origin"][1], 1))
                    if key in at:
                        if key in used:   # one glyph whose text layer gives several characters
                            continue
                        used.add(key)
                        c = at[key]
                    elif _is_math_text(ch["c"]):
                        continue
                    else:
                        c = ch["c"]
                    chars.append((c, sp["size"], ch["origin"][1], tuple(ch["bbox"])))
            if chars:
                lines.append(chars)
    return lines


def _render_line(chars: List[Char]) -> str:
    """One line of text; smaller characters raised or lowered from the line's baseline become ^exponent / _index."""
    vis = [c for c in chars if c[0].strip()]
    if not vis:
        return ""
    main = max(c[1] for c in vis)
    bases = sorted(c[2] for c in vis if c[1] >= 0.85 * main)
    mid = bases[len(bases) // 2] if bases else vis[0][2]
    groups: List[Tuple[str, str]] = []
    for c, size, base, _ in chars:
        kind = ""
        if c.strip() and size < 0.85 * main:
            kind = "^" if base < mid - 0.1 * main else "_" if base > mid + 0.1 * main else ""
        if groups and groups[-1][0] == kind:
            groups[-1] = (kind, groups[-1][1] + c)
        else:
            groups.append((kind, c))
    return "".join(t if not k else k + (t if re.fullmatch(r"[\w.\-]+", t) else f"({t})") for k, t in groups)


def _inside(c: Char, bbox, pad: float = 0.0) -> bool:
    cx, cy = (c[3][0] + c[3][2]) / 2, (c[3][1] + c[3][3]) / 2
    return bbox[0] - pad <= cx <= bbox[2] + pad and bbox[1] - pad <= cy <= bbox[3] + pad


def _line_owners(lines: List[List[Char]], boxes: List[dict]) -> List[int]:
    """For each line, the layout box holding most of its characters (boxes overlap at their edges; the smaller box
    wins a tie), or -1."""
    owners = []
    for ln in lines:
        best, key = -1, (0, 0.0)
        for i, b in enumerate(boxes):
            x0, y0, x1, y1 = b["bbox"]
            n = sum(_inside(c, b["bbox"]) for c in ln) or sum(_inside(c, b["bbox"], 1.0) for c in ln) / 1000
            k = (n, -(x1 - x0) * (y1 - y0))
            if n and k > key:
                best, key = i, k
        owners.append(best)
    return owners


def _box_text(lines: List[List[Char]], owners: List[int], index: int) -> str:
    out = [_render_line(ln) for ln, o in zip(lines, owners) if o == index]
    return re.sub(r"\s+", " ", " ".join(out)).strip()


_NOT_TEXT = ("table", "picture", "page-header", "page-footer")
_FORMULA_NOTE = "*Formulas (reconstructed from the PDF's equation glyphs):*"


def _formula_anchor(boxes: List[dict], top: float, x0: float) -> int:
    """The layout box a reconstructed formula goes after: the text box it sits in, else the nearest one above it."""
    text = [(i, b) for i, b in enumerate(boxes)
            if b.get("class") not in _NOT_TEXT + ("formula",) and b["pos"][1] > b["pos"][0]]
    px, py = x0 + 2, top + 2
    inside = [i for i, b in text if b["bbox"][0] - 2 <= px <= b["bbox"][2] + 2 and b["bbox"][1] - 2 <= py <= b["bbox"][3] + 2]
    if inside:
        return inside[-1]
    above = [(b["bbox"][3], i) for i, b in text if b["bbox"][3] <= top + 2]
    return max(above)[1] if above else -1


def _rebuild_page(md: str, boxes: List[dict], lines: List[List[Char]], formulas: List[Tuple[float, float, str]]) -> str:
    """pymupdf4llm's Markdown with each layout box that still carries garbled math glyphs rewritten from the page's
    decoded characters, and each reconstructed formula placed after the text it belongs to."""
    after: Dict[int, List[str]] = {}
    for top, x0, f in sorted(formulas):
        after.setdefault(_formula_anchor(boxes, top, x0), []).append(f)

    def note(fs: List[str]) -> str:
        return _FORMULA_NOTE + "\n\n" + "\n".join(f"`{f}`  " for f in fs)
    owners = _line_owners(lines, boxes)
    out: List[str] = []
    cur = 0
    for i, b in sorted(enumerate(boxes), key=lambda t: t[1]["pos"]):
        p0, p1 = b["pos"]
        if p0 < cur:
            continue
        out.append(md[cur:p0])
        seg = md[p0:p1]
        if _is_math_text(seg) and b.get("class") not in _NOT_TEXT:
            text = _box_text(lines, owners, i)
            lead = re.match(r"\s*(#+ +|[-*] +)?", seg).group(0)
            seg = (lead + text if text else re.sub(r"[\U0001D400-\U0001D7FF]+", "", seg.rstrip())) + seg[len(seg.rstrip()):]
        if i in after:
            seg = seg.rstrip() + "\n\n" + note(after[i]) + "\n\n"
        out.append(seg)
        cur = p1
    out.append(md[cur:])
    if -1 in after:
        out.append("\n\n" + note(after[-1]) + "\n")
    return "".join(out)


# ------------------------------------------------------------------ document


def _insert_formulas(md: str, page, formulas: List[Tuple[float, float, str]]) -> str:
    """Put the reconstructed equations after the page text that precedes them, or at the end of the page."""
    block = "\n".join(f"`{f}`  " for _, _, f in sorted(formulas))
    note = _FORMULA_NOTE + "\n\n" + block + "\n"
    top = min(y for y, _, _ in formulas)
    before = None
    for b in page.get_text("blocks"):
        y1, text = b[3], b[4]
        if y1 <= top + 1 and text.strip() and not _is_math_text(text) and (before is None or y1 > before[0]):
            before = (y1, text)
    if before:
        tail = re.sub(r"\s+", " ", before[1]).strip()[-40:]
        if len(tail) >= 12:
            flat = re.sub(r"\s+", " ", md)
            k = flat.find(tail)
            if k >= 0:
                need = len(re.sub(r"\s", "", flat[:k + len(tail)]))
                pos = seen = 0
                while pos < len(md) and seen < need:   # back from the whitespace-flattened text to the original
                    if not md[pos].isspace():
                        seen += 1
                    pos += 1
                end = md.find("\n\n", pos)
                end = len(md) if end < 0 else end
                return md[:end] + "\n\n" + note + md[end:]
    return md.rstrip() + "\n\n" + note


def pdf_to_markdown(data: bytes, max_pages: int = 600) -> Optional[str]:
    """Markdown of a PDF, or None when PyMuPDF / pymupdf4llm are not installed."""
    try:
        import pymupdf
        import pymupdf4llm
    except ImportError:
        return None
    doc = pymupdf.open(stream=data, filetype="pdf")
    if doc.needs_pass and not doc.authenticate(""):
        raise ValueError("the PDF is password protected")
    n = min(len(doc), max_pages)
    pages = list(range(n))
    if n > MAX_LAYOUT_PAGES:
        pymupdf4llm.use_layout(False)
        try:
            chunks = pymupdf4llm.to_markdown(doc, pages=pages, page_chunks=True, show_progress=False)
        finally:
            pymupdf4llm.use_layout(True)
    else:
        chunks = pymupdf4llm.to_markdown(doc, pages=pages, page_chunks=True, table_output="html", header=False,
                                         footer=False, show_progress=False)
    decoder = GlyphDecoder(doc)
    out: List[str] = []
    for k, ch in enumerate(chunks):
        out.append(f"<!-- PDF page {k + 1} -->\n\n{page_markdown(doc[k], ch, decoder).strip()}")
    return "\n\n".join(out) + "\n"


def page_markdown(page, chunk: dict, decoder: GlyphDecoder) -> str:
    """One page: pymupdf4llm's chunk with tables flattened, math decoded and formulas reconstructed."""
    md = chunk.get("text") or ""
    try:
        formulas, fixes = page_formulas(page, decoder)
    except Exception:
        formulas, fixes = [], {}
    boxes = chunk.get("page_boxes") or []
    placed = False
    if boxes and (formulas or _is_math_text(md)):
        try:   # the layout boxes say where each piece of Markdown is on the page
            md = _rebuild_page(md, boxes, page_lines(page, decoder) if _is_math_text(md) else [], formulas)
            placed = True
        except Exception:
            pass
    md = html_tables_to_markdown(md)
    for raw in sorted(fixes, key=len, reverse=True):   # inline math left in tables and pictures: the decoded text
        md = md.replace(raw, fixes[raw])
    if formulas and not placed:
        md = _insert_formulas(md, page, formulas)
    return md


