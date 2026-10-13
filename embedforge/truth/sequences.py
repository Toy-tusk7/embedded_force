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
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from embedforge.truth.flow import Event, FlowModel, Part, Trace, is_int, write_reading, writes_of
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
    fields: Dict[str, int] = field(default_factory=dict)   # write: register-map fields that must hold these values

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
    # the fact's own unit, else the unit its quote writes after the value ('wait 2 ms'); the step's hint only as a last resort
    from embedforge.truth.ledger import TIME_UNITS, unit_after
    quoted = unit_after(getattr(fact, "quote", "") or "", m.group(1), TIME_UNITS) if getattr(fact, "quote", "") else None
    unit = (m.group(2) or getattr(fact, "unit", "") or quoted or unit_hint or "").strip().lower()
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

    def _resolve_fields(self, st: Step, raw: Any, ledger: FactLedger, subject: str) -> str:
        """fields {'mode': 'normal', 'press_en': 1} -> {'mode': 3, 'press_en': 1}, checked against the register map; a value
        fact given too must agree with them."""
        from embedforge.truth.registers import RegisterBook
        if st.op != "write":
            return "fields are only for write steps"
        if not isinstance(raw, dict) or not raw:
            return "fields must be an object {field name: value or value name}"
        addr = _fact_int(ledger, st.reg)
        if addr is None:
            return f"reg {st.reg!r} must be the FACT id of the register address"
        book = RegisterBook(self.path.parent)
        reg = _register(book, addr, subject)   # the same lookup the check uses later
        if reg is None or not reg.fields:
            return f"register 0x{addr:02X} has no fields in the register map: record_registers for it first"
        out: Dict[str, int] = {}
        for fname, fv in raw.items():
            f = reg.field(str(fname))
            if f is None:
                return f"{reg.name} has no field {fname!r} (fields: {', '.join(x.name for x in reg.fields)})"
            v = fv if isinstance(fv, int) and not isinstance(fv, bool) else None
            if v is None:
                key = str(fv).strip().lower()
                named = next((vs for n, vs in f.values.items() if n.lower() == key), None)
                v = named[0] if named else parse_int(key) if not re.fullmatch(r"[01]+", key) or len(key) != f.width else int(key, 2)
            if v is None or not 0 <= v < (1 << f.width):
                names = ", ".join(f"{n}={'|'.join(str(x) for x in vs)}" for n, vs in f.values.items())
                return f"{reg.name}.{f.name} = {fv!r} is not a value of this {f.width}-bit field" + (f" (named values: {names})" if names else "")
            out[f.name] = v
        if st.value:
            want, mask = _fields_value(reg, out)
            fv = _fact_int(ledger, st.value)
            if fv is not None and (fv & mask) != want:
                return (f"{st.value} = 0x{fv:02X} decodes as {reg.decode(fv)}, but the fields ask for "
                        f"{', '.join(f'{n}={v}' for n, v in out.items())} (= 0x{want:02X} in those bits). The value fact is wrong "
                        "or the fields are: drop `value` and keep the fields from the register table")
        st.fields = out
        return ""

    def _ground(self, st: Step, raw: Dict[str, Any], ledger: FactLedger, subject: str) -> str:
        """A step may name a register address, value, read length or delay directly when it is already grounded: the
        address is in the register map (or a layout), the written value decodes into that register's recorded fields, the
        read covers recorded bytes, the delay equals a recorded time fact. Everything else must be a FACT id."""
        from embedforge.truth.registers import RegisterBook
        book = RegisterBook(self.path.parent)
        known = book.known_addresses()

        def raw_int(v: str) -> Optional[int]:
            return None if not v or v.upper().startswith("FACT-") else parse_int(v)
        r = raw_int(st.reg)
        if st.reg and not st.reg.upper().startswith("FACT-"):
            if r is None or r not in known:
                return (f"reg {st.reg!r} is neither a FACT id nor a register/layout address you recorded (record_registers / "
                        "record_layouts first)")
            st.reg = f"0x{r:02X}"
        addr = _fact_int(ledger, st.reg)
        if st.op == "write" and st.value and not st.value.upper().startswith("FACT-") and not st.fields:
            v = raw_int(st.value)
            reg = _register(book, addr, subject)
            if v is None or reg is None:
                return f"value {st.value!r} must be a FACT id (or the register must be in the register map)"
            if reg.fields and v & ~reg.defined_mask & 0xFF:
                return f"value {st.value} sets bits {v & ~reg.defined_mask & 0xFF:#04x} that no field of {reg.name} defines"
            named = [f for f in reg.fields if f.values]
            whole = [f for f in reg.fields if (f.hi, f.lo) == (7, 0)]
            if whole and whole[0].values and not whole[0].value_name(v):
                return f"value {st.value} is none of {reg.name}'s named values ({', '.join(f'{n}={vs[0]:#04x}' for n, vs in whole[0].values.items())})"
            st.fields = {f.name: f.get(v) for f in reg.fields} if reg.fields else {}
            st.value = "" if reg.fields else st.value
            if not reg.fields:
                return f"{reg.name} has no fields in the register map: record its fields, or give value as a FACT id"
            del named
        if st.mask and not st.mask.upper().startswith("FACT-"):
            st.mask = ""   # fields say which bits matter
        if st.op == "read" and st.value and not st.value.upper().startswith("FACT-"):
            n = raw_int(st.value)
            if n is None or addr is None or not all(a in known for a in range(addr, addr + n)):
                return f"read length {st.value!r} must be a FACT id, or every byte {addr}..+{st.value} must be recorded registers/layouts"
            st.value = str(n)
        if st.op == "delay_min" and st.value and not st.value.upper().startswith("FACT-"):
            want = delay_us(type("F", (), {"value": st.value, "unit": st.unit})(), st.unit)
            match = next((f for f in ledger.facts.values() if want is not None and delay_us(f, "") == want), None)
            if match is None:
                return f"delay {st.value!r}{(' ' + st.unit) if st.unit else ''} is not a recorded time fact: record it with record_fact (unit ms/us) and cite the FACT id"
            st.value, st.unit = match.id, ""
        return ""

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
            if raw.get("fields"):
                err = self._resolve_fields(st, raw.get("fields"), ledger, subject)
                if err:
                    problems.append(f"step {k}: {err}")
                    continue
            if st.op not in OPS:
                problems.append(f"step {k}: op must be one of {', '.join(OPS)}")
                continue
            if st.op in ("write", "read", "poll") and not st.reg:
                problems.append(f"step {k}: {st.op} needs reg = the FACT id of the register address")
            if st.op == "delay_min" and not st.value:
                problems.append(f"step {k}: delay_min needs value = the FACT id of the required delay")
            ground = self._ground(st, raw, ledger, subject)
            if ground:
                problems.append(f"step {k}: {ground}")
                continue
            for fld in ("reg", "value", "mask"):
                fid = getattr(st, fld)
                if not fid or not fid.upper().startswith("FACT-"):
                    continue   # grounded by the register map (checked above)
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
        if device and not device.upper().startswith("FACT-"):
            # a part name instead of the address fact: the address recorded for that part
            from embedforge.truth.verifier import _ADDR_PROP_RE
            cand = [f for f in ledger.facts.values() if f.subject.lower() == device.lower() and _ADDR_PROP_RE.search(f.property)
                    and parse_int(str(f.value)) is not None]
            device = cand[0].id if len(cand) == 1 or len({c.value for c in cand}) == 1 and cand else device
        if device:
            dev = ledger.get(device)
            if dev is None or parse_int(str(dev.value)) is None:
                problems.append(f"device {device!r} must be the FACT id of the bus address (e.g. the I2C address)")
        from embedforge.truth.registers import verbatim
        src = ledger.store.get(source_id)
        if src is None:
            problems.append(f"source_id {source_id!r} is not a stored source")
        elif len(_squash(order_quote)) < 12 or not verbatim(order_quote, src.text):
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
    """The number a step names: a fact's value, or a register-map address / read length given directly."""
    if fid and not fid.upper().startswith("FACT-"):
        return parse_int(fid)
    f = ledger.get(fid) if fid else None
    return parse_int(str(f.value)) if f is not None else None


