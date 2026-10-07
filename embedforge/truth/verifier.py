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
SRC_DIRS = ("src", "include", "lib")

# Generic literals that carry no hardware claim (masks, sentinels).
GENERIC_OK = {0x00, 0x01, 0x02, 0x03, 0x04, 0x07, 0x0F, 0x10, 0x1F, 0x3F, 0x7F, 0x80, 0xF0, 0xFF, 0xFFFF, 0xFFFFFFFF}

_HEX_RE = re.compile(r"\b0[xX][0-9A-Fa-f]+[uUlL]*\b")
_FACT_TAG_RE = re.compile(r"FACT-\d{3,}")
_DEFINE_RE = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)\s+(.+?)\s*$")
_HW_NAME_RE = re.compile(r"(ADDR|REG|WHO_?AM_?I|CHIP_?ID|DEVICE_?ID|_ID\b|OPCODE|_CMD\b)", re.I)


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

    def public(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "findings": [f.public() for f in self.findings],
            "traceability": self.traceability,
            "stats": self.stats,
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


def collect_sources(project_dir: Path) -> List[Path]:
    files: List[Path] = []
    for d in SRC_DIRS:
        base = project_dir / d
        if base.is_dir():
            files += [p for p in base.rglob("*") if p.is_file() and p.suffix in SRC_EXTS]
    return sorted(files)


def verify_project(project_dir: Path, ledger: FactLedger) -> VerifyReport:
    project_dir = Path(project_dir)
    report = VerifyReport(passed=True)
    int_values = ledger.int_values()
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

            candidates: List[tuple] = []  # (literal text, int value, macro name)
            m = _DEFINE_RE.match(code)
            define_name = m.group(1) if m else ""
            for hm in _HEX_RE.finditer(code):
                lit = hm.group(0)
                n = parse_int(lit.rstrip("uUlL"))
                if n is not None:
                    candidates.append((lit, n, define_name))
            if m and _HW_NAME_RE.search(define_name):
                dm = re.fullmatch(r"\(?\s*(\d+)[uUlL]*\s*\)?", m.group(2).split("//")[0].split("/*")[0].strip())
                if dm:
                    candidates.append((dm.group(1), int(dm.group(1)), define_name))

            for fid in tags:
                fact = ledger.get(fid)
                if fact is None:
                    report.findings.append(Finding("ERROR", "UNKNOWN_FACT_TAG", rel, ln, f"{fid} is not in the Fact Ledger.", suggestion="Record the fact with record_fact first."))

            for lit, n, name in candidates:
                # small "generic" values (masks, 0/1) are skipped unless they name hardware (e.g. #define CTRL_REG 0x01)
                if n in GENERIC_OK and not tags and not (name and _HW_NAME_RE.search(name)):
                    continue
                literals_checked += 1
                tag_facts = [ledger.get(t) for t in tags if ledger.get(t)]
                match = next((f for f in tag_facts if parse_int(f.value) == n), None)
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
                    ids = int_values.get(n, [])
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
                        suggestion="Research it (web_search/fetch_document/read_source), record_fact with a verbatim quote, "
                        "then annotate the line with /* FACT-nnn */. If it is unknown, ask the user.",
                    ))
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

    report.stats = {"files": len(files), "literals_checked": literals_checked, "justified": justified, "facts_used": len(used_facts)}
    report.passed = not any(f.severity == "ERROR" for f in report.findings)
    return report
