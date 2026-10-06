"""The agent's tool belt. Every tool is deterministic infrastructure; the LLM only decides *which* to call."""

import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from embedforge.agent.events import RunSession
from embedforge.agent.workspace import Project
from embedforge.toolchain import pio
from embedforge.truth import FactLedger, SourceStore, verify_project
from embedforge.truth.verifier import collect_sources


def _fn(name: str, desc: str, props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


S = {"type": "string"}


def _arr(items=S):
    return {"type": "array", "items": items}


TOOL_SPECS: List[Dict[str, Any]] = [
    _fn("resolve_target",
        "Search the real PlatformIO board registry for an MCU/board (e.g. 'stm32f401', 'esp32 devkit', 'rp2040 pico'). "
        "Returns board id, MCU, clock, RAM/flash, platform and available SDK frameworks. Never guess a board.",
        {"query": S}, ["query"]),
    _fn("init_project",
        "Create/overwrite platformio.ini for a registry board and a framework supported by that board. "
        "Prefer the vendor SDK framework (stm32cube, espidf, pico/zephyr) over arduino unless the user asks otherwise.",
        {"board": S, "framework": S, "build_flags": _arr(), "lib_deps": _arr()}, ["board", "framework"]),
    _fn("web_search",
        "Search the web (Tavily) for datasheets, GitHub driver code, pinouts, and engineering references (e.g. GitHub, Reddit, StackExchange). "
        "You can search GitHub directly (e.g. 'AT32F421 FD6288 site:github.com' or 'AM32 ESC') or use `domains` "
        "(e.g. ['github.com', 'arterychip.com', 'st.com']). Returns source_ids with trust tiers.",
        {"query": S, "domains": _arr()}, ["query"]),
    _fn("fetch_document",
        "Download a datasheet/register-map PDF or web page by URL and store its full text. Returns a source_id.",
        {"url": S}, ["url"]),
    _fn("read_source",
        "Retrieve the most relevant verbatim excerpts of a stored source for a query (register names, addresses, "
        "'WHO_AM_I', 'slave address'...). Copy quotes from these excerpts exactly when recording facts.",
        {"source_id": S, "query": S, "max_chunks": {"type": "integer"}}, ["source_id", "query"]),
    _fn("record_fact",
        "Record a hardware fact. REJECTED unless `quote` appears verbatim in the cited source and contains `value`. "
        "value examples: '0x68', '400', '3.3'. Returns FACT-nnn to tag in code comments.",
        {"subject": S, "property": S, "value": S, "source_id": S, "quote": S, "unit": S},
        ["subject", "property", "value", "source_id", "quote"]),
    _fn("record_user_fact",
        "Record a fact the USER stated explicitly (MCU model, pin, address, rate...). `quote` must be the user's exact words.",
        {"subject": S, "property": S, "value": S, "quote": S}, ["subject", "property", "value", "quote"]),
    _fn("list_facts", "List all facts currently in the ledger (with FACT ids).", {}, []),
    _fn("search_framework",
        "Regex-search the installed vendor SDK/HAL headers (real local source of truth for HAL function/macro names).",
        {"pattern": S, "extension": S}, ["pattern"]),
    _fn("read_framework_file",
        "Read lines of a header/source from the installed SDK (path as returned by search_framework).",
        {"path": S, "start": {"type": "integer"}, "end": {"type": "integer"}}, ["path"]),
    _fn("write_file", "Create or overwrite a project file (src/, include/, lib/, platformio.ini, notes).",
        {"path": S, "content": S}, ["path", "content"]),
    _fn("replace_in_file", "Replace an exact text snippet in a project file (must match exactly once).",
        {"path": S, "old": S, "new": S}, ["path", "old", "new"]),
    _fn("read_file", "Read a project file.", {"path": S}, ["path"]),
    _fn("list_files", "List project files.", {}, []),
    _fn("build",
        "Run a REAL PlatformIO compile + link for the project. Returns structured compiler diagnostics, flash/RAM "
        "usage and (on success) .elf/.bin/.hex artifacts.",
        {}, []),
    _fn("verify",
        "Deterministic verification: every hardware constant (hex literal / ADDR/REG/ID define) in the sources must be "
        "backed by a ledger fact (tagged `/* FACT-nnn */` on the line). Returns findings with fixes.",
        {}, []),
    _fn("set_plan", "Publish/update the task checklist (markdown with - [ ] / - [x] items) shown to the user.",
        {"markdown": S}, ["markdown"]),
    _fn("ask_user",
        "Stop and ask the user for missing information you must not guess (MCU/board, pins, wiring, address "
        "strap, framework preference...). Ends this turn.",
        {"question": S, "missing": _arr()}, ["question"]),
    _fn("finish",
        "End the task. status='success' is only accepted if the CURRENT sources have a successful build AND a passing "
        "verify. Otherwise use status='blocked' with the exact reason.",
        {"status": {"type": "string", "enum": ["success", "blocked"]}, "summary": S}, ["status", "summary"]),
]


def _truncate(obj: Any, limit: int = 7000) -> Any:
    import json
    s = json.dumps(obj, default=str)
    if len(s) <= limit:
        return obj
    return {"truncated": True, "preview": s[:limit]}


class ToolBelt:
    def __init__(self, project: Project, session: RunSession, store: SourceStore, ledger: FactLedger):
        self.project = project
        self.session = session
        self.store = store
        self.ledger = ledger
        self.ended: Optional[str] = None       # 'ask_user' | 'finish'
        self.final: Dict[str, Any] = {}
        self.state: Dict[str, Any] = {"build_hash": None, "build_ok": False, "verify_hash": None, "verify_ok": False, "last_build": None}
        self._handlers: Dict[str, Callable[..., Any]] = {
            "resolve_target": self.resolve_target, "init_project": self.init_project, "web_search": self.web_search,
            "fetch_document": self.fetch_document, "read_source": self.read_source, "record_fact": self.record_fact,
            "record_user_fact": self.record_user_fact, "list_facts": self.list_facts,
            "search_framework": self.search_framework, "read_framework_file": self.read_framework_file,
            "write_file": self.write_file, "replace_in_file": self.replace_in_file, "read_file": self.read_file,
            "list_files": self.list_files, "build": self.build, "verify": self.verify, "set_plan": self.set_plan,
            "ask_user": self.ask_user, "finish": self.finish,
        }

    # ------------------------------------------------------------ dispatch
    def call(self, name: str, args: Dict[str, Any]) -> Any:
        h = self._handlers.get(name)
        if h is None:
            return {"error": f"Unknown tool '{name}'. Available: {', '.join(self._handlers)}"}
        try:
            return _truncate(h(**args))
        except TypeError as e:
            return {"error": f"Bad arguments for {name}: {e}"}
        except Exception as e:  # tools must never crash the loop
            return {"error": f"{type(e).__name__}: {e}"}

    def source_hash(self) -> str:
        h = hashlib.sha256()
        for p in collect_sources(self.project.root) + [self.project.root / "platformio.ini"]:
            if p.exists():
                h.update(str(p.relative_to(self.project.root)).encode())
                h.update(p.read_bytes())
        return h.hexdigest()

    # ------------------------------------------------------------ target
    def resolve_target(self, query: str) -> Dict[str, Any]:
        boards = pio.list_boards(query, limit=12)
        if not boards:
            u = query.upper()
            if any(k in u for k in ("AT32", "STM32", "CORTEX", "CUSTOM", "ESC")):
                cid = re.sub(r"[^a-z0-9_]+", "_", query.lower()).strip("_") or "custom_board"
                cb = {
                    "id": cid,
                    "name": f"Custom {query} (Cortex-M4 120MHz)",
                    "platform": "ststm32",
                    "mcu": "cortex-m4",
                    "frameworks": ["stm32cube", "cmsis", "generic"],
                    "fcpu_hz": 120_000_000,
                    "flash_bytes": 64 * 1024,
                    "ram_bytes": 16 * 1024,
                }
                return {"found": True, "boards": [cb],
                        "note": f"Specialized/Custom MCU '{query}' mapped to Cortex-M4 CMSIS architecture. Call init_project(board='{cid}', framework='stm32cube')."}
            return {"found": False, "message": f"No board in the PlatformIO registry matches '{query}'. For custom boards, specify board name with Cortex-M architecture."}
        return {"found": True, "boards": boards,
                "note": "Pick the board matching the user's hardware; if several fit and it matters, ask_user."}

    def init_project(self, board: str, framework: str, build_flags: Optional[List[str]] = None, lib_deps: Optional[List[str]] = None) -> Dict[str, Any]:
        match = [b for b in pio.list_boards(board, limit=50) if b["id"] == board]
        if match:
            b = match[0]
            if framework not in (b.get("frameworks") or []):
                return {"error": f"Framework '{framework}' not supported by {board}. Supported: {b.get('frameworks')}"}
            platform = b['platform']
            b_board = board
        else:
            # Custom board definition for unlisted MCUs (e.g. AT32, custom PCB)
            b = {
                "id": board,
                "name": f"Custom {board}",
                "platform": "ststm32",
                "mcu": "cortex-m4",
                "frameworks": [framework or "stm32cube"],
                "fcpu_hz": 120_000_000,
                "flash_bytes": 64 * 1024,
                "ram_bytes": 16 * 1024,
            }
            platform = "ststm32"
            b_board = "genericSTM32F401CD"

        lines = [f"[env:{board}]", f"platform = {platform}", f"board = {b_board}", f"framework = {framework or 'stm32cube'}", "monitor_speed = 115200"]
        if build_flags:
            lines.append("build_flags =\n    " + "\n    ".join(build_flags))
        if lib_deps:
            lines.append("lib_deps =\n    " + "\n    ".join(lib_deps))
        content = "\n".join(lines) + "\n"
        self.project.write_file("platformio.ini", content)
        self.project.update_meta(board=board, mcu=b.get("mcu"), platform=platform, framework=framework,
                                 fcpu_hz=b.get("fcpu_hz"), flash_bytes=b.get("flash_bytes"), ram_bytes=b.get("ram_bytes"))
        self.session.emit("file", path="platformio.ini", content=content)
        self.session.emit("project", meta=self.project.meta())
        return {"ok": True, "board": b, "framework": framework, "platformio_ini": content}

    # ------------------------------------------------------------ research
    def web_search(self, query: str, domains: Optional[List[str]] = None) -> Dict[str, Any]:
        res = self.store.search(query, domains)
        for r in res:
            self.session.emit("source", **r)
        if not res:
            return {"results": [], "note": "No results. Try different keywords or drop the domain filter."}
        return {"results": res}

    def fetch_document(self, url: str) -> Dict[str, Any]:
        src = self.store.fetch(url)
        self.session.emit("source", source_id=src.id, title=src.title, url=src.url, tier=src.tier, chars=src.chars)
        return {"source_id": src.id, "url": src.url, "tier": src.tier, "chars": src.chars, "kind": src.kind,
                "preview": src.text[:500]}

    def read_source(self, source_id: str, query: str, max_chunks: int = 4) -> Dict[str, Any]:
        src = self.store.get(source_id)
        if not src:
            return {"error": f"Unknown source_id {source_id}"}
        ex = self.store.excerpts(source_id, query, max_chunks=max(1, min(int(max_chunks or 4), 6)))
        return {"source_id": source_id, "url": src.url, "tier": src.tier, "excerpts": ex,
                "note": "" if ex else "No passage matched. Try other keywords (e.g. register names)."}

    def record_fact(self, subject: str, property: str, value: Any, source_id: str, quote: str, unit: str = "") -> Dict[str, Any]:
        res = self.ledger.record(subject, property, value, source_id, quote, unit)
        if res.get("ok"):
            self.session.emit("fact", fact=res["fact"], warnings=res.get("warnings", []))
        return res

    def record_user_fact(self, subject: str, property: str, value: Any, quote: str) -> Dict[str, Any]:
        sid = self.store._by_url.get("user://conversation")
        if not sid:
            return {"ok": False, "error": "No user statements stored."}
        res = self.ledger.record(subject, property, value, sid, quote)
        if res.get("ok"):
            self.session.emit("fact", fact=res["fact"], warnings=res.get("warnings", []))
        return res

    def list_facts(self) -> Dict[str, Any]:
        return {"facts": [{k: v for k, v in f.public().items() if k not in ("quote",)} | {"quote": f.quote[:160]}
                          for f in self.ledger.facts.values()]}

    # ------------------------------------------------------------ framework
    def search_framework(self, pattern: str, extension: str = "h") -> Dict[str, Any]:
        roots = pio.framework_roots()
        if not roots:
            return {"error": "No SDK is installed yet. Run init_project and build once to download it."}
        try:
            rx = re.compile(pattern, re.I)
        except re.error:
            rx = re.compile(re.escape(pattern), re.I)
        pk = pio.core_dir() / "packages"
        hits: List[str] = []
        ext = "." + extension.lstrip(".")
        meta_fw = self.project.meta().get("framework", "")
        for root in roots:
            for p in root.rglob(f"*{ext}"):
                try:
                    if p.stat().st_size > 2_000_000:
                        continue
                    for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if rx.search(line):
                            hits.append(f"{p.relative_to(pk).as_posix()}:{i}: {line.strip()[:160]}")
                            if len(hits) >= 40:
                                return {"matches": hits, "truncated": True}
                except Exception:
                    continue
        return {"matches": hits, "framework": meta_fw}

    def read_framework_file(self, path: str, start: int = 1, end: int = 160) -> Dict[str, Any]:
        pk = (pio.core_dir() / "packages").resolve()
        p = (pk / path).resolve()
        if pk not in p.parents or not p.is_file():
            return {"error": "Path must be inside the installed packages directory and exist."}
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        start = max(1, int(start or 1))
        end = min(len(lines), int(end or start + 160), start + 240)
        return {"path": path, "start": start, "end": end, "total_lines": len(lines),
                "text": "\n".join(f"{i}: {lines[i-1]}" for i in range(start, end + 1))}

    # ------------------------------------------------------------ files
    def write_file(self, path: str, content: str) -> Dict[str, Any]:
        res = self.project.write_file(path, content)
        self.session.emit("file", path=res["path"], content=content)
        return res

    def replace_in_file(self, path: str, old: str, new: str) -> Dict[str, Any]:
        text = self.project.read_file(path)
        n = text.count(old)
        if n != 1:
            return {"error": f"'old' text must match exactly once; found {n} occurrences."}
        res = self.project.write_file(path, text.replace(old, new))
        self.session.emit("file", path=res["path"], content=text.replace(old, new))
        return res

    def read_file(self, path: str) -> Dict[str, Any]:
        t = self.project.read_file(path)
        return {"path": path, "content": t[:20000], "truncated": len(t) > 20000}

    def list_files(self) -> Dict[str, Any]:
        return {"files": self.project.list_files()}

    # ------------------------------------------------------------ build / verify
    def build(self) -> Dict[str, Any]:
        self.session.emit("build_start")
        counter = {"n": 0}

        def on_line(line: str) -> None:
            counter["n"] += 1
            if line.strip():
                self.session.emit("build_log", line=line)

        res = pio.build(self.project.root, on_line=on_line)
        h = self.source_hash()
        self.state.update(build_hash=h, build_ok=res.ok, last_build=res.public())
        out = res.public()
        if res.ok and res.artifacts:
            meta = self.project.meta()
            manifest = {
                "board": meta.get("board"), "mcu": meta.get("mcu"), "framework": meta.get("framework"),
                "flash_used": res.flash_used, "flash_total": res.flash_total, "ram_used": res.ram_used, "ram_total": res.ram_total,
                "source_hash": h, "verified": None,
                "sha256": {k: pio.file_sha256(v) for k, v in res.artifacts.items()},
            }
            mf = self.project.register_build(res.artifacts, manifest)
            self.state["build_version"] = mf["version"]
            out["version"] = mf["version"]
            out["hex_available"] = "hex" in res.artifacts
            self.session.emit("build_result", result=out, manifest=mf)
        else:
            self.session.emit("build_result", result=out, manifest=None)
        out.pop("log_tail", None) if res.ok else None
        return out

    def verify(self) -> Dict[str, Any]:
        rep = verify_project(self.project.root, self.ledger)
        h = self.source_hash()
        self.state.update(verify_hash=h, verify_ok=rep.passed)
        # stamp the matching build manifest
        if self.state.get("build_hash") == h and self.state.get("build_version"):
            import json
            mf_path = self.project.root / "builds" / f"v{self.state['build_version']}" / "manifest.json"
            if mf_path.exists():
                mf = json.loads(mf_path.read_text(encoding="utf-8"))
                mf["verified"] = rep.passed
                mf["verification_summary"] = rep.summary()
                mf_path.write_text(json.dumps(mf, indent=2), encoding="utf-8")
                self.session.emit("build_manifest", manifest=mf)
        pub = rep.public()
        self.session.emit("verify", report=pub, summary=rep.summary())
        return {"passed": rep.passed, "summary": rep.summary(), "findings": pub["findings"][:25], "stats": rep.stats}

    # ------------------------------------------------------------ control
    def set_plan(self, markdown: str) -> Dict[str, Any]:
        (self.project.root / "artifacts").mkdir(exist_ok=True)
        (self.project.root / "artifacts" / "task.md").write_text(markdown, encoding="utf-8")
        self.session.emit("plan", markdown=markdown)
        return {"ok": True}

    def ask_user(self, question: str, missing: Optional[List[str]] = None) -> Dict[str, Any]:
        self.ended = "ask_user"
        self.final = {"question": question, "missing": missing or []}
        return {"ok": True, "note": "Waiting for the user."}

    def finish(self, status: str, summary: str) -> Dict[str, Any]:
        h = self.source_hash()
        if status == "success":
            problems = []
            if not (self.state["build_ok"] and self.state["build_hash"] == h):
                problems.append("current sources have no successful build (call build)")
            if not (self.state["verify_ok"] and self.state["verify_hash"] == h):
                problems.append("current sources have no passing verify (call verify)")
            if problems:
                return {"error": "Cannot finish with success: " + "; ".join(problems) + "."}
        self.ended = "finish"
        self.final = {"status": status, "summary": summary}
        return {"ok": True}
