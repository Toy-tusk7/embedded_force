"""Verification Engine: cross-references every hardware constant in generated code against the Fact Ledger.

Rule: every hex literal (and every decimal `#define` that looks like an address/register/ID) in
project sources must be justified by a ledger fact. The agent annotates constants with a
`FACT-nnn` tag in a comment on the same line, and the verifier checks the tag's value equals
the literal. Unjustified constants fail verification -> the agent must research or ask.
"""

import re
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Any, Optional

from embedforge.truth.ledger import FactLedger, parse_int

SRC_EXTS = {".c", ".h", ".cpp", ".hpp", ".cc", ".ino", ".s", ".S"}
SRC_DIRS = ("src", "include", "lib", "betaflight")

# Generic literals that carry no hardware claim (masks, sentinels).
GENERIC_OK = {0x00, 0x01, 0x02, 0x03, 0x04, 0x07, 0x0F, 0x10, 0x1F, 0x3F, 0x7F, 0x80, 0xF0, 0xFF, 0xFFFF, 0xFFFFFFFF}

_HEX_RE = re.compile(r"\b0[xX][0-9A-Fa-f]+[uUlL]*\b")
_FACT_TAG_RE = re.compile(r"FACT-\d{3,}")
_BAD_TAG_RE = re.compile(r"\bFACT(?:[-_]|(?=\d))[A-Za-z0-9_-]*[A-Za-z0-9]")  # anything shaped like a tag attempt
_DEFINE_RE = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)\s+(.+?)\s*$")
_HW_NAME_RE = re.compile(r"(ADDR|REG|WHO_?AM_?I|CHIP_?ID|DEVICE_?ID|_ID\b|OPCODE|_CMD\b)", re.I)
# Pin assignments are wiring claims: they must trace to the schematic or the user (e.g. #define GYRO_CS_PIN 14).
_PIN_NAME_RE = re.compile(r"(_PIN\b|^PIN_|_GPIO\b|_GPIO_NUM\b|_IO_NUM\b)", re.I)
_PIN_VALUE_RE = re.compile(r"^(?:GPIO|GP|IO|P)?(\d{1,3})$", re.I)


def fact_int(value: str) -> Optional[int]:
    """Numeric value of a fact; pin facts like 'GPIO14' / 'GP14' / 'P14' count as 14."""
    n = parse_int(value)
    if n is not None:
        return n
    m = _PIN_VALUE_RE.match(str(value).strip())
    return int(m.group(1)) if m else None


def pin_key(token: str, pico: bool = False) -> Optional[str]:
    """Canonical pin identity. On RP2xxx every spelling of GPIO n (14, GPIO14, GP14, P14 and Betaflight's PA14) is
    'GPIO14'; elsewhere port pins stay port pins (STM32 PA14 is not GPIO14)."""
    t = str(token).strip().strip("()").upper()
    m = re.fullmatch(r"P([A-Z])(\d{1,2})", t)
    if m:
        return f"GPIO{int(m.group(2))}" if pico and m.group(1) == "A" else f"P{m.group(1)}{int(m.group(2))}"
    m = re.fullmatch(r"(?:GPIO|GP|IO|P)?(\d{1,3})", t)
    return f"GPIO{int(m.group(1))}" if m else None


@dataclass
class Finding:
    severity: str  # ERROR | WARNING | INFO
    code: str
    file: str
    line: int
    message: str
    literal: str = ""
    suggestion: str = ""

    def public(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VerifyReport:
    passed: bool
    findings: List[Finding] = field(default_factory=list)
    traceability: List[Dict[str, Any]] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)
    flows: List[Dict[str, Any]] = field(default_factory=list)   # datasheet sequences checked against the code's order

    def public(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "findings": [f.public() for f in self.findings],
            "traceability": self.traceability,
            "stats": self.stats,
            "flows": self.flows,
        }

    def summary(self) -> str:
        errs = sum(1 for f in self.findings if f.severity == "ERROR")
        warns = sum(1 for f in self.findings if f.severity == "WARNING")
        return f"{'PASS' if self.passed else 'FAIL'}: {errs} error(s), {warns} warning(s), {len(self.traceability)} constant(s) traced to sources"


