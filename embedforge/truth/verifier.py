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

from embedforge.truth.flow import is_int
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
# const int LED_PIN = 25;  static constexpr uint8_t SDA = 16;  (a pin when named like one or passed to a pin call)
_CONST_INT_RE = re.compile(r"^\s*(?:static\s+)?(?:(?:constexpr|const)\s+)+(?:unsigned\s+|signed\s+)?[A-Za-z_][\w:]*\s+"
                           r"([A-Za-z_]\w*)\s*=\s*\(?\s*(\d{1,3})[uU]?\s*\)?\s*;")


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
    registers: Dict[str, Any] = field(default_factory=dict)     # register map / layouts / formulas checked against the code

    def public(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "findings": [f.public() for f in self.findings],
            "traceability": self.traceability,
            "stats": self.stats,
            "flows": self.flows,
            "registers": self.registers,
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


def build_inputs_hash(project_dir: Path) -> str:
    """Hash of every build input: equal hashes mean a build or verify result still describes the current sources."""
    import hashlib
    project_dir = Path(project_dir)
    h = hashlib.sha256()
    for p in collect_build_inputs(project_dir):
        if p.exists():
            h.update(str(p.relative_to(project_dir)).replace("\\", "/").encode())
            h.update(p.read_bytes())
    return h.hexdigest()


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


def verify_project(project_dir: Path, ledger: FactLedger, pico: bool = False, chip: str = "") -> VerifyReport:
    """`pico`: the target is an RP2040/RP2350, so GPIO n may be written n, GPIOn, GPn or (Betaflight) PAn. `chip`: the
    MCU/board names (e.g. 'RP2350 weact_rp2350b'), for the pins the package has and the RP2350's extra UART pins."""
    from embedforge.truth import pins as pinmod
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

    file_lines: Dict[Path, List[Dict[str, str]]] = {}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:   # still checked: a Latin-1 comment must not hide the file's constants
            text = path.read_bytes().decode("latin-1")
        file_lines[path] = _strip_comments(text)
    # names the code passes to pin calls (gpio_put(DM388_CS, 0)): their definitions are wiring claims too
    pin_names: Dict[str, str] = {}
    for parts_list in file_lines.values():
        for parts in parts_list:
            for call, _recv, arg in pinmod.pin_args(parts["code"]):
                if re.fullmatch(r"[A-Za-z_]\w*", arg):
                    pin_names.setdefault(arg, pinmod.PIN_CALLS[call] or pinmod.role_of(arg))

    for path in files:
        rel = str(path.relative_to(project_dir)).replace("\\", "/")
        # a FACT tag in a comment on the previous line also covers the next line
        prev_tags: List[str] = []
        for ln, parts in enumerate(file_lines[path], start=1):
            code, comment = parts["code"], parts["comment"]
            tags = _FACT_TAG_RE.findall(comment) or prev_tags
            prev_tags = _FACT_TAG_RE.findall(comment) if (not code.strip() and comment.strip()) else []
            stripped = code.strip()
            if not stripped or stripped.startswith(("#include", "#pragma", "#if", "#endif", "#else")):
                continue

            candidates: List[tuple] = []  # (literal text, key, macro name, is_pin, pin role)
            m = _DEFINE_RE.match(code)
            cm = None if m else _CONST_INT_RE.match(code)
            define_name = m.group(1) if m else cm.group(1) if cm else ""
            is_pin_define = bool((m or cm) and (_PIN_NAME_RE.search(define_name) or define_name in pin_names))
            raw = m.group(2).split("//")[0].split("/*")[0].strip() if m else cm.group(2) if cm else ""
            if is_pin_define and pin_key(raw, pico):
                role = pinmod.role_of(define_name) or pin_names.get(define_name, "")
                candidates.append((raw.strip("() "), pin_key(raw, pico), define_name, True, role))
            else:
                for hm in _HEX_RE.finditer(code):
                    lit = hm.group(0)
                    n = parse_int(lit.rstrip("uUlL"))
                    if n is not None:
                        candidates.append((lit, n, define_name, False, ""))
                if m and _HW_NAME_RE.search(define_name):
                    dm = re.fullmatch(r"\(?\s*(\d+)[uUlL]*\s*\)?", raw)
                    if dm:
                        candidates.append((dm.group(1), int(dm.group(1)), define_name, False, ""))
            for call, _recv, arg in pinmod.pin_args(code):   # Wire.setSDA(16), pinMode(25, OUTPUT)
                lp = pinmod.literal_pin(arg)
                if lp is not None:
                    candidates.append((str(lp), pin_key(str(lp), pico), f"{call}()", True, pinmod.PIN_CALLS[call]))

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

            for lit, n, name, is_pin, role in candidates:
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
                    if re.fullmatch(r"[A-Z][A-Z0-9_]*_PIN", match.property) and name and not name.endswith("()") \
                            and match.property != name:
                        report.findings.append(Finding(
                            "ERROR", "FACT_DEFINE_MISMATCH", rel, ln,
                            f"{name} cites {match.id}, which was recorded for {match.property}.", literal=lit,
                            suggestion=f"record_board_pins with define='{name}' and this pin's board line (add replace: true if "
                                       f"{name} was recorded for another pin), or cite the right fact."))
                        continue
                    fact_role = pinmod.role_of(match.property)
                    if role and fact_role and fact_role != role:   # setSDA(17) citing the SCL pin's fact
                        right = [f"{f.id} {f.property}={f.value}" for f in ledger.facts.values()
                                 if pinmod.role_of(f.property) == role and pin_key(str(f.value), pico)]
                        report.findings.append(Finding(
                            "ERROR", "PIN_ROLE_MISMATCH", rel, ln,
                            f"{name} uses {lit} as {role}, but {match.id} records {match.value} as {match.property} ({fact_role}).",
                            literal=lit, suggestion=("The schematic's " + role + " pin: " + ", ".join(right[:3]) + ". Swap the pins."
                                                     if right else f"Use the schematic's {role} pin (query_board) and cite its fact.")))
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

    if pico:
        report.findings += _pin_capability(project_dir, file_lines, chip)
    from embedforge.truth import robust
    texts = {str(path.relative_to(project_dir)).replace("\\", "/"): [p["code"] for p in parts_list]
             for path, parts_list in file_lines.items()}
    for rel, lines in texts.items():
        for line, msg in robust.unbounded_waits(lines):
            report.findings.append(Finding("ERROR", "UNBOUNDED_WAIT", rel, line, msg,
                                           suggestion="Bound the wait: a timeout (uint32_t t0 = millis(); while (!ready && "
                                                      "millis() - t0 < 100) {...}) or a retry limit, then handle the timeout "
                                                      "(return an error, or continue without the device)."))
    for rel, line, msg in robust.failures_reported_as_success(texts):
        report.findings.append(Finding("ERROR", "FAILURE_AS_SUCCESS", rel, line, msg,
                                       suggestion="Return the failure (false, or the negative error code) from this branch."))
    for rel, line, call, msg in robust.ignored_results(texts):
        report.findings.append(Finding("ERROR", "RESULT_IGNORED", rel, line, msg,
                                       suggestion=f"Check what {call}() returns and handle the failure (return an error, retry "
                                                  "a bounded number of times); write (void){call}(...) only where ignoring it "
                                                  "is safe (MISRA C:2012 Rule 17.7)."))

    for a, b in ledger.conflicts():
        if a.id in used_facts or b.id in used_facts:
            report.findings.append(Finding("ERROR", "FACT_CONFLICT", "", 0,
                f"Conflicting facts used: {a.id}={a.value} vs {b.id}={b.value} for {a.subject}.{a.property}.",
                suggestion="Resolve using the manufacturer datasheet."))

    flow_steps = _check_flows(project_dir, ledger, report)
    _check_registers(project_dir, ledger, report)
    report.findings.extend(_check_flash_size(project_dir, ledger))
    report.stats = {"files": len(files), "literals_checked": literals_checked, "justified": justified, "facts_used": len(used_facts),
                    "sequences": len(report.flows), "sequence_steps_matched": flow_steps,
                    "formulas_matched": sum(1 for x in report.registers.get("formulas", []) if "matches the code" in x)}
    report.passed = not any(f.severity == "ERROR" for f in report.findings)
    return report


def _pin_capability(project_dir: Path, stripped: Dict[Path, List[Dict[str, str]]], chip: str) -> List[Finding]:
    """RP2040/RP2350: each Wire/SPI/Serial pin call names a GPIO that the instance can actually route to (the chip's GPIO
    function table), and a GPIO the package has. A pin the peripheral cannot reach is silently not connected."""
    from embedforge.truth import pins as pinmod
    rp2350 = bool(re.search(r"235[04]", chip))
    max_gpio = 47 if re.search(r"235[04]_?b|rp235[04]b", chip, re.I) else 29 if chip else 47
    chip_name = ("RP2350B" if max_gpio == 47 else "RP2350A") if rp2350 else "RP2040" if chip else "RP2040/RP2350"
    values: Dict[str, int] = {}
    code_all: List[str] = []
    for parts_list in stripped.values():
        for p in parts_list:
            code = p["code"]
            if code.strip().startswith("#include"):
                continue
            code_all.append(code)
            m = _DEFINE_RE.match(code) or _CONST_INT_RE.match(code)
            if m:
                v = re.fullmatch(r"\(?\s*(\d{1,3})[uU]?\s*\)?", m.group(2).split("//")[0].split("/*")[0].strip())
                if v:
                    values[m.group(1)] = int(v.group(1))
    text = "\n".join(code_all)
    used = {fam: [n for n in names if re.search(rf"\b{n}\b", text)] for fam, names in pinmod.INSTANCES.items()}
    out: List[Finding] = []
    for path, parts_list in stripped.items():
        rel = str(path.relative_to(project_dir)).replace("\\", "/")
        for ln, p in enumerate(parts_list, start=1):
            for call, recv, arg in pinmod.pin_args(p["code"]):
                gpio = pinmod.literal_pin(arg)
                gpio = values.get(arg) if gpio is None else gpio
                if gpio is None:
                    continue
                if gpio > max_gpio:
                    out.append(Finding("ERROR", "PIN_NOT_ON_CHIP", rel, ln, f"{call}({arg}): GPIO{gpio} does not exist on the "
                                       f"{chip_name} (GPIO0-{max_gpio}).", suggestion="Use the GPIO the schematic connects."))
                    continue
                fam = pinmod.family(call, recv)
                if not fam:
                    continue
                inst = pinmod.INSTANCES[fam].get(recv)
                obj = recv
                if inst is None:   # a member or reference (_wire): the one instance of that kind the code uses
                    if len(used[fam]) != 1:
                        continue
                    obj = used[fam][0]
                    inst = pinmod.INSTANCES[fam][obj]
                role = pinmod.PIN_CALLS[call]
                fn = pinmod.rp_function(fam, gpio, rp2350)
                if fn == (inst, role):
                    continue
                legal = pinmod.legal_pins(fam, inst, role, rp2350, max_gpio)
                via = f" ({recv} is {obj})" if recv != obj else ""
                out.append(Finding(
                    "ERROR", "PIN_CAPABILITY", rel, ln,
                    f"{recv or obj}.{call}({arg}){via}: GPIO{gpio} cannot be {fam}{inst} {role} on the {chip_name}"
                    + (f"; it is {fam}{fn[0]} {fn[1]}" if fn else "") + ". The peripheral would not be connected to that pin.",
                    suggestion=(f"{fam}{inst} {role} is on GPIO {', '.join(map(str, legal))}. " if legal else "")
                               + (f"For GPIO{gpio} use {next((n for n, i in pinmod.INSTANCES[fam].items() if i == fn[0]), fam + str(fn[0]))}"
                                  f" as {fn[1]}, or " if fn else "")
                               + "check the schematic's pin with query_board."))
    return out


# the board's program flash as a request states it: 'board weact_rp2350b with 2 MB flash', '4 MB of flash'. Not an
# external memory chip on the board ('16 MB SPI flash chip', 'W25Q128 flash'), which the build does not size
_FLASH_STATED = re.compile(r"(?<![\w.])(\d{1,2})\s*(?:MB|MiB|M\b)\s*(?:of\s+)?(?:on-?board\s+|program\s+|q?spi\s+)?flash\b"
                           r"(?!\s*(?:chip|memory|ic\b|module|storage|card))", re.I)


def _check_flash_size(project_dir: Path, ledger: FactLedger) -> List[Finding]:
    """A flash size the user states for the board must be the one the build is sized for: the TMP117 runs were asked for
    'weact_rp2350b with 2 MB flash' and left platformio.ini at the board's default 16 MB (the BMP388 run had set
    board_upload.maximum_size = 2097152). On a 2 MB part an image or filesystem laid out for 16 MB does not fit."""
    ini = Path(project_dir) / "platformio.ini"
    if not ini.exists():
        return []
    stated = None
    for src in ledger.store.sources.values():
        if src.tier != "USER":
            continue
        for line in src.text.splitlines():
            m = _FLASH_STATED.search(line)
            if m and not re.search(r"\bexternal\b|\bW25|\bAT25|\bIS25|\bMX25|\bGD25", line, re.I):
                stated = int(m.group(1))
                break
        if stated:
            break
    if not stated:
        return []
    text = ini.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"^\s*board_upload\.maximum_size\s*=\s*(\d+)", text, re.M)
    want = stated * 1024 * 1024
    if m and int(m.group(1)) == want:
        return []
    have = f"sets board_upload.maximum_size = {m.group(1)}" if m else "does not set board_upload.maximum_size (the build assumes the board's default)"
    line = next((k for k, ln in enumerate(text.splitlines(), 1) if ln.strip().startswith("[env")), 1)
    return [Finding("ERROR", "BUILD_FLASH_SIZE", "platformio.ini", line,
                    f"The request says the board has {stated} MB of flash, but platformio.ini {have}.",
                    suggestion=f"Add 'board_upload.maximum_size = {want}' to the [env] section of platformio.ini "
                               "(replace_in_file), then build and verify again.")]


