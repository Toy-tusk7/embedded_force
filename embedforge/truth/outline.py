"""A datasheet as its numbered sections, what the agent has read of it, and which sections the code depends on.

Keyword search finds the passages a query names, not the ones the code relies on: in a BMP388 rehearsal the agent read
38% of the datasheet and never opened Normal mode, Data ready or Measurement time, where the two bugs its driver kept
were described. Here the datasheet is split into its sections (read whole with read_section), every passage shown to the
agent is logged, and the sections the code's own behaviour points at (the modes it sets, the data it reads, the bus it
uses, the formulas it applies) are required reading: verify reports those never read."""

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class Section:
    num: str      # '3.3.3' (a Markdown heading without a number: '§4')
    title: str
    start: int    # offset of the heading in the source text
    end: int      # offset of the next heading

    @property
    def label(self) -> str:
        return f"{self.num} {self.title}"


_NUM_HEAD = re.compile(r"(?m)^[ \t]*(?:#{1,4}[ \t]+)?(\d{1,2}(?:\.\d{1,2}){0,4})\.?[ \t]+([A-Z][^\n]{2,90}?)[ \t]*$")
_MD_HEAD = re.compile(r"(?m)^[ \t]*#{1,4}[ \t]+(\S[^\n]{2,90}?)[ \t#]*$")
_TOC_LINE = re.compile(r"\.{4,}|…{2,}|\s\d{1,3}\s*$")


def _successor(prev: Tuple[int, ...], cur: Tuple[int, ...]) -> bool:
    """Can heading `cur` follow heading `prev`? A child (3.3 -> 3.3.1), a later sibling or ancestor's sibling (3.3.2 ->
    3.3.3, 3.4, 4), skipping at most two missed headings, then descending only through first children (3.6.5.2 -> 3.6.6.1)."""
    k = next((i for i in range(min(len(prev), len(cur))) if prev[i] != cur[i]), min(len(prev), len(cur)))
    if k == len(cur):
        return False                       # cur is prev itself or an ancestor of it
    if k == len(prev):
        return cur[k] == 1 and all(c == 1 for c in cur[k + 1:])
    return 1 <= cur[k] - prev[k] <= 3 and all(c == 1 for c in cur[k + 1:])


def sections(text: str) -> List[Section]:
    """The document's sections in order. Numbered headings are chained so each follows the one before (a table row or a
    numbered step in the body that looks like a heading does not break the chain); a table of contents loses to the body
    because chains are weighted by the text under each heading. Markdown documents without numbers use their # headings."""
    cands = []
    for m in _NUM_HEAD.finditer(text):
        title = m.group(2).strip()
        if _TOC_LINE.search(title) or title.endswith((".", ",", ";", ":")) and len(title) > 40:
            continue
        cands.append((m.start(), tuple(int(x) for x in m.group(1).split(".")), title, m.group(1)))
    if cands:
        n = len(cands)
        weight = [min((cands[i + 1][0] if i + 1 < n else len(text)) - cands[i][0], 3000) for i in range(n)]
        best = [0.0] * n
        back = [-1] * n
        for i in range(n):
            starts = cands[i][1][0] <= 2 and all(c == 1 for c in cands[i][1][1:])
            best[i] = weight[i] if starts else float("-inf")
            for j in range(max(0, i - 400), i):
                if best[j] > float("-inf") and _successor(cands[j][1], cands[i][1]) and best[j] + weight[i] > best[i]:
                    best[i], back[i] = best[j] + weight[i], j
        i = max(range(n), key=lambda x: best[x])
        chain = []
        while i >= 0 and best[i] > float("-inf"):
            chain.append(cands[i])
            i = back[i]
        chain.reverse()
        if len(chain) >= 4:
            return [Section(c[3], c[2], c[0], chain[k + 1][0] if k + 1 < len(chain) else len(text)) for k, c in enumerate(chain)]
    heads = [(m.start(), m.group(1).strip()) for m in _MD_HEAD.finditer(text)]
    return [Section(f"§{k + 1}", t, s, heads[k + 1][0] if k + 1 < len(heads) else len(text)) for k, (s, t) in enumerate(heads)]


def find_section(secs: List[Section], key: str) -> Optional[Section]:
    k = str(key or "").strip().rstrip(".").lower()
    k = re.sub(r"^(section|chapter|sec\.?)\s*", "", k)
    for s in secs:
        if s.num.lower() == k:
            return s
    hits = [s for s in secs if k and k in s.title.lower()]
    return min(hits, key=lambda s: len(s.title)) if hits else None