def _strip_comments(text: str) -> List[Dict[str, str]]:
    """Return per-line {code, comment}, handling // and /* */ (including multi-line)."""
    lines = text.split("\n")
    out = []
    in_block = False
    for raw in lines:
        code, comment, i = "", "", 0
        while i < len(raw):
            if in_block:
                end = raw.find("*/", i)
                if end == -1:
                    comment += raw[i:]
                    i = len(raw)
                else:
                    comment += raw[i:end]
                    i = end + 2
                    in_block = False
            elif raw.startswith("/*", i):
                in_block = True
                i += 2
            elif raw.startswith("//", i):
                comment += raw[i + 2 :]
                i = len(raw)
            elif raw[i] == '"':  # skip string literal
                j = i + 1
                while j < len(raw) and raw[j] != '"':
                    j += 2 if raw[j] == "\\" else 1
                code += raw[i : j + 1]
                i = j + 1
            else:
                code += raw[i]
                i += 1
        out.append({"code": code, "comment": comment})
    return out


BUILD_INPUT_EXTS = SRC_EXTS | {".ld", ".cmake", ".mk", ".lds"}
BUILD_INPUT_NAMES = {"CMakeLists.txt", "Makefile", "platformio.ini"}


def collect_build_inputs(project_dir: Path) -> List[Path]:
    """Everything that changes what the build produces: sources plus linker/CMake/make files (e.g. Betaflight's
    generated config.mk and pico_flash_mem.ld) and platformio.ini. A change to any of them invalidates the build."""
    project_dir = Path(project_dir)
    files: List[Path] = []
    for d in SRC_DIRS:
        base = project_dir / d
        if base.is_dir():
            files += [p for p in base.rglob("*") if p.is_file() and (p.suffix in BUILD_INPUT_EXTS or p.name in BUILD_INPUT_NAMES)]
    files += [project_dir / n for n in BUILD_INPUT_NAMES if (project_dir / n).is_file()]
    return sorted(set(files))


def collect_sources(project_dir: Path) -> List[Path]:
    files: List[Path] = []
    for d in SRC_DIRS:
        base = project_dir / d
        if base.is_dir():
            files += [p for p in base.rglob("*") if p.is_file() and p.suffix in SRC_EXTS]
    return sorted(files)


_QUOTE_GPIO_RE = re.compile(r"\bGPIO(\d{1,3})(?![0-9])", re.I)


def quote_pins(quote: str) -> set:
    """GPIO pins named in a verbatim quote, e.g. 'U1 pin 3 (GPIO6) -> net BEEPER -> Q3.1 (G)' -> {'GPIO6'}."""
    return {f"GPIO{int(n)}" for n in _QUOTE_GPIO_RE.findall(quote or "")}