def _label(ledger: FactLedger, fid: str) -> str:
    if fid and not fid.upper().startswith("FACT-"):
        return fid
    f = ledger.get(fid)
    return f"{fid} ({f.subject}.{f.property}={f.value})" if f else fid


def _fields_value(reg: Any, fields: Dict[str, int]) -> Tuple[int, int]:
    """(value, mask) that the named fields of a register-map register put on the register."""
    want = mask = 0
    for name, v in fields.items():
        f = reg.field(name)
        if f is not None:
            want |= (v << f.lo) & f.mask
            mask |= f.mask
    return want, mask


def _register(book: Any, addr: Optional[int], subject: str) -> Any:
    """The sequence's own part's register at that address (a board can have two parts with a register there); any part's
    when the register map names this part differently."""
    if book is None or addr is None:
        return None
    own = book.register_at(addr, subject) if subject else None
    if own is not None or (subject and any(r.device.lower() == subject.lower() for r in book.registers.values())):
        return own
    return book.register_at(addr)


def _step_target(step: Step, ledger: FactLedger, book: Any, subject: str = "") -> Tuple[Optional[int], Optional[int]]:
    """(want, mask) for a write step: from its fields (register map) or from its value/mask facts."""
    if step.fields and book is not None:
        addr = _fact_int(ledger, step.reg)
        reg = _register(book, addr, subject)
        if reg is not None:
            return _fields_value(reg, step.fields)
    want = _fact_int(ledger, step.value) if step.value else None
    mask = _fact_int(ledger, step.mask) if step.mask else None
    return want, mask


