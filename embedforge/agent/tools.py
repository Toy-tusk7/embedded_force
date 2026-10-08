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
from embedforge.truth.verifier import collect_build_inputs, collect_sources


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
    _fn("find_reference_code",
        "Get the MANUFACTURER's own reference driver for a part from its official GitHub organisation (Bosch Sensortec, "
        "ST, TDK InvenSense, Sensirion, Infineon, ADI, ...) and store its C sources as OFFICIAL_REPO sources. Use it when the "
        "datasheet's formulas or tables are garbled in the PDF text: the reference code states the same conversions in "
        "plain C. Then read_source and quote it like the datasheet.",
        {"part": S, "vendor": S}, ["part"]),
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
    _fn("record_board_pins",
        "Record many MCU pin facts from the imported schematic in ONE call. Each item: `define` = the config define it "
        "backs (e.g. 'SPI0_SCK_PIN', 'MOTOR1_PIN') and `line` = one query_board line copied exactly, e.g. "
        "'U1 pin 21 (GPIO14) -> net GYRO_SCK -> U4.13 (SCLK)'. The pin value (GPIO14) is taken from the line. Returns the "
        "FACT id per define and, in a Betaflight project, adds the tag to matching config.h lines (never changes a pin). "
        "If a define was recorded for the wrong pin, send that item with replace: true to retire the old fact.",
        {"pins": _arr({"type": "object", "properties": {"define": S, "line": S, "replace": {"type": "boolean"}},
                       "required": ["define", "line"]})}, ["pins"]),
    _fn("record_facts",
        "Record several facts in ONE call (each item follows the record_fact rules). facts: [{subject, property, value, quote, "
        "unit?, source_id?}]; `source_id` is the default for items without one. Returns the FACT id per item; rejected items come "
        "back with candidate quotes — resend only those.",
        {"facts": _arr({"type": "object", "properties": {"subject": S, "property": S, "value": S, "quote": S, "unit": S, "source_id": S},
                        "required": ["subject", "property", "value", "quote"]}), "source_id": S}, ["facts"]),
    _fn("record_registers",
        "Record the datasheet REGISTER MAP so verify checks what every value MEANS, not just that it was cited. registers: "
        "[{name:'PWR_CTRL', addr:'0x1B', quote:<the register's table text, verbatim>, fields:[{name:'mode', bits:'5:4', "
        "values:{sleep:'0b00', forced:['0b01','0b10'], normal:'0b11'}, quote?:<the field's rows if they are elsewhere>}, "
        "{name:'press_en', bits:'0'}], id_value?:<FACT id or value, for an ID register>}]. bursts: [{name:'data', start:'0x04', "
        "end:'0x09', quote:<text requiring one burst read>}]. Field names, bit numbers and each named value must be in the quote. "
        "verify then decodes every register write into its fields, rejects bit masks on undefined bits and reads that split a "
        "burst range. Record every register the code writes, polls or tests.",
        {"device": S, "source_id": S,
         "registers": _arr({"type": "object", "properties": {"name": S, "addr": S, "quote": S, "id_value": S, "source_id": S,
                                                              "fields": _arr({"type": "object", "properties": {"name": S, "bits": S, "values": {"type": "object"}, "quote": S},
                                                                              "required": ["name", "bits"]})},
                            "required": ["name", "addr", "quote"]}),
         "bursts": _arr({"type": "object", "properties": {"name": S, "start": S, "end": S, "quote": S}, "required": ["start", "end", "quote"]})},
        ["device", "source_id", "registers"]),
    _fn("record_layouts",
        "Record how each MULTI-BYTE value is spread over registers (calibration coefficients, raw measurements): layouts: "
        "[{name:'NVM_PAR_T1', bytes:['0x31','0x32'], signed:false, quote:<the rows listing those addresses>}]. `bytes` are "
        "LEAST significant first ('0x31' = bits 7..0, '0x32' = bits 15..8); a partial byte is '0xE5[3:0]'. verify checks every "
        "shift the code applies to those bytes (byte order, which byte belongs to which value). Layout names are the inputs of "
        "record_formula.",
        {"device": S, "source_id": S,
         "layouts": _arr({"type": "object", "properties": {"name": S, "bytes": _arr(), "signed": {"type": "boolean"}, "quote": S, "source_id": S},
                          "required": ["name", "bytes", "quote"]})},
        ["device", "source_id", "layouts"]),
    _fn("record_formula",
        "Record a conversion the code must implement (coefficient scaling, compensation, unit conversion): name='PAR_T1', "
        "steps=['PAR_T1 = NVM_PAR_T1 / 2^-8'] or several 'var = expression' lines whose last var is the result; quote = the "
        "verbatim text that states it (the datasheet, or the manufacturer's reference code on GitHub). Inputs are layout names "
        "or earlier formulas; `inputs` binds the quote's own names to them ({'uncomp_temp': 'UT'}); operators + - * / ^ "
        "(power) // (C integer division) >> << & | and pow/sqrt/exp/log/abs; C casts and struct prefixes (calib->par_t1) are "
        "ignored. Every constant must be in the quote. PDF text loses fraction bars and superscripts: 'X = NVM_X' followed by "
        "'2−8' means X = NVM_X / 2^-8, and '230' is 2^30. verify runs the code on random register bytes and requires it to "
        "compute exactly this value.",
        {"device": S, "source_id": S, "name": S, "steps": _arr(), "quote": S, "unit": S, "inputs": {"type": "object"}},
        ["device", "source_id", "name", "steps", "quote"]),
    _fn("record_sequence",
        "Record an ORDER/TIMING rule from a datasheet (power-up, soft reset, init, measurement start) so verify checks "
        "that the code follows it. Each step cites FACT ids you already recorded — never type values: "
        "{op:'write', reg:FACT(register), fields:{mode:'normal', press_en:1, temp_en:1}} (register-map fields; preferred) or "
        "{op:'write', reg:FACT, value:FACT(value), mask?:FACT(bits)} | {op:'read', reg:FACT, value?:FACT(min length)} | "
        "{op:'delay_min', value:FACT(time with unit)} | {op:'poll', reg:FACT(status register)}. `entry` = the C function "
        "that performs it; `device` = FACT id of the bus address; `order_quote` = verbatim datasheet text stating the order.",
        {"subject": S, "name": S, "entry": S, "device": S, "source_id": S, "order_quote": S,
         "steps": _arr({"type": "object", "properties": {"op": {"type": "string", "enum": ["write", "read", "delay_min", "poll"]},
                                                          "reg": S, "value": S, "mask": S, "unit": S, "fields": {"type": "object"}},
                        "required": ["op"]})},
        ["subject", "name", "entry", "steps", "source_id", "order_quote"]),
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
    _fn("write_file", "Create or overwrite a project file: firmware under src/, include/, lib/ (or platformio.ini), or for a "
        "Betaflight project the board config betaflight/configs/CUST/<BOARD>/config.h. Build scripts (*.py, *.mk, "
        "extra_scripts, library.json) are not allowed. For a Betaflight config the result lists the pin defines and lint problems.",
        {"path": S, "content": S}, ["path", "content"]),
    _fn("replace_in_file", "Replace an exact text snippet in a project file (must match exactly once).",
        {"path": S, "old": S, "new": S}, ["path", "old", "new"]),
    _fn("read_file", "Read a project file as numbered lines (long files come in windows; pass start/end for more).",
        {"path": S, "start": {"type": "integer"}, "end": {"type": "integer"}}, ["path"]),
    _fn("list_files", "List project files (optionally only those whose path starts with `path`).", {"path": S}, []),
    _fn("build",
        "Run a REAL build of the project — PlatformIO, or for a Betaflight project `make CONFIG=<BOARD>` in WSL. Takes no "
        "arguments. Returns compiler diagnostics with the surrounding source lines, flash/RAM usage and (on success) the "
        "firmware files. Re-building unchanged sources after a failure is refused: fix the first error first.",
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


_BETAFLIGHT_HIDDEN_TOOLS = {"resolve_target", "init_project"}


def tools_for(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Tool list for this project: a Betaflight project never needs the PlatformIO target tools, and once its board is
    set up init_betaflight would only re-initialise it."""
    if meta.get("kind") != "betaflight":
        return TOOL_SPECS
    hide = set(_BETAFLIGHT_HIDDEN_TOOLS) | ({"init_betaflight"} if meta.get("board") else set())
    return [t for t in TOOL_SPECS if t["function"]["name"] not in hide]


_KEEP_SCALARS = ("ok", "error", "passed", "summary", "unchanged", "version", "next", "note", "found", "source_id")


def _truncate(obj: Any, limit: int = 7000) -> Any:
    """Shrink an oversized tool result while keeping it a real object: status keys are never lost, long strings are cut and
    lists are trimmed from the tail (with '<key>_omitted' counts) until it fits."""
    import copy
    import json

    def size(o: Any) -> int:
        return len(json.dumps(o, default=str))

    if size(obj) <= limit:
        return obj
    if not isinstance(obj, dict):
        s = json.dumps(obj, default=str)
        return {"truncated": True, "preview": s[: limit - 100]}
    out = copy.deepcopy(obj)  # never mutate cached results (web_search cache)
    for k, v in list(out.items()):
        if isinstance(v, str) and len(v) > 1500 and k not in _KEEP_SCALARS:
            out[k] = v[:1500] + "…"
    lists = [k for k, v in out.items() if isinstance(v, list)]
    while size(out) > limit and any(out.get(k) for k in lists):
        k = max((k for k in lists if out.get(k)), key=lambda x: size(out[x]))
        item = out[k][-1]
        if isinstance(item, str) and len(item) > 300 and len(out[k]) == 1:
            out[k][-1] = item[:300] + "…"
            if size(out) <= limit:
                break
        out[k].pop()
        out[f"{k}_omitted"] = out.get(f"{k}_omitted", 0) + 1
    if size(out) > limit:  # nested blobs: keep the status keys and a preview of the rest
        rest = {k: v for k, v in out.items() if k not in _KEEP_SCALARS}
        out = {k: out[k] for k in _KEEP_SCALARS if k in out}
        out["preview"] = json.dumps(rest, default=str)[: max(200, limit - size(out) - 100)]
    out["truncated"] = True
    return out


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
    if name in ("library.json", "extra_script.py", "cmakelists.txt", "makefile", "gnumakefile") or \
            name.endswith((".py", ".mk", ".sh", ".cmake", ".ld", ".lf", ".rb", ".bat", ".ps1")):
        return (f"The agent may not write '{name}': build scripts and linker scripts run code on the host or change the "
                "memory layout behind the build checks. Linker/flash layout comes from the board definition.")
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
        self.state: Dict[str, Any] = {"build_hash": None, "build_ok": False, "verify_hash": None, "verify_ok": False, "verify_inputs": None, "last_build": None}
        # carry the last real build/verify across runs; finish() still compares against the CURRENT source hash
        meta = project.meta()
        lb, lv = meta.get("last_build") or {}, meta.get("last_verify") or {}
        if lb:
            self.state.update(build_hash=lb.get("hash"), build_ok=bool(lb.get("ok")), build_version=lb.get("version"))
        if lv:
            self.state.update(verify_hash=lv.get("hash"), verify_ok=bool(lv.get("passed")), verify_inputs=lv.get("inputs"))
        self._handlers: Dict[str, Callable[..., Any]] = {
            "resolve_target": self.resolve_target, "init_project": self.init_project, "init_betaflight": self.init_betaflight,
            "web_search": self.web_search,
            "fetch_document": self.fetch_document, "find_reference_code": self.find_reference_code,
            "read_source": self.read_source, "record_fact": self.record_fact,
            "record_user_fact": self.record_user_fact, "record_board_pins": self.record_board_pins,
            "record_sequence": self.record_sequence, "record_facts": self.record_facts, "record_registers": self.record_registers,
            "record_layouts": self.record_layouts, "record_formula": self.record_formula,
            "list_facts": self.list_facts, "query_board": self.query_board,
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
        import inspect
        sig = inspect.signature(h).parameters
        accepted = set(sig)
        ignored = sorted(k for k in args if k not in accepted)
        call_args = {k: v for k, v in args.items() if k in accepted}
        # one parameter under another name (query_board(designator='U3'), read_source(id=...)): use it for the one missing
        spec = next((t["function"]["parameters"] for t in TOOL_SPECS if t["function"]["name"] == name), {})
        missing = [k for k in (spec.get("required") or list(sig)[:1]) if k not in call_args]
        ptype = ((spec.get("properties") or {}).get(missing[0]) or {}).get("type") if len(missing) == 1 else None
        val = args[ignored[0]] if len(ignored) == 1 else None
        if len(missing) == 1 and len(ignored) == 1 and ((ptype == "string" and isinstance(val, str) and val.strip())
                                                        or (ptype == "integer" and isinstance(val, int) and not isinstance(val, bool))):
            call_args[missing[0]] = args[ignored[0]]
            ignored = [f"{ignored[0]} (used as {missing[0]})"]
        limit = 14000 if name == "query_board" else 7000
        try:
            res = _truncate(h(**call_args), limit=limit)
        except TypeError as e:
            spec = next((t["function"]["parameters"] for t in TOOL_SPECS if t["function"]["name"] == name), {})
            params = ", ".join(f"{k}{'' if k in spec.get('required', []) else '?'}" for k in spec.get("properties", {}))
            return {"error": f"Bad arguments for {name}: {e}. Its parameters are: ({params or 'none'})."}
        except Exception as e:  # tools must never crash the loop
            return {"error": f"{type(e).__name__}: {e}"}
        if ignored and isinstance(res, dict):
            res["ignored_args"] = ignored  # harmless extras (e.g. build(project_dir='.')) are dropped, not fatal
        return res

    def source_hash(self) -> str:
        """Hash of every build input (sources, linker/CMake/make files, platformio.ini)."""
        h = hashlib.sha256()
        for p in collect_build_inputs(self.project.root):
            if p.exists():
                h.update(str(p.relative_to(self.project.root)).replace("\\", "/").encode())
                h.update(p.read_bytes())
        return h.hexdigest()

    def verify_inputs_hash(self) -> str:
        """Build inputs plus what verify judges them against (facts, sequences, schematic): a new fact or a re-imported
        board after verify makes the old verify stale."""
        h = hashlib.sha256(self.source_hash().encode())
        for n in ("ledger.json", "sequences.json", "registers.json", "board.json"):
            p = self.project.root / n
            h.update(n.encode())
            if p.exists():
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
        msgs = self.project.user_messages()
        latest = msgs[-1] if msgs else ""
        if re.search(r"platform\s*io|arduino|pico[- ]?sdk|esp[- ]?idf|stm32cube|zephyr", latest, re.I) and not re.search(r"beta\s*flight", latest, re.I):
            return {"error": "The user asked for a PlatformIO/SDK project, not Betaflight. Use resolve_target and init_project "
                             "with the board and framework the user named.", "requested": latest[:200]}
        ok, detail = wsl.available()
        if not ok:
            return {"error": f"Betaflight builds run in WSL, which is not usable here: {detail}"}
        if not betaflight.is_ready():
            self.session.emit("status", text=f"Preparing Betaflight {betaflight.BF_TAG} and its toolchain in WSL — first time takes several minutes…")
            st = betaflight.setup(on_line=self._log("betaflight"))
            if not st["ok"]:
                return {"error": f"Betaflight setup failed: {st['error'][-800:]}"}
        targets = betaflight.available_targets()
        mapped = betaflight.target_for_part(target, targets)
        mapped_note = f"'{target}' is built with the in-tree {mapped} target." if mapped and mapped != target else ""
        if not mapped:
            return {"error": f"'{target}' is not an in-tree Betaflight {betaflight.BF_TAG} target. Available: {', '.join(targets)}"}
        target = mapped
        meta = self.project.meta()
        existing = meta.get("board") if meta.get("kind") == "betaflight" else None
        if existing and (self.project.root / "betaflight" / "configs" / betaflight.MANUFACTURER_ID / existing).is_dir() \
                and meta.get("bf_target") == target:
            rel = f"betaflight/configs/{betaflight.MANUFACTURER_ID}/{existing}"
            return {"ok": True, "already_initialised": True, "board_name": existing, "config_path": f"{rel}/config.h",
                    "target": target,
                    "note": f"This project's board is already set up as {existing} (one board per project) — do not call "
                            "init_betaflight again. Next: record_fact for each pin (query_board lines), write/fix config.h, "
                            "build, verify."}
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
            **({"note": mapped_note} if mapped_note else {}),
            "ok": True, "config_path": f"{rel_dir}/config.h", "target": target, "board_name": board,
            "manufacturer_id": betaflight.MANUFACTURER_ID, "betaflight_version": betaflight.BF_TAG,
            "generated": [f"{rel_dir}/{g}" for g in generated],
            "reference_configs": betaflight.reference_configs(target),
            "next": ("Read one reference config (read_framework_file) for define names and syntax only — never its pins or "
                     "devices; look up every function's pin with query_board('U1') and record them in one record_board_pins "
                     "call with {define, line}; then write config.h with "
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

    def find_reference_code(self, part: str, vendor: str = "") -> Dict[str, Any]:
        from embedforge.truth.refcode import find_reference_code
        res = find_reference_code(self.store, part, vendor)
        for s in res.get("sources") or []:
            self.session.emit("source", source_id=s["source_id"], title=s["url"].rsplit("/", 1)[-1], url=s["url"], tier=s["tier"], chars=s["chars"])
        return res

    def fetch_document(self, url: str) -> Dict[str, Any]:
        if not str(url).lower().startswith(("http://", "https://")):
            board_sid = self.store._by_url.get(eda.BOARD_SOURCE_URL)
            return {"error": "fetch_document only downloads http(s) URLs."
                             + (f" The imported schematic is already stored as {board_sid}: use query_board for its wiring."
                                if board_sid else " Project files are read with read_file.")}
        src = self.store.fetch(url)
        self.session.emit("source", source_id=src.id, title=src.title, url=src.url, tier=src.tier, chars=src.chars)
        return {"source_id": src.id, "url": src.url, "tier": src.tier, "chars": src.chars, "kind": src.kind,
                "preview": src.text[:500],
                "next": "Call read_source(source_id, query) with the register/pin/parameter names you need."}

    @staticmethod
    def _sid(source_id: Any) -> str:
        """'src-2', 'SRC002', 'src_002' -> 'SRC-002' (models often re-case ids)."""
        m = re.fullmatch(r"\s*SRC[-_ ]?0*(\d+)\s*", str(source_id or ""), re.I)
        return f"SRC-{int(m.group(1)):03d}" if m else str(source_id or "").strip()

    def read_source(self, source_id: str, query: str, max_chunks: int = 4) -> Dict[str, Any]:
        source_id = self._sid(source_id)
        src = self.store.get(source_id)
        if not src:
            return {"error": f"Unknown source_id {source_id}. Stored sources: "
                             + ", ".join(f"{s.id} ({s.title[:40]})" for s in list(self.store.sources.values())[:12])}
        ex = self.store.excerpts(source_id, query, max_chunks=max(1, min(int(max_chunks or 4), 6)))
        note = ""
        if not ex:
            note = "No passage matched. Try the vendor's own names (e.g. TMR vs TIM, CHxC vs CHxN) or register names."
            if src.chars < 20_000 and src.url.startswith("http"):
                note += f" This source is only a {src.chars}-char snippet; fetch_document('{src.url}') downloads the full document."
        return {"source_id": source_id, "url": src.url, "tier": src.tier, "chars": src.chars, "excerpts": ex, "note": note}

    def record_fact(self, subject: str, property: str, value: Any, source_id: str, quote: str, unit: str = "") -> Dict[str, Any]:
        # a quote copied from a clipped line may end in an ellipsis; the text before it is still verbatim
        quote = re.sub(r"\s*(…|\.\.\.)\s*$", "", quote or "")
        m = re.fullmatch(r"\s*SRC[-_ ]?0*(\d+)\s*", str(source_id or ""), re.I)   # 'SRC001', 'src-1' -> 'SRC-001'
        if m:
            source_id = f"SRC-{int(m.group(1)):03d}"
        res = self.ledger.record(subject, property, value, source_id, quote, unit)
        if res.get("ok"):
            self.session.emit("fact", fact=res["fact"], warnings=res.get("warnings", []))
        elif self.store.get(source_id) is None:
            res["error"] = (f"Unknown source_id '{source_id}'. Stored sources: "
                            + ", ".join(f"{s.id} ({s.title[:40]})" for s in list(self.store.sources.values())[:12])
                            + ". Wiring facts cite the board source returned by query_board.")
        else:
            if re.fullmatch(r"\d{1,2}", str(value).strip()) and re.search(rf"\bGPIO{int(str(value).strip())}(?!\d)", quote or "", re.I):
                res["hint"] = (f"For a pin, the value is the pin name as the quote writes it: value='GPIO{int(str(value).strip())}' — "
                               "or record several pins at once with record_board_pins.")
                return res
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
        board_sid = self.store._by_url.get(eda.BOARD_SOURCE_URL)
        board = self.store.get(board_sid) if board_sid else None
        if board is not None and self._BOARD_LINE.match(str(quote or "")):
            from embedforge.truth.ledger import _squash
            if _squash(quote) in _squash(board.text):   # a schematic line: it is the board source's fact, not the user's words
                res = self.ledger.record(subject, property, value, board_sid, quote)
                if res.get("ok"):
                    self.session.emit("fact", fact=res["fact"], warnings=res.get("warnings", []))
                    res["note"] = (f"Recorded from the schematic ({board_sid}). For pins, record_board_pins records many "
                                   "at once from query_board lines.")
                return res
        if not sid:
            return {"ok": False, "error": "No user statements stored."}
        res = self.ledger.record(subject, property, value, sid, quote)
        if res.get("ok"):
            self.session.emit("fact", fact=res["fact"], warnings=res.get("warnings", []))
        return res

    _BOARD_LINE = re.compile(r"^\s*(\S+) pin (\S+) \(([^)]*)\)")

    _DEFINE_NAME = re.compile(r"^[A-Z][A-Z0-9_]*_PIN$")

    def record_board_pins(self, pins: List[Dict[str, Any]], tag_config: bool = True) -> Dict[str, Any]:
        """Batch pin facts from board lines. The value is read from the quoted line, and the ledger still checks the line is
        verbatim board-source text, so this cannot record anything the schematic does not say. With `define` items in a
        Betaflight project, matching config.h lines get their FACT tag (only where the define's pin equals the fact)."""
        sid = self.store._by_url.get(eda.BOARD_SOURCE_URL)
        if not sid:
            return {"error": "No schematic is imported for this project, so there are no board lines to record."}
        if not isinstance(pins, list) or not pins:
            return {"error": "`pins` must be a non-empty list of {define, line}."}
        recorded: List[str] = []
        rejected: List[Dict[str, Any]] = []
        retired: List[str] = []
        to_tag: Dict[str, tuple] = {}
        any_new = False
        for item in pins[:60]:
            item = item if isinstance(item, dict) else {}
            define = str(item.get("define") or "").strip().upper()
            fn = re.sub(r"[^a-z0-9_]+", "_", str(item.get("function") or "").strip().lower()).strip("_")
            label = define or fn
            line = re.sub(r"\s*(…|\.\.\.)\s*$", "", str(item.get("line") or "").strip())
            m = self._BOARD_LINE.match(line)
            g = re.search(r"(?<![A-Z0-9])(?:GPIO(\d+)|P([A-K])(\d{1,2}))(?![0-9])", m.group(3).upper()) if m else None
            subject = m.group(1) if m else ""
            if m and not g:
                # a device-side line ('U3 pin 2 (SCK) -> net I2C_SCL -> ..., U1.17 (GPIO17)'): the MCU end names the pin
                far = re.search(r"\b([A-Z]+\d+)\.\d+ \((?:GPIO(\d+)|P([A-K])(\d{1,2}))\)", line[m.end():].upper())
                if far:
                    subject, g = far.group(1), re.search(r"(?:GPIO(\d+)|P([A-K])(\d{1,2}))", far.group(0))
            why = ("give `define` (e.g. 'GYRO_1_CS_PIN')" if not label else
                   f"`define` must be a pin define name like 'MOTOR1_PIN', not '{define}'" if define and not self._DEFINE_NAME.match(define) else
                   "`line` must start like 'U1 pin 21 (GPIO14) -> …' (a pin line of a part, copied exactly)" if not m else
                   f"pin name '{m.group(3)}' has no GPIO number, so it is not a signal pin" if not g else
                   f"{m.group(3)} is not connected on the schematic; remove any define that uses it"
                   if line.rstrip().endswith("not connected") else "")
            if why:
                rejected.append({"define": label or item.get("function"), "error": why})
                continue
            value = f"GPIO{int(g.group(1))}" if g.group(1) else f"P{g.group(2)}{int(g.group(3))}"
            prop = define or (f"{fn}_pin" if not fn.endswith("_pin") else fn)
            clash = next((f for f in self.ledger.facts.values() if f.subject == subject and f.property.upper() == prop.upper()
                          and str(f.value).upper() != value), None)
            if clash and item.get("replace") in (True, "true", "True", 1):
                self.ledger.retire(clash.id, f"{prop} re-recorded as {value} from: {line}")
                retired.append(f"{clash.id} ({prop}={clash.value})")
            elif clash:
                rejected.append({"define": label, "error": f"{prop} is already recorded as {clash.value} ({clash.id}); one define "
                                 f"has one pin. If {clash.id} was bound to the wrong define, repeat this item with replace: true "
                                 f"to retire it."})
                continue
            res = self.ledger.record(subject, prop, value, sid, line)
            if not res.get("ok"):
                rejected.append({"define": label, "error": str(res.get("error"))[:200]})
                continue
            fid = res["fact"]["id"]
            if not any("Duplicate" in str(w) for w in res.get("warnings") or []):
                any_new = True
                self.session.emit("fact", fact=res["fact"], warnings=res.get("warnings", []))
            recorded.append(f"{label}={value} {fid}")
            if define:
                to_tag[define] = (fid, value)
        out: Dict[str, Any] = {"ok": bool(recorded), "recorded": recorded}
        if retired:
            out["retired"] = retired
        if to_tag and tag_config and self._is_betaflight():
            tagging = self._tag_config(to_tag)
            out.update(tagging)
            any_new = any_new or bool(tagging.get("tagged"))
        if recorded and not any_new:
            out["all_duplicates"] = True
        if rejected:
            out["rejected"] = rejected
            out["hint"] = "Copy each line exactly from a query_board result (query 'U1' lists every MCU pin)."
        return out

    def _tag_config(self, tags: Dict[str, tuple]) -> Dict[str, Any]:
        """Put /* FACT-nnn */ on `#define NAME PAn` lines whose GPIO equals the recorded fact. Never changes a pin."""
        board = self.project.meta().get("board")
        rel = f"betaflight/configs/{betaflight.MANUFACTURER_ID}/{board}/config.h"
        path = self.project.root / rel
        if not board or not path.exists():
            return {"note": "config.h does not exist yet: write it with these defines and their FACT ids."}
        lines = path.read_text(encoding="utf-8").split("\n")
        tagged, mismatched, seen = [], [], set()
        for i, raw in enumerate(lines):
            m = re.match(r"^(\s*#\s*define\s+(\w+)\s+)(PA(\d{1,2}))\b(.*)$", raw)
            if not m or m.group(2) not in tags:
                continue
            define, (fid, value) = m.group(2), tags[m.group(2)]
            seen.add(define)
            if f"GPIO{int(m.group(4))}" != value:
                mismatched.append(f"{define} is {m.group(3)} in config.h but the schematic line gives {value} — fix the define")
                continue
            code = re.split(r"/\*|//", m.group(5), maxsplit=1)[0].rstrip()
            new = f"{m.group(1)}{m.group(3)}{code} /* {fid} */"
            if new != raw:
                lines[i] = new
                tagged.append(f"{define} line {i + 1}")
        out: Dict[str, Any] = {}
        if tagged:
            text = "\n".join(lines)
            self.project.write_file(rel, text)
            self.session.emit("file", path=rel, content=text)
            out.update(tagged=tagged, **self._config_feedback(rel, text))
        if mismatched:
            out["mismatched"] = mismatched
        missing = [d for d in tags if d not in seen]
        if missing:
            out["not_in_config"] = missing
        return out

    def record_sequence(self, subject: str, name: str, entry: str, steps: List[Dict[str, Any]], source_id: str,
                        order_quote: str, device: str = "") -> Dict[str, Any]:
        from embedforge.truth.sequences import SequenceBook
        m = re.fullmatch(r"\s*SRC[-_ ]?0*(\d+)\s*", str(source_id or ""), re.I)
        if m:
            source_id = f"SRC-{int(m.group(1)):03d}"
        res = SequenceBook(self.project.root).record(self.ledger, subject, name, entry, steps if isinstance(steps, list) else [],
                                                     source_id, order_quote, device)
        if res.get("ok"):
            self.session.emit("sequence", sequence=res["sequence"])
            res["next"] = (f"Implement it in {res['sequence']['entry']}(), then build and verify: verify traces that function "
                           "statically and reports each step as matched, missing, out of order, wrong value or too short a delay.")
        return res

    def record_facts(self, facts: List[Dict[str, Any]], source_id: str = "") -> Dict[str, Any]:
        if not isinstance(facts, list) or not facts:
            return {"ok": False, "error": "`facts` must be a non-empty list of {subject, property, value, quote, unit?, source_id?}"}
        recorded: List[str] = []
        rejected: List[Dict[str, Any]] = []
        for item in facts[:40]:
            item = item if isinstance(item, dict) else {}
            label = f"{item.get('subject') or '?'}.{item.get('property') or '?'}"
            res = self.record_fact(str(item.get("subject") or ""), str(item.get("property") or ""), item.get("value", ""),
                                   self._sid(item.get("source_id") or source_id), str(item.get("quote") or ""), str(item.get("unit") or ""))
            if res.get("ok"):
                fct = res["fact"]
                dup = any("Duplicate" in str(w) for w in res.get("warnings") or [])
                recorded.append(f"{fct['id']} {fct['subject']}.{fct['property']}={fct['value']}" + (" (already recorded)" if dup else ""))
            else:
                r = {"item": label, "error": str(res.get("error"))[:220]}
                if res.get("candidate_quotes"):
                    r["candidate_quotes"] = res["candidate_quotes"][:2]
                if res.get("hint"):
                    r["hint"] = res["hint"]
                rejected.append(r)
        out: Dict[str, Any] = {"ok": bool(recorded), "recorded": recorded}
        if rejected:
            out["rejected"] = rejected
            if any("are all required" in r["error"] for r in rejected):
                out["format"] = ("Each item is one fact: {subject:'BMP388', property:'chip_id', value:'0x50', quote:'0x00 CHIP_I D "
                                 "0x50 chip_id_fixed chip_id_nvm'} — subject, property, value and quote are separate keys; the "
                                 "quote is copied exactly from read_source and contains the value.")
        return out

    def _register_book(self):
        from embedforge.truth.registers import RegisterBook
        return RegisterBook(self.project.root)

    def record_registers(self, device: str, source_id: str, registers: List[Dict[str, Any]],
                         bursts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        sid = self._sid(source_id)
        regs = registers if isinstance(registers, list) else []
        for r in regs:
            if isinstance(r, dict) and r.get("source_id"):
                r["source_id"] = self._sid(r["source_id"])
        res = self._register_book().record_registers(self.store, self.ledger, device, sid, regs, bursts if isinstance(bursts, list) else [])
        for rej in res.get("rejected") or []:
            raw = next((r for r in regs if isinstance(r, dict) and r.get("name") == rej.get("register")), None)
            if raw and re.search(r"quote|not stated|not in the quote|mentions neither", str(rej.get("error"))):
                cands = self._table_passages(str(raw.get("source_id") or sid), [str(raw.get("name") or ""), str(raw.get("addr") or "")])
                if cands:
                    rej["candidate_quotes"] = cands
            if "burst" in rej and re.search(r"quote", str(rej.get("error"))):
                src = self.store.get(sid)
                if src is not None:   # sentences of the source that require a single burst / transaction
                    sents = re.findall(r"[^.\n]*(?:\n[^.\n]*){0,2}\b(?:single burst|burst read|one burst|in one transaction|"
                                       r"shadow)[^.]*\.", src.text, re.I)
                    rej["candidate_quotes"] = [re.sub(r"\s+", " ", s).strip() for s in sents if len(s) < 400][:3]
        if res.get("recorded") or res.get("bursts"):
            self.session.emit("registers", device=device, recorded=res.get("recorded", []), bursts=res.get("bursts", []))
            res["next"] = ("Record every multi-byte value with record_layouts and every conversion with record_formula; in "
                           "record_sequence, write steps can name fields: {op:'write', reg:FACT, fields:{...}}.")
        return res

    def record_layouts(self, device: str, source_id: str, layouts: List[Dict[str, Any]]) -> Dict[str, Any]:
        lays = layouts if isinstance(layouts, list) else []
        for l in lays:
            if isinstance(l, dict) and l.get("source_id"):
                l["source_id"] = self._sid(l["source_id"])
        res = self._register_book().record_layouts(self.store, device, self._sid(source_id), lays)
        for rej in res.get("rejected") or []:
            raw = next((l for l in lays if isinstance(l, dict) and l.get("name") == rej.get("layout")), None)
            if raw and re.search(r"quote", str(rej.get("error"))):
                keys = [str(b) for b in (raw.get("bytes") or [])][-1:] + [str(raw.get("name") or "")]
                cands = self._table_passages(str(raw.get("source_id") or self._sid(source_id)), keys, want_bits=False)
                if cands:
                    rej["candidate_quotes"] = cands
        if res.get("recorded"):
            self.session.emit("layouts", device=device, recorded=res["recorded"])
        return res

    def _table_passages(self, source_id: str, keys: List[str], want_bits: bool = True, limit: int = 2) -> List[str]:
        """Verbatim windows of a source that start at a table for one of `keys` (register name or address), so a rejected
        register/layout can be retried with an exact quote. Register descriptions ('Table 40: Register 0x1B ...') rank
        before mentions in the memory map or the table of contents."""
        src = self.store.get(source_id)
        if not src:
            return []
        text = src.text
        out: List[str] = []
        scored = []
        for key in [k for k in keys if k and len(k) >= 2]:
            for m in re.finditer(re.escape(key), text, re.I):
                a = text.rfind("\n", 0, m.start()) + 1
                window = text[a:a + 520]
                if "....." in window[:200]:
                    continue   # table of contents
                score = (2 if re.search(r"\bBit\s*\d", window) or not want_bits else 0) + (1 if re.match(r"\s*Table\s+\d+", window) else 0)
                scored.append((-score, a, window))
        for _s, _a, w in sorted(scored)[:12]:
            w = w[:w.rfind("\n")] if "\n" in w[200:] else w
            w = w.strip()
            if w and all(w[:80] not in o for o in out):
                out.append(w)
            if len(out) >= limit:
                break
        return out

    def record_formula(self, device: str, source_id: str, name: str, steps: Any, quote: str, unit: str = "",
                       inputs: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        res = self._register_book().record_formula(self.store, device, self._sid(source_id), name, steps, quote, unit,
                                                   inputs if isinstance(inputs, dict) else None)
        src = self.store.get(self._sid(source_id))
        if not res.get("ok") and src is not None and re.search(r"not verbatim|not in the quote", str(res.get("error"))):
            # the passage the formula comes from, so the model can quote it exactly instead of retyping it
            idents = sorted({w for s in (steps if isinstance(steps, list) else [str(steps)]) for w in re.findall(r"[A-Za-z_]\w{3,}", str(s))
                             if w.lower() not in ("float", "double", "return", "calib", "data")}, key=len, reverse=True)
            cands: List[str] = []
            for w in idents[:6]:
                m = re.search(rf"\b{re.escape(w)}\b\s*=", src.text) or re.search(rf"\b{re.escape(w)}\b", src.text)
                if m:
                    a = src.text.rfind("\n", 0, m.start()) + 1
                    win = src.text[a:a + 1200]
                    win = win[:win.rfind("\n")] if "\n" in win[400:] else win
                    if all(win[:60] not in c for c in cands):
                        cands.append(win.strip())
                if len(cands) >= 2:
                    break
            if cands:
                res["candidate_quotes"] = cands
                res["hint"] = ("Copy the quote from one of these passages exactly (you may cut it short or replace an omitted "
                               "middle part with '...'); C comments may be left out.")
        if res.get("ok"):
            self.session.emit("formula", device=device, formula=res["formula"])
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
            res["how_to_record"] = ("For the pins used in code: record_board_pins(pins=[{define:'GYRO_1_CS_PIN', line:'<a line "
                                    "above, exact>'}, ...]) in one call; it tags existing config.h defines for you.")
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

    def read_framework_file(self, path: str, start: int = 1, end: int = 0) -> Dict[str, Any]:
        path = re.sub(r"^betaflight-src:", "bf:", str(path))  # the form build diagnostics use
        if path.startswith("bf:"):
            start = max(1, int(start or 1))
            res = betaflight.read_file(path, start, int(end) if end else start + 240)
            if not res:
                return {"error": "Not a readable Betaflight file (use a 'bf:src/...' path from search_framework or init_betaflight)."}
            rows, size = [], 0
            for row in res["text"].split("\n"):  # same ~5.5k window as project files, with a 'more' note
                if size + len(row) > 5500:
                    break
                rows.append(row)
                size += len(row) + 1
            last = start + len(rows) - 1
            res.update(text="\n".join(rows), end=last)
            if last < res["total_lines"]:
                res["note"] = f"Lines {start}-{last} of {res['total_lines']}; read_framework_file(path, start={last + 1}) for more."
            return res
        if (self.project.root / str(path)).is_file():  # a project file passed to the SDK reader
            return self.read_file(path, start or 1, end or 0)
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
    def _config_feedback(self, path: str, content: str) -> Dict[str, Any]:
        """For a Betaflight config: lint problems and the pin map, so the model needn't re-read what it just wrote."""
        rel = path.replace("\\", "/")
        if not re.fullmatch(r"betaflight/configs/(?:CUST/)?[^/]+/config\.h", rel):
            return {}
        meta = self.project.meta()
        lint = betaflight.lint_config(content, rel.split("/")[-2], meta.get("bf_target") or "")
        out: Dict[str, Any] = {"pins": betaflight.pin_summary(content)}
        if lint["errors"] or lint["warnings"]:
            out["lint"] = lint["errors"] + [f"warning: {w}" for w in lint["warnings"]]
        return out

    def write_file(self, path: str, content: str) -> Dict[str, Any]:
        problem = check_agent_write(path, content)
        if problem:
            return {"error": problem}
        try:
            unchanged = self.project.read_file(path) == content
        except (FileNotFoundError, ValueError):
            unchanged = False
        res = self.project.write_file(path, content)
        self.session.emit("file", path=res["path"], content=content)
        return {**res, **({"unchanged": True} if unchanged else {}), **self._config_feedback(path, content)}

    def replace_in_file(self, path: str, old: str, new: str) -> Dict[str, Any]:
        text = self.project.read_file(path)
        n = text.count(old)
        if n != 1:
            hint = ""
            if n == 0:  # show where the text probably is, so the model can retry without re-reading the whole file
                first = next((ln for ln in old.splitlines() if ln.strip()), "").strip()
                hits = [f"{i}: {ln}" for i, ln in enumerate(text.splitlines(), 1) if first and first[:40] in ln][:3]
                hint = (" Lines containing its first line: " + " | ".join(hits)) if hits else " Read the lines with read_file(path, start=…) first."
            return {"error": f"'old' text must match exactly once; found {n} occurrences.{hint}"}
        updated = text.replace(old, new)
        problem = check_agent_write(path, updated)
        if problem:
            return {"error": problem}
        res = self.project.write_file(path, updated)
        self.session.emit("file", path=res["path"], content=updated)
        return {**res, **self._config_feedback(path, updated)}

    def read_file(self, path: str, start: int = 1, end: int = 0) -> Dict[str, Any]:
        """Numbered lines; long files are windowed so the reply never collapses into an unreadable preview."""
        if str(path).startswith(("bf:", "betaflight-src:")):
            return self.read_framework_file(path, start or 1, end or 0)
        rel = str(path or "").replace("\\", "/").lstrip("./")
        if rel.startswith("schematics/") and (self.project.root / "board.json").exists():
            # the raw EDA export is thousands of lines of footprints; the parsed board is what facts quote
            return {"error": "This is the raw schematic export. Use query_board('U3') / query_board('<net>') instead: it returns "
                             "the parsed connectivity lines (e.g. 'U3 pin 4 (SDI) -> net I2C_SDA -> ... U1.16 (GPIO16)') that "
                             "record_board_pins and record_fact quote."}
        lines = self.project.read_file(path).splitlines()
        start = max(1, int(start or 1))
        out, size, last = [], 0, start - 1
        stop = min(len(lines), int(end)) if end else len(lines)
        for i in range(start, stop + 1):
            row = f"{i}: {lines[i - 1]}"
            if size + len(row) > 5500:
                break
            out.append(row)
            size += len(row) + 1
            last = i
        res: Dict[str, Any] = {"path": path, "start": start, "end": last, "total_lines": len(lines), "text": "\n".join(out)}
        if last < len(lines) and (not end or last < int(end)):
            res["note"] = f"Showing lines {start}-{last} of {len(lines)}. Call read_file(path, start={last + 1}) for more."
        return res

    def list_files(self, path: str = "") -> Dict[str, Any]:
        files = self.project.list_files()
        prefix = (path or "").replace("\\", "/").lstrip("./")
        return {"files": [f for f in files if f["path"].startswith(prefix)] if prefix else files}

    def _with_context(self, diags: List[Dict[str, Any]], limit: int = 8) -> List[Dict[str, Any]]:
        """Deduplicated diagnostics; project-file ones carry their ±2 numbered source lines."""
        out, seen = [], set()
        for d in diags:
            key = (d.get("file"), d.get("line"), d.get("message"))
            if key in seen:
                continue
            seen.add(key)
            d = dict(d)
            f, ln = str(d.get("file") or ""), int(d.get("line") or 0)
            p = self.project.root / f
            if ln and not f.startswith(("<", "betaflight-src:")) and p.is_file():
                lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
                d["context"] = "\n".join(f"{'>' if i == ln else ' '}{i}: {lines[i - 1]}" for i in range(max(1, ln - 2), min(len(lines), ln + 2) + 1))
            out.append(d)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------ build / verify
    def build(self) -> Dict[str, Any]:
        h = self.source_hash()
        prev = self.project.meta().get("last_build") or {}
        if prev.get("hash") == h and prev.get("ok") is False and prev.get("source_errors"):  # infra failures may be retried
            first = (prev.get("diagnostics") or ["see the previous build result"])[0]
            return {"error": "No source changes since the last failed build, so it would fail the same way. Fix the first error "
                             f"({first}) with replace_in_file, then build.", "unchanged": True}
        self.session.emit("build_start")

        def on_line(line: str) -> None:
            if line.strip():
                self.session.emit("build_log", line=line)

        res = (betaflight.build(self.project.root, on_line=on_line, board_hint=self.project.meta().get("board", ""))
               if self._is_betaflight() else pio.build(self.project.root, on_line=on_line))
        self.state.update(build_hash=h, build_ok=res.ok, last_build=res.public())
        out = res.public()
        out["diagnostics"] = self._with_context(out.get("diagnostics") or [])
        if not res.ok:
            if out["diagnostics"]:
                out.pop("log_tail", None)
                out["next"] = "Fix the FIRST error (later ones are often consequences), then build again."
            else:
                out["log_tail"] = "\n".join((out.get("log_tail") or "").splitlines()[-25:])
        self.project.update_meta(last_build={
            "hash": h, "ok": res.ok, "source_errors": bool(out["diagnostics"]),
            "diagnostics": [f"{d.get('file')}:{d.get('line')}: {str(d.get('message'))[:160]}" for d in out["diagnostics"][:5]]
                           or ([out.get("error")] if out.get("error") else []),
        })
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
            self.project.update_meta(last_build={**(self.project.meta().get("last_build") or {}), "version": mf["version"]})
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
            board = eda.load_board(self.project.root)
            for cfg in betaflight.config_files(self.project.root):
                rel = str(cfg.relative_to(self.project.root)).replace("\\", "/")
                text = cfg.read_text(encoding="utf-8", errors="ignore")
                for f in betaflight_rp2350.check_config(text, meta.get("bf_target") or ""):
                    rep.findings.append(Finding(f["severity"], f["code"], rel, f["line"], f["message"], suggestion=f["suggestion"]))
                for sev, code, ln, msg, sug in betaflight.board_consistency(text, board):
                    rep.findings.append(Finding(sev, code, rel, ln, msg, suggestion=sug))
                own = {}  # name -> line; names the config itself tests (#ifdef X) are its own switches
                for i, raw in enumerate(text.split("\n"), start=1):
                    dm = re.match(r"\s*#\s*define\s+([A-Z][A-Z0-9_]+)", raw)
                    if dm:
                        own.setdefault(dm.group(1), i)
                tested = set(re.findall(r"#\s*(?:if|ifdef|ifndef|elif)\b[^\n]*?\b([A-Z][A-Z0-9_]+)", text))
                for name in betaflight.unknown_defines([n for n in own if n not in tested]) or []:
                    pin_like = name.endswith(("_PIN", "_INSTANCE"))
                    rep.findings.append(Finding(
                        "ERROR" if pin_like else "WARNING", "UNKNOWN_DEFINE", rel, own[name],
                        f"{name} is not used anywhere in Betaflight {betaflight.BF_TAG}, so it has no effect"
                        + (" — that function is not configured." if pin_like else "."),
                        suggestion=(f"Use the define name Betaflight reads (search_framework('{name.split('_')[0]}_') or a reference "
                                    "config); e.g. motors are MOTOR1_PIN..MOTOR8_PIN, gyros USE_ACCGYRO_<CHIP> or USE_GYRO_SPI_<CHIP>.")))
            rep.passed = not any(f.severity == "ERROR" for f in rep.findings)
        h = self.source_hash()
        inputs = self.verify_inputs_hash()
        self.state.update(verify_hash=h, verify_ok=rep.passed, verify_inputs=inputs)
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
        prev_inputs = (meta.get("last_verify") or {}).get("inputs")
        findings = self._compact_findings(pub["findings"])
        flows = [{"sequence": f["sequence"], "subject": f["subject"], "name": f["name"], "entry": f["entry"],
                  "steps": [f"{s['step']}. {s['op']}: {s['status']}" + (f" ({s['file']}:{s['line']})" if s.get("line") else "")
                            for s in f["steps"]]} for f in pub.get("flows", [])[:6]]
        self.project.update_meta(last_verify={
            "hash": h, "inputs": inputs, "passed": rep.passed, "summary": rep.summary(),
            "findings": [f"{x.get('code')} {x.get('file')}:{x.get('line') or x.get('lines')} {str(x.get('message'))[:140]}"
                         for x in findings[:5]],
        })
        out: Dict[str, Any] = {"passed": rep.passed, "summary": rep.summary(), "findings": findings}
        if flows:
            out["flows"] = flows  # each datasheet sequence, step by step, as found in the code
        regs = pub.get("registers") or {}
        if regs.get("writes") or regs.get("formulas"):
            # what the code's register values mean, per the recorded register map, and which formulas it implements
            out["meaning"] = {"register_writes": regs.get("writes", [])[:12], "formulas": regs.get("formulas", [])[:16]}
        if prev_inputs == inputs:
            out["unchanged"] = True  # same sources and facts as the last verify: same answer, not progress
        shown = sum(len(x.get("lines") or [1]) for x in findings)
        if len(pub["findings"]) > shown:
            out["note"] = f"{len(pub['findings']) - shown} more findings not shown; fix these first."
        return out

    def _compact_findings(self, findings: List[Dict[str, Any]], cap: int = 12) -> List[Dict[str, Any]]:
        """Errors first; 3+ findings with the same code and message pattern collapse into one entry with their lines;
        untraced pins get the schematic line(s) that could justify them."""
        order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
        findings = sorted(findings, key=lambda f: order.get(f.get("severity"), 3))
        groups: Dict[tuple, List[Dict[str, Any]]] = {}
        for f in findings:  # only findings that say the same thing collapse (pin numbers, line numbers and fact ids ignored)
            shape = re.sub(r"\b(?:PA|GPIO|FACT-)?\d+\b", "#", str(f.get("message") or ""))
            groups.setdefault((f.get("severity"), f.get("code"), f.get("file"), shape), []).append(f)
        board = eda.load_board(self.project.root) if (self.project.root / "board.json").exists() else None
        sid = self.store._by_url.get(eda.BOARD_SOURCE_URL)
        out: List[Dict[str, Any]] = []
        for (sev, code, file, _shape), fs in groups.items():
            if len(fs) >= 3 and code not in ("UNSOURCED_CONSTANT", "FACT_VALUE_MISMATCH"):
                out.append({"severity": sev, "code": code, "file": file, "lines": [f.get("line") for f in fs][:30],
                            "message": f"{len(fs)}x: {str(fs[0].get('message'))[:200]}",
                            "suggestion": str(fs[0].get("suggestion") or "")[:200]})
                continue
            for f in fs:
                g = {k: v for k, v in f.items() if k in ("severity", "code", "file", "line", "message", "literal")}
                g["suggestion"] = str(f.get("suggestion") or "")[:200]
                m = re.fullmatch(r"PA?(\d{1,2})|GPIO(\d{1,2})", str(f.get("literal") or ""))
                if board is not None and m and code in ("UNSOURCED_CONSTANT", "UNTAGGED_CONSTANT", "FACT_VALUE_MISMATCH"):
                    gpio = int(next(x for x in m.groups() if x))
                    lines = board.mcu_pin_lines(gpio)[:2]
                    if lines and all(ln.rstrip().endswith("not connected") for ln in lines):
                        g["suggestion"] = f"GPIO{gpio} is not connected on the schematic: remove this define."
                    elif lines:
                        g["board_lines"] = lines
                        g["suggestion"] = (f"Only if this net is what the define drives: record_board_pins(pins=[{{define:"
                                           f"'<DEFINE>', line:<a board_lines entry>}}]) (source {sid}); otherwise fix the pin.")
                out.append(g)
        return out[:cap]

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
            elif self.state.get("verify_inputs") != self.verify_inputs_hash():
                problems.append("facts, sequences or the schematic changed after the last verify (call verify again)")
            if problems:
                return {"error": "Cannot finish with success: " + "; ".join(problems) + "."}
        false = self._unsupported_claims(summary or "", h)
        if false:  # any status: a 'blocked' summary may not claim a build or a verify that did not happen either
            return {"error": "The summary claims " + "; ".join(false) + ". Rewrite it to say exactly what was built and verified "
                             "(and what was not), then call finish again."}
        self.ended = "finish"
        self.final = {"status": status, "summary": summary}
        return {"ok": True}

    def _unsupported_claims(self, summary: str, h: str) -> List[str]:
        built = bool(self.state["build_ok"] and self.state["build_hash"] == h)
        verified = bool(self.state["verify_ok"] and self.state["verify_hash"] == h and self.state.get("verify_inputs") == self.verify_inputs_hash())
        text = re.sub(r"\b(not|never|un|could not|couldn't|cannot|can't|failed to|fails?|without|nothing\s+(?:was|is|has been|got)|"
                      r"no(?:thing)?\s+(?:code|firmware|part)?\s*(?:was|is|has been))\s+(yet\s+|been\s+|be\s+)?(fully\s+)?",
                      "NOT_", summary, flags=re.I)
        out = []
        if not verified and re.search(r"(?<!NOT_)\b(verified|verification passed|passe[sd] verif\w*|verify passe[sd])\b", text, re.I):
            out.append("the code is verified, but the current sources have no passing verify")
        if not built and re.search(r"(?<!NOT_)\b(built|builds|compiled|compiles)\b(?!\s+(?:yet|fails|failed))", text, re.I):
            out.append("the code is built, but the current sources have no successful build")
        if re.search(r"(?<!NOT_)\b(tested on (the )?(hardware|board)|flashed|works on the board|flies|flight[- ]tested)\b", text, re.I):
            out.append("hardware testing, which EmbedForge never does (no device evidence)")
        return out
