"""Datasheet sequences (the required ORDER and TIMING of register operations) and the static check of code against them.

A sequence is made only of facts that are already in the Fact Ledger (register addresses, values, delays, the bus address),
plus one verbatim quote that establishes the order. So the model never types a register or a delay into a sequence:

    SEQ-001 BMP388.power_on_init, implemented by bmp388_init(), device FACT-003 (0x76):
      1. write  reg FACT-010 (CMD 0x7E)      value FACT-011 (softreset 0xB6)
      2. delay_min FACT-012 (2 ms)
      3. read   reg FACT-014 (NVM 0x31)      length FACT-015 (21)
      4. write  reg FACT-016 (PWR_CTRL 0x1B) value FACT-017 (0x33)
      order: "After a soft reset ... a start-up time of 2 ms ..."   (verbatim in SRC-004)

`check_flows` traces the entry function with truth.flow (constant propagation, no simulation) and reports each step as
matched at file:line, or as missing / out of order / wrong value / too short a delay.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from embedforge.truth.flow import Event, FlowModel, Trace, is_int, writes_of
from embedforge.truth.ledger import FactLedger, parse_int

OPS = ("write", "read", "delay_min", "poll")
_UNIT_US = {"us": 1, "µs": 1, "μs": 1, "usec": 1, "microseconds": 1, "ms": 1000, "msec": 1000, "milliseconds": 1000,
            "s": 1_000_000, "sec": 1_000_000, "seconds": 1_000_000, "ns": 0.001}


@dataclass
class Step:
    op: str
    reg: str = ""        # FACT id of the register address (write/read/poll)
    value: str = ""      # FACT id: written value (write), minimum length (read), minimum delay (delay_min), bit mask (poll)
    mask: str = ""       # FACT id: for a write, only these bits must equal the value
    unit: str = ""       # delay unit when the fact has none: us | ms | s

    def public(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v}


@dataclass
class SequenceRecord:
    id: str
    subject: str
    name: str
    entry: str
    steps: List[Step]
    source_id: str
    order_quote: str
    device: str = ""     # FACT id of the bus address, used to ignore other devices on the same bus

    def public(self) -> Dict[str, Any]:
        d = asdict(self)
        d["steps"] = [s.public() for s in self.steps]
        return d


def delay_us(fact: Any, unit_hint: str = "") -> Optional[float]:
    """A delay fact's value in microseconds ('2' + unit 'ms', or '2 ms', or '1.5ms')."""
    raw = str(fact.value).strip()
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Zµμ]*)", raw.replace(",", "."))
    if not m:
        return None
    unit = (m.group(2) or getattr(fact, "unit", "") or unit_hint or "").strip().lower()
    scale = _UNIT_US.get(unit)
    if scale is None:
        return None
    return float(m.group(1)) * scale


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