def _matches(step: Step, e: Event, ledger: FactLedger, book: Any = None, subject: str = "") -> str:
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
        want, mask = _step_target(step, ledger, book, subject)
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
            if isinstance(v, Part):   # a read-modify-write: judge the bits it decides
                m = (mask if mask is not None else 0xFF) & 0xFF
                if (v.value ^ want) & v.mask & m:
                    continue   # a decided bit is wrong
                reg_def = _register(book, r, subject)
                fields = reg_def.defined_mask if reg_def is not None and reg_def.fields else 0xFF
                if m & fields & ~v.mask == 0:   # every field bit the step sets is decided (reserved bits may be kept)
                    return "yes"
                unresolved = True
                continue
            if not is_int(v):
                unresolved = True
                continue
            if (v & mask) == (want & mask) if mask is not None else v == want:
                return "yes"
        if saw_reg:
            return "unresolved" if unresolved else "value"
        return "unresolved" if unresolved and not e.data else ""
    return ""


def _mapped(book: Any, subject: str) -> Optional[Callable[[int], bool]]:
    """r -> r is a register or data byte of the sequence's part in the register map; None without a map."""
    if book is None or not book.registers:
        return None
    return lambda r: _register(book, r, subject) is not None or any(
        not subject or lay.device.lower() == subject.lower() for lay, _ in book.layout_part(r))


def _one_register_each(events: List[Event], mapped: Optional[Callable[[int], bool]]) -> List[Event]:
    """A write that sets several registers (a burst, or reg/value pairs) becomes one write per register, in the order the
    part receives them, so consecutive steps can each match their own register of the same transaction."""
    out: List[Event] = []
    for e in events:
        pairs = write_reading(e, mapped) if e.kind == "write" and is_int(e.reg) else None
        if pairs and len(pairs) > 1 and all(is_int(r) for r, _ in pairs):
            out.extend(replace(e, reg=r, data=[v], length=1) for r, v in pairs)
        else:
            out.append(e)
    return out


