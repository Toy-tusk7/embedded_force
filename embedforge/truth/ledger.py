"""Fact Ledger: the deterministic gate that makes 'zero hallucination' enforceable.

A fact can only enter the ledger if:
  1. it cites a source that exists in the Source Store,
  2. its `quote` appears VERBATIM in that source's text (whitespace/case-insensitive), and
  3. its `value` appears INSIDE that quote.

The LLM therefore cannot create a fact from its own weights: it has to point at real text.
"""

import json
import re
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from embedforge.truth.source_store import SourceStore
from embedforge.truth.trust import TrustTier, is_authoritative

MIN_QUOTE_CHARS = 12
MAX_QUOTE_CHARS = 600

_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212"), "-")
_QUOTES = {ord("\u2018"): "'", ord("\u2019"): "'", ord("\u201c"): '"', ord("\u201d"): '"'}


def _squash(s: str) -> str:
    """Whitespace-free, case-folded form used for robust verbatim-quote containment."""
    s = s.replace("\u00ad", "").replace("\ufb01", "fi").replace("\ufb02", "fl").replace("\u00b5", "\u03bc")   # micro sign = Greek mu
    s = s.translate(_DASHES).translate(_QUOTES)
    # Markdown table/emphasis markup from the PDF converter ('| Bit 5..4 | mode |', '**Table 40**', '<br>') is layout,
    # not text: a quote matches with or without it
    s = re.sub(r"</?(?:mark|u|sup|sub|b|i|em|strong)>|<br\s*/?>|\*\*|[|`^]", " ", s)   # 2<sup>osr_p</sup> = 2^osr_p
    # quote marks carry no meaning and PDFs use curly ones the model often drops: Register 0x1B “PWR_CTRL”
    # (final sigma: 'ΔΣ'.lower() is 'δς' but 'Σ'.lower() is 'σ' — one form, so a text squashes the same in pieces)
    return re.sub(r"[\s\"'`´]+", "", s).lower().replace("ς", "σ")


_MARKUP = re.compile(r"</?(?:mark|u|sup|sub|b|i|em|strong)>|<br\s*/?>|\*\*|[|`^]")
_DROP = re.compile(r"[\s\"'`´]")
_PRE = {ord("­"): "", ord("ﬁ"): "fi", ord("ﬂ"): "fl", ord("µ"): "μ", **_DASHES, **_QUOTES}


def _squash_map(s: str) -> Tuple[str, List[int]]:
    """_squash(s), and for each of its characters the index in s it came from (used on small pieces of a source)."""
    chars: List[Tuple[str, int]] = []
    for i, c in enumerate(s):
        chars.extend((r, i) for r in c.translate(_PRE))
    t = "".join(c for c, _ in chars)
    kept: List[Tuple[str, int]] = []
    pos = 0
    for m in _MARKUP.finditer(t):
        kept.extend(chars[pos:m.start()])
        pos = m.end()
    kept.extend(chars[pos:])
    out: List[Tuple[str, int]] = []
    for c, i in kept:
        if not _DROP.match(c):
            out.extend((x, i) for x in c.lower().replace("ς", "σ"))
    return "".join(c for c, _ in out), [i for _, i in out]


class _Index:
    """A source text in squashed form, and the way back from a squashed span to the original text. The text is
    squashed in line-aligned pieces, so the per-character map is only built for the piece a match falls in: a 5 MB
    reference manual costs one fast _squash, not a 5-million-step Python loop and a 190 MB index."""

    PIECE = 4000

    def __init__(self, text: str):
        starts, i = [0], self.PIECE
        while i < len(text):
            j = text.find("\n", i)
            if j < 0:
                break
            starts.append(j + 1)
            i = j + 1 + self.PIECE
        # pieces end at line breaks, which no squashed markup spans, so the pieces squash like the whole text
        pieces = [_squash(text[a:b]) for a, b in zip(starts, starts[1:] + [len(text)])]
        self.text, self.s = text, "".join(pieces)
        self.orig = starts + [len(text)]
        self.sq = [0]
        for p in pieces:
            self.sq.append(self.sq[-1] + len(p))
        self._maps: Dict[Tuple[int, int], List[int]] = {}

    def span(self, i: int, j: int) -> Optional[Tuple[int, int]]:
        """Original (start, end) of squashed characters i..j-1."""
        import bisect
        k = bisect.bisect_right(self.sq, i) - 1
        m = bisect.bisect_right(self.sq, j - 1) - 1
        key = (k, m)
        if key not in self._maps:
            s, idx = _squash_map(self.text[self.orig[k]:self.orig[m + 1]])
            self._maps[key] = idx if s == self.s[self.sq[k]:self.sq[m + 1]] else []
        idx = self._maps[key]
        if not idx:
            return None
        return self.orig[k] + idx[i - self.sq[k]], self.orig[k] + idx[j - 1 - self.sq[k]] + 1


