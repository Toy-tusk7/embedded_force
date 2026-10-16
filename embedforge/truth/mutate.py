"""Mutation testing of a delivered driver: how strong the verification is on THAT code.

Realistic faults are planted one at a time in a copy of the project (a constant off by one bit, a pin moved, / for *,
a byte shifted to the wrong place, a signed type made unsigned, a delay deleted, a register write sent to the next
register, a comparison inverted, an error path that reports success) and verify runs on each. A fault is caught when
verify reports an error the delivered code does not have. The score and the faults that survived go into the delivery
report: a survivor is a change no check would notice — a gap in what is on record, or a change that does not matter.

  python -m embedforge.truth.mutate <project dir>
"""

import re
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


@dataclass
class Mutant:
    operator: str
    file: str
    line: int
    before: str
    after: str
    what: str
    caught: Optional[bool] = None
    by: str = ""          # the first new error


_CODE_ONLY = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_DELAY = re.compile(r"^\s*(?:[\w.]+\s*\.\s*)?(delay|delayMicroseconds|sleep_ms|sleep_us|busy_wait_us\w*|HAL_Delay|vTaskDelay|"
                    r"k_msleep|k_usleep|usleep|ets_delay_us)\s*\(.*\)\s*;\s*$")
_SIGN = {"uint8_t": "int8_t", "int8_t": "uint8_t", "uint16_t": "int16_t", "int16_t": "uint16_t", "uint32_t": "int32_t",
         "int32_t": "uint32_t"}


def _code(line: str) -> str:
    """The line without comments and string literals (where a mutation must not land)."""
    return re.sub(r'"(\\.|[^"\\])*"', lambda m: " " * len(m.group()), _CODE_ONLY.sub(lambda m: " " * len(m.group()), line))


def _sub_first(line: str, rx: "re.Pattern[str]", repl: Callable[["re.Match[str]"], str]) -> Optional[str]:
    """Replace the first match of rx that lies in code (not a comment or string)."""
    code = _code(line)
    for m in rx.finditer(code):
        new = repl(m)
        if new != m.group():
            return line[:m.start()] + new + line[m.end():]
    return None


def candidates(root: Path, write_funcs: List[str]) -> List[Mutant]:
    """Every planted fault the operators can make in the project's sources."""
    from embedforge.truth.pins import PIN_CALLS
    from embedforge.truth.verifier import collect_sources
    out: List[Mutant] = []
    for path in collect_sources(root):
        rel = str(path.relative_to(root)).replace("\\", "/")
        lines = path.read_text(encoding="utf-8", errors="ignore").split("\n")
        for i, line in enumerate(lines, start=1):
            code = _code(line)
            if not code.strip() or code.strip().startswith("#include"):
                continue

            def add(op: str, new: Optional[str], what: str) -> None:
                if new is not None and new != line:
                    out.append(Mutant(op, rel, i, line, new, what))
            tagged = "FACT-" in line
            if tagged:   # a constant the datasheet states
                add("constant", _sub_first(line, re.compile(r"\b0[xX][0-9A-Fa-f]+\b"),
                                           lambda m: f"0x{int(m.group(), 16) ^ 1:0{max(2, len(m.group()) - 2)}X}"),
                    "a datasheet constant with its lowest bit flipped")
                dm = re.match(r"\s*#\s*define\s+\w+\s+\(?(\d+)\)?", code)
                if dm and "0x" not in code.lower():
                    add("constant", line[:dm.start(1)] + str(int(dm.group(1)) + 1) + line[dm.end(1):], "a datasheet number + 1")
            for call in PIN_CALLS:
                if re.search(rf"\b{call}\s*\(\s*\d+", code):
                    add("pin", _sub_first(line, re.compile(rf"\b{call}\s*\(\s*(\d+)"),
                                          lambda m: m.group().replace(m.group(1), str(int(m.group(1)) + 1))),
                        "a pin moved to the neighbouring GPIO")
            if re.search(r"\d\.\d|\bdouble\b|\bfloat\b|pow", code) and "/" in code and "=" in code:
                add("arith", _sub_first(line, re.compile(r"(?<![/*])/(?![/*=])"), lambda m: "*"), "a conversion multiplies instead of divides")
            if re.search(r"<<\s*(8|16|24)\b", code):
                add("shift", _sub_first(line, re.compile(r"<<\s*(8|16)\b"), lambda m: m.group().replace(m.group(1), "16" if m.group(1) == "8" else "8")),
                    "a byte shifted into the wrong place")
            if re.search(r"\w\[\d+\]", code) and re.search(r"<<|\|", code):
                add("index", _sub_first(line, re.compile(r"(?<=\w)\[(\d+)\]"), lambda m: f"[{int(m.group(1)) + 1}]"),
                    "a value assembled from the wrong byte")
            # the type a stored value is declared with (struct members, globals): casts and parameters change nothing
            # when the value lands in a variable of the declared type
            if re.fullmatch(r"\s*(?:static\s+|volatile\s+|const\s+)*u?int(8|16|32)_t\s+\w+\s*(\[\s*\w*\s*\])?\s*;\s*", code):
                add("sign", _sub_first(line, re.compile(r"\bu?int(8|16|32)_t\b"), lambda m: _SIGN[m.group()]),
                    "a signed value declared unsigned (or the reverse)")
            if _DELAY.match(code) and not _in_loop(lines, i):
                add("delay", "", "a wait deleted")
            for wf in write_funcs:   # a register write sent to the next register, or with one bit of its value flipped
                wm = re.search(rf"\b{re.escape(wf)}\s*\(([^,()]+),\s*([^()]+?)\)", code)
                if wm and not re.search(r"\b(u?int\d+_t|int|char|bool|const|void)\b", wm.group(1) + wm.group(2)):   # a call, not its declaration
                    add("write", line[:wm.start(1)] + f"({wm.group(1).strip()}) + 1" + line[wm.end(1):], "a register write sent to the next register")
                    add("write", line[:wm.start(2)] + f"({wm.group(2).strip()}) ^ 0x01" + line[wm.end(2):], "a register written with one bit wrong")
                    break
            if re.search(r"\bif\s*\(", code) and "!=" in code:
                add("compare", _sub_first(line, re.compile(r"!="), lambda m: "=="), "a check inverted")
            if re.search(r"\breturn\s+false\s*;", code):
                add("error_path", _sub_first(line, re.compile(r"\breturn\s+false\b"), lambda m: "return true"),
                    "a failure reported as success")
    return out


