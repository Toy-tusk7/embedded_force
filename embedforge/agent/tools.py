"""The agent's tool belt. Every tool is deterministic infrastructure; the LLM only decides *which* to call."""

import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from embedforge import eda
from embedforge.agent.events import RunSession
from embedforge.agent.workspace import Project
from embedforge.toolchain import betaflight, pio, wsl
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
        "Search the real PlatformIO board registry by MCU part number or board (e.g. 'stm32f401', 'at32f421', 'rp2354b', 'esp32 devkit'). "
        "Allow-listed platforms not in the registry (ArteryTek AT32; RP2350/RP2354 via the community Arduino-Pico platform) are "
        "installed automatically. Returns board id, MCU, clock, RAM/flash, SDK frameworks and, for part families without an "
        "exact board, a `note` on which board to use. Never guess or substitute a different MCU.",
        {"query": S}, ["query"]),
    _fn("init_project",
        "Create platformio.ini for an exact board id from resolve_target and one of its frameworks, then download the "
        "toolchain and SDK so search_framework works immediately. Prefer the vendor SDK framework "
        "(stm32cube, at32firmlib, espidf, picosdk) over arduino unless the user asks otherwise. `options` takes board "
        "overrides such as 'board_upload.maximum_size = 2097152' (only board_build.*, board_upload.*, upload_protocol, build_unflags).",
        {"board": S, "framework": S, "build_flags": _arr(), "lib_deps": _arr(), "options": _arr()}, ["board", "framework"]),
    _fn("web_search",
        "Search the web (Tavily) for official datasheets, reference manuals and register maps. Prefer `domains` with the "
        "manufacturer's site. Each result has a source_id, trust tier and an `excerpt` (the passage best matching your query); "
        "datasheet PDFs are downloaded in full automatically. Do not repeat a search — use read_source on what you have.",
        {"query": S, "domains": _arr()}, ["query"]),
    _fn("fetch_document",
        "Download a datasheet/register-map PDF or web page by URL and store its full text. Only needed for URLs that "
        "web_search did not already store in full. Returns a source_id.",
        {"url": S}, ["url"]),
    _fn("read_source",
        "Retrieve the most relevant verbatim excerpts of a stored source for a query (register names, addresses, "
        "'WHO_AM_I', 'slave address'...). Copy quotes from these excerpts exactly when recording facts.",
        {"source_id": S, "query": S, "max_chunks": {"type": "integer"}}, ["source_id", "query"]),
    _fn("record_fact",
        "Record a hardware fact. REJECTED unless `quote` appears verbatim in the cited source and contains `value`. "
        "value examples: '0x68', '400', '3.3'. Returns FACT-nnn to tag in code comments. On rejection, "
        "`candidate_quotes` lists exact passages containing the value that you can retry with.",
        {"subject": S, "property": S, "value": S, "source_id": S, "quote": S, "unit": S},
        ["subject", "property", "value", "source_id", "quote"]),
    _fn("record_user_fact",
        "Record a fact the USER stated explicitly (MCU model, pin, address, rate...). `quote` must be the user's exact words.",
        {"subject": S, "property": S, "value": S, "quote": S}, ["subject", "property", "value", "quote"]),
    _fn("list_facts", "List all facts currently in the ledger (with FACT ids).", {}, []),
    _fn("query_board",
        "Look up the user's imported schematic/netlist (EasyEDA, Protel, PADS, KiCad): a designator ('U1'), a net ('HIN_A'), "
        "an MCU pin name ('PA8') or a part number ('FD6288Q'); empty query returns the board summary. This is the ground truth "
        "for pins and wiring. Each returned line is verbatim text of the board source — quote one with "
        "record_fact(source_id=<returned source_id>) to turn a connection into a fact.",
        {"query": S}, []),
    _fn("init_betaflight",
        "Make this project a Betaflight board: a custom config built against an in-tree Betaflight target (e.g. RP2350B "
        "for RP2350B/RP2354B, STM32F405, STM32H743, AT32F435...). Installs the pinned Betaflight release and its toolchain in "
        "WSL on first use. For RP2350/RP2354 boards pass flash_mb (RP2354A/B: 2) so the flash size is configured. Returns "
        "the board config path to write and real reference configs for the same target.",
        {"board_name": S, "target": S, "flash_mb": {"type": "integer"}}, ["board_name", "target"]),
    _fn("search_framework",
        "Regex-search the real SDK the project builds against: the installed vendor SDK/HAL headers, or for a Betaflight "
        "project the Betaflight source and its board configs (define names such as GYRO_1_CS_PIN, SDCARD_SPI_INSTANCE).",
        {"pattern": S, "extension": S}, ["pattern"]),
    _fn("read_framework_file",
        "Read lines of a file returned by search_framework (SDK header, or a Betaflight source/config file).",
        {"path": S, "start": {"type": "integer"}, "end": {"type": "integer"}}, ["path"]),
    _fn("write_file", "Create or overwrite a project file under src/, include/, lib/, test/ or docs/ (or platformio.ini). "
        "Build scripts (*.py, extra_scripts, library.json) are not allowed.",
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


# ------------------------------------------------------------------ write policy
# platformio.ini keys that make PlatformIO execute arbitrary code or commands on the host during a build.
_INI_FORBIDDEN_KEYS = {
    "extra_scripts", "platform_packages", "upload_command", "debug_server", "debug_init_cmds",
    "debug_extra_cmds", "debug_load_cmds", "test_testing_command",
}
_REGISTRY_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+(@[A-Za-z0-9_.~^<>=\-\s]+)?$")
_LIB_DEP_RE = re.compile(r"^[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)?(\s*@\s*[A-Za-z0-9_.~^<>=\-\s]+)?$")
_AGENT_WRITABLE_DIRS = ("src", "include", "lib", "test", "docs", "betaflight")


def check_platformio_ini(text: str) -> Optional[str]:
    """Return an error message if platformio.ini content could execute code on the host, else None."""
    key = ""
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.lstrip().startswith("["):
            key = ""
            continue
        is_cont = raw[:1] in (" ", "\t") and key
        if not is_cont:
            if "=" not in line:
                continue
            key, value = (p.strip() for p in line.split("=", 1))
            key = key.lower()
        else:
            value = line.strip()
        if key in _INI_FORBIDDEN_KEYS:
            return f"'{key}' is not allowed in platformio.ini (it runs code on the host)."
        if value.startswith("!"):
            return f"Dynamic '!command' values are not allowed in platformio.ini ({key})."
        if key == "platform" and value:
            allowed_urls = {spec["url"] for spec in pio.VENDOR_PLATFORMS.values()}
            if value not in allowed_urls and not _REGISTRY_NAME_RE.match(value):
                return f"platform must be a PlatformIO registry name or an allow-listed vendor platform, not '{value}'."
        if key == "lib_deps" and value and not _LIB_DEP_RE.match(value):
            return f"lib_deps entry '{value}' must be a PlatformIO registry library (owner/name@version), not a URL or path."
    return None


def check_agent_write(path: str, content: str) -> Optional[str]:
    """Policy for files the agent (LLM) may create: firmware sources, docs and a safe platformio.ini only."""
    rel = (path or "").replace("\\", "/").lstrip("/")
    name = rel.rsplit("/", 1)[-1].lower()
    if rel == "platformio.ini":
        return check_platformio_ini(content)
    if name in ("library.json", "extra_script.py") or name.endswith((".py", ".mk", ".sh")) or name in ("makefile", "gnumakefile"):
        return f"The agent may not write '{name}' (build scripts run code on the host)."
    if rel.startswith("betaflight/") and not re.fullmatch(r"betaflight/configs/(?:CUST/)?[A-Za-z0-9_\-]{1,40}/config\.[ch]", rel):
        return ("Betaflight boards live at betaflight/configs/CUST/<BOARD>/config.h (and optional config.c); "
                "flash-size files are generated by init_betaflight(flash_mb=...).")
    top = rel.split("/", 1)[0] if "/" in rel else ""
    if top not in _AGENT_WRITABLE_DIRS and not (not top and name.endswith((".md", ".txt"))):
        return f"Write firmware under {', '.join(d + '/' for d in _AGENT_WRITABLE_DIRS)} (or notes as *.md at the root)."
    return None


def _looks_like_datasheet(url: str) -> bool:
    u = url.lower()
    return u.split("?")[0].endswith(".pdf") or any(k in u for k in ("datasheet", "/ds/", "reference-manual", "referencemanual", "/rm/", "user-manual"))


class ToolBelt:
    def __init__(self, project: Project, session: RunSession, store: SourceStore, ledger: FactLedger):
        self.project = project
        self.session = session
        self.store = store
        self.ledger = ledger
        self.ended: Optional[str] = None       # 'ask_user' | 'finish'
        self.final: Dict[str, Any] = {}
        self._searches: Dict[str, List[Dict[str, Any]]] = {}  # normalized query -> results (per run)
        self.state: Dict[str, Any] = {"build_hash": None, "build_ok": False, "verify_hash": None, "verify_ok": False, "last_build": None}
        self._handlers: Dict[str, Callable[..., Any]] = {
            "resolve_target": self.resolve_target, "init_project": self.init_project, "init_betaflight": self.init_betaflight,
            "web_search": self.web_search,
            "fetch_document": self.fetch_document, "read_source": self.read_source, "record_fact": self.record_fact,
            "record_user_fact": self.record_user_fact, "list_facts": self.list_facts, "query_board": self.query_board,
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
    def _log(self, stream: str) -> Callable[[str], None]:
        def sink(line: str) -> None:
            if line.strip():
                self.session.emit("log", stream=stream, line=line)
        return sink

    def resolve_target(self, query: str) -> Dict[str, Any]:
        boards = pio.list_boards(query, limit=12)
        vp = pio.vendor_platform_for(query)
        spec = pio.VENDOR_PLATFORMS.get(vp or "", {})
        if vp and (not boards or spec.get("family")) and not pio.platform_installed(vp):
            kind = "official" if spec.get("official") else "community"
            self.session.emit("status", text=f"Installing the {kind} {spec['vendor']} PlatformIO platform ({vp}) — one-time download…")
            inst = pio.install_platform(vp, on_line=self._log("platform"))
            if not inst["ok"]:
                return {"found": False, "error": f"Installing vendor platform '{vp}' failed: {inst['error'][-600:]}"}
            boards = pio.list_boards(query, limit=12)
        if vp and not boards and spec.get("family"):
            boards = pio.list_boards(spec["family"], limit=40)
            if boards:
                return {"found": True, "boards": boards, "platform_source": spec["url"],
                        "official_platform": bool(spec.get("official")),
                        "note": spec.get("note", "No board for this exact part; these boards use the same MCU family.")}
        if not boards:
            return {
                "found": False,
                "message": (
                    f"No board/MCU in the PlatformIO registry matches '{query}'. EmbedForge never substitutes a "
                    "different chip — firmware built for another MCU is not valid. Retry with just the MCU part "
                    "number or family (e.g. 'stm32f401', 'at32f421', 'esp32'), or ask_user for a supported board."
                ),
            }
        return {"found": True, "boards": boards,
                "note": "Pick the board whose MCU exactly matches the user's part number; if several fit and it matters, ask_user."}

    def init_project(self, board: str, framework: str, build_flags: Optional[List[str]] = None, lib_deps: Optional[List[str]] = None,
                     options: Optional[List[str]] = None) -> Dict[str, Any]:
        match = [b for b in pio.list_boards(board, limit=50) if b["id"] == board]
        if not match:
            return {"error": f"Unknown board id '{board}'. Call resolve_target and use an exact board id it returned."}
        b = match[0]
        if framework not in (b.get("frameworks") or []):
            return {"error": f"Framework '{framework}' not supported by {board}. Supported: {b.get('frameworks')}"}
        platform = b["platform"]
        vp = pio.vendor_platform_for(b.get("mcu") or "")
        if vp and pio.VENDOR_PLATFORMS[vp].get("use_url"):
            platform = pio.VENDOR_PLATFORMS[vp]["url"]  # pin the exact platform (its name collides with another)
        lines = [f"[env:{board}]", f"platform = {platform}", f"board = {board}", f"framework = {framework}", "monitor_speed = 115200"]
        for opt in options or []:
            key = opt.split("=", 1)[0].strip()
            if "=" not in opt or "\n" in opt or "\r" in opt or not key.startswith(("board_build.", "board_upload.", "upload_protocol", "build_unflags")):
                return {"error": f"Option '{opt}' not allowed; use 'board_build.* = …', 'board_upload.* = …', upload_protocol or build_unflags."}
            lines.append(f"{key} = {opt.split('=', 1)[1].strip()}")
        if build_flags:
            lines.append("build_flags =\n    " + "\n    ".join(build_flags))
        if lib_deps:
            lines.append("lib_deps =\n    " + "\n    ".join(lib_deps))
        content = "\n".join(lines) + "\n"
        problem = check_platformio_ini(content)
        if problem:
            return {"error": problem}
        self.project.write_file("platformio.ini", content)
        self.project.update_meta(board=board, mcu=b.get("mcu"), platform=platform, framework=framework,
                                 fcpu_hz=b.get("fcpu_hz"), flash_bytes=b.get("flash_bytes"), ram_bytes=b.get("ram_bytes"))
        self.session.emit("file", path="platformio.ini", content=content)
        self.session.emit("project", meta=self.project.meta())
        # Download platform + toolchain + SDK now, so search_framework works before the first build.
        self.session.emit("status", text=f"Downloading toolchain and {framework} SDK for {board} (first time can take a few minutes)…")
        pk = pio.install_project_packages(self.project.root, on_line=self._log("packages"))
        roots = pio.framework_roots(framework)
        return {
            "ok": True, "board": b, "framework": framework, "platformio_ini": content,
            "sdk_ready": bool(pk["ok"] and roots), "sdk_packages": [r.name for r in roots],
            "next": "Use search_framework to find the SDK's real function and macro names for each peripheral, then write the firmware.",
            **({"sdk_error": pk["error"][-600:]} if not pk["ok"] else {}),
        }

    def _is_betaflight(self) -> bool:
        return self.project.meta().get("kind") == "betaflight"

    def init_betaflight(self, board_name: str, target: str, flash_mb: int = 0) -> Dict[str, Any]:
        board = re.sub(r"[^A-Za-z0-9_]", "_", (board_name or "").strip().upper()).strip("_")[:40]
        if not board:
            return {"error": "board_name is required (letters, digits, underscores)."}
        ok, detail = wsl.available()
        if not ok:
            return {"error": f"Betaflight builds run in WSL, which is not usable here: {detail}"}
        if not betaflight.is_ready():
            self.session.emit("status", text=f"Preparing Betaflight {betaflight.BF_TAG} and its toolchain in WSL — first time takes several minutes…")
            st = betaflight.setup(on_line=self._log("betaflight"))
            if not st["ok"]:
                return {"error": f"Betaflight setup failed: {st['error'][-800:]}"}
        targets = betaflight.available_targets()
        if target not in targets:
            return {"error": f"'{target}' is not an in-tree Betaflight {betaflight.BF_TAG} target. Available: {', '.join(targets)}"}
        board_dir = self.project.root / "betaflight" / "configs" / betaflight.MANUFACTURER_ID / board
        board_dir.mkdir(parents=True, exist_ok=True)
        generated: List[str] = []
        if flash_mb:
            if not target.startswith("RP2350"):
                return {"error": "flash_mb is only for RP2350/RP2354 targets (other MCUs have fixed internal flash)."}
            try:
                generated = betaflight.write_flash_override(board_dir, int(flash_mb))
            except ValueError as e:
                return {"error": str(e)}
        self.project.update_meta(kind="betaflight", board=board, mcu=target, bf_target=target, platform="betaflight",
                                 framework=f"betaflight {betaflight.BF_TAG}")
        self.session.emit("project", meta=self.project.meta())
        rel_dir = f"betaflight/configs/{betaflight.MANUFACTURER_ID}/{board}"
        return {
            "ok": True, "config_path": f"{rel_dir}/config.h", "target": target, "board_name": board,
            "manufacturer_id": betaflight.MANUFACTURER_ID, "betaflight_version": betaflight.BF_TAG,
            "generated": [f"{rel_dir}/{g}" for g in generated],
            "reference_configs": betaflight.reference_configs(target),
            "next": ("Read one or two reference configs (read_framework_file) to copy the exact define names and structure; "
                     "look up every function's pin with query_board and record it with record_fact; then write config.h with "
                     f"'#define FC_TARGET_MCU {target}', '#define BOARD_NAME {board}', '#define MANUFACTURER_ID "
                     f"{betaflight.MANUFACTURER_ID}' and each pin tagged /* FACT-nnn */; then build and verify (verify also "
                     "checks every pin against the MCU's peripheral pin tables)."),
        }

    # ------------------------------------------------------------ research
    def web_search(self, query: str, domains: Optional[List[str]] = None) -> Dict[str, Any]:
        key = re.sub(r"\s+", " ", query.lower().strip()) + "|" + ",".join(sorted(d.lower() for d in (domains or [])))
        if key in self._searches:
            return {"results": self._searches[key], "repeated": True,
                    "note": "You already ran this exact search in this run. Use the sources above (read_source) instead of searching again."}
        res = self.store.search(query, domains)
        fetched = 0
        for r in res:
            src = self.store.get(r["source_id"])
            # Search engines return partial text for long PDFs; pull the full document for datasheet-like hits.
            if src and fetched < 1 and src.url.startswith("http") and _looks_like_datasheet(src.url) and src.chars < 30_000:
                try:
                    self.session.emit("status", text=f"Downloading the full document: {src.url}")
                    src = self.store.fetch(src.url)
                    fetched += 1
                    r["chars"] = src.chars
                except Exception:
                    pass
            ex = self.store.excerpts(r["source_id"], query, max_chunks=1)
            r["excerpt"] = re.sub(r"[ \t]+", " ", ex[0]["text"])[:900] if ex else ""
            r["is_datasheet"] = bool(src and _looks_like_datasheet(src.url))
            r.pop("snippet", None)
            self.session.emit("source", **{k: v for k, v in r.items() if k != "excerpt"})
        self._searches[key] = res
        if not res:
            return {"results": [], "note": "No results. Try the vendor's own part number / register names, or drop the domain filter."}
        return {"results": res,
                "note": "Each result's `excerpt` is the passage that best matches your query. If it contains the value you need, "
                        "record_fact now with a verbatim quote copied from it; use read_source(source_id, query) for other passages."}

    def fetch_document(self, url: str) -> Dict[str, Any]:
        src = self.store.fetch(url)
        self.session.emit("source", source_id=src.id, title=src.title, url=src.url, tier=src.tier, chars=src.chars)
        return {"source_id": src.id, "url": src.url, "tier": src.tier, "chars": src.chars, "kind": src.kind,
                "preview": src.text[:500],
                "next": "Call read_source(source_id, query) with the register/pin/parameter names you need."}

    def read_source(self, source_id: str, query: str, max_chunks: int = 4) -> Dict[str, Any]:
        src = self.store.get(source_id)
        if not src:
            return {"error": f"Unknown source_id {source_id}"}
        ex = self.store.excerpts(source_id, query, max_chunks=max(1, min(int(max_chunks or 4), 6)))
        note = ""
        if not ex:
            note = "No passage matched. Try the vendor's own names (e.g. TMR vs TIM, CHxC vs CHxN) or register names."
            if src.chars < 20_000 and src.url.startswith("http"):
                note += f" This source is only a {src.chars}-char snippet; fetch_document('{src.url}') downloads the full document."
        return {"source_id": source_id, "url": src.url, "tier": src.tier, "chars": src.chars, "excerpts": ex, "note": note}

    def record_fact(self, subject: str, property: str, value: Any, source_id: str, quote: str, unit: str = "") -> Dict[str, Any]:
        res = self.ledger.record(subject, property, value, source_id, quote, unit)
        if res.get("ok"):
            self.session.emit("fact", fact=res["fact"], warnings=res.get("warnings", []))
        else:
            cands = self._candidate_quotes(source_id, str(value))
            if cands:
                res["candidate_quotes"] = cands
                res["hint"] = "These passages from the source contain the value verbatim. If one states the fact, retry with it as `quote`."
        return res

    def _candidate_quotes(self, source_id: str, value: str, limit: int = 3) -> List[str]:
        """Verbatim windows of the source that contain `value`, to help the model copy an exact quote."""
        from embedforge.truth.ledger import value_variants
        src = self.store.get(source_id)
        if not src or not value.strip():
            return []
        text = src.text
        low = text.lower()
        out: List[str] = []
        for _form, rx, conf in value_variants(value):
            if conf != "STRICT":
                continue
            for m in re.finditer(rx, low):
                a, b = max(0, m.start() - 110), min(len(text), m.end() + 110)
                # trim partial words at both ends without cutting into the matched value
                a = next((i for i in range(a, m.start()) if text[i].isspace()), a) if a > 0 else a
                b = next((i for i in range(b, m.end(), -1) if text[i - 1].isspace()), b) if b < len(text) else b
                w = re.sub(r"\s+", " ", text[a:b]).strip()
                if w and w not in out:
                    out.append(w)
                if len(out) >= limit:
                    return out
        return out

    def record_user_fact(self, subject: str, property: str, value: Any, quote: str) -> Dict[str, Any]:
        sid = self.store._by_url.get("user://conversation")
        if not sid:
            return {"ok": False, "error": "No user statements stored."}
        res = self.ledger.record(subject, property, value, sid, quote)
        if res.get("ok"):
            self.session.emit("fact", fact=res["fact"], warnings=res.get("warnings", []))
        return res

    def query_board(self, query: str = "") -> Dict[str, Any]:
        board = eda.load_board(self.project.root)
        if board is None:
            return {"error": "No schematic is imported for this project. Ask the user to upload their EasyEDA exports "
                             "(netlist + schematic source or BOM), or to describe the wiring."}
        sid = self.store._by_url.get(eda.BOARD_SOURCE_URL)
        res = board.query(query or "")
        res["source_id"] = sid
        if res.get("lines"):
            res["how_to_record"] = (f"record_fact(subject='<ref>', property='<e.g. PA8_net>', value='<net or pin>', "
                                    f"source_id='{sid}', quote='<one line above, verbatim>')")
        return res

    def list_facts(self) -> Dict[str, Any]:
        return {"facts": [{k: v for k, v in f.public().items() if k not in ("quote",)} | {"quote": f.quote[:160]}
                          for f in self.ledger.facts.values()]}

    # ------------------------------------------------------------ framework
    def search_framework(self, pattern: str, extension: str = "h") -> Dict[str, Any]:
        if self._is_betaflight():
            hits = betaflight.search(pattern)
            return {"matches": hits, "framework": f"betaflight {betaflight.BF_TAG}",
                    **({} if hits else {"note": "No matches in the Betaflight source/configs. Try a shorter fragment, e.g. 'SDCARD_SPI'."})}
        meta_fw = self.project.meta().get("framework", "")
        roots = pio.framework_roots(meta_fw)
        if not roots and (self.project.root / "platformio.ini").exists():
            self.session.emit("status", text="Downloading the project's SDK so its headers can be searched…")
            pio.install_project_packages(self.project.root, on_line=self._log("packages"))
            roots = pio.framework_roots(meta_fw)
        if not roots:
            return {"error": "No SDK is installed. Call resolve_target, then init_project with the exact board id and framework."}
        try:
            rx = re.compile(pattern, re.I)
        except re.error:
            rx = re.compile(re.escape(pattern), re.I)
        pk = pio.core_dir() / "packages"
        hits: List[str] = []
        ext = "." + extension.lstrip(".")
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
        out: Dict[str, Any] = {"matches": hits, "framework": meta_fw, "searched": [r.name for r in roots]}
        if not hits:
            out["note"] = ("No matches. Vendor SDKs use their own prefixes (e.g. STM32 HAL_TIM_*, Artery tmr_*/crm_*, "
                           "ESP-IDF gpio_*/i2c_*). Search a shorter fragment such as the peripheral name.")
        return out

    def read_framework_file(self, path: str, start: int = 1, end: int = 160) -> Dict[str, Any]:
        if str(path).startswith("bf:"):
            res = betaflight.read_file(path, int(start or 1), int(end or 160))
            return res or {"error": "Not a readable Betaflight file (use a 'bf:src/...' path from search_framework or init_betaflight)."}
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
        problem = check_agent_write(path, content)
        if problem:
            return {"error": problem}
        res = self.project.write_file(path, content)
        self.session.emit("file", path=res["path"], content=content)
        return res

    def replace_in_file(self, path: str, old: str, new: str) -> Dict[str, Any]:
        text = self.project.read_file(path)
        n = text.count(old)
        if n != 1:
            return {"error": f"'old' text must match exactly once; found {n} occurrences."}
        updated = text.replace(old, new)
        problem = check_agent_write(path, updated)
        if problem:
            return {"error": problem}
        res = self.project.write_file(path, updated)
        self.session.emit("file", path=res["path"], content=updated)
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

        res = betaflight.build(self.project.root, on_line=on_line) if self._is_betaflight() else pio.build(self.project.root, on_line=on_line)
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
        meta = self.project.meta()
        pico = bool(re.search(r"RP2[03]", f"{meta.get('mcu') or ''} {meta.get('bf_target') or ''}", re.I))
        rep = verify_project(self.project.root, self.ledger, pico=pico)
        if self._is_betaflight():
            from embedforge.truth import betaflight_rp2350
            from embedforge.truth.verifier import Finding
            for cfg in betaflight.config_files(self.project.root):
                rel = str(cfg.relative_to(self.project.root)).replace("\\", "/")
                for f in betaflight_rp2350.check_config(cfg.read_text(encoding="utf-8", errors="ignore"), meta.get("bf_target") or ""):
                    rep.findings.append(Finding(f["severity"], f["code"], rel, f["line"], f["message"], suggestion=f["suggestion"]))
            rep.passed = not any(f.severity == "ERROR" for f in rep.findings)
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