def check_sequence(seq: SequenceRecord, trace: Trace, ledger: FactLedger, book: Any = None) -> FlowResult:
    res = FlowResult(seq.id, seq.subject, seq.name, seq.entry, trace.found, notes=list(trace.notes))
    if not trace.found:
        return res
    dev = _fact_int(ledger, seq.device) if seq.device else None
    events = [e for e in trace.events if e.kind == "delay" or dev is None or not is_int(e.addr) or e.addr == dev]
    res.trace = [f"{e.file}:{e.entry_line} {e.describe()}" + ("" if e.guaranteed else " (conditional)") for e in events]
    events = _one_register_each(events, _mapped(book, seq.subject))
    pos, last_idx = 0, -1
    pending: Optional[Tuple[int, float, int]] = None   # (from event index, required us, step number)

    def delay_between(a: int, b: int) -> Tuple[float, bool, float, str]:
        """(guaranteed known delay, any unknown delay present, conditional known delay, an assumption the known delay
        rests on) between events a and b."""
        known, unknown, cond, assumed = 0.0, False, 0.0, ""
        for e in events[a + 1:b]:
            if e.kind != "delay":
                continue
            if isinstance(e.delay_us, (int, float)):
                if e.guaranteed:
                    known += e.delay_us
                    assumed = assumed or e.assumed
                else:
                    cond += e.delay_us
            else:
                unknown = True
        return known, unknown, cond, assumed

    def settle(pend: Tuple[int, float, int], until: int) -> None:
        a, need, num = pend
        known, unknown, cond, assumed = delay_between(a, until)
        if known + 1e-9 >= need and assumed:   # long enough only under the assumption: a slower tick can make it short
            res.steps.append(StepResult(num, "delay_min", "unverifiable",
                                        f"needs >= {need:g} us; the code waits {known:g} us only if the assumption holds ({assumed}). "
                                        "State the tick rate (CONFIG_FREERTOS_HZ in sdkconfig.defaults) or wait with a fixed-time call"))
        elif known + 1e-9 >= need:
            res.steps.append(StepResult(num, "delay_min", "matched", f"{known:g} us >= {need:g} us"))
        elif unknown:
            res.steps.append(StepResult(num, "delay_min", "unverifiable",
                                        f"needs >= {need:g} us; {known:g} us is certain, other delays depend on runtime values"))
        elif cond:
            res.steps.append(StepResult(num, "delay_min", "conditional",
                                        f"needs >= {need:g} us; only {known:g} us always runs (+{cond:g} us inside an if/loop)"))
        else:
            res.steps.append(StepResult(num, "delay_min", "delay_short", f"needs >= {need:g} us, the code waits {known:g} us"
                                        + (f" even at the fastest common tick rate ({assumed})" if assumed else "")))

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
            m = _matches(step, events[k], ledger, book, seq.subject)
            if m == "yes":
                hit = k
                break
            if m == "unresolved" and unres is None:
                unres = k
            if m == "value" and value_hit is None:
                value_hit = k
        desc = f"{step.op} {_label(ledger, step.reg)}" + (f" = {_label(ledger, step.value)}" if step.value and step.op == "write" and not step.fields else "")
        if step.fields:
            w, _m = _step_target(step, ledger, book, seq.subject)
            desc += f" with {', '.join(f'{n}={v}' for n, v in step.fields.items())}" + (f" (0x{w:02X} in those bits)" if w is not None else "")
        if hit is not None:
            e = events[hit]
            if pending is not None:
                settle(pending, hit)
                pending = None
            res.steps.append(StepResult(num, step.op, "matched" if e.guaranteed else "conditional",
                                        desc + ("" if e.guaranteed else " — only inside an if/loop"), e.file, e.entry_line))
            pos, last_idx = hit + 1, hit
            continue
        earlier = next((k for k in range(0, pos) if _matches(step, events[k], ledger, book, seq.subject) == "yes"), None)
        if earlier is not None:
            e = events[earlier]
            res.steps.append(StepResult(num, step.op, "order", f"{desc} happens at {e.file}:{e.entry_line}, before step {num - 1}", e.file, e.entry_line))
        elif value_hit is not None:
            e = events[value_hit]
            rnum = _fact_int(ledger, step.reg)
            rdef = _register(book, rnum, seq.subject)
            got = ", ".join((f"0x{v:02X}" + (f" ({rdef.decode(v)})" if rdef is not None and rdef.fields else "")) if is_int(v) else repr(v)
                            for _, v in writes_of(e) if is_int(_) and _ == rnum) if e.kind == "write" else f"length {e.length}"
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
    from embedforge.truth.registers import RegisterBook, entry_points
    model = model or FlowModel.from_dir(Path(project_dir))
    regs = RegisterBook(project_dir)
    results, findings = [], []
    entries = None
    for seq in book.items.values():
        fn = model.function(seq.entry)
        if entries is None:
            entries = entry_points(model)
        # the entry as the program calls it: main() may hand it the device handle (&dev with .addr) or set globals first
        trace = model.trace(seq.entry, context=model.caller_context(seq.entry, entries))
        r = check_sequence(seq, trace, ledger, regs)
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
