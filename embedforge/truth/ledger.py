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
    return re.sub(r"[\s\"'`´]+", "", s).lower()


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
    for form, rx, conf in value_variants(value):
        if re.search(rx, low):
            return form, conf
    return None


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
        if _squash(quote) not in _squash(src.text):
            return {
                "ok": False,
                "error": (
                    f"REJECTED: quote does not appear verbatim in {source_id}. Facts must be copied "
                    "exactly from the source text (use read_source to get exact text)."
                ),
            }
        match = value_in_text(value, quote)
        if match is None:
            return {
                "ok": False,
                "error": f"REJECTED: value '{value}' does not appear inside the quoted text, so the quote does not support it.",
            }
        form, conf = match
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
