"""What the code MEANS, checked against the datasheet: register fields, multi-byte layouts, burst reads and formulas.

Facts and sequences prove that the right numbers appear in the right order. They cannot tell that 0x03 written to
PWR_CTRL means "sleep" when the datasheet's table says mode is bits 5..4, or that `data[0] << 16` puts the XLSB byte on
top. So the model also records, from verbatim datasheet text:

  * registers   — name, address and fields (bit positions, named values):  PWR_CTRL 0x1B: press_en bit 0, temp_en bit 1,
                  mode bits 5..4 {sleep: 0, forced: 1|2, normal: 3}
  * layouts     — how a multi-byte value is spread over registers:  UP = 0x04 | 0x05 << 8 | 0x06 << 16, unsigned
  * bursts      — registers that must be read in one transaction (data shadowing)
  * formulas    — the conversions the code must implement:  PAR_T1 = NVM_PAR_T1 / 2^-8;  T_LIN = ...

and verify checks the code statically (truth.flow, no simulation):

  * every register write is decoded into its fields; reserved bits and unnamed field values are reported;
  * a bit mask applied to a register byte must select defined fields (STATUS & 0x0C on a register whose flags are bits
    4..6 is an error);
  * every shift applied to a byte read from a layout register must be that byte's position in the layout;
  * reads that touch a burst range must read the whole range at once;
  * probe run: the code is walked with concrete bytes for the layout registers (two random sets); every recorded formula
    is evaluated on the same bytes and its result must be among the numbers the code computes.
"""

from __future__ import annotations

import ast
import json
import math
import random
import re
import threading
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from embedforge.truth.flow import FlowModel, Part, RxByte, Walker, is_int, writes_of
from embedforge.truth.ledger import FactLedger, _index, _squash, bounded, parse_int, value_in_text
from embedforge.truth.source_store import SourceStore

MAX_QUOTE = 2000
_ALNUM = re.compile(r"[^a-z0-9]")


def _alnum(s: str) -> str:
    return _ALNUM.sub("", (s or "").lower())


_ESC = re.compile(r"\\u([0-9a-fA-F]{4})|\\[nrt]")
_GAP = re.compile(r"\s*(?:…|\.{3,}|\[\.\.\.\])\s*")


def _unescape(q: str) -> str:
    """Literal '\\u2013' / '\\n' left in a quote by double JSON escaping are the characters they stand for."""
    return _ESC.sub(lambda m: chr(int(m.group(1), 16)) if m.group(1) else " ", q or "")


@lru_cache(maxsize=8)
def _squashed(text: str, strip_comments: bool) -> str:
    if strip_comments:
        text = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)
    return _squash(text)


def verbatim(quote: str, text: str) -> bool:
    """The quote is in the text, ignoring whitespace, case and quote marks; '…' / '...' may stand for omitted text between
    verbatim pieces (in order, close together), and C comments may be left out of a quoted code block."""
    q = _unescape(quote)
    variants = [(q, False)]
    if "/*" in text or "//" in text:
        variants.append((re.sub(r"/\*.*?\*/|//[^\n]*", " ", q, flags=re.S), True))
    for qq, strip in variants:
        parts = [_squash(p) for p in _GAP.split(qq) if _squash(p)]
        if not parts or (len(parts) > 1 and min(len(p) for p in parts) < 8):
            continue
        ix = None if strip else _index(text)   # word boundaries at both ends: '1B PWR_CTRL' is not a quote of '0x1B PWR_CTRL'
        tt = _squashed(text, strip) if strip else ix.s
        start = tt.find(parts[0])
        while start >= 0:
            pos, ok = start + len(parts[0]), True
            for p in parts[1:]:
                i = tt.find(p, pos)
                if i < 0 or i - pos > 1500:
                    ok = False
                    break
                pos = i + len(p)
            if ok and ix is not None:
                sp = ix.span(start, pos)
                ok = sp is not None and bounded(text, sp[0], sp[1], parts[0][0], parts[-1][-1])
            if ok:
                return True
            start = tt.find(parts[0], start + 1)
    return False


def _quote_ok(store: SourceStore, source_id: str, quote: str, what: str) -> Optional[str]:
    src = store.get(source_id)
    if src is None:
        return f"{what}: source_id {source_id!r} is not a stored source"
    if len(_squash(quote)) < 12:
        return f"{what}: quote too short — copy the full table rows / sentence from read_source"
    if len(quote) > MAX_QUOTE:
        return f"{what}: quote longer than {MAX_QUOTE} chars — quote only the rows that state it"
    if not verbatim(quote, src.text):
        return f"{what}: quote is not verbatim in {source_id} (copy it exactly from read_source)"
    return None