def _in_loop(lines: List[str], i: int) -> bool:
    """Is line i (1-based) inside a loop body or Arduino's loop()? There a delay paces or polls; no datasheet rule needs
    it, so deleting it is not a fault verify could be expected to see."""
    depth = 0
    for j in range(i - 2, -1, -1):
        code = _code(lines[j])
        depth += code.count("}") - code.count("{")
        if depth < 0:   # the line that opens the block around line i
            header = " ".join(_code(x) for x in lines[max(0, j - 1):j + 1])
            if re.search(r"\b(while|for|do)\b|\bvoid\s+loop\s*\(", header):
                return True
            depth = 0   # an if/else block: look further out
    return False


def _select(muts: List[Mutant], per_op: int, total: int) -> List[Mutant]:
    """An even spread: at most per_op of each operator (taken across the files), at most total in all."""
    by: Dict[str, List[Mutant]] = {}
    for m in muts:
        by.setdefault(m.operator, []).append(m)
    picked: List[Mutant] = []
    for op, ms in by.items():
        step = max(1, len(ms) // per_op)
        picked += ms[::step][:per_op]
    return picked[:total]


def run(root: Path, verify: Callable[[Path], List[str]], per_op: int = 6, total: int = 60,
        write_funcs: Optional[List[str]] = None, on_progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, object]:
    """Plant each selected fault in a copy of the project and verify it. `verify(copy)` returns the ERROR findings as
    'CODE file:line' strings."""
    work = Path(tempfile.mkdtemp(prefix="embedforge-mutate-"))
    try:
        copy = work / root.name
        shutil.copytree(root, copy, ignore=shutil.ignore_patterns(".pio", "builds", ".git", "artifacts"))
        base = set(verify(copy))
        muts = _select(candidates(copy, write_funcs or []), per_op, total)
        for k, m in enumerate(muts):
            f = copy / m.file
            text = f.read_text(encoding="utf-8", errors="ignore")
            lines = text.split("\n")
            lines[m.line - 1] = m.after
            f.write_text("\n".join(lines), encoding="utf-8")
            try:
                new = [e for e in verify(copy) if e not in base]
            finally:
                f.write_text(text, encoding="utf-8")
            m.caught, m.by = bool(new), (new[0] if new else "")
            if on_progress:
                on_progress(k + 1, len(muts))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    caught = sum(1 for m in muts if m.caught)
    ops: Dict[str, List[int]] = {}
    for m in muts:
        ops.setdefault(m.operator, [0, 0])
        ops[m.operator][1] += 1
        ops[m.operator][0] += int(bool(m.caught))
    return {"total": len(muts), "caught": caught, "score": round(caught / len(muts), 3) if muts else None,
            "by_operator": {op: f"{c}/{n}" for op, (c, n) in ops.items()},
            "survivors": [{k: v for k, v in asdict(m).items() if k not in ("caught", "by")} for m in muts if not m.caught],
            "caught_by": {f"{m.file}:{m.line} {m.operator}": m.by for m in muts if m.caught}}


def main() -> None:
    from embedforge.agent.events import RunSession
    from embedforge.agent.tools import ToolBelt
    from embedforge.agent.workspace import Project
    from embedforge.truth import FactLedger, SourceStore
    root = Path(sys.argv[1]).resolve()

    def verify(copy: Path) -> List[str]:
        p = Project(root.name, copy)
        store = SourceStore(copy)
        r = ToolBelt(p, RunSession("mutate", root.name), store, FactLedger(copy, store)).verify()
        return [f"{f['code']} {f.get('file')}:{f.get('line') or ''}" for f in r.get("findings", []) if f.get("severity") == "ERROR"]
    res = run(root, verify, write_funcs=write_functions(root), on_progress=lambda k, n: print(f"\r{k}/{n}", end="", flush=True))
    print(f"\ncaught {res['caught']}/{res['total']} ({res['by_operator']})")
    for s in res["survivors"]:
        print(f"  SURVIVED {s['operator']:10} {s['file']}:{s['line']}  {s['what']}\n      {s['before'].strip()}\n   -> {s['after'].strip()}")


def write_functions(root: Path) -> List[str]:
    """The project's own register-write helpers (write_reg, _writeRegister, bmp_write...): their (register, value)
    arguments are what the write operator changes."""
    from embedforge.truth.verifier import collect_sources
    names = set()
    for p in collect_sources(root):
        for m in re.finditer(r"\b(?:[A-Za-z_]\w*::)?((?:[A-Za-z_]\w*)?(?:[Ww]rite|_wr|wr_)\w*)\s*\(\s*(?:const\s+)?u?int8_t\s+\w+\s*,",
                             p.read_text(encoding="utf-8", errors="ignore")):
            names.add(m.group(1))
    return sorted(names)


if __name__ == "__main__":
    main()
