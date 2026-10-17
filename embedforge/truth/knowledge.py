"""The project's requirements memory: every rule sentence ('must', 'should', 'before', 'wait', 'only'...) of the
datasheet sections the code depends on becomes a numbered requirement, kept in requirements.json across runs.

A requirement is covered when a recorded truth item (a fact, register, layout, burst, formula, sequence or constraint)
quotes text inside it; otherwise the agent resolves it explicitly: implemented (the code does it; said how) or
not_applicable (why: a board-level rule, a feature the code does not use). verify fails while one is open, so every
rule the datasheet states for what the code does is considered, not just the ones a keyword search happened to reach.
This is the requirements-traceability practice of certified development (DO-178C, ISO 26262), for one part's datasheet."""

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

STATUSES = ("open", "covered", "implemented", "not_applicable")


@dataclass
class Requirement:
    id: str
    source_id: str
    section: str          # '3.10.1 Data register shadowing'
    text: str
    start: int            # span of the sentence in the source text
    end: int
    status: str = "open"
    refs: List[str] = field(default_factory=list)   # truth items that quote it (covered)
    reason: str = ""                                # how the code implements it / why it does not apply
    review: Dict[str, str] = field(default_factory=dict)   # the independent reviewer's verdict on the claim
    ref: str = ""                                   # the record the agent cites for an 'implemented' claim
    disputes: int = 0                               # how often the reviewer disagreed with a claim about it


class Requirements:
    """requirements.json next to ledger.json."""

    def __init__(self, root: Path):
        self.path = Path(root) / "requirements.json"
        self._lock = threading.Lock()
        self.items: Dict[str, Requirement] = {}
        try:
            for r in json.loads(self.path.read_text(encoding="utf-8")):
                self.items[r["id"]] = Requirement(**r)
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def _save(self) -> None:
        self.path.write_text(json.dumps([asdict(r) for r in self.items.values()], indent=1), encoding="utf-8")

    def sync(self, source_id: str, found: Iterable[Tuple[str, str, int, int]]) -> List[Requirement]:
        """Add the requirements (section, text, start, end) of a source not yet on record; returns them all."""
        out = []
        with self._lock:
            known = {(r.source_id, r.start, r.end): r for r in self.items.values()}
            for section, text, a, b in found:
                r = known.get((source_id, a, b))
                if r is None:
                    rid = f"REQ-{max([int(k.split('-')[1]) for k in self.items] + [0]) + 1:03d}"
                    r = self.items[rid] = Requirement(rid, source_id, section, text, a, b)
                    known[(source_id, a, b)] = r
                out.append(r)
            self._save()
        return out

    def cover(self, spans: Dict[str, List[Tuple[int, int, str]]]) -> None:
        """Mark requirements a truth item quotes as covered (spans: source -> (start, end, truth id)). An explicit
        implemented / not_applicable stays."""
        with self._lock:
            for r in self.items.values():
                refs = sorted({ref for a, b, ref in spans.get(r.source_id, []) if a < r.end and r.start < b})
                if r.status in ("open", "covered"):
                    r.status, r.refs = ("covered", refs) if refs else ("open", [])
                elif refs:
                    r.refs = refs
            self._save()

    def resolve(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """[{id, status: implemented | not_applicable, reason}]: the agent's explicit decision for each."""
        done, rejected = [], []
        with self._lock:
            for it in items[:60]:
                it = it if isinstance(it, dict) else {}
                rid = re.sub(r"^\s*REQ[-_ ]?0*(\d+)\s*$", lambda m: f"REQ-{int(m.group(1)):03d}", str(it.get("id") or ""), flags=re.I)
                status = str(it.get("status") or "").strip().lower().replace(" ", "_").replace("-", "_")
                reason = str(it.get("reason") or "").strip()
                r = self.items.get(rid)
                if r is None:
                    rejected.append({"id": rid or "?", "error": "no such requirement (coding_brief lists them)"})
                elif status not in ("implemented", "not_applicable"):
                    rejected.append({"id": rid, "error": "status must be 'implemented' or 'not_applicable' (a requirement a "
                                                         "record quotes is covered by itself)"})
                elif len(reason) < 12:
                    rejected.append({"id": rid, "error": "reason: say how the code does it (file/function) or why it does "
                                                         "not apply (e.g. 'board-level: pull-ups are on the schematic')"})
                else:
                    r.status, r.reason, r.review = status, reason[:300], {}   # a new claim needs a new review
                    r.ref = str(it.get("ref") or "").strip().upper()[:20]
                    done.append(f"{rid} {status}")
            self._save()
        out: Dict[str, Any] = {"ok": bool(done), "resolved": done}
        if rejected:
            out["rejected"] = rejected
        return out

    def set_reviews(self, reviews: Dict[str, Dict[str, str]]) -> None:
        with self._lock:
            for rid, rv in reviews.items():
                r = self.items.get(rid)
                if r is None or r.status not in ("implemented", "not_applicable"):
                    continue
                r.review = rv
                if rv.get("verdict") == "disagree":
                    r.disputes += 1
            self._save()

    def disputed(self) -> List[Requirement]:
        return [r for r in self.items.values() if r.status in ("implemented", "not_applicable")
                and r.review.get("verdict") == "disagree"]

    def counts(self) -> Dict[str, int]:
        c = {s: 0 for s in STATUSES}
        for r in self.items.values():
            c[r.status] = c.get(r.status, 0) + 1
        return c


def truth_spans(root: Path, store: Any, ledger: Any) -> Dict[str, List[Tuple[int, int, str]]]:
    """Where in each source the recorded truth is quoted: source -> [(start, end, truth id)]."""
    from embedforge.truth.ledger import anchor_pieces
    from embedforge.truth.registers import RegisterBook
    from embedforge.truth.sequences import SequenceBook
    quotes: List[Tuple[str, str, str]] = [(f.source_id, f.quote, f.id) for f in ledger.facts.values()]
    book = RegisterBook(root)
    for r in book.registers.values():
        quotes += [(r.source_id, r.quote, r.id)] + ([(r.source_id, r.reset_quote, r.id)] if r.reset_quote else [])
    quotes += [(x.source_id, x.quote, x.id) for x in list(book.layouts.values()) + list(book.bursts.values()) + list(book.formulas.values())]
    quotes += [(c.source_id, q, c.id) for c in book.constraints.values() for q in c.quotes]
    quotes += [(s.source_id, s.order_quote, s.id) for s in SequenceBook(root).items.values()]
    out: Dict[str, List[Tuple[int, int, str]]] = {}
    for sid, q, ref in quotes:
        src = store.get(sid)
        if src is None or not q:
            continue
        for a, b in anchor_pieces(q, src.text) or []:
            out.setdefault(sid, []).append((a, b, ref))
    return out
