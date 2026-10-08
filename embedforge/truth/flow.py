"""Static flow extraction for embedded drivers: what a function does on the bus, in order, without running it.

Given C/C++ sources, `FlowModel.trace(entry)` walks `entry` statement by statement, inlines calls into project functions,
evaluates constants (macros, enums, const globals, local buffers) and records every bus transfer and delay it reaches:

    write  addr=0x76 reg=0x7E data=[0xB6]          (i2c_write_blocking(i2c0, ADDR, (uint8_t[]){CMD, SOFTRESET}, 2, false))
    delay  >= 2000 us                               (sleep_ms(2))
    read   addr=0x76 reg=0x31 len=21                (write of [0x31] followed by a read of 21 bytes)

Bus primitives of the common HALs are built in (pico-sdk, Arduino Wire, STM32 HAL, ESP-IDF, Zephyr, Bosch-API style
function pointers). Each event says whether it always runs on the path (`guaranteed`) or only inside an if/loop.
Nothing is executed: this is constant propagation over the source text, so values that depend on runtime data stay
symbolic and are reported as unresolved rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

SOURCE_EXTS = {".c", ".h", ".cpp", ".hpp", ".cc", ".ino"}
MAX_DEPTH = 10
MAX_EVENTS = 4000

# ------------------------------------------------------------------ values


@dataclass(frozen=True)
class Sym:
    """A value the static walk cannot know (runtime data, struct fields, unknown externals)."""
    text: str

    def __repr__(self) -> str:
        return f"<{self.text}>"


class Arr:
    """A byte/int array (local buffer, compound literal or initializer list). Mutable, shared by reference."""

    def __init__(self, items: List[Any]):
        self.items = list(items)

    def __repr__(self) -> str:
        return f"Arr({self.items})"


@dataclass(frozen=True)
class Obj:
    """A named struct/object (global or local) whose fields the walk tracks: &the_sensor, dev->write = &i2c_write."""
    name: str

    def __repr__(self) -> str:
        return self.name


def obj_key(v: Any) -> Optional[str]:
    if isinstance(v, Obj):
        return v.name
    if isinstance(v, Sym):
        return v.text
    if isinstance(v, Ptr) and v.offset == 0 and len(v.arr.items) == 1 and isinstance(v.arr.items[0], (Sym, Obj)):
        return obj_key(v.arr.items[0])
    return None


@dataclass
class Ptr:
    arr: Arr
    offset: int = 0

    def get(self, n: Optional[int]) -> List[Any]:
        items = self.arr.items[self.offset:]
        return items if n is None else items[:n]


Value = Union[int, float, Sym, Arr, Ptr, str, None]


def is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def as_bytes(v: Any, n: Any) -> List[Any]:
    """The first n items a buffer argument points to (symbolic items stay symbolic)."""
    count = n if is_int(n) else None
    if isinstance(v, Ptr):
        return v.get(count)
    if isinstance(v, Arr):
        return v.items if count is None else v.items[:count]
    if is_int(v) and count == 1:  # &single_byte
        return [v]
    return [Sym(f"buf:{v!r}")] if count is None else [Sym(f"buf:{v!r}[{i}]") for i in range(min(count, 64))]


# ------------------------------------------------------------------ events


@dataclass
class Event:
    kind: str                      # write | read | delay
    addr: Any = None               # bus address (int) or Sym / None (SPI or unknown)
    reg: Any = None                # register address after normalisation
    data: List[Any] = field(default_factory=list)
    length: Any = None
    delay_us: Any = None
    file: str = ""
    line: int = 0
    chain: Tuple[str, ...] = ()
    guaranteed: bool = True        # False when inside if/else/switch or a loop that may not run
    bus: str = "i2c"
    note: str = ""
    sites: Tuple[int, ...] = ()    # call-site lines from the entry function down to the primitive

    @property
    def entry_line(self) -> int:
        return self.sites[0] if self.sites else self.line

    def public(self) -> Dict[str, Any]:
        def fmt(v: Any) -> Any:
            return f"0x{v:02X}" if is_int(v) else (repr(v) if v is not None else None)
        via = " > ".join(f"{c}:{s}" if k < len(self.sites) else c for k, (c, s) in enumerate(zip(self.chain, list(self.sites) + [0] * len(self.chain))))
        d = {"kind": self.kind, "file": self.file, "line": self.line, "entry_line": self.entry_line, "via": via, "guaranteed": self.guaranteed}
        if self.kind == "delay":
            d["delay_us"] = self.delay_us if is_int(self.delay_us) else repr(self.delay_us)
        else:
            d.update(addr=fmt(self.addr), reg=fmt(self.reg), bus=self.bus)
            if self.kind == "write":
                d["data"] = [fmt(x) for x in self.data]
            else:
                d["length"] = self.length if is_int(self.length) else repr(self.length)
        if self.note:
            d["note"] = self.note
        return d

    def describe(self) -> str:
        p = self.public()
        if self.kind == "delay":
            return f"delay {p['delay_us']} us"
        if self.kind == "write":
            return f"write {p['reg']} <- {', '.join(str(x) for x in p['data'])}"
        return f"read {p['reg']} x{p['length']}"


# ------------------------------------------------------------------ lexer

_TOKEN_RE = re.compile(r"""
    (?P<ws>[ \t\r\f\v]+)
  | (?P<nl>\n)
  | (?P<num>0[xX][0-9A-Fa-f']+[uUlL]*|0[bB][01']+[uUlL]*|\d[\d']*\.\d*(?:[eE][+-]?\d+)?[fFlL]?|\d[\d']*(?:[eE][+-]?\d+)?[uUlLfF]*|\.\d+(?:[eE][+-]?\d+)?[fF]?)
  | (?P<id>[A-Za-z_]\w*)
  | (?P<str>"(?:\\.|[^"\\\n])*")
  | (?P<chr>'(?:\\.|[^'\\\n])+')
  | (?P<op>::|->|\+\+|--|<<=|>>=|<<|>>|<=|>=|==|!=|&&|\|\||\+=|-=|\*=|/=|%=|&=|\|=|\^=|\.\.\.|[{}()\[\];,.?:~!%^&*+\-=<>|/\#])
  | (?P<other>.)
""", re.X)


@dataclass
class Tok:
    kind: str
    text: str
    line: int
    cond: bool = False   # inside a preprocessor branch whose condition is unknown at analysis time


def _strip_comments(src: str) -> str:
    """Replace comments with spaces (keeping newlines), leave strings/chars intact."""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in src[i:j]))
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def tokenize(text: str, first_line: int = 1) -> List[Tok]:
    toks, line = [], first_line
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        if kind == "nl":
            line += 1
            continue
        if kind == "ws":
            continue
        toks.append(Tok(kind, m.group(), line))
    return toks


def parse_number(text: str) -> Union[int, float, None]:
    t = text.replace("'", "")
    t2 = re.sub(r"[uUlL]+$", "", t)
    try:
        if t2.lower().startswith("0x"):
            return int(t2, 16)
        if t2.lower().startswith("0b"):
            return int(t2[2:], 2)
        if re.fullmatch(r"0[0-7]+", t2):
            return int(t2, 8)
        if re.fullmatch(r"\d+", t2):
            return int(t2)
        return float(re.sub(r"[fFlL]$", "", t2))
    except ValueError:
        return None


_ESC = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92, "'": 39, '"': 34, "a": 7, "b": 8, "f": 12, "v": 11}


def parse_char(text: str) -> Optional[int]:
    body = text[1:-1]
    if not body:
        return None
    if body[0] != "\\":
        return ord(body[0])
    if body[1:2] in ("x", "X"):
        try:
            return int(body[2:], 16)
        except ValueError:
            return None
    if body[1:].isdigit():
        return int(body[1:], 8)
    return _ESC.get(body[1:2])


# ------------------------------------------------------------------ source model


@dataclass
class Function:
    name: str
    params: List[str]
    body: List[Tok]
    file: str
    line: int


@dataclass
class Macro:
    params: Optional[List[str]]  # None = object-like
    body: List[Tok]


_KEYWORDS = {"if", "else", "for", "while", "do", "switch", "case", "default", "return", "break", "continue", "goto",
             "sizeof", "struct", "union", "enum", "typedef", "static", "const", "volatile", "extern", "inline",
             "unsigned", "signed", "void", "char", "short", "int", "long", "float", "double", "bool", "_Bool",
             "class", "namespace", "template", "typename", "public", "private", "protected", "virtual", "operator",
             "new", "delete", "this", "true", "false", "nullptr", "NULL", "auto", "register", "constexpr", "using"}
_TYPE_WORDS = {"unsigned", "signed", "const", "volatile", "char", "short", "int", "long", "float", "double", "bool",
               "_Bool", "void", "struct", "enum", "union", "static", "register", "auto", "constexpr", "size_t", "ssize_t"}
_TYPE_RE = re.compile(r"^(u?int(8|16|32|64)_t|u?int_(least|fast)(8|16|32|64)_t|uintptr_t|intptr_t|size_t|ssize_t|byte|uint|u8|u16|u32|s8|s16|s32|BYTE|WORD|DWORD|uint8|uint16|uint32|int8|int16|int32)$")


def _looks_type(tokens: Sequence[Tok], known_types: set) -> bool:
    """`(uint8_t)` / `(const uint8_t *)` / `(struct foo *)` — a cast, not a parenthesised expression."""
    if not tokens:
        return False
    seen_name = False
    for t in tokens:
        if t.kind == "id" and (t.text in _TYPE_WORDS or _TYPE_RE.match(t.text) or t.text in known_types):
            seen_name = True
        elif t.kind == "op" and t.text in ("*", "&", "::"):
            continue
        elif t.kind == "id" and seen_name is False and tokens[0].text in ("struct", "enum", "union"):
            seen_name = True
        else:
            return False
    return seen_name


def _match(tokens: List[Tok], i: int, open_: str = "(", close: str = ")") -> int:
    """Index of the token closing the bracket opened at tokens[i]."""
    depth = 0
    for j in range(i, len(tokens)):
        t = tokens[j].text
        if t == open_:
            depth += 1
        elif t == close:
            depth -= 1
            if depth == 0:
                return j
    return len(tokens) - 1


def _split_args(tokens: List[Tok]) -> List[List[Tok]]:
    out, cur, depth = [], [], 0
    for t in tokens:
        if t.text in ("(", "[", "{"):
            depth += 1
        elif t.text in (")", "]", "}"):
            depth -= 1
        if t.text == "," and depth == 0:
            out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur or out:
        out.append(cur)
    return out


class FlowModel:
    """All functions, macros, enums and constant globals of a set of C/C++ sources."""

    def __init__(self, files: Dict[str, str]):
        self.functions: Dict[str, List[Function]] = {}
        self.macros: Dict[str, Macro] = {}
        self.consts: Dict[str, List[Tok]] = {}       # enum members and const globals: name -> expression tokens
        self.global_arrays: Dict[str, List[Tok]] = {}
        self.global_sizes: Dict[str, List[Tok]] = {}   # NAME[N] declared at file scope: name -> N tokens
        self.fnptr_fields: Dict[str, List[str]] = {}  # struct field name -> project functions assigned to it
        self.types: set = set()
        self._defined = {m for text in files.values() for m in re.findall(r"^\s*#\s*define\s+([A-Za-z_]\w*)", text, re.M)}
        for name, text in sorted(files.items()):
            self._load(name, text)

    @classmethod
    def from_dir(cls, root: Path, dirs: Iterable[str] = ("src", "include", "lib")) -> "FlowModel":
        files: Dict[str, str] = {}
        root = Path(root)
        for d in dirs:
            base = root / d
            if base.is_dir():
                for p in base.rglob("*"):
                    if p.is_file() and p.suffix.lower() in SOURCE_EXTS and ".pio" not in p.parts:
                        try:
                            files[str(p.relative_to(root)).replace("\\", "/")] = p.read_text(encoding="utf-8", errors="ignore")
                        except OSError:
                            continue
        return cls(files)

    # ---------------------------------------------------------- loading
    def _load(self, name: str, text: str) -> None:
        text = _strip_comments(text.replace("\r\n", "\n"))
        # join backslash continuations, keeping one (empty) line per consumed line so line numbers stay true
        lines: List[str] = []
        pending = ""
        swallowed = 0
        for raw in text.split("\n"):
            if raw.endswith("\\"):
                pending += raw[:-1] + " "
                swallowed += 1
                continue
            lines.append(pending + raw)
            lines.extend([""] * swallowed)
            pending, swallowed = "", 0
        if pending:
            lines.append(pending)
        code_lines: List[str] = []
        cond_lines: set = set()
        stack: List[List[Any]] = []   # [state 'true'|'false'|'unknown', some_branch_taken (True/False/None=unknown)]
        directives = [(k, re.match(r"\s*#\s*(\w+)\s*(.*)$", ln)) for k, ln in enumerate(lines)]
        for k, raw in enumerate(lines):
            s = raw.strip()
            active = all(st[0] != "false" for st in stack)
            if s.startswith("#"):
                d = directives[k][1]
                directive, rest = (d.group(1), d.group(2).strip()) if d else ("", "")
                if directive in ("if", "ifdef", "ifndef"):
                    state = self._pp_state(directive, rest, lines, k) if active else "false"
                    stack.append([state, {"true": True, "false": False}.get(state)])
                elif directive == "elif" and stack:
                    prev_taken = stack[-1][1]
                    if prev_taken is True:
                        stack[-1][0] = "false"
                    else:
                        st = self._pp_state("if", rest, lines, k)
                        stack[-1][0] = "unknown" if (prev_taken is None and st != "false") else st
                        stack[-1][1] = True if st == "true" and prev_taken is False else (None if st == "unknown" or prev_taken is None else prev_taken)
                elif directive == "else" and stack:
                    prev_taken = stack[-1][1]
                    stack[-1][0] = "false" if prev_taken is True else ("true" if prev_taken is False else "unknown")
                elif directive == "endif" and stack:
                    stack.pop()
                elif directive == "define" and active:
                    self._define(rest)
                code_lines.append("")
            else:
                code_lines.append(raw if active else "")
                if active and any(st[0] == "unknown" for st in stack):
                    cond_lines.add(k + 1)
        toks = tokenize("\n".join(code_lines))
        if cond_lines:
            for tk in toks:
                if tk.line in cond_lines:
                    tk.cond = True
        self._scan(name, toks)

    def _pp_state(self, directive: str, rest: str, lines: List[str], k: int) -> str:
        """'true' / 'false' / 'unknown' for a conditional, using only what the project itself defines."""
        name = rest.split()[0] if rest.split() else ""
        if directive in ("ifdef", "ifndef"):
            nxt = next((ln.strip() for ln in lines[k + 1:k + 4] if ln.strip()), "")
            if directive == "ifndef" and re.match(rf"#\s*define\s+{re.escape(name)}\b", nxt):
                return "true"   # include guard / overridable default
            if name in self._defined:
                return "true" if directive == "ifdef" else "false"
            return "unknown"
        expr = re.sub(r"\bdefined\s*\(\s*(\w+)\s*\)|\bdefined\s+(\w+)",
                      lambda m: "1" if (m.group(1) or m.group(2)) in self._defined else "__UNKNOWN__", rest)
        if "__UNKNOWN__" in expr:
            return "unknown"
        v = Evaluator(self, {}, ()).eval(tokenize(expr))
        if is_int(v):
            return "true" if v else "false"
        return "unknown"

    def _define(self, rest: str) -> None:
        m = re.match(r"([A-Za-z_]\w*)(\((.*?)\))?\s*(.*)$", rest)
        if not m:
            return
        name, params = m.group(1), m.group(3)
        if m.group(2) is not None and rest[len(name)] == "(":  # function-like: '(' immediately after the name
            plist = [p.strip() for p in params.split(",") if p.strip()]
            self.macros[name] = Macro(plist, tokenize(m.group(4)))
        else:
            body = rest[len(name):].strip()
            self.macros[name] = Macro(None, tokenize(body))

    def _scan(self, fname: str, toks: List[Tok]) -> None:
        i, n = 0, len(toks)
        depth_stack: List[str] = []   # 'scope' (namespace/class/extern) or 'other'
        while i < n:
            t = toks[i]
            if t.text == "{":
                # function definition?  ... name ( params ) [qualifiers] {
                j = i - 1
                while j >= 0 and toks[j].kind == "id" and toks[j].text in ("const", "override", "noexcept", "final", "volatile"):
                    j -= 1
                if j >= 0 and toks[j].text == ")":
                    k = j
                    depth = 0
                    while k >= 0:
                        if toks[k].text == ")":
                            depth += 1
                        elif toks[k].text == "(":
                            depth -= 1
                            if depth == 0:
                                break
                        k -= 1
                    name_i = k - 1
                    if name_i >= 0 and toks[name_i].kind == "id" and toks[name_i].text not in _KEYWORDS:
                        name = toks[name_i].text
                        if name_i >= 2 and toks[name_i - 1].text == "::" and toks[name_i - 2].kind == "id":
                            name = f"{toks[name_i - 2].text}::{name}"
                        end = _match(toks, i, "{", "}")
                        params = self._params(toks[k + 1:j])
                        fn = Function(name, params, toks[i + 1:end], fname, toks[name_i].line)
                        self.functions.setdefault(name.split("::")[-1], []).append(fn)
                        if "::" in name:
                            self.functions.setdefault(name, []).append(fn)
                        self._scan_fnptr_assignments(fn.body)
                        i = end + 1
                        continue
                prev = toks[i - 1].text if i else ""
                # enum body
                head = [x.text for x in toks[max(0, i - 4):i]]
                if "enum" in head:
                    end = _match(toks, i, "{", "}")
                    self._enum(toks[i + 1:end])
                    self._typedef_name(toks, end)
                    i = end + 1
                    continue
                if prev == "=":  # initializer at file scope: const uint8_t tbl[] = { ... };  or a vtable { .read = f, ... }
                    end = _match(toks, i, "{", "}")
                    self._scan_fnptr_assignments(toks[i:end + 1])
                    i = end + 1
                    continue
                if "struct" in head or "union" in head or "class" in head:
                    end = _match(toks, i, "{", "}")
                    self._typedef_name(toks, end)
                    # class bodies may contain inline methods: scan inside
                    self._scan(fname, toks[i + 1:end])
                    i = end + 1
                    continue
                depth_stack.append("scope")
                i += 1
                continue
            if t.text == "}":
                if depth_stack:
                    depth_stack.pop()
                i += 1
                continue
            if t.text == "typedef":
                semi = self._find_semicolon(toks, i)
                seg = toks[i:semi]
                if seg and seg[-1].kind == "id" and "{" not in [x.text for x in seg]:
                    self.types.add(seg[-1].text)
                if "{" not in [x.text for x in seg]:
                    i = semi + 1
                    continue
            prev_text = toks[i - 1].text if i else ";"
            if t.text == "[" and i >= 2 and toks[i - 1].kind == "id" and toks[i - 2].kind == "id":
                rb = _match(toks, i, "[", "]")
                if rb + 1 < n and toks[rb + 1].text in (";", "="):
                    self.global_sizes.setdefault(toks[i - 1].text, toks[i + 1:rb])
            # const globals: [static] const <type> NAME = expr ;  and arrays NAME[] = { ... };  (only at statement start)
            if t.kind == "id" and t.text in ("const", "constexpr", "static") and prev_text in (";", "}", "{"):
                semi = self._find_semicolon(toks, i)
                seg = toks[i:semi]
                texts_seg = [x.text for x in seg]
                if "=" in texts_seg and not any(x in ("(", "{") for x in texts_seg[:texts_seg.index("=")]):
                    eq = [x.text for x in seg].index("=")
                    lhs = seg[:eq]
                    if lhs and lhs[-1].text == "]":
                        lb = max(k for k, x in enumerate(lhs) if x.text == "[")
                        if lb > 0 and lhs[lb - 1].kind == "id":
                            self.global_arrays[lhs[lb - 1].text] = seg[eq + 1:]
                    elif lhs and lhs[-1].kind == "id" and any(x.text in ("const", "constexpr") for x in lhs):
                        self.consts[lhs[-1].text] = seg[eq + 1:]
                    self._scan_fnptr_assignments(seg)   # const vtables: { .read = bmp388_read_i2c, ... }
                    i = semi + 1
                    continue
            i += 1

    @staticmethod
    def _find_semicolon(toks: List[Tok], i: int) -> int:
        depth = 0
        for j in range(i, len(toks)):
            x = toks[j].text
            if x in ("(", "[", "{"):
                depth += 1
            elif x in (")", "]", "}"):
                depth -= 1
            elif x == ";" and depth <= 0:
                return j
        return len(toks) - 1

    def _typedef_name(self, toks: List[Tok], end: int) -> None:
        if end + 1 < len(toks) and toks[end + 1].kind == "id":
            self.types.add(toks[end + 1].text)

    def _enum(self, body: List[Tok]) -> None:
        nxt = 0
        for item in _split_args(body):
            if not item or item[0].kind != "id":
                continue
            if len(item) >= 3 and item[1].text == "=":
                self.consts[item[0].text] = item[2:]
                v = Evaluator(self, {}, ()).eval(item[2:])
                nxt = v + 1 if is_int(v) else nxt + 1
            else:
                self.consts[item[0].text] = [Tok("num", str(nxt), item[0].line)]
                nxt += 1

    def _params(self, toks: List[Tok]) -> List[str]:
        names = []
        for p in _split_args(toks):
            ids = [t for t in p if t.kind == "id" and t.text not in _TYPE_WORDS]
            if not p or (len(p) == 1 and p[0].text == "void"):
                continue
            # 'uint8_t buf[]', 'const uint8_t *data', 'int (*cb)(int)'
            if any(t.text == "(" for t in p) and len(ids) >= 1:
                inner = [t for t in p if t.kind == "id"]
                names.append(inner[1].text if len(inner) > 1 else inner[0].text)
                continue
            core = [t for t in p if t.text not in ("[", "]") and t.kind != "num"]
            if "=" in [t.text for t in core]:
                core = core[:[t.text for t in core].index("=")]
            last = [t for t in core if t.kind == "id"]
            names.append(last[-1].text if last else f"_{len(names)}")
        return names

    def _scan_fnptr_assignments(self, body: List[Tok]) -> None:
        """`dev.write = &i2c_write;`, `dev->delay_us = user_delay;` and vtable initializers `.read = bmp388_read_i2c,`:
        remember which project functions a field may point to."""
        for k in range(len(body) - 3):
            if body[k].text in (".", "->") and body[k + 1].kind == "id" and body[k + 2].text == "=":
                j = k + 3
                if j < len(body) and body[j].text == "&":
                    j += 1
                if j < len(body) and body[j].kind == "id" and j + 1 < len(body) and body[j + 1].text in (";", ",", "}"):
                    cands = self.fnptr_fields.setdefault(body[k + 1].text, [])
                    if body[j].text not in cands:
                        cands.append(body[j].text)

    def fnptr_candidates(self, field: str) -> List[str]:
        """Project functions stored in a field of this name, I2C implementations first (then the rest, as written)."""
        cands = [c for c in self.fnptr_fields.get(field, []) if c in self.functions]
        return sorted(cands, key=lambda c: (0 if "i2c" in c.lower() else 1 if "spi" not in c.lower() else 2))

    # ---------------------------------------------------------- api
    def function(self, name: str) -> Optional[Function]:
        cands = self.functions.get(name) or self.functions.get(name.split("::")[-1])
        if not cands:
            return None
        return max(cands, key=lambda f: len(f.body))  # prefer the definition over a stub

    def trace(self, entry: str, args: Optional[List[Any]] = None, assume: Optional[Dict[str, Any]] = None) -> "Trace":
        """Walk `entry`. `assume` fixes what the caller would provide: {'dev.intf': 1} sets a field of a parameter,
        {'addr': 0x76} a parameter's value."""
        fn = self.function(entry)
        if fn is None:
            return Trace(entry, [], [f"function '{entry}' not found in the project sources"], found=False)
        w = Walker(self)
        call_args = list(args or [])
        for k, p in enumerate(fn.params):
            if k >= len(call_args):
                call_args.append(Obj(f"<{p}>") if any(a.startswith(p + ".") for a in (assume or {})) else Sym(p))
        for key, value in (assume or {}).items():
            if "." in key:
                base, fld = key.split(".", 1)
                w.fields[(f"<{base}>", fld)] = value
            elif key in fn.params:
                call_args[fn.params.index(key)] = value
        w.call(fn, call_args, (), guaranteed=True)
        return Trace(entry, normalize(w.events), w.notes, found=True, opaque=sorted(w.opaque))


@dataclass
class Trace:
    entry: str
    events: List[Event]
    notes: List[str]
    found: bool = True
    opaque: List[str] = field(default_factory=list)

    def ops(self) -> List[Event]:
        return [e for e in self.events if e.kind in ("write", "read")]


# ------------------------------------------------------------------ expression evaluation


class _Return(Exception):
    def __init__(self, value: Any):
        self.value = value


class Evaluator:
    """Constant-propagating expression evaluator with a call hook."""

    def __init__(self, model: FlowModel, env: Dict[str, Any], chain: Tuple[str, ...], walker: Optional["Walker"] = None,
                 guaranteed: bool = True, file: str = ""):
        self.m, self.env, self.chain, self.w, self.guaranteed, self.file = model, env, chain, walker, guaranteed, file
        self._macro_depth = 0

    # Pratt parser over a token list
    def eval(self, toks: List[Tok]) -> Any:
        if not toks:
            return None
        self.toks, self.i = self._expand(toks), 0
        try:
            v = self._expr(0)
        except (IndexError, ZeroDivisionError, ValueError, OverflowError, TypeError, RecursionError):
            return Sym(" ".join(t.text for t in toks)[:80])
        return v

    def _expand(self, toks: List[Tok], depth: int = 0) -> List[Tok]:
        """Expand function-like macros (object-like ones are resolved lazily as identifiers)."""
        if depth > 8:
            return toks
        out: List[Tok] = []
        i = 0
        changed = False
        while i < len(toks):
            t = toks[i]
            mac = self.m.macros.get(t.text) if t.kind == "id" else None
            if mac and mac.params is not None and i + 1 < len(toks) and toks[i + 1].text == "(":
                end = _match(toks, i + 1)
                args = _split_args(toks[i + 2:end])
                sub = {p: (args[k] if k < len(args) else []) for k, p in enumerate(mac.params)}
                if mac.params and mac.params[-1] == "...":
                    sub["__VA_ARGS__"] = sum(([Tok("op", ",", t.line)] + a for a in args[len(mac.params) - 1:]), [])[1:]
                body = []
                for bt in mac.body:
                    if bt.kind == "id" and bt.text in sub:
                        body.extend(Tok(x.kind, x.text, t.line) for x in sub[bt.text])
                    elif bt.text == "#":
                        continue
                    else:
                        body.append(Tok(bt.kind, bt.text, t.line))
                out.append(Tok("op", "(", t.line))
                out.extend(body)
                out.append(Tok("op", ")", t.line))
                i = end + 1
                changed = True
            else:
                out.append(t)
                i += 1
        return self._expand(out, depth + 1) if changed else out

    def _peek(self, k: int = 0) -> Optional[Tok]:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def _next(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    _BIN = {"||": 1, "&&": 2, "|": 3, "^": 4, "&": 5, "==": 6, "!=": 6, "<": 7, ">": 7, "<=": 7, ">=": 7,
            "<<": 8, ">>": 8, "+": 9, "-": 9, "*": 10, "/": 10, "%": 10}
    _ASSIGN = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}

    def _expr(self, min_prec: int) -> Any:
        left = self._unary()
        while True:
            t = self._peek()
            if t is None:
                return left
            if t.text == "," and min_prec == 0:
                self._next()
                left = self._expr(0)  # comma operator: value of the last operand
                continue
            if t.text in self._ASSIGN and min_prec == 0:
                self._next()
                right = self._expr(0)
                return right
            if t.text == "?" and min_prec <= 0:
                self._next()
                a = self._expr(0)
                if self._peek() is not None and self._peek().text == ":":
                    self._next()
                b = self._expr(0)
                if is_int(left):
                    return a if left else b
                return a if a == b else Sym("ternary")
            prec = self._BIN.get(t.text)
            if prec is None or prec <= min_prec - 1 or prec < min_prec:
                return left
            self._next()
            right = self._expr(prec + 1)
            left = self._binop(t.text, left, right)

    @staticmethod
    def _binop(op: str, a: Any, b: Any) -> Any:
        # a buffer, object or function is a non-null pointer: true in && / || / !
        if op in ("&&", "||") and (isinstance(a, (Ptr, Arr, Obj)) or isinstance(b, (Ptr, Arr, Obj))):
            a = 1 if isinstance(a, (Ptr, Arr, Obj)) else a
            b = 1 if isinstance(b, (Ptr, Arr, Obj)) else b
        if not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
            if op == "&&" and (a == 0 or b == 0):
                return 0
            if op == "||" and ((is_int(a) and a) or (is_int(b) and b)):
                return 1
            return Sym(f"{a!r}{op}{b!r}")
        try:
            r = {"||": lambda: int(bool(a) or bool(b)), "&&": lambda: int(bool(a) and bool(b)), "|": lambda: int(a) | int(b),
                 "^": lambda: int(a) ^ int(b), "&": lambda: int(a) & int(b), "==": lambda: int(a == b), "!=": lambda: int(a != b),
                 "<": lambda: int(a < b), ">": lambda: int(a > b), "<=": lambda: int(a <= b), ">=": lambda: int(a >= b),
                 "<<": lambda: int(a) << int(b), ">>": lambda: int(a) >> int(b), "+": lambda: a + b, "-": lambda: a - b,
                 "*": lambda: a * b, "/": lambda: (int(a) // int(b) if is_int(a) and is_int(b) else a / b), "%": lambda: int(a) % int(b)}[op]()
            return r
        except (ZeroDivisionError, ValueError, OverflowError):
            return Sym(f"{a}{op}{b}")

    def _unary(self) -> Any:
        t = self._peek()
        if t is None:
            return None
        if t.text in ("-", "+", "~", "!"):
            self._next()
            v = self._unary()
            if isinstance(v, (int, float)):
                return {"-": -v, "+": v, "~": ~int(v), "!": int(not v)}[t.text]
            return Sym(f"{t.text}{v!r}")
        if t.text in ("++", "--"):
            self._next()
            return self._unary()
        if t.text == "&":
            self._next()
            nxt = self._peek()
            name = nxt.text if nxt is not None and nxt.kind == "id" else None
            follow = self._peek(1)
            v = self._unary()
            if isinstance(v, (Arr, Ptr, Obj)):
                return v
            if isinstance(v, Sym) and v.text.startswith("fn:"):
                return v
            if name and (follow is None or follow.text not in (".", "->", "[")):
                if name not in self.env and isinstance(v, Sym):
                    return Obj(name)  # &global_struct
                if name in self.env and isinstance(v, Sym):
                    return Ptr(Arr([v]), 0)   # &local: a one-element buffer (and a handle for field access)
                if name in self.env:
                    self.env[name] = Sym(f"{name}@")  # its address escapes: whatever is called may write it
            if isinstance(v, Sym) and follow is not None and follow.text in (".", "->"):
                return Obj(v.text)  # &a.b / &dev->cfg: an object handle for that field path
            return Ptr(Arr([v]), 0)
        if t.text == "*":
            self._next()
            v = self._unary()
            if isinstance(v, Ptr):
                items = v.get(1)
                return items[0] if items else Sym("*ptr")
            if isinstance(v, Arr):
                return v.items[0] if v.items else Sym("*arr")
            return Sym(f"*{v!r}")
        if t.text == "sizeof":
            self._next()
            if self._peek() is not None and self._peek().text == "(":
                start = self.i
                end = _match(self.toks, self.i)
                inner = self.toks[start + 1:end]
                self.i = end + 1
                if len(inner) == 1 and inner[0].kind == "id":
                    v = self.env.get(inner[0].text)
                    if isinstance(v, Arr):
                        return len(v.items)
                    name = inner[0].text
                    if name in self.m.global_sizes:
                        size = Evaluator(self.m, {}, self.chain).eval(self.m.global_sizes[name])
                        if is_int(size):
                            return size
                    if name in self.m.global_arrays:
                        arr = self.m_const(name, 0)
                        if isinstance(arr, Arr):
                            return len(arr.items)
                    if name in ("uint8_t", "int8_t", "char", "unsigned char", "byte"):
                        return 1
                    if name in ("uint16_t", "int16_t", "short"):
                        return 2
                    if name in ("uint32_t", "int32_t", "int", "float"):
                        return 4
                return Sym("sizeof")
            v = self._unary()
            return len(v.items) if isinstance(v, Arr) else Sym("sizeof")
        if t.text == "(":
            end = _match(self.toks, self.i)
            inner = self.toks[self.i + 1:end]
            if _looks_type(inner, self.m.types):
                self.i = end + 1
                nxt = self._peek()
                if nxt is not None and nxt.text == "{":  # compound literal (uint8_t[]){a, b}
                    return self._postfix(self._init_list())
                v = self._unary()
                return self._cast(inner, v)
            self._next()
            v = self._expr(0)
            if self._peek() is not None and self._peek().text == ")":
                self._next()
            return self._postfix(v)
        if t.text == "{":
            return self._postfix(self._init_list())
        return self._postfix(self._primary())

    @staticmethod
    def _cast(type_toks: List[Tok], v: Any) -> Any:
        names = " ".join(t.text for t in type_toks)
        if is_int(v) and "*" not in names:
            if re.search(r"\b(uint8_t|unsigned char|byte|u8|BYTE|uint8)\b", names):
                return v & 0xFF
            if re.search(r"\b(uint16_t|unsigned short|u16|WORD|uint16)\b", names):
                return v & 0xFFFF
            if re.search(r"\b(int8_t|s8|int8)\b", names):
                return ((v & 0xFF) ^ 0x80) - 0x80
        return v

    def _init_list(self) -> Arr:
        end = _match(self.toks, self.i, "{", "}")
        items = []
        for part in _split_args(self.toks[self.i + 1:end]):
            if not part:
                continue
            if len(part) > 2 and part[0].text == "[":  # designated [i] = v
                close = next(k for k, x in enumerate(part) if x.text == "]")
                part = part[close + 2:]
            items.append(Evaluator(self.m, self.env, self.chain, self.w, self.guaranteed, self.file).eval(part))
        self.i = end + 1
        return Arr(items)

    def _primary(self) -> Any:
        t = self._next()
        if t.kind == "num":
            return parse_number(t.text)
        if t.kind == "chr":
            return parse_char(t.text)
        if t.kind == "str":
            return Arr([ord(c) for c in t.text[1:-1]] + [0])
        if t.kind == "id":
            # call?
            if self._peek() is not None and self._peek().text == "(":
                end = _match(self.toks, self.i)
                args = _split_args(self.toks[self.i + 1:end])
                self.i = end + 1
                bound = self.env.get(t.text)
                if isinstance(bound, Sym) and bound.text.startswith("fn:"):   # call through a function-pointer variable
                    return self._call(bound.text[3:], None, args, t)
                return self._call(t.text, None, args, t)
            # namespaced name a::b
            if self._peek() is not None and self._peek().text == "::" and self._peek(1) is not None:
                self._next()
                nxt = self._next()
                if self._peek() is not None and self._peek().text == "(":
                    end = _match(self.toks, self.i)
                    args = _split_args(self.toks[self.i + 1:end])
                    self.i = end + 1
                    return self._call(nxt.text, t.text, args, nxt)
                return self._lookup(nxt.text)
            return self._lookup(t.text)
        if t.text == ")":
            return None
        return Sym(t.text)

    def _lookup(self, name: str) -> Any:
        if name in self.env:
            return self.env[name]
        if name in ("true",):
            return 1
        if name in ("false", "NULL", "nullptr"):
            return 0
        return self.m_const(name, 0)

    def m_const(self, name: str, depth: int) -> Any:
        if depth > 12:
            return Sym(name)
        mac = self.m.macros.get(name)
        if mac and mac.params is None:
            if not mac.body:
                return Sym(name)
            ev = Evaluator(self.m, {}, self.chain, None, True, self.file)
            ev._macro_depth = depth + 1
            v = ev.eval(mac.body)
            return v if v is not None else Sym(name)
        if name in self.m.consts:
            v = Evaluator(self.m, {}, self.chain, None, True, self.file).eval(self.m.consts[name])
            return v if v is not None else Sym(name)
        if name in self.m.global_arrays:
            v = Evaluator(self.m, {}, self.chain, None, True, self.file).eval(self.m.global_arrays[name])
            return v if isinstance(v, Arr) else Sym(name)
        if name in self.m.functions or name in _PRIMITIVES:
            return Sym(f"fn:{name}")   # a function used as a value (callback, function pointer)
        return Sym(name)

    def _postfix(self, v: Any) -> Any:
        while True:
            t = self._peek()
            if t is None:
                return v
            if t.text == "[":
                end = _match(self.toks, self.i, "[", "]")
                idx = Evaluator(self.m, self.env, self.chain, self.w, self.guaranteed, self.file).eval(self.toks[self.i + 1:end])
                self.i = end + 1
                if isinstance(v, Arr) and is_int(idx) and 0 <= idx < len(v.items):
                    v = v.items[idx]
                elif isinstance(v, Ptr) and is_int(idx):
                    items = v.get(None)
                    v = items[idx] if 0 <= idx < len(items) else Sym("ptr[]")
                else:
                    v = Sym(f"{v!r}[{idx!r}]")
                continue
            if t.text in (".", "->"):
                self._next()
                member = self._next()
                if self._peek() is not None and self._peek().text == "(":
                    end = _match(self.toks, self.i)
                    args = _split_args(self.toks[self.i + 1:end])
                    self.i = end + 1
                    v = self._call(member.text, v, args, member, arrow=t.text)
                    continue
                key = obj_key(v)
                if key is not None and self.w is not None and (key, member.text) in self.w.fields:
                    v = self.w.fields[(key, member.text)]
                elif key is not None:
                    v = Sym(f"{key}.{member.text}")
                else:
                    v = Sym(f"{v!r}{t.text}{member.text}")
                continue
            if t.text in ("++", "--"):
                self._next()
                continue
            if t.text == "(" and isinstance(v, Sym) and v.text.startswith("fn:"):
                end = _match(self.toks, self.i)
                args = _split_args(self.toks[self.i + 1:end])
                self.i = end + 1
                v = self._call(v.text[3:], None, args, t)
                continue
            return v

    def _call(self, name: str, obj: Any, arg_toks: List[List[Tok]], tok: Tok, arrow: str = "") -> Any:
        if self.w is None:
            helper = _CONST_HELPERS.get(name) if obj is None else None
            if helper is not None:  # constant helpers inside macro bodies: #define REG_CONFIG _u(0xF5)
                args = [Evaluator(self.m, self.env, self.chain, None, self.guaranteed, self.file).eval(a) for a in arg_toks]
                return helper(None, args, self, tok.line)
            return Sym(f"{name}()")
        return self.w.on_call(name, obj, arg_toks, self, tok.line, arrow)


# ------------------------------------------------------------------ statement walker


class Walker:
    def __init__(self, model: FlowModel):
        self.m = model
        self.events: List[Event] = []
        self.notes: List[str] = []
        self.opaque: set = set()
        self._wire: Dict[str, Tuple[Any, List[Any]]] = {}   # object -> (addr, bytes) for Arduino Wire transactions
        self._stack: List[str] = []
        self._sites: List[int] = []
        self.fields: Dict[Tuple[str, str], Any] = {}        # (object, field) -> value, e.g. ('the_sensor', 'write') -> fn
        self._frames: List[Dict[str, bool]] = []

    # ---------------------------------------------------------- uncertain control flow
    _MISSING = object()

    @staticmethod
    def _same(a: Any, b: Any) -> bool:
        if a is b:
            return True
        if type(a) is not type(b) or isinstance(a, (Arr, Ptr)):
            return False
        return a == b

    def _uncertain(self, bodies: List[List[Tok]], env: Dict[str, Any], chain, file: str, may_skip: bool, loop: bool = False) -> None:
        """Run bodies of which at most one (or, for a loop, an unknown number) executes. Afterwards a variable or struct
        field keeps its value only if every path agrees; otherwise it becomes unknown (a 'phi' of the paths)."""
        base_env, base_fields = dict(env), dict(self.fields)
        outcomes: List[Tuple[Dict[str, Any], Dict[Tuple[str, str], Any]]] = []
        for body in bodies:
            e = dict(base_env)
            self.fields = dict(base_fields)
            self._run(body, e, chain, False, file, loop=loop)
            outcomes.append((e, self.fields))
        if may_skip:
            outcomes.append((base_env, base_fields))
        merged_env: Dict[str, Any] = {}
        for k in set().union(*(o[0].keys() for o in outcomes)):
            vals = [o[0].get(k, self._MISSING) for o in outcomes]
            first = vals[0]
            merged_env[k] = first if all(self._same(first, v) for v in vals[1:]) and first is not self._MISSING else Sym(f"phi:{k}")
        merged_fields: Dict[Tuple[str, str], Any] = {}
        for k in set().union(*(o[1].keys() for o in outcomes)):
            vals = [o[1].get(k, self._MISSING) for o in outcomes]
            first = vals[0]
            merged_fields[k] = first if all(self._same(first, v) for v in vals[1:]) and first is not self._MISSING else Sym(f"phi:{k[0]}.{k[1]}")
        env.clear()
        env.update(merged_env)
        self.fields = merged_fields

    # ---------------------------------------------------------- functions
    def call(self, fn: Function, args: List[Any], chain: Tuple[str, ...], guaranteed: bool) -> Any:
        if len(self._stack) >= MAX_DEPTH or fn.name in self._stack:
            self.notes.append(f"stopped at {' > '.join(chain + (fn.name,))} (recursion/depth limit)")
            return Sym(f"{fn.name}()")
        env = {p: (args[k] if k < len(args) else Sym(p)) for k, p in enumerate(fn.params)}
        self._stack.append(fn.name)
        self._frames.append({"may_return": False})
        try:
            self.block(fn.body, env, chain + (fn.name,), guaranteed, fn.file)
            value = None
        except _Return as r:
            value = r.value
        finally:
            self._stack.pop()
            frame = self._frames.pop()
        # a return inside an uncertain branch means the caller cannot know which value comes back
        return Sym(f"{fn.name}()") if frame["may_return"] else value

    def block(self, toks: List[Tok], env: Dict[str, Any], chain: Tuple[str, ...], guaranteed: bool, file: str) -> None:
        i, n = 0, len(toks)
        while i < n and len(self.events) < MAX_EVENTS:
            i = self.statement(toks, i, env, chain, guaranteed, file)

    def statement(self, toks: List[Tok], i: int, env: Dict[str, Any], chain: Tuple[str, ...], guaranteed: bool, file: str) -> int:
        t = toks[i]
        if t.cond:
            guaranteed = False   # depends on a #if the analysis cannot decide
        ev = lambda g: Evaluator(self.m, env, chain, self, g, file)  # noqa: E731
        if t.text == "{":
            end = _match(toks, i, "{", "}")
            self.block(toks[i + 1:end], env, chain, guaranteed, file)
            return end + 1
        if t.text == ";":
            return i + 1
        if t.kind == "id" and t.text in ("case", "default"):
            j = i
            while j < len(toks) and toks[j].text != ":":
                j += 1
            return j + 1
        if t.kind == "id" and t.text == "if":
            end = _match(toks, i + 1)
            cond = ev(guaranteed).eval(toks[i + 2:end])
            j = end + 1
            then_end = self._stmt_end(toks, j)
            has_else = then_end < len(toks) and toks[then_end].text == "else"
            else_end = self._stmt_end(toks, then_end + 1) if has_else else then_end
            if is_int(cond):  # constant condition: only the live branch
                if cond:
                    self._run(toks[j:then_end], env, chain, guaranteed, file)
                elif has_else:
                    self._run(toks[then_end + 1:else_end], env, chain, guaranteed, file)
            else:
                bodies = [toks[j:then_end]] + ([toks[then_end + 1:else_end]] if has_else else [])
                self._uncertain(bodies, env, chain, file, may_skip=not has_else)
            return else_end
        if t.kind == "id" and t.text in ("while", "for", "switch"):
            end = _match(toks, i + 1)
            head = toks[i + 2:end]
            body_end = self._stmt_end(toks, end + 1)
            body = toks[end + 1:body_end]
            if t.text == "for":
                parts = self._split_semis(head)
                if parts and parts[0]:
                    self._expr_stmt(parts[0], env, chain, guaranteed, file)
                cond = ev(guaranteed).eval(parts[1]) if len(parts) > 1 and parts[1] else 1
                runs = bool(is_int(cond) and cond)
                if runs:
                    self._run(body, env, chain, guaranteed, file, loop=True)
                else:
                    self._uncertain([body], env, chain, file, may_skip=True, loop=True)
            elif t.text == "while":
                cond = ev(guaranteed).eval(head)
                if is_int(cond) and cond:          # while(1) { ... break; }: the body runs
                    self._run(body, env, chain, guaranteed, file, loop=True)
                elif not (is_int(cond) and cond == 0):
                    self._uncertain([body], env, chain, file, may_skip=True, loop=True)
            else:
                ev(guaranteed).eval(head)
                self._uncertain([body], env, chain, file, may_skip=True)
            return body_end
        if t.kind == "id" and t.text == "do":
            body_end = self._stmt_end(toks, i + 1)
            self._run(toks[i + 1:body_end], env, chain, guaranteed, file, loop=True)  # a do-body runs at least once
            j = body_end
            if j < len(toks) and toks[j].text == "while":
                end = _match(toks, j + 1)
                ev(False).eval(toks[j + 2:end])
                j = end + 1
            return j + 1 if j < len(toks) and toks[j].text == ";" else j
        if t.kind == "id" and t.text == "return":
            semi = self._semi(toks, i)
            val = ev(guaranteed).eval(toks[i + 1:semi]) if semi > i + 1 else None
            if guaranteed:
                raise _Return(val)
            if self._frames:
                self._frames[-1]["may_return"] = True
            return semi + 1
        if t.kind == "id" and t.text in ("break", "continue", "goto"):
            return self._semi(toks, i) + 1
        semi = self._semi(toks, i)
        self._expr_stmt(toks[i:semi], env, chain, guaranteed, file)
        return semi + 1

    def _run(self, toks: List[Tok], env, chain, guaranteed, file, loop: bool = False) -> None:
        try:
            self.block(toks, env, chain, guaranteed, file)
        except _Return:
            if guaranteed and not loop:
                raise

    @staticmethod
    def _semi(toks: List[Tok], i: int) -> int:
        depth = 0
        for j in range(i, len(toks)):
            x = toks[j].text
            if x in ("(", "[", "{"):
                depth += 1
            elif x in (")", "]", "}"):
                depth -= 1
                if depth < 0:
                    return j
            elif x == ";" and depth == 0:
                return j
        return len(toks)

    def _stmt_end(self, toks: List[Tok], j: int) -> int:
        """Index just past the statement starting at toks[j]."""
        if j >= len(toks):
            return j
        t = toks[j]
        if t.text == "{":
            return _match(toks, j, "{", "}") + 1
        if t.text == "if":
            end = _match(toks, j + 1)
            then_end = self._stmt_end(toks, end + 1)
            if then_end < len(toks) and toks[then_end].text == "else":
                return self._stmt_end(toks, then_end + 1)
            return then_end
        if t.text in ("while", "for", "switch"):
            end = _match(toks, j + 1)
            return self._stmt_end(toks, end + 1)
        if t.text == "do":
            b = self._stmt_end(toks, j + 1)
            if b < len(toks) and toks[b].text == "while":
                end = _match(toks, b + 1)
                return end + 2
            return b
        return self._semi(toks, j) + 1

    @staticmethod
    def _split_semis(toks: List[Tok]) -> List[List[Tok]]:
        out, cur, depth = [], [], 0
        for t in toks:
            if t.text in ("(", "[", "{"):
                depth += 1
            elif t.text in (")", "]", "}"):
                depth -= 1
            if t.text == ";" and depth == 0:
                out.append(cur)
                cur = []
            else:
                cur.append(t)
        out.append(cur)
        return out

    def _expr_stmt(self, toks: List[Tok], env: Dict[str, Any], chain, guaranteed: bool, file: str) -> None:
        """Declarations and expressions: track local variables and buffers, and evaluate calls in order."""
        if not toks:
            return
        texts = [t.text for t in toks]
        ev = Evaluator(self.m, env, chain, self, guaranteed, file)
        # declarations:  [qualifiers] type name [N] = init , name2 = init2 ;
        if self._is_decl(toks):
            decl_start = self._decl_names_start(toks)
            for part in _split_args(toks[decl_start:]):
                if not part:
                    continue
                pt = [x.text for x in part]
                name_tok = next((x for x in part if x.kind == "id" and x.text not in _TYPE_WORDS and x.text != "const"), None)
                if name_tok is None:
                    continue
                name = name_tok.text
                if "=" in pt:
                    eq = pt.index("=")
                    val = Evaluator(self.m, env, chain, self, guaranteed, file).eval(part[eq + 1:])
                    if "[" in pt[:eq] and is_int(val):
                        val = Arr([val])
                    if isinstance(val, Arr):
                        size = self._array_size(part[:eq], env)
                        if is_int(size) and size > len(val.items):
                            val.items.extend([0] * (size - len(val.items)))
                        val = Arr(list(val.items))  # each declaration owns its buffer
                    env[name] = val
                elif "[" in pt:
                    size = self._array_size(part, env)
                    env[name] = Arr([Sym(f"{name}[{k}]") for k in range(size if is_int(size) and size <= 1024 else 0)])
                else:
                    env[name] = Sym(name)
            return
        # assignment to a local: name = expr / name[idx] = expr / name op= expr
        for k, x in enumerate(texts):
            if x in Evaluator._ASSIGN and self._depth_at(toks, k) == 0:
                lhs, rhs = toks[:k], toks[k + 1:]
                val = Evaluator(self.m, env, chain, self, guaranteed, file).eval(rhs)
                if x != "=":
                    cur = Evaluator(self.m, env, chain, None, guaranteed, file).eval(lhs)
                    val = Evaluator._binop(x[:-1], cur, val)
                self._assign(lhs, val, env, chain, guaranteed, file)
                return
        ev.eval(toks)

    @staticmethod
    def _depth_at(toks: List[Tok], k: int) -> int:
        d = 0
        for t in toks[:k]:
            if t.text in ("(", "[", "{"):
                d += 1
            elif t.text in (")", "]", "}"):
                d -= 1
        return d

    def _assign(self, lhs: List[Tok], val: Any, env, chain, guaranteed, file) -> None:
        if len(lhs) == 1 and lhs[0].kind == "id":
            env[lhs[0].text] = val
            return
        if len(lhs) >= 4 and lhs[0].kind == "id" and lhs[1].text == "[" and lhs[-1].text == "]":
            arr = env.get(lhs[0].text)
            idx = Evaluator(self.m, env, chain, self, guaranteed, file).eval(lhs[2:-1])
            target = arr.arr if isinstance(arr, Ptr) else arr
            base = arr.offset if isinstance(arr, Ptr) else 0
            if isinstance(target, Arr) and is_int(idx):
                j = base + idx
                if 0 <= j < 4096:
                    while len(target.items) <= j:
                        target.items.append(Sym("uninit"))
                    target.items[j] = val
            return
        if lhs and lhs[0].text == "*" and len(lhs) == 2 and lhs[1].kind == "id":
            p = env.get(lhs[1].text)
            if isinstance(p, Ptr) and p.offset < len(p.arr.items):
                p.arr.items[p.offset] = val
            return
        # struct field:  base.f = v  /  base->f = v   (function pointers, interface selectors, addresses)
        k = len(lhs) - 2
        if k >= 1 and lhs[-1].kind == "id" and lhs[k].text in (".", "->") and self._depth_at(lhs, k) == 0:
            base = Evaluator(self.m, env, chain, self, guaranteed, file).eval(lhs[:k])
            if len(lhs[:k]) == 1 and lhs[0].kind == "id" and lhs[0].text not in env and isinstance(base, Sym):
                base = Obj(lhs[0].text)
            key = obj_key(base)
            if key is not None:
                self.fields[(key, lhs[-1].text)] = val
                return
        # anything else: evaluate for side effects (calls), value not tracked
        Evaluator(self.m, env, chain, self, guaranteed, file).eval(lhs)

    def _is_decl(self, toks: List[Tok]) -> bool:
        k = 0
        while k < len(toks) and toks[k].kind == "id" and toks[k].text in ("static", "const", "volatile", "register", "auto", "constexpr", "unsigned", "signed", "struct", "enum", "union"):
            k += 1
        if k >= len(toks) or toks[k].kind != "id":
            return False
        first = toks[k].text
        is_type = first in _TYPE_WORDS or bool(_TYPE_RE.match(first)) or first in self.m.types or first.endswith("_t")
        if not is_type:
            return False
        rest = toks[k + 1:]
        while rest and rest[0].text in ("*", "&", "const"):
            rest = rest[1:]
        return bool(rest) and rest[0].kind == "id"

    @staticmethod
    def _decl_names_start(toks: List[Tok]) -> int:
        k = 0
        while k < len(toks) and (toks[k].kind == "id" and (toks[k].text in _TYPE_WORDS or toks[k].text.endswith("_t") or bool(_TYPE_RE.match(toks[k].text))) or toks[k].text in ("*", "&")):
            k += 1
        # one more type name (typedef'd struct etc.) when followed by another identifier
        if k + 1 < len(toks) and toks[k].kind == "id" and toks[k + 1].kind == "id":
            k += 1
        while k < len(toks) and toks[k].text in ("*", "&"):
            k += 1
        return k

    def _array_size(self, toks: List[Tok], env) -> Any:
        texts = [t.text for t in toks]
        if "[" not in texts:
            return None
        lb = texts.index("[")
        rb = _match(toks, lb, "[", "]")
        if rb == lb + 1:
            return None
        return Evaluator(self.m, env, (), None).eval(toks[lb + 1:rb])

    # ---------------------------------------------------------- calls
    def on_call(self, name: str, obj: Any, arg_toks: List[List[Tok]], ev: Evaluator, line: int, arrow: str) -> Any:
        prim = _PRIMITIVES.get(name)
        args = [Evaluator(self.m, ev.env, ev.chain, self, ev.guaranteed, ev.file).eval(a) for a in arg_toks]
        if obj is not None:
            # a function pointer the walk saw being stored in this object's field: dev->write = &i2c_write
            key = obj_key(obj)
            fv = self.fields.get((key, name)) if key is not None else None
            if isinstance(fv, Sym) and fv.text.startswith("fn:"):
                fn = self.m.function(fv.text[3:])
                if fn is not None and fn.name not in self._stack:
                    return self._inline(fn, args, ev, line)
        if obj is not None and name in _WIRE_METHODS and not (name in ("write", "read") and arrow == "->" and len(args) >= 3):
            return self._wire_call(name, obj, args, ev, line)
        if obj is not None:
            # otherwise: a project function some code stores in a field of that name (vtable, Bosch-style callbacks) —
            # unless we are already inside one of them, in which case this call is the layer below (e.g. Adafruit BusIO)
            cands = self.m.fnptr_candidates(name)
            if cands and not any(self.m.function(c) is not None and self.m.function(c).name in self._stack for c in cands):
                fn = self.m.function(cands[0])
                if fn is not None:
                    if len(cands) > 1:
                        self.notes.append(f"{name}() could be {', '.join(cands)}; followed {cands[0]}")
                    return self._inline(fn, args, ev, line)
            first_is_buffer = bool(args) and isinstance(args[0], (Ptr, Arr))
            if name in ("write", "write_then_read", "read") and len(args) >= 2 and (first_is_buffer or name == "write_then_read"):
                return _BUSIO_STYLE(self, name, args, ev, line)   # Adafruit BusIO: write(buf, len, stop, prefix, prefix_len)
            if name in _FNPTR_STYLE and len(args) >= 2:
                return _FNPTR_STYLE[name](self, args, ev, line)   # Bosch callbacks: write(reg, data, len, intf_ptr)
            fn = self.m.function(name)
            if fn is not None and fn.params and len(fn.params) == len(args):
                return self._inline(fn, args, ev, line)
            self.opaque.add(f"{name}()")
            return Sym(f"{name}()")
        if prim is not None:
            return prim(self, args, ev, line)
        fn = self.m.function(name)
        if fn is not None:
            return self._inline(fn, args, ev, line)
        self.opaque.add(f"{name}()")
        return Sym(f"{name}()")

    def _inline(self, fn: Function, args: List[Any], ev: Evaluator, line: int) -> Any:
        self._sites.append(line)
        try:
            return self.call(fn, args, ev.chain, ev.guaranteed)
        finally:
            self._sites.pop()

    def emit(self, ev: Evaluator, line: int, **kw: Any) -> None:
        if len(self.events) < MAX_EVENTS:
            self.events.append(Event(file=ev.file, line=line, chain=ev.chain, guaranteed=ev.guaranteed, sites=tuple(self._sites), **kw))

    def _wire_call(self, name: str, obj: Any, args: List[Any], ev: Evaluator, line: int) -> Any:
        key = repr(obj)
        if name == "beginTransmission":
            self._wire[key] = (args[0] if args else None, [])
            return None
        if name == "write" and key in self._wire:
            addr, buf = self._wire[key]
            if len(args) >= 2:
                buf.extend(as_bytes(args[0], args[1]))
            elif args:
                v = args[0]
                buf.extend(v.items[:-1] if isinstance(v, Arr) and v.items and v.items[-1] == 0 else (as_bytes(v, None) if isinstance(v, (Arr, Ptr)) else [v]))
            return 1
        if name == "endTransmission":
            addr, buf = self._wire.pop(key, (None, []))
            self.emit(ev, line, kind="write", addr=addr, data=list(buf), length=len(buf), note="Wire")
            return 0
        if name == "requestFrom":
            self.emit(ev, line, kind="read", addr=args[0] if args else None, length=args[1] if len(args) > 1 else None, note="Wire")
            return args[1] if len(args) > 1 else Sym("n")
        return Sym(f"{name}()")


# ------------------------------------------------------------------ HAL primitives


def _us(v: Any, scale: int) -> Any:
    return int(v * scale) if isinstance(v, (int, float)) else (Sym(f"{v!r}*{scale}") if v is not None else None)


def _delay(scale: int):
    def h(w: Walker, a: List[Any], ev: Evaluator, line: int) -> Any:
        w.emit(ev, line, kind="delay", delay_us=_us(a[0] if a else None, scale))
        return None
    return h


def _write_buf(addr_i: int, buf_i: int, len_i: int, bus: str = "i2c", addr_shift: int = 0):
    def h(w: Walker, a: List[Any], ev: Evaluator, line: int) -> Any:
        addr = a[addr_i] if addr_i >= 0 and addr_i < len(a) else None
        if is_int(addr) and addr_shift:
            addr >>= addr_shift
        n = a[len_i] if len_i < len(a) else None
        w.emit(ev, line, kind="write", addr=addr, data=as_bytes(a[buf_i] if buf_i < len(a) else None, n), length=n, bus=bus)
        return n if is_int(n) else Sym("ret")
    return h


def taint(v: Any, n: Any = None) -> None:
    """A read fills its destination with runtime data: nothing about it is known at build time."""
    target, base = (v.arr, v.offset) if isinstance(v, Ptr) else ((v, 0) if isinstance(v, Arr) else (None, 0))
    if target is None:
        return
    end = len(target.items) if not is_int(n) else min(len(target.items), base + n)
    for k in range(base, end):
        target.items[k] = Sym("rx")


def _read_buf(addr_i: int, len_i: int, bus: str = "i2c", addr_shift: int = 0, dst_i: int = 2):
    def h(w: Walker, a: List[Any], ev: Evaluator, line: int) -> Any:
        addr = a[addr_i] if addr_i >= 0 and addr_i < len(a) else None
        if is_int(addr) and addr_shift:
            addr >>= addr_shift
        n = a[len_i] if len_i < len(a) else None
        if dst_i < len(a):
            taint(a[dst_i], n)
        w.emit(ev, line, kind="read", addr=addr, length=n, bus=bus)
        return n if is_int(n) else Sym("ret")
    return h


def _mem_write(addr_i: int, reg_i: int, buf_i: int, len_i: int, addr_shift: int = 0, bus: str = "i2c"):
    def h(w: Walker, a: List[Any], ev: Evaluator, line: int) -> Any:
        addr = a[addr_i] if addr_i >= 0 and addr_i < len(a) else None
        if is_int(addr) and addr_shift:
            addr >>= addr_shift
        n = a[len_i] if len_i < len(a) else None
        w.emit(ev, line, kind="write", addr=addr, reg=a[reg_i] if reg_i < len(a) else None,
               data=as_bytes(a[buf_i] if buf_i < len(a) else None, n), length=n, bus=bus, note="mem")
        return 0
    return h


def _mem_read(addr_i: int, reg_i: int, len_i: int, addr_shift: int = 0, bus: str = "i2c", dst_i: Optional[int] = None):
    def h(w: Walker, a: List[Any], ev: Evaluator, line: int) -> Any:
        addr = a[addr_i] if addr_i >= 0 and addr_i < len(a) else None
        if is_int(addr) and addr_shift:
            addr >>= addr_shift
        d = dst_i if dst_i is not None else (len_i - 1)
        if 0 <= d < len(a):
            taint(a[d], a[len_i] if len_i < len(a) else None)
        w.emit(ev, line, kind="read", addr=addr, reg=a[reg_i] if reg_i < len(a) else None,
               length=a[len_i] if len_i < len(a) else None, bus=bus, note="mem")
        return 0
    return h


def _reg_write_byte(addr_i: int, reg_i: int, val_i: int):
    def h(w: Walker, a: List[Any], ev: Evaluator, line: int) -> Any:
        addr = a[addr_i] if addr_i >= 0 and addr_i < len(a) else None
        w.emit(ev, line, kind="write", addr=addr, reg=a[reg_i] if reg_i < len(a) else None,
               data=[a[val_i] if val_i < len(a) else None], length=1, note="mem")
        return 0
    return h


def _write_read(addr_i: int, wbuf_i: int, wlen_i: int, rlen_i: int, bus: str = "i2c"):
    def h(w: Walker, a: List[Any], ev: Evaluator, line: int) -> Any:
        addr = a[addr_i] if addr_i >= 0 and addr_i < len(a) else None
        wn = a[wlen_i] if wlen_i < len(a) else None
        w.emit(ev, line, kind="write", addr=addr, data=as_bytes(a[wbuf_i] if wbuf_i < len(a) else None, wn), length=wn, bus=bus)
        if rlen_i - 1 < len(a):
            taint(a[rlen_i - 1], a[rlen_i] if rlen_i < len(a) else None)
        w.emit(ev, line, kind="read", addr=addr, length=a[rlen_i] if rlen_i < len(a) else None, bus=bus)
        return 0
    return h


def _spi_write_read(buf_i: int, len_i: int):
    def h(w: Walker, a: List[Any], ev: Evaluator, line: int) -> Any:
        n = a[len_i] if len_i < len(a) else None
        w.emit(ev, line, kind="write", data=as_bytes(a[buf_i] if buf_i < len(a) else None, n), length=n, bus="spi")
        return n
    return h


def _vtask_delay(w: Walker, a: List[Any], ev: Evaluator, line: int) -> Any:
    # vTaskDelay(pdMS_TO_TICKS(ms)) — pdMS_TO_TICKS is resolved by the caller as an opaque call returning its arg
    w.emit(ev, line, kind="delay", delay_us=_us(a[0] if a else None, 1000), note="ticks assumed 1 ms")
    return None


def _passthrough(w: Walker, a: List[Any], ev: Evaluator, line: int) -> Any:
    return a[0] if a else None


def _k_sleep(w: Walker, a: List[Any], ev: Evaluator, line: int) -> Any:
    w.emit(ev, line, kind="delay", delay_us=a[0] if a else None)
    return None


def _pure(fn):
    def h(w: "Walker", a: List[Any], ev: "Evaluator", line: int) -> Any:
        try:
            return fn(*a) if a and all(isinstance(x, (int, float)) for x in a) else Sym("expr")
        except (TypeError, ValueError, OverflowError):
            return Sym("expr")
    return h


# constant helpers from SDK headers that are not part of the project
_CONST_HELPERS = {
    "_u": _pure(lambda x, *r: x), "UINT8_C": _pure(lambda x, *r: x), "UINT16_C": _pure(lambda x, *r: x),
    "UINT32_C": _pure(lambda x, *r: x), "UINT64_C": _pure(lambda x, *r: x), "INT8_C": _pure(lambda x, *r: x),
    "INT16_C": _pure(lambda x, *r: x), "INT32_C": _pure(lambda x, *r: x), "INT64_C": _pure(lambda x, *r: x),
    "BIT": _pure(lambda n: 1 << int(n)), "_BV": _pure(lambda n: 1 << int(n)), "BIT_MASK": _pure(lambda n: (1 << int(n)) - 1),
    "GENMASK": _pure(lambda h, l: ((1 << (int(h) - int(l) + 1)) - 1) << int(l)),
    "FIELD_PREP": _pure(lambda mask, v: (int(v) << ((int(mask) & -int(mask)).bit_length() - 1)) & int(mask)),
}

_PRIMITIVES = {
    # pico-sdk
    "i2c_write_blocking": _write_buf(1, 2, 3), "i2c_write_timeout_us": _write_buf(1, 2, 3), "i2c_write_blocking_until": _write_buf(1, 2, 3),
    "i2c_write_timeout_per_char_us": _write_buf(1, 2, 3),
    "i2c_read_blocking": _read_buf(1, 3), "i2c_read_timeout_us": _read_buf(1, 3), "i2c_read_blocking_until": _read_buf(1, 3),
    "i2c_read_timeout_per_char_us": _read_buf(1, 3),
    "spi_write_blocking": _spi_write_read(1, 2), "spi_write_read_blocking": _spi_write_read(1, 3),
    "spi_read_blocking": lambda w, a, ev, line: (w.emit(ev, line, kind="read", length=a[3] if len(a) > 3 else None, bus="spi"), 0)[1],
    "sleep_ms": _delay(1000), "sleep_us": _delay(1), "busy_wait_us": _delay(1), "busy_wait_us_32": _delay(1), "busy_wait_ms": _delay(1000),
    # Arduino
    "delay": _delay(1000), "delayMicroseconds": _delay(1),
    # STM32 HAL (7-bit address passed shifted left by one)
    "HAL_I2C_Mem_Write": _mem_write(1, 2, 4, 5, addr_shift=1), "HAL_I2C_Mem_Read": _mem_read(1, 2, 5, addr_shift=1, dst_i=4),
    "HAL_I2C_Mem_Write_IT": _mem_write(1, 2, 4, 5, addr_shift=1), "HAL_I2C_Mem_Read_IT": _mem_read(1, 2, 5, addr_shift=1, dst_i=4),
    "HAL_I2C_Mem_Write_DMA": _mem_write(1, 2, 4, 5, addr_shift=1), "HAL_I2C_Mem_Read_DMA": _mem_read(1, 2, 5, addr_shift=1, dst_i=4),
    "HAL_I2C_Master_Transmit": _write_buf(1, 2, 3, addr_shift=1), "HAL_I2C_Master_Receive": _read_buf(1, 3, addr_shift=1, dst_i=2),
    "HAL_SPI_Transmit": _spi_write_read(1, 2), "HAL_SPI_TransmitReceive": _spi_write_read(1, 3),
    "HAL_Delay": _delay(1000), "osDelay": _delay(1000),
    # ESP-IDF
    "i2c_master_write_to_device": _write_buf(1, 2, 3), "i2c_master_read_from_device": _read_buf(1, 3),
    "i2c_master_write_read_device": _write_read(1, 2, 3, 5),
    "i2c_master_transmit": _write_buf(-1, 1, 2), "i2c_master_receive": _read_buf(-1, 2, dst_i=1),
    "i2c_master_transmit_receive": _write_read(-1, 1, 2, 4),
    "esp_rom_delay_us": _delay(1), "ets_delay_us": _delay(1), "vTaskDelay": _vtask_delay, "pdMS_TO_TICKS": _passthrough,
    # Zephyr
    "i2c_reg_write_byte": _reg_write_byte(1, 2, 3), "i2c_reg_write_byte_dt": _reg_write_byte(-1, 1, 2),
    "i2c_reg_read_byte": _mem_read(1, 2, 99), "i2c_reg_read_byte_dt": _mem_read(-1, 1, 99),
    "i2c_burst_read": _mem_read(1, 2, 4, dst_i=3), "i2c_burst_read_dt": _mem_read(-1, 1, 3, dst_i=2),
    "i2c_burst_write": _mem_write(1, 2, 3, 4), "i2c_burst_write_dt": _mem_write(-1, 1, 2, 3),
    "i2c_write": lambda w, a, ev, line: _write_buf(3, 1, 2)(w, a, ev, line) if len(a) >= 4 else _write_buf(-1, 1, 2)(w, a, ev, line),
    "i2c_write_dt": _write_buf(-1, 1, 2), "i2c_read_dt": _read_buf(-1, 2, dst_i=1),
    "i2c_write_read": _write_read(1, 2, 3, 5), "i2c_write_read_dt": _write_read(-1, 1, 2, 4),
    "k_msleep": _delay(1000), "k_usleep": _delay(1), "k_busy_wait": _delay(1), "k_sleep": _k_sleep,
    "K_MSEC": lambda w, a, ev, line: _us(a[0] if a else None, 1000), "K_USEC": lambda w, a, ev, line: a[0] if a else None,
    # Linux / generic
    "usleep": _delay(1), "msleep": _delay(1000), "mdelay": _delay(1000), "udelay": _delay(1), "delay_ms": _delay(1000), "delay_us": _delay(1),
}
_PRIMITIVES.update(_CONST_HELPERS)
_PRIMITIVES["i2c_reg_read_byte"] = lambda w, a, ev, line: (w.emit(ev, line, kind="read", addr=a[1] if len(a) > 1 else None, reg=a[2] if len(a) > 2 else None, length=1, note="mem"), 0)[1]
_PRIMITIVES["i2c_reg_read_byte_dt"] = lambda w, a, ev, line: (w.emit(ev, line, kind="read", reg=a[1] if len(a) > 1 else None, length=1, note="mem"), 0)[1]

_WIRE_METHODS = {"beginTransmission", "write", "endTransmission", "requestFrom"}


def _fnptr_write(w: Walker, a: List[Any], ev: Evaluator, line: int) -> Any:
    # Bosch API: write(reg_addr, const uint8_t *data, uint32_t len, void *intf_ptr)
    n = a[2] if len(a) > 2 else None
    w.emit(ev, line, kind="write", reg=a[0], data=as_bytes(a[1], n), length=n, note="fnptr")
    return 0


def _fnptr_read(w: Walker, a: List[Any], ev: Evaluator, line: int) -> Any:
    if len(a) > 1:
        taint(a[1], a[2] if len(a) > 2 else None)
    w.emit(ev, line, kind="read", reg=a[0], length=a[2] if len(a) > 2 else None, note="fnptr")
    return 0


def _fnptr_delay_us(w: Walker, a: List[Any], ev: Evaluator, line: int) -> Any:
    w.emit(ev, line, kind="delay", delay_us=a[0] if a else None, note="fnptr")
    return None


_FNPTR_STYLE = {"write": _fnptr_write, "read": _fnptr_read, "delay_us": _fnptr_delay_us,
                "delay_ms": lambda w, a, ev, line: (w.emit(ev, line, kind="delay", delay_us=_us(a[0] if a else None, 1000), note="fnptr"), None)[1]}


def _BUSIO_STYLE(w: Walker, name: str, a: List[Any], ev: Evaluator, line: int) -> Any:
    """Adafruit_BusIO: dev->write(buf, len, stop, prefix, prefix_len), dev->write_then_read(wbuf, wlen, rbuf, rlen), dev->read(buf, len)."""
    if name == "write":
        data = as_bytes(a[0], a[1] if len(a) > 1 else None)
        if len(a) >= 5:
            data = as_bytes(a[3], a[4]) + data
        w.emit(ev, line, kind="write", data=data, length=len(data), note="busio")
    elif name == "write_then_read":
        w.emit(ev, line, kind="write", data=as_bytes(a[0], a[1] if len(a) > 1 else None), length=a[1] if len(a) > 1 else None, note="busio")
        w.emit(ev, line, kind="read", length=a[3] if len(a) > 3 else None, note="busio")
    else:
        w.emit(ev, line, kind="read", length=a[1] if len(a) > 1 else None, note="busio")
    return 1


# ------------------------------------------------------------------ normalisation


def normalize(events: List[Event]) -> List[Event]:
    """Raw bus transfers -> register operations.

    * a write of [reg, data...] is a register write;
    * a write of [reg] immediately followed by a read of the same device is a register read;
    * on SPI, bit 7 of the first byte marks a read (Bosch convention) and the register is the low 7 bits.
    """
    out: List[Event] = []
    i = 0
    while i < len(events):
        e = events[i]
        if e.kind == "write" and e.reg is None:
            first = e.data[0] if e.data else None
            rest = e.data[1:]
            nxt = events[i + 1] if i + 1 < len(events) else None
            spi_read = e.bus == "spi" and is_int(first) and first & 0x80
            if (len(e.data) == 1 or spi_read) and nxt is not None and nxt.kind == "read" and nxt.reg is None and (nxt.addr == e.addr or nxt.addr is None or e.addr is None):
                reg = first & 0x7F if spi_read else first
                out.append(Event("read", addr=e.addr, reg=reg, length=nxt.length, file=e.file, line=e.line, chain=e.chain,
                                 guaranteed=e.guaranteed and nxt.guaranteed, bus=e.bus, note=e.note, sites=e.sites))
                i += 2
                continue
            if spi_read:
                out.append(Event("read", addr=e.addr, reg=first & 0x7F, length=max(0, len(e.data) - 1), file=e.file, line=e.line,
                                 chain=e.chain, guaranteed=e.guaranteed, bus=e.bus, note=e.note, sites=e.sites))
                i += 1
                continue
            reg = first & 0x7F if (e.bus == "spi" and is_int(first)) else first
            out.append(Event("write", addr=e.addr, reg=reg, data=rest, length=len(rest), file=e.file, line=e.line,
                             chain=e.chain, guaranteed=e.guaranteed, bus=e.bus, note=e.note, sites=e.sites))
            i += 1
            continue
        out.append(e)
        i += 1
    return out


def writes_of(e: Event) -> List[Tuple[Any, Any]]:
    """(register, value) pairs a write event can stand for: consecutive (auto-increment) and interleaved reg/value pairs."""
    pairs: List[Tuple[Any, Any]] = []
    if e.kind != "write":
        return pairs
    for k, v in enumerate(e.data):
        pairs.append(((e.reg + k) if is_int(e.reg) else e.reg, v))
    if len(e.data) >= 3 and len(e.data) % 2 == 1:  # interleaved: reg0 v0 reg1 v1 ... (Bosch burst write)
        pairs.append((e.reg, e.data[0]))
        for k in range(1, len(e.data) - 1, 2):
            pairs.append((e.data[k], e.data[k + 1]))
    return pairs