def verify_project(project_dir: Path, ledger: FactLedger, pico: bool = False) -> VerifyReport:
    """`pico`: the target is an RP2040/RP2350, so GPIO n may be written n, GPIOn, GPn or (Betaflight) PAn."""
    project_dir = Path(project_dir)
    report = VerifyReport(passed=True)

    def key_of(value: str, is_pin: bool) -> Any:
        return pin_key(value, pico) if is_pin else parse_int(value)

    known: Dict[Any, List[str]] = {}
    for f in ledger.facts.values():
        for is_pin in (False, True):
            k = key_of(f.value, is_pin)
            if k is not None:
                known.setdefault((is_pin, k), []).append(f.id)
    literals_checked = justified = tagged_ok = 0
    used_facts: Dict[str, int] = {}
    files = collect_sources(project_dir)
    if not files:
        report.passed = False
        report.findings.append(Finding("ERROR", "NO_SOURCES", "", 0, "No source files found under src/, include/ or lib/."))
        return report

    for path in files:
        rel = str(path.relative_to(project_dir)).replace("\\", "/")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        # a FACT tag in a comment on the previous line also covers the next line
        prev_tags: List[str] = []
        for ln, parts in enumerate(_strip_comments(text), start=1):
            code, comment = parts["code"], parts["comment"]
            tags = _FACT_TAG_RE.findall(comment) or prev_tags
            prev_tags = _FACT_TAG_RE.findall(comment) if (not code.strip() and comment.strip()) else []
            stripped = code.strip()
            if not stripped or stripped.startswith(("#include", "#pragma", "#if", "#endif", "#else")):
                continue

            candidates: List[tuple] = []  # (literal text, key, macro name, is_pin)
            m = _DEFINE_RE.match(code)
            define_name = m.group(1) if m else ""
            is_pin_define = bool(m and _PIN_NAME_RE.search(define_name))
            raw = m.group(2).split("//")[0].split("/*")[0].strip() if m else ""
            if is_pin_define and pin_key(raw, pico):
                candidates.append((raw.strip("() "), pin_key(raw, pico), define_name, True))
            else:
                for hm in _HEX_RE.finditer(code):
                    lit = hm.group(0)
                    n = parse_int(lit.rstrip("uUlL"))
                    if n is not None:
                        candidates.append((lit, n, define_name, False))
                if m and _HW_NAME_RE.search(define_name):
                    dm = re.fullmatch(r"\(?\s*(\d+)[uUlL]*\s*\)?", raw)
                    if dm:
                        candidates.append((dm.group(1), int(dm.group(1)), define_name, False))

            for fid in tags:
                fact = ledger.get(fid)
                if fact is None:
                    report.findings.append(Finding("ERROR", "UNKNOWN_FACT_TAG", rel, ln, f"{fid} is not in the Fact Ledger.", suggestion="Record the fact with record_fact first."))
            for bad in _BAD_TAG_RE.findall(comment):
                if not _FACT_TAG_RE.fullmatch(bad):  # e.g. FACT-SRC001, FACT_12, FACT-7: looks like a tag, cites nothing
                    report.findings.append(Finding(
                        "ERROR", "MALFORMED_FACT_TAG", rel, ln,
                        f"'{bad}' is not a fact id. Fact ids look like FACT-001 and come from record_fact results.",
                        suggestion="Replace it with the FACT-nnn id that record_fact returned (list_facts shows them)."))

            for lit, n, name, is_pin in candidates:
                # small "generic" values (masks, 0/1) are skipped unless they name hardware (e.g. #define CTRL_REG 0x01)
                if not is_pin and n in GENERIC_OK and not tags and not (name and _HW_NAME_RE.search(name)):
                    continue
                literals_checked += 1
                tag_facts = [ledger.get(t) for t in tags if ledger.get(t)]
                match = next((f for f in tag_facts if key_of(f.value, is_pin) == n), None)
                if match is None and is_pin:
                    # a fact whose value is a net name still proves the pin when its verbatim quote names exactly one GPIO
                    match = next((f for f in tag_facts if key_of(f.value, True) is None and quote_pins(f.quote) == {n}), None)
                if match is None and tag_facts:
                    report.findings.append(Finding(
                        "ERROR", "FACT_VALUE_MISMATCH", rel, ln,
                        f"Literal {lit} does not equal the value of the cited fact(s) "
                        + ", ".join(f"{f.id}={f.value}" for f in tag_facts) + ".",
                        literal=lit, suggestion="Use the exact value from the cited fact, or cite the correct fact.",
                    ))
                    continue
                if match is None and not tags:
                    # A fact with the same number is not proof this constant means the same thing (0x68 can be an
                    # I2C address in one fact and a WHO_AM_I value in another). Require an explicit tag.
                    ids = known.get((is_pin, n), [])
                    if ids:
                        hints = ", ".join(f"{i} ({ledger.get(i).subject}.{ledger.get(i).property})" for i in ids[:3])
                        report.findings.append(Finding(
                            "ERROR", "UNTAGGED_CONSTANT", rel, ln,
                            f"Constant {lit}" + (f" ({name})" if name else "") + f" is not tagged. Ledger facts with this value: {hints}.",
                            literal=lit,
                            suggestion=f"If it means the same thing, add /* {ids[0]} */ on this line; otherwise record the correct fact.",
                        ))
                        continue
                if match is None:
                    report.findings.append(Finding(
                        "ERROR", "UNSOURCED_CONSTANT", rel, ln,
                        f"Constant {lit}" + (f" ({name})" if name else "") + " has no supporting fact in the ledger.",
                        literal=lit,
                        suggestion=("Find the pin in the schematic (query_board), record_fact with the exact board line as "
                                    "the quote and value='GPIO<n>', then add /* FACT-nnn */ on this line. If the schematic "
                                    "does not have it, remove the line or ask the user." if is_pin else
                                    "Research it (web_search/fetch_document/read_source), record_fact with a verbatim quote, "
                                    "then annotate the line with /* FACT-nnn */. If it is unknown, ask the user."),
                    ))
                    continue
                if is_pin:
                    qp = quote_pins(match.quote)
                    if qp and n not in qp:  # the quote is about another pin (e.g. value GPIO4 quoted from a GPIO41 line)
                        report.findings.append(Finding(
                            "ERROR", "FACT_QUOTE_PIN_MISMATCH", rel, ln,
                            f"{lit} is {n}, but the quote of {match.id} names {', '.join(sorted(qp))}.", literal=lit,
                            suggestion="Cite a fact whose board line is for this pin."))
                        continue
                    if re.fullmatch(r"[A-Z][A-Z0-9_]*_PIN", match.property) and name and match.property != name:
                        report.findings.append(Finding(
                            "ERROR", "FACT_DEFINE_MISMATCH", rel, ln,
                            f"{name} cites {match.id}, which was recorded for {match.property}.", literal=lit,
                            suggestion=f"record_board_pins with define='{name}' and this pin's board line (add replace: true if "
                                       f"{name} was recorded for another pin), or cite the right fact."))
                        continue
                justified += 1
                used_facts[match.id] = used_facts.get(match.id, 0) + 1
                report.traceability.append({
                    "file": rel, "line": ln, "constant": name or lit, "literal": lit, "fact_id": match.id,
                    "subject": match.subject, "property": match.property, "tier": match.tier,
                    "source_url": match.source_url, "quote": match.quote,
                })
                if not match.authoritative:
                    report.findings.append(Finding("WARNING", "NON_AUTHORITATIVE_SOURCE", rel, ln,
                        f"{lit} is backed only by an UNOFFICIAL source ({match.source_url}).", literal=lit,
                        suggestion="Find the same value in the manufacturer datasheet."))
                elif match.confidence == "LOOSE":
                    report.findings.append(Finding("INFO", "LOOSE_MATCH", rel, ln,
                        f"{lit}: {match.id} matched under form '{match.match_form}'.", literal=lit))

    for a, b in ledger.conflicts():
        if a.id in used_facts or b.id in used_facts:
            report.findings.append(Finding("ERROR", "FACT_CONFLICT", "", 0,
                f"Conflicting facts used: {a.id}={a.value} vs {b.id}={b.value} for {a.subject}.{a.property}.",
                suggestion="Resolve using the manufacturer datasheet."))

    flow_steps = _check_flows(project_dir, ledger, report)
    report.stats = {"files": len(files), "literals_checked": literals_checked, "justified": justified, "facts_used": len(used_facts),
                    "sequences": len(report.flows), "sequence_steps_matched": flow_steps}
    report.passed = not any(f.severity == "ERROR" for f in report.findings)
    return report