def table_passages(text: str, keys: List[str], want_bits: bool = True, limit: int = 2, size: int = 520) -> List[str]:
    """Verbatim windows of a source starting at the line where one of `keys` (register name or address) appears, best
    first: register descriptions with bit rows ('Table 40: Register 0x1B ... Bit 5..4 mode') before memory-map rows and
    the table of contents."""
    scored = []
    for key in [k for k in keys if k and len(k) >= 2]:
        rx = rf"(?<![0-9A-Za-z]){re.escape(key)}(?![0-9A-Za-z])" if re.fullmatch(r"0x[0-9A-Fa-f]+", key) else re.escape(key)
        for m in re.finditer(rx, text, re.I):
            a = text.rfind("\n", 0, m.start()) + 1
            window = text[a:a + size]
            if "....." in window[:200]:
                continue   # table of contents
            score = (2 if re.search(r"\bBit\s*\d", window) or not want_bits else 0) + (1 if re.match(r"\s*Table\s+\d+", window) else 0)
            scored.append((-score, a, window))
    out: List[str] = []
    for _s, _a, w in sorted(scored):
        w = (w[:w.rfind("\n")] if "\n" in w[size // 3:] else w).strip()
        if w and all(w[:80] not in o for o in out):
            out.append(w)
        if len(out) >= limit:
            break
    return out


def layout_passages(text: str, addrs: List[int], limit: int = 3) -> List[str]:
    """Shortest verbatim spans (whole lines) that contain every address of a multi-byte value: its table rows."""
    pats = [re.compile(rf"(?<![0-9a-z])0x0*{a:x}(?![0-9a-z])", re.I) for a in addrs]
    if not pats:
        return []
    out: List[str] = []
    for m in pats[0].finditer(text):
        lo, hi = m.start(), m.end()
        ok = True
        for p in pats[1:]:
            near = [x for x in p.finditer(text, max(0, m.start() - 700), m.end() + 700)]
            if not near:
                ok = False
                break
            x = min(near, key=lambda x: abs(x.start() - m.start()))
            lo, hi = min(lo, x.start()), max(hi, x.end())
        if not ok or hi - lo > 900:
            continue
        a = text.rfind("\n", 0, lo) + 1
        b = text.find("\n", hi)
        span = text[a:b if b >= 0 else len(text)].strip()
        if "....." not in span and span not in out:
            out.append(span)
        if len(out) >= limit:
            break
    return sorted(out, key=len)


_ROW_RANGE = re.compile(r"(?<![0-9a-z])0x([0-9a-f]{1,4})(?![0-9a-z])[^\n<\[]{0,40}?([a-z][\w ]{1,30}?)\s*[<\[]\s*(\d+)\s*:\s*(\d+)\s*[>\]]", re.I)


@lru_cache(maxsize=8)
def row_ranges(text: str) -> Tuple[Tuple[int, str, int, int], ...]:
    """Register-content rows written as NAME<hi:lo> / NAME[hi:lo] next to their address ('0x32 NVM_PAR_T1<15:8>'):
    (address, normalised name, hi, lo). They state exactly which bytes make up a multi-byte value."""
    return tuple((int(m.group(1), 16), _alnum(m.group(2)), int(m.group(3)), int(m.group(4))) for m in _ROW_RANGE.finditer(text))


def _num(v: Any) -> Optional[int]:
    if is_int(v):
        return v
    if isinstance(v, str):
        s = v.strip().lower().replace("_", "")
        if re.fullmatch(r"[01]+b", s):
            return int(s[:-1], 2)
        return parse_int(s)
    return None


def _hex(v: int) -> str:
    return f"0x{v:02X}"


def _bits_in_quote(quote: str, hi: int, lo: int, fname: str = "") -> bool:
    """The field's bit position is stated in its own row: right before its name ('Bit 5..4 mode', '0 press_en') or right
    after it ('mode[5:4]', 'press_en (bit 0)'). A bit number elsewhere in the quote belongs to another row."""
    q = quote.translate(dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212"), "-")).lower()
    sep = r"\s*(?:\.\.\.?|:|-|to|…)\s*"
    spec = rf"{lo}" if hi == lo else rf"(?:{hi}{sep}{lo}|{lo}{sep}{hi})"
    before = re.compile(rf"(?:^|[^0-9a-z.])(?:bits?\s*|[\[<]\s*|[bd])?{spec}(?![0-9.])[\s|:\]>)\-]*$")
    after = re.compile(rf"^[\s:|(\[<\-=]*(?:bits?\s*|[bd])?{spec}(?![0-9])")
    for a, b in _name_spans(q, fname or ""):
        if before.search(q[max(0, a - 16):a]) or after.search(q[b:b + 16]):
            return True
    return False


def _stated_bits(quotes: List[str], fname: str) -> Optional[Tuple[int, int]]:
    """The bit position a quote states for a field in its own row ('Bit 4..0 odr_sel' -> (4, 0)), if any."""
    for q in quotes:
        low = q.lower().translate(dict.fromkeys(map(ord, "‐‑‒–—−"), "-"))
        for a, _b in _name_spans(low, fname):
            m = re.search(r"(?:^|[^0-9a-z.])(?:bits?\s*)?(\d{1,2})(?:\s*(?:\.\.\.?|:|-|to)\s*(\d{1,2}))?[\s|:\]>)\-]*$", low[max(0, a - 16):a])
            if m:
                h = int(m.group(1))
                l = int(m.group(2)) if m.group(2) else h
                return (max(h, l), min(h, l))
    return None


def _name_spans(quote: str, name: str) -> List[Tuple[int, int]]:
    """Where `name` occurs in the quote, ignoring case, spaces, underscores and hyphens inside it."""
    chars = [re.escape(c) for c in name if c.isalnum()]
    if not chars:
        return []
    rx = r"(?<![A-Za-z0-9_])" + r"[\s_\-]*".join(chars) + r"(?![A-Za-z0-9_])"   # a whole word: drdy is not drdy_press
    return [(m.start(), m.end()) for m in re.finditer(rx, quote, re.I)]


def _stated_addrs(text: str, name: str) -> set:
    """Addresses written right before a register's name, with only spacing, quote marks or table bars between them:
    'Register 0x1B “PWR_CTRL”', '| 0x1B | PWR_C TRL |' (table cells wrap names), '0x1B PWR_CTRL'."""
    chars = [re.escape(c) for c in name if c.isalnum()]
    if len(chars) < 3:
        return set()
    rx = (r"(?<![0-9A-Za-z])0x([0-9A-Fa-f]{1,4})(?![0-9A-Za-z])[\s|\"'“”‘’]{0,8}(?<![A-Za-z0-9_])"
          + r"[\s_\-]*".join(chars) + r"(?![A-Za-z0-9_])")
    return {int(m.group(1), 16) for m in re.finditer(rx, text, re.I)}


_REG_HEADER = re.compile(r"Register\s+(0x[0-9A-Fa-f]+)\b", re.I)


def own_section(quote: str, addr: int) -> str:
    """The part of a quote that describes the register at `addr`: a quote running from one register's table into the
    next ('Register 0x1B PWR_CTRL … Table 41: Register 0x1C OSR … Bit 5..3 osr_t') must not lend the other register's
    fields. Without a header for addr in the quote, all of it."""
    heads = [(m.start(), int(m.group(1), 16)) for m in _REG_HEADER.finditer(quote)]
    own = [s for s, a in heads if a == addr]
    if not own:
        return quote
    first = own[0]
    start = first if any(a != addr for s, a in heads if s < first) else 0
    end = next((s for s, a in heads if s > first and a != addr), len(quote))
    section = quote[start:end]
    # PDF text can put a table's rows after the next header ('Table 41: Register 0x1C OSR', '4.3.18 Register 0x1D ODR',
    # then OSR's 'Bit 2..0 osr_p' rows): a section with no bit rows of its own was cut wrong — keep the whole quote
    return section if re.search(r"\bBit\s*\d", section, re.I) else quote


def _value_forms(v: int, width: int) -> List[str]:
    forms = {str(v), f"0x{v:x}", f"0x{v:02x}", f"{v:x}h", format(v, f"0{width}b") if width <= 16 else "",
             "0b" + format(v, f"0{width}b") if width <= 16 else "", "0b" + format(v, "b")}
    return [f for f in forms if f]


_VAL = r"(?:0x[0-9a-f]+|0b[01]+|[0-9a-f]+h|[0-9]+)"
_VAL_SEP = r"\s*(?:/|,|\band\b|\bor\b)\s*"
_VAL_LIST = rf"{_VAL}(?:{_VAL_SEP}{_VAL})*"
_VALUE_BEFORE = re.compile(rf"(?:^|[^0-9a-z.])({_VAL_LIST})[\s|:=>\-,(]*$")
_VALUE_AFTER = re.compile(rf"^\s*(?:(?:mode|state|setting|value)\s*)?(?:=|:|\(|->|→)\s*({_VAL_LIST})(?![0-9a-z])")
_VALUE_AFTER_BARE = re.compile(rf"^\s*(?:(?:mode|state|setting|value)\s*)?[\s|]*({_VAL_LIST})(?![0-9a-z])")


def _value_near(quote: str, name: str, v: int, width: int, reach: int = 16) -> bool:
    """A named value is stated right next to its name: the value token(s) directly before it ('11 normal mode', '01 and 10
    Forced mode', '| 0xB6 | softreset') or after it with an assignment mark ('normal = 0b11', 'sleep: 00'), or bare after
    it when no value stands before the name ('Normal mode 11' at the start of a row). A value further away is a
    neighbouring row's: in '00 sleep mode 01/10 forced', 01 is forced's, not sleep's."""
    low = quote.lower()
    forms = {f.lower() for f in _value_forms(v, width)}

    def hit(m: Any) -> bool:
        return bool(m) and any(tok.strip() in forms for tok in re.split(_VAL_SEP, m.group(1)))
    for a, b in _name_spans(quote, name):
        before = _VALUE_BEFORE.search(low[max(0, a - reach):a])
        after = low[b:b + reach]
        if hit(before) or hit(_VALUE_AFTER.search(after)) or (not before and hit(_VALUE_AFTER_BARE.search(after))):
            return True
    return False


# ------------------------------------------------------------------ records


@dataclass
class FieldDef:
    name: str
    hi: int
    lo: int
    values: Dict[str, List[int]] = field(default_factory=dict)

    @property
    def width(self) -> int:
        return self.hi - self.lo + 1

    @property
    def mask(self) -> int:
        return ((1 << self.width) - 1) << self.lo

    def get(self, reg_value: int) -> int:
        return (reg_value & self.mask) >> self.lo

    def label(self) -> str:
        return f"{self.name} bit {self.lo}" if self.hi == self.lo else f"{self.name} bits {self.hi}..{self.lo}"

    def value_name(self, v: int) -> str:
        return next((n for n, vs in self.values.items() if v in vs), "")


@dataclass
class RegisterDef:
    id: str
    device: str
    name: str
    addr: int
    fields: List[FieldDef]
    source_id: str
    quote: str
    id_value: Optional[int] = None

    @property
    def defined_mask(self) -> int:
        m = 0
        for f in self.fields:
            m |= f.mask
        return m

    def field(self, name: str) -> Optional[FieldDef]:
        key = _alnum(name)
        return next((f for f in self.fields if _alnum(f.name) == key), None)

    def decode(self, v: int) -> str:
        parts = []
        for f in sorted(self.fields, key=lambda x: -x.lo):
            fv = f.get(v)
            nm = f.value_name(fv)
            parts.append(f"{f.name}={fv}" + (f" ({nm})" if nm else ""))
        extra = v & ~self.defined_mask & 0xFF
        if extra:
            parts.append(f"undefined bits {_hex(extra)}")
        return ", ".join(parts)


@dataclass
class LayoutPart:
    addr: int
    hi: int = 7
    lo: int = 0

    @property
    def width(self) -> int:
        return self.hi - self.lo + 1

    def text(self) -> str:
        return _hex(self.addr) + ("" if (self.hi, self.lo) == (7, 0) else f"[{self.hi}:{self.lo}]")


@dataclass
class LayoutDef:
    id: str
    device: str
    name: str
    parts: List[LayoutPart]   # least significant first
    signed: bool
    source_id: str
    quote: str

    @property
    def width(self) -> int:
        return sum(p.width for p in self.parts)

    def offset_of(self, k: int) -> int:
        return sum(p.width for p in self.parts[:k])

    def decode(self, byte_of: Callable[[int], int]) -> int:
        v = 0
        for k, p in enumerate(self.parts):
            v |= ((byte_of(p.addr) >> p.lo) & ((1 << p.width) - 1)) << self.offset_of(k)
        if self.signed and v >> (self.width - 1):
            v -= 1 << self.width
        return v

    def describe(self) -> str:
        terms = [p.text() + (f" << {self.offset_of(k)}" if self.offset_of(k) else "") for k, p in enumerate(self.parts)]
        return f"{self.name} = {' | '.join(terms)} ({'signed' if self.signed else 'unsigned'} {self.width}-bit)"


@dataclass
class BurstDef:
    id: str
    device: str
    name: str
    start: int
    end: int
    source_id: str
    quote: str


@dataclass
class FormulaDef:
    id: str
    device: str
    name: str
    steps: List[List[str]]   # [var, expression] in order; the formula's value is the last var
    source_id: str
    quote: str
    unit: str = ""

    def text(self) -> str:
        return "; ".join(f"{v} = {e}" for v, e in self.steps)


# ------------------------------------------------------------------ formula expressions

_FUNCS: Dict[str, Callable[..., float]] = {
    "pow": lambda a, b: float(a) ** float(b), "powf": lambda a, b: float(a) ** float(b), "sqrt": math.sqrt, "sqrtf": math.sqrt,
    "exp": math.exp, "expf": math.exp, "log": math.log, "logf": math.log, "log10": math.log10, "abs": abs, "fabs": abs,
    "fabsf": abs, "ldexp": lambda x, e: math.ldexp(float(x), int(e)),
}
_CAST_RE = re.compile(r"\(\s*(?:const\s+)?(?:float|double|u?int(?:8|16|32|64)_t|int|long|unsigned(?:\s+int|\s+long)?|signed)\s*\)")


def prep_expr(expr: str) -> str:
    """Datasheet/C notation -> the expression language: ^ is power, casts and struct prefixes (calib->par_t1) drop out,
    C number suffixes (0.5f, 16384UL) go, // is C integer division."""
    s = (expr or "").strip().rstrip(";")
    s = s.translate(dict.fromkeys(map(ord, "‐‑‒–—−"), "-")).replace("×", "*").replace("÷", "/").replace("·", "*")
    s = _CAST_RE.sub("", s)
    s = re.sub(r"\b[A-Za-z_]\w*\s*(?:->|\.)\s*(?=[A-Za-z_])", "", s)
    s = re.sub(r"(?<=[0-9.])([fF]|[uU]?[lL]{1,2}|[uU])\b", "", s)
    s = s.replace("^", "**")
    return s


def _parse(expr: str) -> ast.AST:
    tree = ast.parse(prep_expr(expr), mode="eval")
    for node in ast.walk(tree):
        ok = isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load, ast.Call, ast.Add, ast.Sub, ast.Mult,
                               ast.Div, ast.FloorDiv, ast.Pow, ast.USub, ast.UAdd, ast.LShift, ast.RShift, ast.BitAnd, ast.BitOr, ast.Mod))
        ok = ok or (isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool))
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id.lower() in _FUNCS and not node.keywords):
            ok = False
        if not ok:
            raise ValueError(f"unsupported syntax '{ast.unparse(node) if hasattr(ast, 'unparse') else type(node).__name__}' "
                             "(use numbers, names, + - * / // ** >> << & | and pow/sqrt/exp/log/abs)")
    return tree


def _names(tree: ast.AST) -> List[str]:
    funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return [n.id.lower() for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in funcs]


def _constants(tree: ast.AST) -> List[Tuple[str, float, Optional[int]]]:
    """(text, value, power-of-two exponent or None) for the numbers a formula uses; 2**-8 counts as one constant."""
    out: List[Tuple[str, float, Optional[int]]] = []
    skip = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow) and isinstance(n.left, ast.Constant) and n.left.value == 2:
            e = n.right
            k = e.value if isinstance(e, ast.Constant) else (-e.operand.value if isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.USub) and isinstance(e.operand, ast.Constant) else None)
            if isinstance(k, int):
                out.append((f"2^{k}", 2.0 ** k, k))
                skip.update({id(n.left), id(e), id(getattr(e, "operand", None))})
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and id(n) not in skip and n.value not in (0, 1):
            v = float(n.value)
            e = math.log2(abs(v)) if v else 0.5
            out.append((repr(n.value), v, int(round(e)) if abs(e - round(e)) < 1e-12 and abs(e) >= 2 else None))
    return out


def _const_in_quote(quote: str, value: float, exp: Optional[int]) -> bool:
    """Is the constant stated in the quote? PDF text loses superscripts and fraction bars ('2−8' is 2^-8, '230' is 2^30),
    so a power of two may appear as 2^k, 2**k, 2-k, 2k (k written right after the 2) or as its decimal value."""
    q = re.sub(r"\s+", " ", quote.translate(dict.fromkeys(map(ord, "‐‑‒–—−"), "-"))).lower()
    q = re.sub(r"\s*(\^|\*\*)\s*", r"\1", q)
    nums = [float(x) for x in re.findall(r"(?<![0-9.])[0-9]+(?:\.[0-9]+)?(?:e-?[0-9]+)?", q)]
    if any(abs(x - value) <= 1e-9 * max(1.0, abs(value)) for x in nums):
        return True
    if exp is not None:
        k = str(exp)
        pats = [rf"2\^\(?{re.escape(k)}\)?", rf"2\*\*\(?{re.escape(k)}\)?", rf"1<<{k}(?![0-9])", rf"(?<![0-9])2{re.escape(k)}(?![0-9])"]
        if exp < 0:
            pats.append(rf"/ ?2\^?{-exp}(?![0-9])")
        else:
            pats.append(rf"(?<![0-9])2\^? ?- ?{k}(?![0-9])")   # x / 2^-k written as a fraction: multiplying by 2^k
        if any(re.search(p, q) for p in pats):
            return True
        if exp < 0 and any(abs(x - 2.0 ** -exp) <= 1e-9 * 2.0 ** -exp for x in nums):
            return True   # x * 2^-8 stated as x / 256
    return False