def subtree_end(secs: List[Section], sec: Section) -> int:
    """End of a section including its subsections (3.3 runs to the start of 3.4)."""
    i = secs.index(sec)
    for s in secs[i + 1:]:
        if not s.num.startswith(sec.num + ".") or sec.num.startswith("§"):
            return s.start
    return secs[-1].end


# ------------------------------------------------------------------ requirement sentences
_NORMATIVE = re.compile(r"\b(must|shall|should|required|requires?|recommended|strongly|has to|have to|needs? to|only|never|"
                        r"do not|not allowed|prohibited|before|after|prior to|until|wait|at least|at most|minimum|maximum|"
                        r"ensure|valid|invalid|error)\b", re.I)


def requirements(text: str, limit: int = 12) -> List[str]:
    """Sentences of a section that state a rule (must, should, before, wait, at least, only...)."""
    flat = re.sub(r"[ \t]*\n[ \t]*(?=[a-z(])", " ", text)            # rejoin lines a PDF wrapped mid-sentence
    out = []
    for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z])|\n{2,}|\n(?=[•\-–]\s)", flat):
        s = re.sub(r"\s+", " ", sent).strip(" •-–")
        if 25 <= len(s) <= 400 and _NORMATIVE.search(s) and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ what the agent has read
class ReadLog:
    """Spans of each source shown to the agent (read_source excerpts, read_section), kept with the project so coverage
    adds up across runs."""

    def __init__(self, root: Path):
        self.path = Path(root) / "sources" / "reads.json"
        self._lock = threading.Lock()
        try:
            self.spans: Dict[str, List[List[int]]] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.spans = {}

    def add(self, source_id: str, a: int, b: int) -> None:
        if b <= a:
            return
        with self._lock:
            spans = sorted(self.spans.get(source_id, []) + [[int(a), int(b)]])
            merged: List[List[int]] = []
            for s, e in spans:
                if merged and s <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], e)
                else:
                    merged.append([s, e])
            self.spans[source_id] = merged
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.spans), encoding="utf-8")

    def seen(self, source_id: str, a: int, b: int) -> float:
        """Share of a..b shown so far."""
        if b <= a:
            return 1.0
        got = sum(max(0, min(e, b) - max(s, a)) for s, e in self.spans.get(source_id, []))
        return got / (b - a)


# ------------------------------------------------------------------ required reading
_TOPICS = {   # topic -> section titles about it
    "reset": r"power[- ]?(up|on)|start[- ]?up|\breset\b|power management|power sequenc|initiali[sz]ation",
    "modes": r"\bmodes?\b",
    "timing": r"measurement (time|timing|rate)|conversion (time|rate)|output data rate|\bodr\b|sampling (rate|period)",
    "ready": r"data[- ]?ready|\bdrdy\b|new data|status",
    "readout": r"read[- ]?out|data registers?|shadowing|burst",
    "formulas": r"compensat|calibration|trimming|formula|conversion of|output scaling",
    "oversampling": r"oversampl",
    "filter": r"\bfilter|\biir\b",
    "i2c": r"\bI\s?(2|²)\s?C\b",
    "spi": r"\bSPI\b",
}


# features whose sections matter only when the code uses them
_UNLESS_USED = {"fifo": r"\bFIFO\b", "interrupts": r"interrupt|\bINT_|watermark"}


def required(secs: List[Section], uses: Dict[str, str]) -> List[Tuple[Section, str]]:
    """Sections the code depends on, with why. `uses`: topic -> why (e.g. {'modes': 'the code sets PWR_CTRL.mode =
    normal'}). Two lists ride along as comma-separated values: uses['mode_words'] narrows mode sections to the modes the
    code uses, uses['register_names'] adds the description of every register the code touches."""
    words = [w for w in (uses.get("mode_words") or "").lower().split(",") if w]
    regs = [r for r in (uses.get("register_names") or "").split(",") if r]
    out: List[Tuple[Section, str]] = []
    for s in secs:
        if s.end - s.start < 120:          # a heading over a figure or table only
            continue
        if any(t not in uses and re.search(rx, s.title, re.I) for t, rx in _UNLESS_USED.items()):
            continue
        hit = next((r for r in regs if re.search(rf"(?<![A-Za-z0-9_]){re.escape(r)}(?![A-Za-z0-9_])", s.title)), None)
        if hit:
            out.append((s, f"the code uses register {hit}"))
            continue
        for topic, why in uses.items():
            if topic not in _TOPICS or not re.search(_TOPICS[topic], s.title, re.I):
                continue
            if topic == "modes":
                named = [w for w in re.findall(r"([A-Za-z-]+)\s+mode\b", s.title, re.I) if w.lower() not in ("power", "operating", "the")]
                if named and not any(n.lower() in words for n in named):
                    continue               # 'Forced mode' when the code uses normal mode
            out.append((s, why))
            break
    return out