def _check_registers(project_dir: Path, ledger: FactLedger, report: VerifyReport) -> None:
    """Meaning checks (truth/registers.py): register fields, byte layouts, burst reads and formulas. A device the code
    configures needs its register map on record, like it needs its sequence."""
    from embedforge.truth.flow import FlowModel
    from embedforge.truth.registers import RegisterBook, check_registers, coverage_findings
    try:
        model = FlowModel.from_dir(project_dir)
        summary, found = check_registers(project_dir, ledger, model)
        found = found + coverage_findings(project_dir, ledger, model)
    except Exception as e:  # never hide the rest of verify, and never pass code the check could not look at
        report.findings.append(Finding("ERROR", "REGISTER_CHECK_ERROR", "", 0, f"The register check could not run: {type(e).__name__}: {e}",
                                       suggestion="This is an EmbedForge analysis failure, not necessarily a bug in the code: "
                                                  "report it to the user; simplifying the construct named above may help."))
        return
    report.registers = summary
    for f in found:
        report.findings.append(Finding(f.severity, f.code, f.file, f.line, f.message, suggestion=f.suggestion))
    if RegisterBook(project_dir).empty():
        configured = {x.message.split(" (address")[0] for x in report.findings
                      if x.code == "FLOW_UNSPECIFIED" and not x.message.startswith("An SPI device")}
        configured |= _configured_devices(project_dir, ledger, report)
        if getattr(report, "spi_configured", False):
            configured.add("The SPI device")
        for dev in sorted(configured):
            report.findings.append(Finding(
                "ERROR", "REG_MAP_MISSING", "", 0,
                f"{dev} is configured by the code but no register map was recorded, so what its register values MEAN (fields, "
                "byte order, formulas) is unchecked.",
                suggestion="record_registers (fields of every register the code writes or tests), record_layouts (every multi-byte "
                           "value), record_formula (every conversion), then verify again."))