_INDEXES: "Dict[str, _Index]" = {}


def _index(text: str) -> _Index:
    hit = _INDEXES.pop(text, None)   # keyed by the text itself (equality, not just hash); most recent last
    if hit is None:
        hit = _Index(text)
        while len(_INDEXES) >= 8:
            _INDEXES.pop(next(iter(_INDEXES)))
    _INDEXES[text] = hit
    return hit


def _word(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def bounded(text: str, a: int, b: int, first: str, last: str) -> bool:
    """A match a..b of a quote whose squashed form starts with `first` and ends with `last` does not begin or end inside
    a word or number of the text."""
    return not (_word(first) and a > 0 and _word(text[a - 1])) and not (_word(last) and b < len(text) and _word(text[b]))


def anchor(quote: str, text: str) -> Optional[Tuple[int, int]]:
    """Where a quote stands in the source text: (start, end) of the first occurrence that begins and ends on word
    boundaries — '2 ms after power-on' is not a quote of 'wait 12 ms after power-on'. None when it is not there."""
    q = _squash(quote)
    if not q:
        return None
    ix = _index(text)
    pos = ix.s.find(q)
    while pos >= 0:
        sp = ix.span(pos, pos + len(q))
        if sp is not None and bounded(text, sp[0], sp[1], q[0], q[-1]):
            return sp
        pos = ix.s.find(q, pos + 1)
    return None


_UNIT_NAMES = {"s": "s", "sec": "s", "second": "s", "seconds": "s", "ms": "ms", "msec": "ms", "millisecond": "ms",
               "milliseconds": "ms", "us": "us", "µs": "us", "μs": "us", "usec": "us", "microsecond": "us", "microseconds": "us",
               "ns": "ns", "nanoseconds": "ns", "hz": "Hz", "khz": "kHz", "mhz": "MHz", "v": "V", "mv": "mV", "a": "A", "ma": "mA",
               "µa": "uA", "μa": "uA", "ua": "uA", "°c": "°C", "c": "°C"}
_UNIT_AFTER = re.compile(r"^[\s|]{0,4}(µs|μs|us|ms|ns|sec|s|[kKM]?Hz|mV|V|mA|[µμu]A|A|°C)(?![A-Za-z])")


def norm_unit(unit: str) -> str:
    u = (unit or "").strip()
    if u in ("MHz", "mHz"):   # mega vs milli: case matters here
        return "MHz" if u == "MHz" else "mHz"
    return _UNIT_NAMES.get(u.lower(), u)


TIME_UNITS = ("s", "ms", "us", "ns")


def units_after(text: str, value: str) -> List[str]:
    """The units written right after each occurrence of the value in text ('2 ms', '| 2 | ms |'), normalised, in order:
    a row can state the same number twice ('VDD = 3 V | 3 | ms')."""
    out: List[str] = []
    for form, rx, _conf in value_variants(value):
        if form not in ("number", "decimal"):
            continue
        for m in re.finditer(rx, text.lower().translate(_DASHES)):
            u = _UNIT_AFTER.match(text[m.end():m.end() + 12])
            if u and norm_unit(u.group(1)) not in out:
                out.append(norm_unit(u.group(1)))
    return out


def unit_after(text: str, value: str, kinds: Optional[Tuple[str, ...]] = None) -> Optional[str]:
    """The first unit written right after the value (of the given kinds, e.g. TIME_UNITS), or None."""
    return next((u for u in units_after(text, value) if kinds is None or u in kinds), None)


def parse_int(value: str) -> Optional[int]:
    v = str(value).strip().lower().replace("_", "")
    try:
        if v.startswith("0x"):
            return int(v, 16)
        if v.startswith("0b"):
            return int(v, 2)
        if re.fullmatch(r"-?\d+", v):
            return int(v, 10)
    except ValueError:
        return None
    return None


def value_variants(value: str) -> List[Tuple[str, str, str]]:
    """Regexes (form, regex, confidence) under which `value` may legitimately appear in a datasheet."""
    v = str(value).strip()
    n = parse_int(v)
    out: List[Tuple[str, str, str]] = []
    if n is not None and v.lower().startswith("0x"):
        h = format(n, "x")
        out.append(("hex-prefixed", rf"(?<![0-9a-z])0x0*{h}(?![0-9a-z])", "STRICT"))
        out.append(("hex-suffix", rf"(?<![0-9a-z]){h}h(?![0-9a-z])", "STRICT"))
        out.append(("hex-quoted", rf"h'0*{h}'", "STRICT"))
        out.append(("hex-bare", rf"(?<![0-9a-z.]){h.zfill(2)}(?![0-9a-z.])", "LOOSE"))
        out.append(("decimal", rf"(?<![0-9a-z.]){n}(?![0-9a-z.])", "LOOSE"))
        if n > 0:
            b = format(n, "b")
            out.append(("binary", rf"(?<![0-9a-z])(?:0b|b)?{b}b?(?![0-9a-z])", "LOOSE"))
    elif n is not None:
        out.append(("number", rf"(?<![0-9a-z.]){n}(?![0-9]|\.\d)", "STRICT"))
    else:
        try:
            float(v)
            out.append(("number", rf"(?<![0-9a-z]){re.escape(v)}(?![0-9])", "STRICT"))
        except ValueError:
            if re.fullmatch(r"(?i)(gpio|gp|p[a-k])\d+", v):  # pin names: GPIO4 must not match inside GPIO41
                out.append(("pin", rf"(?<![0-9a-z]){re.escape(v.lower())}(?![0-9])", "STRICT"))
            else:
                out.append(("text", re.escape(v.lower()), "STRICT"))
    return out


def value_in_text(value: str, text: str) -> Optional[Tuple[str, str]]:
    low = text.lower().translate(_DASHES)
    negative = str(value).strip().startswith("-")
    for form, rx, conf in value_variants(value):
        for m in re.finditer(rx, low):
            k = m.start()
            # '-40 °C' states minus forty, not 40; a range ('1-40', '0x00-0x7F', '1.71 V -3.6 V') still states 40 / 3.6
            if not negative and k > 0 and low[k - 1] == "-" and not _RANGE_BEFORE.search(low[max(0, k - 14):k - 1]):
                continue
            return form, conf
    return None


_RANGE_BEFORE = re.compile(r"(?:\d|0x[0-9a-f]+)\s*(?:[a-zµμ°%]{1,3})?\s*$")


@dataclass
class Fact:
    id: str
    subject: str
    property: str
    value: str
    unit: str
    source_id: str
    source_url: str
    tier: str
    quote: str
    match_form: str
    confidence: str  # STRICT | LOOSE
    authoritative: bool

    def public(self) -> Dict[str, Any]:
        return asdict(self)


class FactLedger:
    def __init__(self, root: Path, store: SourceStore):
        self.path = Path(root) / "ledger.json"
        self.store = store
        self._lock = threading.Lock()
        self.facts: Dict[str, Fact] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                for item in json.loads(self.path.read_text(encoding="utf-8")):
                    f = Fact(**item)
                    self.facts[f.id] = f
            except Exception:
                pass

    def _save(self) -> None:
        self.path.write_text(json.dumps([f.public() for f in self.facts.values()], indent=2), encoding="utf-8")

    @property
    def _retired_path(self) -> Path:
        return self.path.with_name("ledger_retired.json")

    def _next_id(self) -> str:
        """Ids are never reused, even after a fact is retired."""
        ids = list(self.facts)
        if self._retired_path.exists():
            try:
                ids += [r.get("id", "") for r in json.loads(self._retired_path.read_text(encoding="utf-8"))]
            except Exception:
                pass
        nums = [int(i.split("-")[1]) for i in ids if re.fullmatch(r"FACT-\d+", i)]
        return f"FACT-{max(nums + [0]) + 1:03d}"

    def retire(self, fact_id: str, reason: str) -> bool:
        """Take a fact out of use (e.g. a pin recorded under the wrong define). It stays on record in ledger_retired.json;
        code still citing it then fails verify with UNKNOWN_FACT_TAG."""
        with self._lock:
            f = self.facts.pop(fact_id, None)
            if f is None:
                return False
            retired = []
            if self._retired_path.exists():
                try:
                    retired = json.loads(self._retired_path.read_text(encoding="utf-8"))
                except Exception:
                    retired = []
            retired.append({**f.public(), "retired_reason": reason})
            self._retired_path.write_text(json.dumps(retired, indent=2), encoding="utf-8")
            self._save()
            return True

    # ---------- recording ----------
    def record(
        self, subject: str, prop: str, value: Any, source_id: str, quote: str, unit: str = ""
    ) -> Dict[str, Any]:
        """Validate and record a fact. Returns {ok, fact|error, warnings}."""
        value = str(value).strip()
        quote = (quote or "").strip()
        subject = (subject or "").strip()
        prop = (prop or "").strip()
        if not (subject and prop and value):
            return {"ok": False, "error": "subject, property and value are all required."}
        src = self.store.get(source_id)
        if src is None:
            return {"ok": False, "error": f"Unknown source_id '{source_id}'. Use web_search/fetch_document first."}
        if len(_squash(quote)) < MIN_QUOTE_CHARS:
            return {"ok": False, "error": f"Quote too short (<{MIN_QUOTE_CHARS} chars). Quote the full sentence/table row."}
        if len(quote) > MAX_QUOTE_CHARS:
            return {"ok": False, "error": f"Quote too long (>{MAX_QUOTE_CHARS} chars). Quote only the relevant row/sentence."}
        span = anchor(quote, src.text)
        if span is None:
            if _squash(quote) in _squash(src.text):
                return {"ok": False, "error": (f"REJECTED: the quote starts or ends inside a word or number of {source_id} (e.g. "
                                               "'2 ms' inside '12 ms'). Quote whole words and numbers.")}
            return {
                "ok": False,
                "error": (
                    f"REJECTED: quote does not appear verbatim in {source_id}. Facts must be copied "
                    "exactly from the source text (use read_source to get exact text)."
                ),
            }
        stated_text = src.text[span[0]:span[1]]   # the value is checked in the source's own text, not the model's copy
        match = value_in_text(value, stated_text)
        if match is None:
            return {
                "ok": False,
                "error": f"REJECTED: value '{value}' does not appear inside the quoted text, so the quote does not support it.",
            }
        form, conf = match
        stated_units = units_after(stated_text, value) if unit else []
        if stated_units and norm_unit(unit) not in stated_units and norm_unit(unit) in set(_UNIT_NAMES.values()) | {"mHz"}:
            return {"ok": False, "error": f"REJECTED: the quote gives {value} {stated_units[0]}, not {value} {unit}."}
        tier = TrustTier(src.tier)
        warnings: List[str] = []
        if not is_authoritative(tier):
            warnings.append(f"Source {source_id} is {tier.value}; fact is recorded but NOT authoritative.")
        if conf == "LOOSE":
            warnings.append(f"Value matched loosely ({form}); double-check the register/number meaning in the quote.")

        with self._lock:
            for ex in self.facts.values():
                if ex.subject.lower() == subject.lower() and ex.property.lower() == prop.lower():
                    if ex.value.lower() == value.lower() and ex.source_id == source_id:
                        return {"ok": True, "fact": ex.public(), "warnings": ["Duplicate; existing fact returned."]}
                    if str(ex.value).lower() != value.lower():
                        warnings.append(f"CONFLICT with {ex.id} ({ex.value}) for the same property.")
            fid = self._next_id()
            fact = Fact(
                id=fid, subject=subject, property=prop, value=value, unit=unit, source_id=source_id,
                source_url=src.url, tier=tier.value, quote=quote, match_form=form, confidence=conf,
                authoritative=is_authoritative(tier),
            )
            self.facts[fid] = fact
            self._save()
        return {"ok": True, "fact": fact.public(), "warnings": warnings}

    # ---------- queries ----------
    def get(self, fact_id: str) -> Optional[Fact]:
        return self.facts.get(fact_id)

    def find(self, subject: str = "", prop: str = "") -> List[Fact]:
        s, p = subject.lower(), prop.lower()
        return [f for f in self.facts.values() if (not s or s in f.subject.lower()) and (not p or p in f.property.lower())]

    def conflicts(self) -> List[Tuple[Fact, Fact]]:
        seen: Dict[Tuple[str, str], Fact] = {}
        out = []
        for f in self.facts.values():
            key = (f.subject.lower(), f.property.lower())
            if key in seen and seen[key].value.lower() != f.value.lower():
                out.append((seen[key], f))
            seen.setdefault(key, f)
        return out

    def int_values(self) -> Dict[int, List[str]]:
        """Map integer value -> fact ids (for literal justification in the verifier)."""
        m: Dict[int, List[str]] = {}
        for f in self.facts.values():
            n = parse_int(f.value)
            if n is not None:
                m.setdefault(n, []).append(f.id)
        return m