class SequenceBook:
    """sequences.json next to ledger.json."""

    def __init__(self, root: Path):
        self.path = Path(root) / "sequences.json"
        self._lock = threading.Lock()
        self.items: Dict[str, SequenceRecord] = {}
        if self.path.exists():
            try:
                for d in json.loads(self.path.read_text(encoding="utf-8")):
                    d["steps"] = [Step(**s) for s in d.get("steps", [])]
                    rec = SequenceRecord(**d)
                    self.items[rec.id] = rec
            except (ValueError, TypeError, KeyError):
                pass

    def _save(self) -> None:
        self.path.write_text(json.dumps([r.public() for r in self.items.values()], indent=2), encoding="utf-8")

    def record(self, ledger: FactLedger, subject: str, name: str, entry: str, steps: List[Dict[str, Any]],
               source_id: str, order_quote: str, device: str = "") -> Dict[str, Any]:
        subject, name, entry = (subject or "").strip(), re.sub(r"[^A-Za-z0-9_]+", "_", (name or "").strip()).strip("_"), (entry or "").strip()
        problems: List[str] = []
        if not subject or not name:
            problems.append("subject and name are required (e.g. subject='BMP388', name='power_on_init')")
        if not re.fullmatch(r"[A-Za-z_][\w:]{0,80}", entry):
            problems.append("entry must be the C function that performs the sequence (e.g. 'bmp388_init')")
        if not isinstance(steps, list) or not steps:
            problems.append("steps must be a non-empty list")
            steps = []
        parsed: List[Step] = []
        for k, raw in enumerate(steps[:40], start=1):
            if not isinstance(raw, dict):
                problems.append(f"step {k}: must be an object")
                continue
            st = Step(op=str(raw.get("op") or "").strip(), reg=str(raw.get("reg") or "").strip(), value=str(raw.get("value") or "").strip(),
                      mask=str(raw.get("mask") or "").strip(), unit=str(raw.get("unit") or "").strip().lower())
            if st.op not in OPS:
                problems.append(f"step {k}: op must be one of {', '.join(OPS)}")
                continue
            if st.op in ("write", "read", "poll") and not st.reg:
                problems.append(f"step {k}: {st.op} needs reg = the FACT id of the register address")
            if st.op == "delay_min" and not st.value:
                problems.append(f"step {k}: delay_min needs value = the FACT id of the required delay")
            for fld in ("reg", "value", "mask"):
                fid = getattr(st, fld)
                if not fid:
                    continue
                fact = ledger.get(fid)
                if fact is None:
                    problems.append(f"step {k}: {fld} {fid!r} is not a fact id in the ledger (record the value with record_fact first)")
                    continue
                if fld in ("reg", "mask") or (fld == "value" and st.op in ("write", "read", "poll")):
                    if parse_int(str(fact.value)) is None:
                        problems.append(f"step {k}: {fid} = {fact.value!r} is not a number")
                if fld == "value" and st.op == "delay_min" and delay_us(fact, st.unit) is None:
                    problems.append(f"step {k}: {fid} = {fact.value!r} has no time unit; record the fact with unit='ms' or 'us', or give unit in the step")
            parsed.append(st)
        if device:
            dev = ledger.get(device)
            if dev is None or parse_int(str(dev.value)) is None:
                problems.append(f"device {device!r} must be the FACT id of the bus address (e.g. the I2C address)")
        src = ledger.store.get(source_id)
        if src is None:
            problems.append(f"source_id {source_id!r} is not a stored source")
        elif len(_squash(order_quote)) < 12 or _squash(order_quote) not in _squash(src.text):
            problems.append("order_quote must be a verbatim passage (12+ chars) of that source stating the order/timing")
        if problems:
            return {"ok": False, "error": "; ".join(problems[:8])}
        with self._lock:
            existing = next((r for r in self.items.values() if r.subject.lower() == subject.lower() and r.name.lower() == name.lower()), None)
            sid = existing.id if existing else f"SEQ-{max([int(x.split('-')[1]) for x in self.items] + [0]) + 1:03d}"
            rec = SequenceRecord(sid, subject, name, entry, parsed, source_id, order_quote.strip(), device)
            self.items[sid] = rec
            self._save()
        return {"ok": True, "sequence": rec.public(), **({"replaced": True} if existing else {})}


# ------------------------------------------------------------------ checking


@dataclass
class StepResult:
    step: int
    op: str
    status: str             # matched | missing | order | value | delay_short | unverifiable | conditional | unresolved
    detail: str = ""
    file: str = ""
    line: int = 0


@dataclass
class FlowResult:
    sequence: str
    subject: str
    name: str
    entry: str
    found: bool
    steps: List[StepResult] = field(default_factory=list)
    trace: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def public(self) -> Dict[str, Any]:
        return {"sequence": self.sequence, "subject": self.subject, "name": self.name, "entry": self.entry, "found": self.found,
                "steps": [asdict(s) for s in self.steps], "trace": self.trace[:40], "notes": self.notes[:10]}


def _fact_int(ledger: FactLedger, fid: str) -> Optional[int]:
    f = ledger.get(fid) if fid else None
    return parse_int(str(f.value)) if f is not None else None


def _label(ledger: FactLedger, fid: str) -> str:
    f = ledger.get(fid)
    return f"{fid} ({f.subject}.{f.property}={f.value})" if f else fid