_STMT_LHS = re.compile(r"(?<![\w>.])((?:[A-Za-z_]\w*\s*(?:->|\.)\s*)*[A-Za-z_]\w*)\s*=(?!=)")
_PROSE_CUT = re.compile(r"\s(?:and|or|where|with|for|when|if|which|is|are|the|to|as|in|of|then|while|respectively)\b|[,;]\s")
_AMBIGUOUS_POW = re.compile(r"(?<![\w.^*])2(-?\d{1,2})(?![\w.])")   # '214' / '2-8': 2^14 / 2^-8 with the superscript lost


def _quote_statements(quote: str) -> List[Tuple[str, str]]:
    """The formulas a quote itself states, as (assigned name, expression text): 'PAR_P1 = (NVM_PAR_P1 - 2^14) / 2^20' from
    a converted datasheet, 'calib_data->t_lin = partial_data2 + ...' from reference code. Text that does not parse
    cleanly (a lost fraction bar, prose, garbled math fonts) states nothing here."""
    import unicodedata
    q = unicodedata.normalize("NFKC", _unescape(quote))
    q = re.sub(r"<sup>(.*?)</sup>", r"^(\1)", q)
    q = re.sub(r"<sub>(.*?)</sub>", r"_\1", q)
    q = re.sub(r"</?\w+>", " ", q)
    q = re.sub(r"/\*.*?\*/|//[^\n]*", " ", q, flags=re.S)
    # Markdown bold around words ('**PAR_T1**'), never a power ('2**14', 'x**-8'); code spans; table bars
    q = re.sub(r"(?<![\w)])\*\*(?=[A-Za-z_])([^*\n]{1,80}?)(?<=[\w)])\*\*(?![\w(])", r"\1", q)
    q = re.sub(r"[`|]", " ", q)
    q = q.translate({ord("∙"): "*", ord("⋅"): "*", ord("·"): "*", ord("×"): "*", ord("÷"): "/"})
    marks = list(_STMT_LHS.finditer(q))
    out: List[Tuple[str, str]] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(q)
        rhs = q[m.end():end].split(";", 1)[0]
        for text in (rhs, rhs[:c.start()] if (c := _PROSE_CUT.search(rhs)) else None):
            text = re.sub(r"\s+", " ", text or "").strip().rstrip(".,:")
            if not text:
                continue
            try:
                _parse(text)
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                continue
            out.append((re.sub(r"^.*(?:->|\.)\s*", "", m.group(1)).strip().lower(), text))
            break
    return out


def _variants(text: str) -> List[ast.AST]:
    """The expression, plus readings where '2k' / '2-k' is the power 2^k / 2^-k whose superscript the PDF lost, and where
    C code's '/' between integers is integer division (the steps write that as '//')."""
    spots = list(_AMBIGUOUS_POW.finditer(text))[:5]
    texts = []
    for mask in range(1 << len(spots)):
        t, shift = text, 0
        for j, sp in enumerate(spots):
            if mask >> j & 1:
                new = f"2^({sp.group(1)})"
                t = t[:sp.start() + shift] + new + t[sp.end() + shift:]
                shift += len(new) - (sp.end() - sp.start())
        texts.append(t)
    if "/" in text and "//" not in text:
        texts += [t.replace("/", "//") for t in texts]
    out = []
    for t in texts:
        try:
            out.append(_parse(t))
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            pass
    return out


def formula_structure_problem(steps: List[Any], quote: str, name: str) -> Optional[str]:
    """A recorded formula must compute what the quote states, not merely reuse its numbers. On sample inputs, each step
    whose variable the quote assigns is compared with the quote's own expression for it (earlier quote statements feed
    later ones; the step's own inputs are the same on both sides), and the result must be what the quote computes: under
    the formula's name if the quote assigns that name, else one of the quote's values. '/ 2^8' recorded for a quote that
    says '/ 2^-8' is rejected, an algebraically equal rewrite ('* 2^8') is not. A quote with no parseable statement is left
    to the constant check."""
    import random
    stmts = _quote_statements(quote)
    if not stmts:
        return None
    parsed: List[Tuple[str, str, ast.AST]] = []
    try:
        for s in steps:
            s = re.sub(r"^\s*(?:[A-Za-z_]\w*\s*(?:->|\.)\s*)+(?=[A-Za-z_]\w*\s*=)", "", str(s).strip().rstrip(";"))
            m = _STEP_RE.match(s) or (_STEP_RE.match(f"{name} = {s}") if len(steps) == 1 else None)
            if not m:
                return None
            parsed.append((m.group(1).lower(), m.group(2).strip(), _parse(m.group(2))))
        qs = [(lhs, text, _parse(text), _variants(text)) for lhs, text in stmts]
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    assigned = [v for v, _, _ in parsed]
    inputs = {n for _, _, t in parsed for n in _names(t)} - set(assigned)
    quote_names = {n for _, _, t, _ in qs for n in _names(t)} | {lhs for lhs, _, _, _ in qs}
    names = inputs | set(assigned) | quote_names
    bases = []
    for seed in (11, 23, 37):
        r = random.Random(seed)
        bases.append({n: float(r.randint(3, 4000)) for n in sorted(names)})

    def run(seq, base):   # sequential values: each statement sees the ones before it
        env, vals = dict(base), []
        for var, tree in seq:
            try:
                v = float(eval_expr(tree, env))
                env[var] = v
            except (ArithmeticError, ValueError, KeyError, TypeError, RecursionError):
                v = None
            vals.append((dict(env), v))
        return vals
    step_runs = [run([(v, t) for v, _, t in parsed], b) for b in bases]
    quote_runs = [run([(lhs, t) for lhs, _, t, _ in qs], b) for b in bases]

    def agrees(i: int, value_of, overrides_of) -> Optional[bool]:
        """Does some reading of quote statement i give the step's value on every sample? None: nothing comparable."""
        decided = False
        for variant in qs[i][3]:
            pairs = []
            for b, base in enumerate(bases):
                sv = value_of(b)
                env = dict(quote_runs[b][i - 1][0]) if i > 0 else dict(base)
                env.update(overrides_of(b))
                try:
                    qv = float(eval_expr(variant, env))
                except (ArithmeticError, ValueError, KeyError, TypeError, RecursionError):
                    continue
                if sv is not None and math.isfinite(sv) and math.isfinite(qv):
                    pairs.append((sv, qv))
            if pairs:
                decided = True
                if all(_close(a, b) for a, b in pairs):
                    return True
        return False if decided else None

    def env_before(b: int, k: int) -> Dict[str, float]:
        return step_runs[b][k - 1][0] if k > 0 else bases[b]
    seen: Dict[str, int] = {}
    compared_last = False
    for k, (var, text, tree) in enumerate(parsed):
        idxs = [i for i, (lhs, _, _, _) in enumerate(qs) if lhs == var]
        if not idxs:
            continue
        j = seen.get(var, 0)
        seen[var] = j + 1
        if j < len(idxs):
            idxs = [idxs[j]] + [i for i in idxs if i != idxs[j]]
        used = set(_names(tree)) | set(assigned[:k])   # what the step reads and what earlier steps made: same values in the quote
        res = [agrees(i, lambda b, k=k: step_runs[b][k][1],
                      lambda b, k=k, used=used: {n: v for n, v in env_before(b, k).items() if n in used}) for i in idxs]
        if any(r is True for r in res):
            compared_last = compared_last or k == len(parsed) - 1
            continue
        if any(r is False for r in res):
            lhs, qtext = qs[idxs[0]][0], qs[idxs[0]][1]
            return (f"step {k + 1} ({var} = {text}) does not compute what the quote states: {lhs} = {qtext}. Copy the "
                    "quote's expression — its operators, grouping and the signs of its exponents — not just its numbers.")
    if compared_last or not parsed:
        return None
    # the result itself, whatever the last step's variable is called
    named = [i for i, (lhs, _, _, _) in enumerate(qs) if lhs == name.lower()]
    pool = named or (list(range(len(qs))) if inputs & quote_names else [])
    if not pool:
        return None
    last = len(parsed) - 1
    res = [agrees(i, lambda b: step_runs[b][last][1], lambda b: {n: bases[b][n] for n in inputs}) for i in pool]
    if any(r is True for r in res) or not any(r is False for r in res):
        return None
    if named:
        return (f"the result ({parsed[-1][0]} = {parsed[-1][1]}) is not what the quote computes for {name}: "
                f"{qs[named[0]][0]} = {qs[named[0]][1]}. Copy the quote's expression.")
    return (f"the result ({parsed[-1][0]} = {parsed[-1][1]}) is none of the values the quote's formulas compute "
            f"({'; '.join(f'{l} = {t}' for l, t, _, _ in qs[:3])}). Copy the quote's expression, or quote the text that states this one.")


def eval_expr(tree: ast.AST, env: Dict[str, float]) -> float:
    def ev(n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            return env[n.id.lower()]
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.Call):
            return _FUNCS[n.func.id.lower()](*[ev(a) for a in n.args])
        a, b = ev(n.left), ev(n.right)
        op = n.op
        if isinstance(op, ast.Add):
            return a + b
        if isinstance(op, ast.Sub):
            return a - b
        if isinstance(op, ast.Mult):
            return a * b
        if isinstance(op, ast.Div):
            return a / b
        if isinstance(op, ast.FloorDiv):   # C integer division: truncates toward zero
            q = abs(int(a)) // abs(int(b))
            return q if (a >= 0) == (b >= 0) else -q
        if isinstance(op, ast.Mod):
            return math.fmod(a, b)
        if isinstance(op, ast.Pow):
            return a ** b
        if isinstance(op, ast.LShift):
            return int(a) << int(b)
        if isinstance(op, ast.RShift):
            return int(a) >> int(b)
        if isinstance(op, ast.BitAnd):
            return int(a) & int(b)
        if isinstance(op, ast.BitOr):
            return int(a) | int(b)
        raise ValueError("unsupported operator")
    return ev(tree)


# ------------------------------------------------------------------ the book


_BURST_WORDS = re.compile(r"burst|single|one\s+(?:read|transaction|command)|consecutive|sequential|auto-?increment|multi-?byte|shadow", re.I)
_STEP_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=(?!=)\s*(.+)$", re.S)


def _parse_part(raw: Any) -> Optional[LayoutPart]:
    if isinstance(raw, dict):
        a = _num(raw.get("reg") or raw.get("addr") or raw.get("register"))
        bits = str(raw.get("bits") or "7:0")
    else:
        m = re.fullmatch(r"\s*(0x[0-9A-Fa-f]+|\d+)\s*(?:\[\s*(\d+)\s*(?::|\.\.)\s*(\d+)\s*\])?\s*", str(raw or ""))
        if not m:
            return None
        a = _num(m.group(1))
        bits = f"{m.group(2)}:{m.group(3)}" if m.group(2) else "7:0"
    mb = re.fullmatch(r"\s*(\d+)\s*(?::|\.\.)\s*(\d+)\s*|\s*(\d+)\s*", bits)
    if a is None or not mb:
        return None
    hi, lo = (int(mb.group(1)), int(mb.group(2))) if mb.group(1) else (int(mb.group(3)), int(mb.group(3)))
    if hi < lo:
        hi, lo = lo, hi
    if hi > 7:
        return None
    return LayoutPart(a, hi, lo)