def _configured_devices(project_dir: Path, ledger: FactLedger, report: VerifyReport) -> set:
    """Subjects of address facts whose device the traced code writes to (from the sequence results)."""
    subjects = set()
    addr_subject = {}
    for f in ledger.facts.values():
        n = address_value(f)
        if n is not None:
            addr_subject.setdefault(n, f.subject)
    if not addr_subject:
        return subjects
    for r in report.flows:
        if any(s["status"] in ("matched", "conditional", "value", "order") for s in r.get("steps", [])):
            subjects.add(r.get("subject", ""))
    return {s for s in subjects if s and s.lower() in {v.lower() for v in addr_subject.values()}}


_ADDR_PROP_RE = re.compile(r"(^|_)(i2c|spi|bus|slave|device|dev)?_?addr(ess)?($|_)", re.I)


def address_value(fact: Any) -> Optional[int]:
    """The bus address an address fact gives, else None. TI prints 7-bit addresses in binary ('1001000x' in the TMP117's
    address table): seven binary digits are read as binary, not as the decimal 1001000 (which hid the TMP117's address
    from every check that finds a device by it)."""
    if not _ADDR_PROP_RE.search(str(getattr(fact, "property", ""))):
        return None
    v = str(getattr(fact, "value", "")).strip()
    n = int(v, 2) if re.fullmatch(r"[01]{7}", v) else parse_int(v)
    return n if n is not None and 0 < n < 0x400 else None