def _matches(step: Step, e: Event, ledger: FactLedger) -> str:
    """'' (no), 'yes', 'unresolved' (could be: register or value unknown at build time), 'value' (right register, other value)."""
    reg = _fact_int(ledger, step.reg)
    if step.op in ("read", "poll"):
        if e.kind != "read":
            return ""
        if not is_int(e.reg):
            return "unresolved" if e.reg is not None or reg is not None else ""
        if e.reg != reg:
            return ""
        need = _fact_int(ledger, step.value) if step.op == "read" and step.value else None
        if need is not None and is_int(e.length) and e.length < need:
            return "value"
        return "yes"
    if step.op == "write":
        if e.kind != "write":
            return ""
        want = _fact_int(ledger, step.value) if step.value else None
        mask = _fact_int(ledger, step.mask) if step.mask else None
        saw_reg = False
        unresolved = not is_int(e.reg)
        for r, v in writes_of(e):
            if not is_int(r):
                unresolved = True
                continue
            if r != reg:
                continue
            saw_reg = True
            if want is None:
                return "yes"
            if not is_int(v):
                unresolved = True
                continue
            if (v & mask) == (want & mask) if mask is not None else v == want:
                return "yes"
        if saw_reg:
            return "unresolved" if unresolved else "value"
        return "unresolved" if unresolved and not e.data else ""
    return ""


def check_sequence(seq: SequenceRecord, trace: Trace, ledger: FactLedger) -> FlowResult:
    res = FlowResult(seq.id, seq.subject, seq.name, seq.entry, trace.found, notes=list(trace.notes))
    if not trace.found:
        return res
    dev = _fact_int(ledger, seq.device) if seq.device else None
    events = [e for e in trace.events if e.kind == "delay" or dev is None or not is_int(e.addr) or e.addr == dev]
    res.trace = [f"{e.file}:{e.entry_line} {e.describe()}" + ("" if e.guaranteed else " (conditional)") for e in events]
    pos, last_idx = 0, -1
    pending: Optional[Tuple[int, float, int]] = None   # (from event index, required us, step number)

    def delay_between(a: int, b: int) -> Tuple[float, bool, float]:
        """(guaranteed known delay, any unknown delay present, conditional known delay) between events a and b."""
        known, unknown, cond = 0.0, False, 0.0
        for e in events[a + 1:b]:
            if e.kind != "delay":
                continue
            if isinstance(e.delay_us, (int, float)):
                if e.guaranteed:
                    known += e.delay_us
                else:
                    cond += e.delay_us
            else:
                unknown = True
        return known, unknown, cond

    def settle(pend: Tuple[int, float, int], until: int) -> None:
        a, need, num = pend
        known, unknown, cond = delay_between(a, until)
        if known + 1e-9 >= need:
            res.steps.append(StepResult(num, "delay_min", "matched", f"{known:g} us >= {need:g} us"))
        elif unknown:
            res.steps.append(StepResult(num, "delay_min", "unverifiable",
                                        f"needs >= {need:g} us; {known:g} us is certain, other delays depend on runtime values"))
        elif cond:
            res.steps.append(StepResult(num, "delay_min", "conditional",
                                        f"needs >= {need:g} us; only {known:g} us always runs (+{cond:g} us inside an if/loop)"))
        else:
            res.steps.append(StepResult(num, "delay_min", "delay_short", f"needs >= {need:g} us, the code waits {known:g} us"))

    for num, step in enumerate(seq.steps, start=1):
        if step.op == "delay_min":
            need = delay_us(ledger.get(step.value), step.unit) if ledger.get(step.value) else None
            if need is None:
                res.steps.append(StepResult(num, "delay_min", "unverifiable", f"{_label(ledger, step.value)} has no usable time unit"))
                continue
            if pending is not None:  # two delays in a row add up
                pending = (pending[0], pending[1] + need, pending[2])
            else:
                pending = (last_idx, need, num)
            continue
        hit = unres = value_hit = None
        for k in range(pos, len(events)):
            m = _matches(step, events[k], ledger)
            if m == "yes":
                hit = k
                break
            if m == "unresolved" and unres is None:
                unres = k
            if m == "value" and value_hit is None:
                value_hit = k
        desc = f"{step.op} {_label(ledger, step.reg)}" + (f" = {_label(ledger, step.value)}" if step.value and step.op == "write" else "")
        if hit is not None:
            e = events[hit]
            if pending is not None:
                settle(pending, hit)
                pending = None
            res.steps.append(StepResult(num, step.op, "matched" if e.guaranteed else "conditional",
                                        desc + ("" if e.guaranteed else " — only inside an if/loop"), e.file, e.entry_line))
            pos, last_idx = hit + 1, hit
            continue
        earlier = next((k for k in range(0, pos) if _matches(step, events[k], ledger) == "yes"), None)
        if earlier is not None:
            e = events[earlier]
            res.steps.append(StepResult(num, step.op, "order", f"{desc} happens at {e.file}:{e.entry_line}, before step {num - 1}", e.file, e.entry_line))
        elif value_hit is not None:
            e = events[value_hit]
            got = ", ".join(f"0x{v:02X}" if is_int(v) else repr(v) for _, v in writes_of(e) if is_int(_) and _ == _fact_int(ledger, step.reg)) if e.kind == "write" else f"length {e.length}"
            res.steps.append(StepResult(num, step.op, "value", f"{desc}: the code uses {got}", e.file, e.entry_line))
        elif unres is not None:
            e = events[unres]
            res.steps.append(StepResult(num, step.op, "unresolved", f"{desc}: possibly {e.describe()} at {e.file}:{e.entry_line} (value known only at run time)", e.file, e.entry_line))
        else:
            res.steps.append(StepResult(num, step.op, "missing", f"{desc} never happens in {seq.entry}()"))
        if pending is not None:
            pending = None  # the delay is checked against the next step that is found; a missing step makes it moot
    if pending is not None:
        settle(pending, len(events))
    return res