def _parse_bits(raw: Any) -> Optional[Tuple[int, int]]:
    s = str(raw if raw is not None else "").strip().lower().replace("bits", "").replace("bit", "").strip()
    m = re.fullmatch(r"(\d+)\s*(?::|\.\.|-|to)\s*(\d+)", s) or re.fullmatch(r"(\d+)", s)
    if not m:
        return None
    hi = int(m.group(1))
    lo = int(m.group(2)) if m.lastindex and m.lastindex > 1 else hi
    return (max(hi, lo), min(hi, lo)) if max(hi, lo) <= 31 else None


class RegisterBook:
    """registers.json next to ledger.json."""

    def __init__(self, root: Path):
        self.path = Path(root) / "registers.json"
        self._lock = threading.Lock()
        self.registers: Dict[str, RegisterDef] = {}
        self.layouts: Dict[str, LayoutDef] = {}
        self.bursts: Dict[str, BurstDef] = {}
        self.formulas: Dict[str, FormulaDef] = {}
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                for r in d.get("registers", []):
                    r["fields"] = [FieldDef(**f) for f in r.get("fields", [])]
                    self.registers[r["id"]] = RegisterDef(**r)
                for l in d.get("layouts", []):
                    l["parts"] = [LayoutPart(**p) for p in l.get("parts", [])]
                    self.layouts[l["id"]] = LayoutDef(**l)
                for b in d.get("bursts", []):
                    self.bursts[b["id"]] = BurstDef(**b)
                for f in d.get("formulas", []):
                    self.formulas[f["id"]] = FormulaDef(**f)
            except (ValueError, TypeError, KeyError):
                pass

    def empty(self) -> bool:
        return not (self.registers or self.layouts or self.bursts or self.formulas)

    def _save(self) -> None:
        self.path.write_text(json.dumps({
            "registers": [asdict(r) for r in self.registers.values()], "layouts": [asdict(l) for l in self.layouts.values()],
            "bursts": [asdict(b) for b in self.bursts.values()], "formulas": [asdict(f) for f in self.formulas.values()]}, indent=1),
            encoding="utf-8")

    @staticmethod
    def _next(items: Dict[str, Any], prefix: str) -> str:
        return f"{prefix}-{max([int(k.split('-')[1]) for k in items] + [0]) + 1:03d}"

    # lookups
    def register_at(self, addr: int, device: str = "") -> Optional[RegisterDef]:
        return next((r for r in self.registers.values() if r.addr == addr and (not device or r.device.lower() == device.lower())), None)

    def layout_part(self, addr: int) -> List[Tuple[LayoutDef, int]]:
        return [(l, k) for l in self.layouts.values() for k, p in enumerate(l.parts) if p.addr == addr]

    def layout(self, name: str) -> Optional[LayoutDef]:
        return next((l for l in self.layouts.values() if l.name.lower() == name.lower()), None)

    def formula(self, name: str) -> Optional[FormulaDef]:
        return next((f for f in self.formulas.values() if f.name.lower() == name.lower()), None)

    def known_addresses(self) -> set:
        return {r.addr for r in self.registers.values()} | {p.addr for l in self.layouts.values() for p in l.parts}

    # ---------------------------------------------------------- recording
    def record_registers(self, store: SourceStore, ledger: Optional[FactLedger], device: str, source_id: str,
                         registers: List[Dict[str, Any]], bursts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        device = (device or "").strip()
        if not device:
            return {"ok": False, "error": "device is required (the part, e.g. 'BMP388')"}
        recorded, rejected, notes = [], [], []
        for k, raw in enumerate((registers or [])[:60], start=1):
            raw = raw if isinstance(raw, dict) else {}
            dropped: List[str] = []
            res = self._one_register(store, ledger, device, str(raw.get("source_id") or source_id), raw, dropped)
            (recorded if isinstance(res, RegisterDef) else rejected).append(res if isinstance(res, RegisterDef) else
                                                                           {"register": raw.get("name") or f"#{k}", "error": res})
            if isinstance(res, RegisterDef):
                values = [d for d in dropped if not d.startswith("@")]
                if values:
                    notes.append(f"{res.name}: kept without the value name(s) {', '.join(values)} (not stated next to the name in the quote)")
                if "@located" in dropped:
                    notes.append(f"{res.name}: your quote was not verbatim; the fields were checked against the datasheet's own "
                                 f"text for it instead: {res.quote[:120]!r}…")
        out_b, rej_b = [], []
        for k, raw in enumerate((bursts or [])[:10], start=1):
            raw = raw if isinstance(raw, dict) else {}
            res = self._one_burst(store, device, str(raw.get("source_id") or source_id), raw)
            (out_b if isinstance(res, BurstDef) else rej_b).append(res if isinstance(res, BurstDef) else {"burst": raw.get("name") or f"#{k}", "error": res})
        with self._lock:
            for r in recorded:
                old = self.register_at(r.addr, device)
                r.id = old.id if old else self._next(self.registers, "REG")
                self.registers[r.id] = r
            for b in out_b:
                old = next((x for x in self.bursts.values() if x.device.lower() == device.lower() and x.name.lower() == b.name.lower()), None)
                b.id = old.id if old else self._next(self.bursts, "BURST")
                self.bursts[b.id] = b
            if recorded or out_b:
                self._save()
        out: Dict[str, Any] = {"ok": bool(recorded or out_b),
                               "recorded": [f"{r.id} {r.name} {_hex(r.addr)}: " + (", ".join(f.label() + (" {" + ", ".join(f"{n}={'|'.join(str(x) for x in vs)}" for n, vs in f.values.items()) + "}" if f.values else "") for f in r.fields) or "no fields")
                                            for r in recorded]}
        if out_b:
            out["bursts"] = [f"{b.id} {b.name}: {_hex(b.start)}..{_hex(b.end)} in one read" for b in out_b]
        if notes:
            out["notes"] = notes
        if rejected or rej_b:
            out["rejected"] = rejected + rej_b
            out["hint"] = ("Each register needs `quote` = its table text verbatim (name or address in it); each field's name and bit "
                           "number(s) must be in that quote or the field's own `quote`, and each named value must appear next to its "
                           "name (e.g. '11 normal mode' -> values {normal: '0b11'}). Recorded items are kept; resend only the rejected ones.")
        return out

    def _one_register(self, store: SourceStore, ledger: Optional[FactLedger], device: str, source_id: str, raw: Dict[str, Any],
                      dropped: Optional[List[str]] = None) -> Any:
        """Check a register against its quote; if the quote is not verbatim or does not state the fields, check the same
        claims against the datasheet's own text for that register (name/address). The claims are what must be true; a
        model that cannot copy a table exactly is not blocked, a wrong bit position still is."""
        res = self._check_register(store, ledger, device, source_id, raw, dropped)
        if isinstance(res, RegisterDef) or not isinstance(res, str):
            return res
        src = store.get(source_id)
        name = str(raw.get("name") or "").strip()
        addr = _num(raw.get("addr") if raw.get("addr") is not None else raw.get("address"))
        if src is None or not name or addr is None or re.search(r"required|overlap|does not fit|beyond|give name|must be a number", res):
            return res
        fields = []
        for f in raw.get("fields") or []:
            if isinstance(f, dict):
                fq = str(f.get("quote") or "")
                keep = fq and _quote_ok(store, str(f.get("source_id") or source_id), fq, "field") is None
                fields.append(dict(f, quote=fq if keep else ""))
        for passage in table_passages(src.text, [name, _hex(addr)], limit=4, size=900):
            d2: List[str] = []
            r2 = self._check_register(store, ledger, device, source_id, dict(raw, quote=passage, fields=fields), d2)
            if isinstance(r2, RegisterDef):
                if dropped is not None:
                    dropped.extend(d2 + ["@located"])
                return r2
        return res

    def _check_register(self, store: SourceStore, ledger: Optional[FactLedger], device: str, source_id: str, raw: Dict[str, Any],
                        dropped: Optional[List[str]] = None) -> Any:
        name = str(raw.get("name") or "").strip()
        addr = _num(raw.get("addr") if raw.get("addr") is not None else raw.get("address"))
        quote = re.sub(r"\s*(…|\.\.\.)\s*$", "", str(raw.get("quote") or ""))
        if not name or addr is None:
            return "name and addr (e.g. 'PWR_CTRL', '0x1B') are required"
        err = _quote_ok(store, source_id, quote, name)
        if err:
            return err
        if not (_name_spans(quote, name) or value_in_text(_hex(addr), quote)):
            return f"the quote mentions neither {name} nor {_hex(addr)}"
        # the address the datasheet gives this name ('Register 0x1B “PWR_CTRL”', '| 0x1B | PWR_CTRL |'): in the quote, else
        # anywhere in the source; a register quoted by name only cannot sit at another address
        src = store.get(source_id)
        stated = _stated_addrs(quote, name) or (_stated_addrs(src.text, name) if src is not None else set())
        if stated and addr not in stated:
            return f"the datasheet gives {name} at {', '.join(_hex(a) for a in sorted(stated))}, not {_hex(addr)}"
        fields: List[FieldDef] = []
        for fr in (raw.get("fields") or [])[:16]:
            fr = fr if isinstance(fr, dict) else {}
            fname = str(fr.get("name") or "").strip()
            bits = _parse_bits(fr.get("bits"))
            if not fname or bits is None:
                return f"field {fname or '?'}: give name and bits ('0', '5:4')"
            hi, lo = bits
            if hi > 7 and raw.get("width") not in (16, 32):
                return f"field {fname}: bit {hi} is beyond an 8-bit register"
            fq = re.sub(r"\s*(…|\.\.\.)\s*$", "", str(fr.get("quote") or ""))
            quotes = [own_section(quote, addr)]
            if fq:
                err = _quote_ok(store, str(fr.get("source_id") or source_id), fq, f"{name}.{fname}")
                if err:
                    return err
                quotes.append(fq)
            whole = (hi, lo) == (7, 0) and raw.get("width") in (None, 8, "8")   # the whole register: no position to check
            if whole:
                stated = _stated_bits(quotes, fname)
                if stated and stated != (7, 0):
                    return (f"field {fname}: the datasheet gives it bit {stated[1]}" if stated[0] == stated[1] else
                            f"field {fname}: the datasheet gives it bits {stated[0]}..{stated[1]}, not the whole register")
            if not whole and not any(_name_spans(q, fname) for q in quotes):
                return f"field {fname}: its name is not in the quote(s)"
            if not whole and not any(_bits_in_quote(q, hi, lo, fname) for q in quotes):
                return (f"field {fname}: bit {lo} is not stated in the quote(s)" if hi == lo else
                        f"field {fname}: bits {hi}..{lo} are not stated in the quote(s)")
            values: Dict[str, List[int]] = {}
            width = hi - lo + 1

            def fval(x: Any) -> Optional[int]:   # '11' for a 2-bit field is binary, as datasheet tables write it
                s = str(x).strip()
                if re.fullmatch(r"[01]+", s) and len(s) == width and width > 1:
                    return int(s, 2)
                return _num(x)
            for vname, vraw in (fr.get("values") or {}).items() if isinstance(fr.get("values"), dict) else []:
                vs = [fval(x) for x in (vraw if isinstance(vraw, list) else str(vraw).replace("|", ",").split(","))] if not is_int(vraw) else [vraw]
                if not vs or any(v is None for v in vs):
                    if dropped is not None:   # optional detail: keep the field without it
                        dropped.append(f"{fname}.{vname}")
                        continue
                    return f"field {fname}: value {vname!r} must be a number or list of numbers ('0b11', '3', ['0b01','0b10'])"
                if any(v >= (1 << (hi - lo + 1)) for v in vs):
                    return f"field {fname}: value {vname}={vs} does not fit in {hi - lo + 1} bit(s)"
                if not re.search(r"[A-Za-z]", str(vname)) or not any(all(_value_near(q, vname, v, hi - lo + 1) for v in vs) for q in quotes):
                    if dropped is not None:   # a value name is optional: keep the field, leave out what the quote does not show
                        dropped.append(f"{fname}.{vname}")
                        continue
                    return (f"field {fname}: value {vname}={'/'.join(format(v, f'0{hi - lo + 1}b') for v in vs)} is not stated next to "
                            f"'{vname}' in the quote(s)")
                values[vname] = vs
            fields.append(FieldDef(fname, hi, lo, values))
        masks = [f.mask for f in fields]
        if any(masks[i] & masks[j] for i in range(len(masks)) for j in range(i + 1, len(masks))):
            return "fields overlap: each bit belongs to one field"
        id_value = None
        if raw.get("id_value") not in (None, ""):
            iv = str(raw.get("id_value")).strip()
            fact = ledger.get(iv) if ledger is not None and iv.upper().startswith("FACT-") else None
            if fact is not None:
                id_value = _num(fact.value)
            else:
                id_value = _num(iv)
                backed = ledger is not None and id_value is not None and any(
                    f.subject.lower() == device.lower() and _num(str(f.value)) == id_value for f in ledger.facts.values())
                if id_value is None or not (value_in_text(_hex(id_value), quote) or backed):
                    return f"id_value {iv} must be a FACT id, a value already recorded as a fact for {device}, or appear in the register quote"
        return RegisterDef("", device, name, addr, fields, source_id, quote.strip(), id_value)

    def _one_burst(self, store: SourceStore, device: str, source_id: str, raw: Dict[str, Any]) -> Any:
        name = str(raw.get("name") or "data").strip()
        start, end = _num(raw.get("start")), _num(raw.get("end"))
        quote = str(raw.get("quote") or "")
        if start is None or end is None or end < start or end - start > 64:
            return "give start and end register addresses (e.g. '0x04', '0x09')"
        err = _quote_ok(store, source_id, quote, f"burst {name}")
        if err:
            return err
        if not _BURST_WORDS.search(quote):
            return "the quote must be the datasheet text that requires reading these registers in one burst/transaction"
        known = self.known_addresses()
        if start not in known or end not in known:
            return (f"{_hex(start)} and {_hex(end)} must be registers or layout bytes you recorded first "
                    "(record_registers / record_layouts) so the range is grounded in the datasheet")
        return BurstDef("", device, name, start, end, source_id, quote.strip())

    def record_layouts(self, store: SourceStore, device: str, source_id: str, layouts: List[Dict[str, Any]]) -> Dict[str, Any]:
        device = (device or "").strip()
        if not device:
            return {"ok": False, "error": "device is required (the part, e.g. 'BMP388')"}
        recorded, rejected = [], []
        for k, raw in enumerate((layouts or [])[:60], start=1):
            raw = raw if isinstance(raw, dict) else {}
            res = self._one_layout(store, device, str(raw.get("source_id") or source_id), raw)
            if isinstance(res, LayoutDef):
                recorded.append(res)
            else:
                rejected.append({"layout": raw.get("name") or f"#{k}", "error": res})
        with self._lock:
            for l in recorded:
                old = next((x for x in self.layouts.values() if x.device.lower() == device.lower() and x.name.lower() == l.name.lower()), None)
                l.id = old.id if old else self._next(self.layouts, "LAY")
                self.layouts[l.id] = l
            if recorded:
                self._save()
        out: Dict[str, Any] = {"ok": bool(recorded), "recorded": [f"{l.id} {l.describe()}" for l in recorded]}
        if rejected:
            out["rejected"] = rejected
            out["hint"] = ("`bytes` lists the registers LEAST significant first ('0x31' = bits 7..0, '0x32' = bits 15..8); a partial "
                           "byte is '0xE5[3:0]'. The quote must contain every address and, if it says, the signedness.")
        return out

    @staticmethod
    def _after_addr(low: str, addr: int) -> str:
        out = []
        for m in re.finditer(rf"(?<![0-9a-z])0x0*{addr:x}(?![0-9a-z])", low):
            tail = low[m.end():m.end() + 90]
            nxt = re.search(r"(?<![0-9a-z])(?:\d+\s+)?0x[0-9a-f]+", tail)
            out.append(tail[:nxt.start()] if nxt else tail)
        return " ".join(out)

    _RANGE_AFTER = re.compile(r"<\s*(\d+)\s*:\s*(\d+)\s*>|\[\s*(\d+)\s*:\s*(\d+)\s*\]|_(\d+)_(\d+)\b")

    def _one_layout(self, store: SourceStore, device: str, source_id: str, raw: Dict[str, Any]) -> Any:
        res = self._check_layout(store, device, source_id, raw)
        src = store.get(source_id)
        if isinstance(res, LayoutDef) or src is None or not re.search(r"not verbatim|not in the quote|too short|longer than", str(res)):
            return res
        parts = [_parse_part(p) for p in self._layout_bytes(raw)]
        if not parts or any(p is None for p in parts):
            return res
        for passage in layout_passages(src.text, [p.addr for p in parts]):
            r2 = self._check_layout(store, device, source_id, dict(raw, quote=passage))
            if isinstance(r2, LayoutDef):
                return r2
            if not re.search(r"not verbatim|not in the quote|too short|longer than", str(r2)):
                return r2   # the datasheet's own rows contradict the claim: report that, not the quote problem
        return res

    @staticmethod
    def _layout_bytes(raw: Dict[str, Any]) -> List[Any]:
        parts = raw.get("bytes") or raw.get("parts") or raw.get("registers") or raw.get("addresses") or raw.get("regs") or []
        parts = list(parts) if isinstance(parts, (list, tuple)) else [x for x in re.split(r"[,\s]+", str(parts)) if x]
        order = str(raw.get("bytes_order") or raw.get("byte_order") or raw.get("order") or raw.get("endianness") or "").lower()
        if re.search(r"msb|big|high.*first", order):
            parts.reverse()   # stated most significant first
        return parts

    def _check_layout(self, store: SourceStore, device: str, source_id: str, raw: Dict[str, Any]) -> Any:
        name = str(raw.get("name") or "").strip()
        quote = re.sub(r"\s*(…|\.\.\.)\s*$", "", str(raw.get("quote") or ""))
        parts_raw = self._layout_bytes(raw)
        if not name or not parts_raw:
            return "give name and bytes (register addresses, least significant first)"
        parts = [_parse_part(p) for p in parts_raw]
        if any(p is None for p in parts):
            return "each byte is a register address ('0x31') or a partial byte ('0xE5[3:0]')"
        if len(parts) > 8:
            return "at most 8 bytes"
        err = _quote_ok(store, source_id, quote, name)
        if err:
            return err
        low = quote.lower()
        for p in parts:
            hit = value_in_text(_hex(p.addr), quote)
            if hit is None or hit[1] != "STRICT":
                return f"register {_hex(p.addr)} is not in the quote (quote the rows that list each address)"
        signed = raw.get("signed")
        signed = str(signed).strip().lower() in ("true", "1", "yes", "signed") if signed is not None else False
        lay = LayoutDef("", device, name, parts, signed, source_id, quote.strip())
        # what the rows of these addresses say (text after each address up to the next one): size and sign
        rows = " ".join(self._after_addr(low, p.addr) for p in parts)
        local = rows if re.search(r"signed|\bbits?\b", rows) else low
        says_unsigned = "unsigned" in local
        says_signed = bool(re.search(r"(?<!un)signed", local))
        if says_unsigned and not says_signed and signed:
            return "the datasheet rows say unsigned but signed is true"
        if says_signed and not says_unsigned and not signed:
            return "the datasheet rows say signed but signed is false"
        if signed and not says_signed and not re.search(r"two'?s[\s-]*complement|2'?s[\s-]*complement", low):
            return ("nothing in the quote says the value is signed (\"signed\" / \"two's complement\"): quote the text that says so, "
                    "or record it unsigned")
        sizes = {int(x) for x in re.findall(r"(?<![\w.])(8|16|20|24|32)\s*-?\s*bits?\b", rows)}
        if sizes and lay.width not in sizes and len(sizes) == 1:
            return (f"the datasheet rows give {name} {sizes.pop()} bits, but the bytes listed make {lay.width} bits — list every "
                    "byte of the value")
        # where the datasheet writes content as NAME<hi:lo> next to each address, the bytes must be exactly those rows
        src = store.get(source_id)
        table = row_ranges(src.text) if src is not None else ()
        mine = {p.addr for p in parts}
        # the value's name must agree with what the datasheet's rows at these addresses hold (P9 is not P7)
        tag = re.search(r"([a-z])(\d+)$", _alnum(name))
        for a, nm, h, l in table:
            if a in mine:
                other = re.search(r"([a-z])(\d+)$", nm)
                if tag and other and tag.groups() != other.groups():
                    return (f"{_hex(a)} holds {nm.upper()} per the datasheet ({_hex(a)}<{h}:{l}>), not {name}: "
                            "take each value's addresses from its own rows")
                if _alnum(name) != nm:
                    # raw register content keeps the datasheet's name (NVM_PAR_T1); a converted value (PAR_T1) is a formula
                    shown = next((m.group(2).strip() for m in _ROW_RANGE.finditer(src.text) if int(m.group(1), 16) == a), nm.upper())
                    return (f"name this layout {shown.replace(' ', '')!r} as the datasheet does ({_hex(a)} {shown}<{h}:{l}>): it "
                            f"is the raw register content. A scaled or converted value is a record_formula on top of it.")
        for n in {nm for a, nm, h, l in table if a in mine}:
            nrows = {(a, h, l) for a, nm, h, l in table if nm == n and h - l == 7}
            if not nrows or not any(a in mine for a, _h, _l in nrows) or max(h for _a, h, _l in nrows) <= 7:
                continue   # one byte per name (ACC_X_LSB<7:0>): the name does not describe the whole value
            want = {a for a, _h, _l in nrows}
            if want != mine and all((p.hi, p.lo) == (7, 0) for p in parts):
                return (f"the datasheet lists {n.upper()} in {', '.join(f'{_hex(a)}<{h}:{l}>' for a, h, l in sorted(nrows, key=lambda r: r[2]))}; "
                        f"bytes must be exactly those, least significant first")
            for a, h, l in nrows:
                k = next(i for i, p in enumerate(parts) if p.addr == a)
                if lay.offset_of(k) != l:
                    return (f"the datasheet puts {_hex(a)} at bits {h}:{l} of {n.upper()}, but `bytes` puts it at bits "
                            f"{lay.offset_of(k) + 7}:{lay.offset_of(k)} — list bytes least significant first")
        # where the quote states the bit range of an address (e.g. '0x32 NVM_PAR_T1<15:8>'), the order must agree
        for k, p in enumerate(parts):
            for m in re.finditer(rf"(?<![0-9a-z])0x0*{p.addr:x}(?![0-9a-z])", low):
                tail = low[m.end():m.end() + 28]
                nxt = re.search(r"0x[0-9a-f]+", tail)
                tail = tail[:nxt.start()] if nxt else tail
                r = self._RANGE_AFTER.search(tail)
                if not r:
                    continue
                g = [int(x) for x in r.groups() if x is not None]
                hi, lo = max(g), min(g)
                if hi - lo + 1 == p.width and lo != lay.offset_of(k) and lay.width > 8:
                    return (f"the quote puts {_hex(p.addr)} at bits {hi}:{lo} of the value, but `bytes` puts it at bits "
                            f"{lay.offset_of(k) + p.width - 1}:{lay.offset_of(k)} — list bytes least significant first")
        return lay

    def record_formula(self, store: SourceStore, device: str, source_id: str, name: str, steps: Any, quote: str,
                       unit: str = "", inputs: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        device, name = (device or "").strip(), (name or "").strip()
        if not device or not re.fullmatch(r"[A-Za-z_]\w{0,40}", name):
            return {"ok": False, "error": "device and name (an identifier such as 'PAR_T1' or 'T_LIN') are required"}
        if isinstance(steps, str):
            steps = [s for s in re.split(r";\s*|\n", steps) if s.strip()]
        steps = [s for s in steps if not re.match(r"\s*return\b", str(s))] if isinstance(steps, list) else steps
        orig_steps = list(steps) if isinstance(steps, list) else []
        problem = self._binding_problem(store, device, inputs if isinstance(inputs, dict) else {})
        if problem:
            return {"ok": False, "error": problem}
        if isinstance(inputs, dict) and isinstance(steps, list):
            # the quote's own names (uncomp_temp, calib_data->par_t1) bound to recorded layouts/formulas (UT, PAR_T1)
            def bind(s: str) -> str:
                s = prep_expr(s) if "=" not in s else s.split("=", 1)[0] + "=" + prep_expr(s.split("=", 1)[1])
                for src_name, rec_name in inputs.items():
                    short = re.sub(r"^.*(?:->|\.)", "", str(src_name)).strip()
                    if re.fullmatch(r"[A-Za-z_]\w*", short) and re.fullmatch(r"[A-Za-z_]\w*", str(rec_name).strip()):
                        lhs, _, rhs = s.partition("=")
                        s = lhs + "=" + re.sub(rf"\b{re.escape(short)}\b", str(rec_name).strip(), rhs, flags=re.I) if rhs else s
                return s
            steps = [bind(str(s)) for s in steps]
        if not isinstance(steps, list) or not steps:
            return {"ok": False, "error": "steps: list of 'var = expression' strings, in order; the last var is the result"}
        quote = re.sub(r"\s*(…|\.\.\.)\s*$", "", str(quote or ""))
        err = _quote_ok(store, source_id, quote, f"formula {name}")
        if err:
            return {"ok": False, "error": err}
        known = {l.name.lower() for l in self.layouts.values() if l.device.lower() == device.lower()}
        known |= {f.name.lower() for f in self.formulas.values() if f.device.lower() == device.lower() and f.name.lower() != name.lower()}
        parsed: List[List[str]] = []
        defined: set = set()
        missing_consts: List[str] = []
        for k, s in enumerate(steps[:40], start=1):
            s = str(s).strip().rstrip(";")
            s = re.sub(r"^\s*(?:[A-Za-z_]\w*\s*(?:->|\.)\s*)+(?=[A-Za-z_]\w*\s*=)", "", s)   # calib_data->t_lin = ... -> t_lin = ...
            m = _STEP_RE.match(s)
            if not m:
                if len(steps) == 1:
                    m = _STEP_RE.match(f"{name} = {s}")
                if not m:
                    return {"ok": False, "error": f"step {k}: write it as 'var = expression'"}
            var, expr = m.group(1), m.group(2)
            try:
                tree = _parse(expr)
            except (SyntaxError, ValueError) as e:
                return {"ok": False, "error": f"step {k} ({var}): {e}"}
            except (RecursionError, MemoryError):
                return {"ok": False, "error": f"step {k} ({var}): the expression is nested too deeply; split it into steps"}
            unknown = [n for n in _names(tree) if n not in defined and n not in known]
            if unknown:
                return {"ok": False, "error": f"step {k}: unknown name(s) {', '.join(sorted(set(unknown)))}. Every input must be a layout "
                                              f"(record_layouts) or a formula recorded before (known: {', '.join(sorted(known)) or 'none'}). "
                                              "Keep the quote's names and bind them with inputs={'" + sorted(set(unknown))[0] +
                                              "': '<recorded name>'}; a coefficient the code scales (par_t1 = NVM_PAR_T1 / 2^-8) "
                                              "is its own record_formula first."}
            for text, value, exp in _constants(tree):
                if not _const_in_quote(quote, value, exp):
                    missing_consts.append(text)
            defined.add(var.lower())
            parsed.append([var, prep_expr(expr)])
        if parsed[-1][0].lower() != name.lower():
            parsed.append([name, parsed[-1][0]])
        foreign = self._names_not_in_quote(orig_steps, quote, inputs if isinstance(inputs, dict) else {})
        if foreign:
            return {"ok": False, "error": f"{', '.join(foreign)} do(es) not appear in the quote. Write the steps with the quote's own "
                                          "names (e.g. calib_data->par_t1, uncomp_temp) and bind them to recorded layouts/formulas "
                                          "with inputs={...}: a value the quote uses must be the value it means, not a similar raw one."}
        if missing_consts:
            return {"ok": False, "error": f"constant(s) {', '.join(sorted(set(missing_consts)))} are not in the quote. Quote the "
                                          "text that states the formula (the datasheet, or the manufacturer's reference code)."}
        problem = formula_structure_problem(orig_steps, quote, name)
        if problem:
            return {"ok": False, "error": problem}
        with self._lock:
            old = self.formula(name) if (self.formula(name) and self.formula(name).device.lower() == device.lower()) else None
            fid = old.id if old else self._next(self.formulas, "FRM")
            f = FormulaDef(fid, device, name, parsed, source_id, quote.strip(), unit)
            self.formulas[fid] = f
            self._save()
        return {"ok": True, "formula": f"{fid} {name}: {f.text()}" + (f" [{unit}]" if unit else "")}


    _RAW_MARK = re.compile(r"(?:^|[^a-z])(?:reg|raw|nvm|uncomp|adc)", re.I)

    def _binding_problem(self, store: SourceStore, device: str, inputs: Dict[str, Any]) -> str:
        """A quote's name may stand for a raw register value only if the quote marks it as raw: the datasheet's own row name
        (NVM_PAR_T1) or a raw-marked path (reg_calib_data->par_t1). `par_t1` alone is the converted coefficient."""
        for a, b in inputs.items():
            a_full, b_name = str(a).strip(), str(b).strip()
            short = re.sub(r"^.*(?:->|\.)", "", a_full).strip()
            lay = next((l for l in self.layouts.values() if l.name.lower() == b_name.lower() and l.device.lower() == device.lower()), None)
            if lay is None or _alnum(short) == _alnum(b_name):
                continue
            src = store.get(lay.source_id)
            rownames = {nm for _a, nm, _h, _l in row_ranges(src.text)} if src is not None else set()
            if _alnum(lay.name) in rownames and not self._RAW_MARK.search(a_full[: len(a_full) - len(short)] + " " + short):
                return (f"inputs: {a_full!r} is bound to the raw register content {lay.name}, but nothing in that name marks it "
                        f"as raw — in datasheet code {short} is normally the converted coefficient (e.g. {short.upper()} = "
                        f"{lay.name} / 2^n). Record that conversion with record_formula first (find_reference_code if the PDF "
                        "formula is garbled), or bind the quote's full raw name (e.g. 'reg_calib_data->" + short + "').")
        return ""

    @staticmethod
    def _names_not_in_quote(steps: List[Any], quote: str, inputs: Dict[str, Any]) -> List[str]:
        import unicodedata
        q = unicodedata.normalize("NFKC", _unescape(quote))
        garbled = any(0x1D400 <= ord(c) <= 0x1D7FF for c in _unescape(quote))
        qn = re.sub(r"\b[A-Za-z_]\w*\s*(?:->|\.)\s*(?=[A-Za-z_])", "", q)
        tags = {(m.group(1), m.group(2)) for m in re.finditer(r"([a-z])\1*(\d+)", q.lower())}
        defined: set = set()
        bound = {re.sub(r"^.*(?:->|\.)", "", str(k)).strip().lower() for k in inputs}
        out: List[str] = []
        for s in steps:
            lhs, eq, rhs = str(s).partition("=")
            if not eq:
                rhs, lhs = lhs, ""
            for n in re.findall(r"[A-Za-z_]\w*", re.sub(r"\b\w+\s*\(", " ", prep_expr(rhs))):
                if n.lower() in defined or n.lower() in _FUNCS or n in out:
                    continue
                if _name_spans(qn, n) or (n.lower() in bound and _name_spans(qn, n)):
                    continue
                tag = re.search(r"([a-z])(\d+)$", _alnum(n))
                if garbled and tag and tag.groups() in tags:
                    continue   # PDF math glyphs: only the index (T1, P11) of a name survives
                out.append(n)
            if lhs.strip():
                defined.add(re.sub(r"^.*(?:->|\.)", "", lhs).strip().lower())
        return out


# ------------------------------------------------------------------ checking


@dataclass
class RegFinding:
    severity: str
    code: str
    file: str
    line: int
    message: str
    suggestion: str = ""


_ISR_RE = re.compile(r"(_IRQHandler|_ISR|_isr|_Callback|_callback|_cb|_handler)$")


def callbacks(model: FlowModel) -> List[str]:
    """Functions the program runs without calling them by name: tasks and threads (xTaskCreate(sensor_task, ...)),
    interrupt and timer callbacks (attachInterrupt(pin, isr, ...), HAL_*_Callback, *_IRQHandler)."""
    names = {n for n in model.functions if "::" not in n}
    called, referenced = set(), []
    for fns in model.functions.values():
        for fn in fns:
            b = fn.body
            for k, t in enumerate(b):
                if t.kind != "id" or t.text not in names or t.text == fn.name.split("::")[-1]:
                    continue
                nxt = b[k + 1].text if k + 1 < len(b) else ""
                prev = b[k - 1].text if k else ""
                if nxt == "(":
                    called.add(t.text)
                elif prev not in (".", "->") and t.text not in referenced:
                    referenced.append(t.text)   # used as a value: handed to an RTOS, a timer or an interrupt API
    out = [n for n in referenced if n not in called and n not in ("setup", "loop", "main", "app_main")]
    out += sorted(n for n in names if _ISR_RE.search(n) and n not in called and n not in out)
    return out[:12]


def entry_points(model: FlowModel) -> List[List[str]]:
    """Programs to walk: setup()+loop() (Arduino) or main/app_main, then the tasks and callbacks they hand to the RTOS or
    to interrupts; otherwise every function nothing else calls."""
    names = set(model.functions)
    extra = callbacks(model)
    if "setup" in names:
        return [["setup"] + (["loop"] if "loop" in names else []) + extra]
    for n in ("main", "app_main"):
        if n in names:
            return [[n] + extra]
    called = set()
    for fns in model.functions.values():
        for fn in fns:
            called |= {t.text for k, t in enumerate(fn.body[:-1]) if t.kind == "id" and fn.body[k + 1].text == "("}
    roots = sorted(n for n in names if "::" not in n and n not in called)
    return [[n] for n in roots[:20]]


_WRAPPER_RE = re.compile(r"^[a-z0-9]*?(?:i2c|spi|bus)?(?:read|write|rd|wr|get|set|xfer|transfer)"
                         r"(?:reg|regs|register|registers|byte|bytes|burst|block|data|buf|u8|u16|8|16)?$")


def _is_wrapper(name: str) -> bool:
    """A generic register-access helper (readRegister, write_reg, bmp3_get_regs, rd): its caller decides the register."""
    return bool(_WRAPPER_RE.match(name.split("::")[-1].lower().replace("_", "")))


def where(model: FlowModel, e: Any) -> Tuple[str, int]:
    """The line that decides a bus operation: the call in the nearest caller that is not a generic register helper
    (begin() line 164 for a write done through writeRegister()), or the primitive itself when it is called directly."""
    chain, sites = list(e.chain), list(e.sites)
    if not chain or not _is_wrapper(chain[-1]):
        return e.file, e.line
    for k in range(min(len(sites), len(chain) - 1) - 1, -1, -1):
        if not _is_wrapper(chain[k]):
            fn = model.function(chain[k])
            return (fn.file if fn else e.file), sites[k]
    return e.file, e.line


def _device_addrs(ledger: Optional[FactLedger], device: str) -> set:
    from embedforge.truth.verifier import _ADDR_PROP_RE
    out = set()
    for f in (ledger.facts.values() if ledger is not None else []):
        n = parse_int(str(f.value))
        if n is not None and 0 < n < 0x400 and f.subject.lower() == device.lower() and _ADDR_PROP_RE.search(f.property):
            out.add(n)
    return out


def _spi_dummy_fact(ledger: Optional[FactLedger], devices: List[str]) -> Optional[int]:
    """Dummy bytes an SPI read returns before the data, if the datasheet's statement of it is on record."""
    names = {d.lower() for d in devices}
    for f in (ledger.facts.values() if ledger is not None else []):
        if f.subject.lower() in names and re.search(r"dummy", f.property, re.I):
            n = parse_int(str(f.value))
            if n is not None and 0 <= n <= 4:
                return n
    return None


def _probe_vector(book: RegisterBook, seed: int, expects: Optional[Dict[int, int]] = None) -> Dict[int, int]:
    """Register bytes for a probe run: random layout bytes, ID registers at their ID (recorded, or the constant the code
    compares them with), status flags 'ready', error flags clear."""
    rnd = random.Random(seed)
    vec: Dict[int, int] = {}
    for r in book.registers.values():
        if r.id_value is None and re.search(r"id|who_?am_?i", r.name, re.I) and (expects or {}).get(r.addr) is not None:
            vec[r.addr] = expects[r.addr] & 0xFF
    for l in book.layouts.values():
        for p in l.parts:
            vec.setdefault(p.addr, rnd.randrange(256))
    for l in book.layouts.values():   # input 1: every value's top bit set (a signedness error shows); input 2: clear
        top = l.parts[-1]
        bit = 1 << top.hi
        vec[top.addr] = (vec[top.addr] | bit) if seed == 1 else (vec[top.addr] & ~bit)
    for r in book.registers.values():
        if r.addr in vec:
            continue
        if r.id_value is not None:
            vec[r.addr] = r.id_value & 0xFF
            continue
        v = 0
        for f in r.fields:
            if re.search(r"rdy|ready|drdy|done|complete|valid|avail|new_?data|data_?ready", f.name, re.I) and not re.search(r"err|fault|fail|ovf|overflow|busy|measuring", f.name, re.I):
                v |= f.mask
        if v:
            vec[r.addr] = v
    return vec


def _close(a: float, b: float) -> bool:
    if not (math.isfinite(a) and math.isfinite(b)):
        return False
    return abs(a - b) <= 1e-5 * max(abs(a), abs(b)) + 1e-300


def evaluate_formulas(book: RegisterBook, vec: Dict[int, int]) -> Dict[str, float]:
    """Each formula's value on the probe input, keyed by formula id. Names resolve within the formula's own device: two
    parts on one board may both have a 'T' or a 'UT'."""
    out: Dict[str, float] = {}
    for dev in {f.device.lower() for f in book.formulas.values()}:
        env: Dict[str, float] = {l.name.lower(): float(l.decode(lambda a: vec.get(a, 0)))
                                 for l in book.layouts.values() if l.device.lower() == dev}
        pending = [f for f in book.formulas.values() if f.device.lower() == dev]
        for _ in range(len(pending) + 1):
            rest = []
            for f in pending:
                local = dict(env)
                try:
                    for var, expr in f.steps:
                        local[var.lower()] = float(eval_expr(_parse(expr), local))
                except KeyError:
                    rest.append(f)
                    continue
                except (ValueError, ZeroDivisionError, OverflowError, TypeError, SyntaxError):
                    continue
                env[f.name.lower()] = local[f.steps[-1][0].lower()]
                out[f.id] = env[f.name.lower()]
            if not rest or len(rest) == len(pending):
                break
            pending = rest
    return out


def check_registers(project_dir: Path, ledger: Optional[FactLedger], model: Optional[FlowModel] = None) -> Tuple[Dict[str, Any], List[RegFinding]]:
    """Returns (summary for the verify report, findings)."""
    book = RegisterBook(project_dir)
    if book.empty():
        return {}, []
    model = model or FlowModel.from_dir(Path(project_dir))
    findings: List[RegFinding] = []
    devices = sorted({x.device for x in list(book.registers.values()) + list(book.layouts.values())}, key=str.lower)
    dev_addrs = {d: _device_addrs(ledger, d) for d in devices}
    all_addrs = set().union(*dev_addrs.values()) if dev_addrs else set()

    def mine(addr: Any) -> bool:   # an event/byte of a device this book describes
        return not all_addrs or not is_int(addr) or addr in all_addrs

    dev_of: Dict[int, set] = {}
    for d, addrs in dev_addrs.items():
        for a in addrs:
            dev_of.setdefault(a, set()).add(d.lower())

    def at(addr: Any) -> Optional[set]:
        """The devices traffic at this bus address talks to; None when it cannot tell (SPI, callbacks, unknown address)."""
        return dev_of.get(addr) if is_int(addr) else None

    def reg_at(r: int, addr: Any) -> Optional[RegisterDef]:   # the register map of the device at that address, not another part's
        devs = at(addr)
        return next((x for x in book.registers.values() if x.addr == r and (not devs or x.device.lower() in devs)), None)

    def parts_at(r: int, addr: Any) -> List[Tuple[LayoutDef, int]]:
        devs = at(addr)
        return [(l, k) for l, k in book.layout_part(r) if not devs or l.device.lower() in devs]

    stated_dummy = _spi_dummy_fact(ledger, devices)   # the datasheet's SPI dummy bytes, when recorded
    walkers: List[Tuple[List[str], Walker]] = []
    for entries in entry_points(model):
        try:
            walkers.append((entries, model.run(entries, spi_dummy=stated_dummy or 0)))
        except RecursionError:   # code that was not looked at must not pass as checked
            findings.append(RegFinding("ERROR", "REGISTER_CHECK_ERROR", "", 0,
                                       f"Tracing {' + '.join(entries)} exceeded the recursion limit, so its register use is unchecked.",
                                       "Report it to the user; flattening deeply nested code may help."))
    # SPI parts differ in whether a read returns a dummy byte before the data (Bosch BMP388/BMI160 do): judge the code
    # under both readings, and report a byte-level error only if it is wrong under both
    spi = any(e.bus == "spi" and e.kind == "read" for _, w in walkers for e in w.events)
    guess = spi and stated_dummy is None
    dummies = (0, 1) if guess else (stated_dummy or 0,)
    alt: List[Tuple[List[str], Walker]] = []
    if guess:
        for entries, _ in walkers:
            try:
                alt.append((entries, model.run(entries, spi_dummy=1)))
            except RecursionError:
                pass
    summary: Dict[str, Any] = {"entries": [" + ".join(e) for e, _ in walkers], "writes": [], "layouts": [], "formulas": []}
    used = {e.addr for _, w in walkers for e in w.events if is_int(e.addr)}
    if all_addrs and not used & all_addrs:
        all_addrs = set()   # the code talks to another address: judge all its device traffic (a wrong address must not hide errors)

    # 1. register writes, decoded
    seen_w, fieldless = set(), set()
    for _, w in walkers:
        for e in w.events:
            if e.kind != "write" or not mine(e.addr):
                continue
            for r, v in writes_of(e, lambda r, a=e.addr: reg_at(r, a) is not None or bool(parts_at(r, a))):
                if is_int(r) and isinstance(v, Part):   # read-modify-write: reserved bits it sets for certain
                    reg = reg_at(r, e.addr)
                    extra = v.value & v.mask & 0xFF & ~(reg.defined_mask if reg is not None else 0xFF)
                    if reg is not None and reg.fields and extra and (e.file, e.line, r) not in seen_w:
                        seen_w.add((e.file, e.line, r))
                        wf, wl = where(model, e)
                        findings.append(RegFinding("WARNING", "REG_RESERVED_BITS", wf, wl,
                                                   f"{reg.name} ({_hex(r)}) is written with bit(s) {_hex(extra)} set, which no field of "
                                                   f"{reg.id} defines ({', '.join(f.label() for f in reg.fields)}).",
                                                   "Write only defined fields (reserved bits are usually kept or written 0)."))
                    continue
                if not (is_int(r) and is_int(v)):
                    continue
                reg = reg_at(r, e.addr)
                key = (e.file, e.line, r, v)
                if reg is None or key in seen_w:
                    continue
                seen_w.add(key)
                wf, wl = where(model, e)
                summary["writes"].append(f"{wf}:{wl} {reg.name} ({_hex(r)}) <- {_hex(v)}: {reg.decode(v)}")
                if not reg.fields and r == e.reg and reg.id not in fieldless:
                    fieldless.add(reg.id)   # a record without fields would make every value it is written pass unchecked
                    findings.append(RegFinding("ERROR", "REG_FIELDS_MISSING", wf, wl,
                                               f"{reg.name} ({_hex(r)}) <- {_hex(v)}, but {reg.id} records no fields, so what this "
                                               "value sets is unchecked.",
                                               "record_registers again for this register with its fields (name and bits from its "
                                               "table; a whole-register value is one field with bits '7:0')."))
                extra = v & ~reg.defined_mask & 0xFF
                if reg.fields and extra:
                    findings.append(RegFinding("WARNING", "REG_RESERVED_BITS", wf, wl,
                                               f"{reg.name} ({_hex(r)}) <- {_hex(v)} sets bit(s) {_hex(extra)} that no field of {reg.id} defines "
                                               f"({', '.join(f.label() for f in reg.fields)}).",
                                               "Write only defined fields (reserved bits are usually written 0), or record the missing field."))
                for f in reg.fields:
                    fv = f.get(v)
                    if f.values and not f.value_name(fv):
                        findings.append(RegFinding("WARNING", "REG_FIELD_VALUE", wf, wl,
                                                   f"{reg.name}.{f.name} = {fv} is none of its named values "
                                                   f"({', '.join(f'{n}={chr(124).join(str(x) for x in vs)}' for n, vs in f.values.items())}).",
                                                   "Use one of the datasheet's values for this field."))

    def bit_findings(ws: List[Tuple[List[str], Walker]]) -> List[RegFinding]:
        out: List[RegFinding] = []
        # 2. bit masks on bytes read from registers
        seen_m = set()
        for _, w in ws:
            for rx, mask, file, line in w.obs_masks:
                reg = reg_at(rx.reg, rx.dev)
                if reg is None or not reg.fields or not mine(rx.dev) or parts_at(rx.reg, rx.dev):
                    continue
                bad = mask & ~reg.defined_mask & 0xFF
                key = (rx.reg, mask, file, line)
                if bad and key not in seen_m:
                    seen_m.add(key)
                    out.append(RegFinding("ERROR", "REG_MASK_UNDEFINED", file, line,
                                          f"{reg.name} ({_hex(reg.addr)}) is tested with mask {_hex(mask)}, but bit(s) {_hex(bad)} are not "
                                          f"defined there; its fields are {', '.join(f.label() for f in reg.fields)} ({reg.id}).",
                                          f"Mask with the field's bits from the register map (e.g. {', '.join(f'{f.name} = {_hex(f.mask)}' for f in reg.fields)})."))
        # 3. shifts on bytes of multi-byte values
        seen_s = set()
        for _, w in ws:
            for rx, shift, file, line in w.obs_shifts:
                if not mine(rx.dev):
                    continue
                parts = parts_at(rx.reg, rx.dev)
                for lay, k in parts:
                    p = lay.parts[k]
                    if (p.hi, p.lo) != (7, 0) or lay.width <= 8 and shift == 0:
                        continue
                    want = lay.offset_of(k)
                    key = (rx.reg, shift, file, line)
                    if shift != want and key not in seen_s and not any(sh == shift for l2, k2 in parts for sh in [l2.offset_of(k2)]):
                        seen_s.add(key)
                        out.append(RegFinding("ERROR", "LAYOUT_SHIFT", file, line,
                                              f"the byte read from {_hex(rx.reg)} is shifted left by {shift}, but in {lay.name} ({lay.id}) it is "
                                              f"bits {want + 7}..{want}: {lay.describe()}.",
                                              f"Assemble {lay.name} exactly as the layout says (that byte << {want})."))
        return out
    bits = bit_findings(walkers)
    if alt:
        other = {(f.code, f.file, f.line) for f in bit_findings(alt)}
        bits = [f for f in bits if (f.code, f.file, f.line) in other]
    findings += bits
    for lay in book.layouts.values():
        summary["layouts"].append(f"{lay.id} {lay.describe()}")

    # 4. burst reads
    for b in book.bursts.values():
        for _, w in walkers:
            for e in w.events:
                if e.kind != "read" or not is_int(e.reg) or not mine(e.addr) or (at(e.addr) and b.device.lower() not in at(e.addr)):
                    continue
                n = e.length if is_int(e.length) else 1
                if e.reg > b.end or e.reg + n - 1 < b.start:
                    continue
                if not (e.reg <= b.start and e.reg + n - 1 >= b.end):
                    findings.append(RegFinding("ERROR", "REG_BURST_SPLIT", *where(model, e),
                                               f"read of {_hex(e.reg)} x{n} touches {b.name} {_hex(b.start)}..{_hex(b.end)}, which the datasheet "
                                               f"requires to be read in one burst ({b.id}).",
                                               f"Read all {b.end - b.start + 1} bytes from {_hex(b.start)} in one transaction and take the values "
                                               "from that buffer."))
                    break

    # SPI reads must carry the read flag: without bit 7 the part takes the register byte as a write command
    seen_f = set()
    for _, w in walkers:
        for e in w.events:
            if e.kind == "read" and e.note == "spi_no_read_flag" and (e.file, e.line) not in seen_f:
                seen_f.add((e.file, e.line))
                findings.append(RegFinding("ERROR", "SPI_READ_FLAG", *where(model, e),
                                           f"register {_hex(e.reg)} is read over SPI without bit 7 (0x80) set in the register byte, so "
                                           "the part takes it as a write command.",
                                           "Send (reg | 0x80) to start an SPI read, as the datasheet's SPI protocol section requires."))

    # 5. formulas: probe runs with concrete register bytes
    if book.formulas:
        results: Dict[str, List[Optional[str]]] = {f.id: [] for f in book.formulas.values()}   # by id: names repeat across parts

        def same_dev(items: Any, f: FormulaDef) -> List[Any]:
            return [x for x in items if x.device.lower() == f.device.lower()]
        detail: Dict[str, str] = {}
        loc: Dict[str, Tuple[str, int]] = {}
        read_regs: set = set()
        expects = {rx.reg: c for _, w in walkers for rx, c, _f, _l in w.obs_compares}
        for seed in (1, 2):
            vec = _probe_vector(book, seed, expects)
            expected = evaluate_formulas(book, vec)
            observed: List[Tuple[Any, str, str]] = []
            for entries, _ in walkers:
                for dummy in dummies:   # a formula matches if the code computes it under either SPI reading
                    try:
                        pw = model.run(entries, probe=vec, spi_dummy=dummy)
                    except RecursionError:
                        continue
                    observed += [v for v in pw.values if isinstance(v[0], (int, float))]
                    read_regs |= {e.reg + k for e in pw.events if e.kind == "read" and is_int(e.reg) for k in range(e.length if is_int(e.length) else 1)}
            for f in book.formulas.values():
                want = expected.get(f.id)
                if want is None:
                    results[f.id].append(None)
                    continue
                hit = next((o for o in observed if _close(float(o[0]), want)), None)
                results[f.id].append(f"{hit[1]} ({hit[2]})" if hit else "")
                if not hit and f.id not in detail:
                    inputs_used = [l for l in same_dev(book.layouts.values(), f) if re.search(rf"\b{re.escape(l.name.lower())}\b", " ".join(e for _, e in f.steps).lower())]
                    raw = []
                    for l in inputs_used:
                        rv = float(l.decode(lambda a: vec.get(a, 0)))
                        o = next((o for o in observed if _close(float(o[0]), rv)), None)
                        if o is not None:
                            raw.append(f"{l.name} = {rv:g} at {o[1]} ({o[2]})")
                            fl, _, ln = str(o[1]).rpartition(":")
                            loc.setdefault(f.id, (fl, int(ln) if ln.isdigit() else 0))
                    deps = [g.name for g in same_dev(book.formulas.values(), f) if g.name != f.name and re.search(rf"\b{re.escape(g.name.lower())}\b", " ".join(e for _, e in f.steps).lower())]
                    detail[f.id] = (f"on probe input the formula gives {want:.7g}, a value the code never computes"
                                      + (f"; the code has the raw {'; '.join(raw[:2])} — the conversion is missing or different" if raw and len(f.steps) <= 2 else "")
                                      + (f" (it uses {', '.join(deps)}: fix those first)" if deps else ""))
        for f in book.formulas.values():
            res = results[f.id]
            inputs = {p.addr for l in same_dev(book.layouts.values(), f) if l.name.lower() in " ".join(e for _, e in f.steps).lower() for p in l.parts}
            deps_read = not inputs or inputs & read_regs
            if all(r for r in res if r is not None) and any(res):
                summary["formulas"].append(f"{f.id} {f.name}: matches the code at {res[0]}")
            elif not deps_read:
                summary["formulas"].append(f"{f.id} {f.name}: not observed (its registers are never read from {', '.join(summary['entries'])})")
                findings.append(RegFinding("WARNING", "FORMULA_UNOBSERVED", "", 0,
                                           f"{f.id} {f.name}: the walk of {', '.join(summary['entries']) or 'the program'} never reads its input "
                                           "registers, so the formula could not be compared with the code.",
                                           "Call the measurement code from setup()/loop()/main so it can be checked."))
            elif any(r is None for r in res):
                summary["formulas"].append(f"{f.id} {f.name}: could not be evaluated")
                findings.append(RegFinding("WARNING", "FORMULA_UNCHECKED", "", 0,
                                           f"{f.id} {f.name}: the recorded formula could not be evaluated on the probe input, so the "
                                           "code's conversion was not compared with it.",
                                           "Check that its inputs are recorded layouts or formulas; report it to the user if they are."))
            else:
                summary["formulas"].append(f"{f.id} {f.name}: MISMATCH")
                findings.append(RegFinding("ERROR", "FORMULA_MISMATCH", *loc.get(f.id, ("", 0)),
                                           f"{f.id} {f.text() if len(f.steps) == 1 else f.name}: {detail.get(f.id, 'matches only one of two probe inputs')}.",
                                           "Implement the recorded formula exactly (inputs from the layouts, constants and order of operations), "
                                           "or re-record the formula if the datasheet / reference code says otherwise."))
    return summary, findings


def coverage_findings(project_dir: Path, ledger: Optional[FactLedger], model: FlowModel) -> List[RegFinding]:
    """Registers the code touches on a device that has a register map but are not in it: their meaning is unchecked."""
    book = RegisterBook(project_dir)
    if book.empty():
        return []
    out, seen = [], set()
    known = book.known_addresses() | {a for b in book.bursts.values() for a in range(b.start, b.end + 1)}
    per_dev: Dict[str, set] = {}   # a register of one part does not map the same address on another part
    for x in book.registers.values():
        per_dev.setdefault(x.device.lower(), set()).add(x.addr)
    for l in book.layouts.values():
        per_dev.setdefault(l.device.lower(), set()).update(p.addr for p in l.parts)
    for b in book.bursts.values():
        per_dev.setdefault(b.device.lower(), set()).update(range(b.start, b.end + 1))
    addrs, dev_of = set(), {}
    for d in {x.device for x in book.registers.values()} | {x.device for x in book.layouts.values()}:
        for a in _device_addrs(ledger, d):
            addrs.add(a)
            dev_of.setdefault(a, set()).add(d.lower())
    for entries in entry_points(model):
        try:
            w = model.run(entries)
        except RecursionError:
            continue
        for e in w.events:
            if e.kind not in ("read", "write") or (addrs and is_int(e.addr) and e.addr not in addrs):
                continue
            devs = dev_of.get(e.addr) if is_int(e.addr) else None
            mapped = set().union(*(per_dev.get(d, set()) for d in devs)) if devs else known
            regs = [r for r, _ in writes_of(e, mapped.__contains__)] if e.kind == "write" else ([e.reg] if is_int(e.reg) else [])
            for r in regs:
                if is_int(r) and r not in mapped and (r, e.kind, e.addr) not in seen:
                    seen.add((r, e.kind, e.addr))
                    out.append(RegFinding("ERROR", "REG_UNMAPPED", *where(model, e),
                                          f"the code {'writes' if e.kind == 'write' else 'reads'} register {_hex(r)}, which is not in the register map, "
                                          "so the meaning of its value is unchecked.",
                                          f"record_registers for {_hex(r)} (its fields from the datasheet table) or record_layouts if it holds data."))
    return out