_ADDR_PROP_RE = re.compile(r"(^|_)(i2c|spi|bus|slave|device|dev)?_?addr(ess)?($|_)", re.I)
_ENTRY_RE = re.compile(r"(init|begin|setup|start|config|probe|reset)", re.I)


def _check_flows(project_dir: Path, ledger: FactLedger, report: VerifyReport) -> int:
    """Datasheet sequences vs the code's order and timing (static; see truth/flow.py). Also warns when a device is
    configured by the code but no sequence for it was recorded. Returns the number of matched sequence steps."""
    from embedforge.truth.flow import FlowModel
    from embedforge.truth.sequences import SequenceBook, check_flows
    try:
        model = FlowModel.from_dir(project_dir)
        flows, found = check_flows(project_dir, ledger, model)
    except Exception as e:  # a flow-check bug must never hide the rest of verify
        report.findings.append(Finding("WARNING", "FLOW_CHECK_ERROR", "", 0, f"The flow check could not run: {type(e).__name__}: {e}"))
        return 0
    report.flows = flows
    for sev, code, file, line, msg, sug in found:
        report.findings.append(Finding(sev, code, file, line, msg, suggestion=sug))
    # coverage: a device the code configures needs its datasheet sequence on record
    covered = {r.subject.lower() for r in SequenceBook(project_dir).items.values()}
    addr_facts = {}
    for f in ledger.facts.values():
        n = parse_int(str(f.value))
        if n is not None and 0 < n < 0x400 and _ADDR_PROP_RE.search(f.property):
            addr_facts.setdefault(n, f)
    if addr_facts:
        entries = [name for name in model.functions if "::" not in name and (_ENTRY_RE.search(name) or name in ("main", "setup", "app_main"))]
        reported = set()
        for name in entries[:40]:
            for e in model.trace(name).events:
                f = addr_facts.get(e.addr) if isinstance(e.addr, int) else None
                if f is None or e.kind != "write" or f.subject.lower() in covered or f.subject.lower() in reported:
                    continue
                reported.add(f.subject.lower())
                report.findings.append(Finding(
                    "ERROR", "FLOW_UNSPECIFIED", e.file, e.entry_line,
                    f"{f.subject} (address {f.value}, {f.id}) is configured in {name}() but no datasheet sequence was recorded, "
                    "so the order and timing of its init are not checked.",
                    suggestion="record_sequence with the datasheet's required init/reset order (steps citing register, value and delay facts)."))
    return sum(1 for r in report.flows for s in r["steps"] if s["status"] == "matched")