_ERROR = {"missing": "FLOW_STEP_MISSING", "order": "FLOW_ORDER", "value": "FLOW_VALUE_MISMATCH", "delay_short": "FLOW_DELAY_SHORT"}
_WARN = {"unverifiable": "FLOW_DELAY_UNVERIFIABLE", "conditional": "FLOW_CONDITIONAL", "unresolved": "FLOW_UNRESOLVED"}


def check_flows(project_dir: Path, ledger: FactLedger, model: Optional[FlowModel] = None) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str, str, int, str, str]]]:
    """All recorded sequences against the code. Returns (per-sequence results, findings as
    (severity, code, file, line, message, suggestion))."""
    book = SequenceBook(project_dir)
    if not book.items:
        return [], []
    model = model or FlowModel.from_dir(Path(project_dir))
    results, findings = [], []
    for seq in book.items.values():
        fn = model.function(seq.entry)
        trace = model.trace(seq.entry)
        r = check_sequence(seq, trace, ledger)
        results.append(r.public())
        where = fn.file if fn else ""
        if not trace.found:
            findings.append(("ERROR", "FLOW_ENTRY_MISSING", where, 0,
                             f"{seq.id} {seq.subject}.{seq.name}: function {seq.entry}() is not in the sources.",
                             "Implement the sequence in that function, or re-record the sequence with the function that does it."))
            continue
        for s in r.steps:
            code = _ERROR.get(s.status) or _WARN.get(s.status)
            if not code:
                continue
            findings.append(("ERROR" if s.status in _ERROR else "WARNING", code, s.file or where, s.line or (fn.line if fn else 0),
                             f"{seq.id} {seq.subject}.{seq.name} step {s.step}: {s.detail}",
                             _hint(s.status)))
    return results, findings


def _hint(status: str) -> str:
    return {
        "missing": "Add the operation the datasheet requires to the entry function (or a function it calls).",
        "order": "Move the operation so it happens after the previous step, in the order the datasheet requires.",
        "value": "Write the value from the cited fact (or record the right fact if the datasheet says otherwise).",
        "delay_short": "Wait at least the datasheet time between these steps (e.g. sleep_ms / delay / HAL_Delay).",
        "unverifiable": "Use a constant delay so it can be checked, or wait in a loop on the status bit the datasheet names.",
        "conditional": "Make the operation unconditional on the init path, or confirm the condition always holds.",
        "unresolved": "Use named constants (#define / enum) for registers and values so the order can be checked.",
    }.get(status, "")