_ENTRY_RE = re.compile(r"(init|begin|setup|start|config|probe|reset)", re.I)


def _check_flows(project_dir: Path, ledger: FactLedger, report: VerifyReport) -> int:
    """Datasheet sequences vs the code's order and timing (static; see truth/flow.py). Also warns when a device is
    configured by the code but no sequence for it was recorded. Returns the number of matched sequence steps."""
    from embedforge.truth.flow import FlowModel
    from embedforge.truth.sequences import SequenceBook, check_flows
    try:
        model = FlowModel.from_dir(project_dir)
        flows, found = check_flows(project_dir, ledger, model)
    except Exception as e:  # a flow-check bug must never hide the rest of verify, nor let unchecked code pass
        report.findings.append(Finding("ERROR", "FLOW_CHECK_ERROR", "", 0, f"The flow check could not run: {type(e).__name__}: {e}",
                                       suggestion="This is an EmbedForge analysis failure, not necessarily a bug in the code: "
                                                  "report it to the user; simplifying the construct named above may help."))
        return 0
    report.flows = flows
    for sev, code, file, line, msg, sug in found:
        report.findings.append(Finding(sev, code, file, line, msg, suggestion=sug))
    # coverage: a device the code configures needs its datasheet sequence on record, and bus traffic the analysis
    # cannot follow must not pass as checked
    sequences = SequenceBook(project_dir).items.values()
    covered = {r.subject.lower() for r in sequences}
    addr_facts = {}
    for f in ledger.facts.values():
        n = address_value(f)
        if n is not None:
            addr_facts.setdefault(n, f)
    entries = [name for name in model.functions if "::" not in name and (_ENTRY_RE.search(name) or name in ("main", "setup", "app_main"))]
    reported, opaque_seen = set(), set()
    spi_write = None
    for name in entries[:40]:
        try:
            tr = model.trace(name)
        except Exception as e:   # e.g. RecursionError on deeply nested code: say so, do not crash verify
            report.findings.append(Finding("ERROR", "FLOW_CHECK_ERROR", "", 0,
                                           f"Tracing {name}() failed: {type(e).__name__}: {str(e)[:200]}"))
            continue
        for o in tr.opaque:
            call = o[:-2] if o.endswith("()") else o
            if _BUS_CALL_RE.match(call) and call not in opaque_seen:
                opaque_seen.add(call)
                report.findings.append(Finding(
                    "ERROR", "FLOW_OPAQUE_BUS", "", 0,
                    f"{name}() talks to the bus through {call}(), which verify does not model, so that traffic (registers, "
                    "values, order) is unchecked.",
                    suggestion="Use a bus call verify follows (pico-sdk i2c_write_blocking/i2c_read_blocking/spi_*_blocking, "
                               "HAL_I2C_Mem_Read/Write, HAL_SPI_*, ESP-IDF i2c_master_*, Zephyr i2c_burst_read_dt/"
                               "i2c_reg_write_byte_dt, Arduino Wire/SPI.transfer), or report it to the user."))
        for e in tr.events:
            if e.kind == "write" and e.bus == "spi" and is_int(e.reg) and spi_write is None:
                spi_write = (name, e)
            f = addr_facts.get(e.addr) if isinstance(e.addr, int) else None
            if f is None or e.kind != "write" or f.subject.lower() in covered or f.subject.lower() in reported:
                continue
            reported.add(f.subject.lower())
            report.findings.append(Finding(
                "ERROR", "FLOW_UNSPECIFIED", e.file, e.entry_line,
                f"{f.subject} (address {f.value}, {f.id}) is configured in {name}() but no datasheet sequence was recorded, "
                "so the order and timing of its init are not checked.",
                suggestion="record_sequence with the datasheet's required init/reset order (steps citing register, value and delay facts)."))
    if spi_write is not None:   # SPI traffic carries no bus address: any SPI register write needs the sequence and the map
        report.spi_configured = True
        if not covered:
            name, e = spi_write
            report.findings.append(Finding(
                "ERROR", "FLOW_UNSPECIFIED", e.file, e.entry_line,
                f"An SPI device is configured in {name}() (register {e.reg:#04x}) but no datasheet sequence was recorded, so the "
                "order and timing of its init are not checked.",
                suggestion="record_sequence with the datasheet's required init/reset order (steps citing register, value and delay facts)."))
    return sum(1 for r in report.flows for s in r["steps"] if s["status"] == "matched")


# bus transfers through SDK calls the flow model does not follow (i2c_write_burst_blocking, spi_write_dt, i2c_transfer_dt,
# spi_device_polling_transmit, HAL_I2C_Master_Seq_Transmit_IT): their register traffic would go unchecked
_BUS_CALL_RE = re.compile(r"^(?:[A-Za-z0-9]+_)*(?:i2c|spi|twi|smbus)\w*?(?:read|write|transfer|transceive|xfer|transmit|receive|"
                          r"cmd_begin|exec)\w*$", re.I)
