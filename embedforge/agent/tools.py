"""The agent's tool belt. Every tool is deterministic infrastructure; the LLM only decides *which* to call."""

import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from embedforge import eda
from embedforge.agent.events import RunSession
from embedforge.agent.workspace import Project
from embedforge.core.config import config
from embedforge.core.files import write_atomic
from embedforge.toolchain import betaflight, pio, wsl
from embedforge.truth import FactLedger, SourceStore, verify_project
from embedforge.truth.ledger import MATH_FOLD
from embedforge.truth.verifier import build_inputs_hash, collect_sources


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
        "Search the web (Tavily) for official datasheets, reference manuals and register maps. Give `domains` with the "
        "manufacturer's site: its pages rank first. Each result has a source_id, trust tier, Tavily relevance `score` and "
        "an `excerpt` (the passage best matching your query); datasheet PDFs are downloaded in full automatically. Put a "
        "part number in double quotes to require it exactly. Do not repeat a search — use read_source on what you have.",
        {"query": S, "domains": _arr()}, ["query"]),
    _fn("find_documents",
        "List the official documents a manufacturer's page links to (datasheet, reference manual, register map, errata, "
        "application notes) by mapping the site with Tavily Map: give the part's product or documentation page URL (or "
        "the vendor's documentation root) and the part number. Use it when web_search finds only snippets, mirrors or "
        "forum posts; then fetch_document the right document.",
        {"url": S, "part": S}, ["url"]),
    _fn("fetch_document",
        "Store the full text of a datasheet/register-map PDF or web page by URL (web pages through Tavily Extract, "
        "tables kept as Markdown). Only needed for URLs that web_search did not already store in full. Returns a source_id.",
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
    _fn("datasheet_outline",
        "A stored datasheet's numbered sections with how much of each you have read; sections your code depends on and "
        "you have not read are marked '<< needed' (known after the first verify).",
        {"source_id": S}, ["source_id"]),
    _fn("read_section",
        "Read whole datasheet sections: section='3.3.3' or 'Normal mode', or several in one call: '3.3.3, 3.9.1, Data "
        "ready'. Each comes with `requirements`: its sentences that state a rule (must, should, before, wait, at least). "
        "Modes, timing, valid settings and readout rules live in such sections, not in the register table.",
        {"source_id": S, "section": S, "part": {"type": "integer"}}, ["source_id", "section"]),
    _fn("search_datasheet",
        "Find the passages of a long datasheet that answer a question (NVIDIA Nemotron embeddings over the stored "
        "source): question='conversion time for x8 pressure oversampling'. Returns up to k passages with their section "
        "and offset; then read_section the sections you need. It does not replace reading the sections verify requires.",
        {"source_id": S, "question": S, "k": {"type": "integer"}}, ["source_id", "question"]),
    _fn("read_figure",
        "Read a datasheet FIGURE or TABLE drawn as an image (timing diagrams, mode transitions, state machines): "
        "figure='Figure 3' or a caption word. A vision model transcribes it from the stored PDF (order of steps, "
        "values, arrows). read_section lists the figures of each section; rules about order and timing are often "
        "only in the figure.",
        {"source_id": S, "figure": S}, ["source_id", "figure"]),
    _fn("load_skill",
        "Load a skill: a playbook for a kind of work (the SKILLS list in your instructions names them), e.g. "
        "'sensor-driver' before writing a sensor driver, 'datasheet-rules' before settling datasheet requirements.",
        {"name": S}, ["name"]),
    _fn("coding_brief",
        "Everything the code must do, compiled from what is on record: the user's request, wiring and IDs, register map "
        "with reset values, init sequences, rules that must hold after every register write, readout (bursts, layouts), "
        "conversions, the datasheet's requirements (open / implemented / covered) and the gates verify applies. Call it "
        "before writing code and before fixing verify findings.",
        {}, []),
    _fn("resolve_requirements",
        "Settle datasheet requirements verify reports as REQUIREMENT_OPEN, several per call: items=[{id:'REQ-004', "
        "status:'implemented', reason:'readRawData() reads 0x04..0x09 in one burst'}, {id:'REQ-002', "
        "status:'not_applicable', reason:'board-level: power ramp is the supply's job'}]. A requirement that one of your "
        "records quotes (record_fact / record_sequence / record_constraint) is covered by itself. A rule about timing, "
        "the mode the code uses or the recommended settings needs ref = the id of a record quoting its section "
        "(e.g. the CON- of the conversion-time rule), and cannot be not_applicable. For timing and the recommended "
        "settings that record must be one verify checks on the code: a constraint, a sequence, or a fact the code cites "
        "on the constant it sets.",
        {"items": _arr({"type": "object", "properties": {"id": S, "status": S, "reason": S, "ref": S}, "required": ["id", "status", "reason"]})},
        ["items"]),
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
        "{name:'press_en', bits:'0'}], id_value?:<FACT id or value, for an ID register>, reset?:'0x00', reset_quote?:<the "
        "memory map row with its default value, e.g. '0x1D ODR 0x00 reserved odr_sel'>}]. bursts: [{name:'data', "
        "start:'0x04', end:'0x09', quote:<text requiring one burst read>}]. Field names, bit numbers and each named value must "
        "be in the quote. verify then decodes every register write into its fields, rejects bit masks on undefined bits and "
        "reads that split a burst range; reset values let it check record_constraint rules before the code writes a register. "
        "Record every register the code writes, polls or tests.",
        {"device": S, "source_id": S,
         "registers": _arr({"type": "object", "properties": {"name": S, "addr": S, "quote": S, "id_value": S, "source_id": S,
                                                              "reset": S, "reset_quote": S,
                                                              "fields": _arr({"type": "object", "properties": {"name": S, "bits": S, "values": {"type": "object"}, "quote": S},
                                                                              "required": ["name", "bits"]})},
                            "required": ["name", "addr", "quote"]}),
         "bursts": _arr({"type": "object", "properties": {"name": S, "start": S, "end": S, "quote": S}, "required": ["start", "end", "quote"]})},
        ["device", "source_id", "registers"]),
    _fn("record_constraint",
        "Record a RULE the datasheet states over register settings, so verify checks it after EVERY register write the code "
        "makes (starting from the reset values): valid combinations and timing, e.g. normal mode needs the conversion to fit "
        "in the sampling period: name='normal_mode_timing', when='PWR_CTRL.mode == normal', require='234 + PWR_CTRL.press_en "
        "* (392 + 2^OSR.osr_p * 2000) + PWR_CTRL.temp_en * (313 + 2^OSR.osr_t * 2000) <= 5000 * 2^ODR.odr_sel', unit='us', "
        "quotes=[<the formula passage>, <the sampling-period table rows>]. Fields are REGISTER.field from record_registers; "
        "named values (normal) may be used; operators + - * / ^, comparisons, and/or/not. Every number must be in the quotes. "
        "A violation shows the write after which the rule breaks (e.g. OSR raised while ODR is still at its 5 ms default).",
        {"device": S, "source_id": S, "name": S, "require": S, "when": S, "quotes": _arr(), "unit": S},
        ["device", "source_id", "name", "require", "quotes"]),
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
        "'2−8' means X = NVM_X / 2^-8, and '230' is 2^30. A number the quoted code sets (temp_var = 0.00390625f;) may be "
        "used directly. Several at once: formulas=[{name, steps, quote, inputs?, unit?}, ...] (recorded in order). verify "
        "runs the code on random register bytes and requires it to compute exactly this value.",
        {"device": S, "source_id": S, "name": S, "steps": _arr(), "quote": S, "unit": S, "inputs": {"type": "object"},
         "formulas": _arr({"type": "object"})},
        ["device", "source_id"]),
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
    _fn("rename_file", "Rename a project source file, e.g. src/driver.c -> src/driver.cpp (Arduino's Wire/Serial/SPI are C++).",
        {"path": S, "new_path": S}, ["path", "new_path"]),
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
        self._tavily_unavailable: Optional[Dict[str, Any]] = None   # set once Tavily refuses for good (credits, key)
        self._tavily_calls = 0                                       # searches and maps made in this run
        self._failed_calls: Dict[str, int] = {}      # identical record_* calls that were rejected (per run)
        self._failed_errors: Dict[str, str] = {}
        self.state: Dict[str, Any] = {"build_hash": None, "build_ok": False, "verify_hash": None, "verify_ok": False, "verify_inputs": None, "last_build": None}
        from embedforge.truth.outline import ReadLog
        self.reads = ReadLog(project.root)   # what of each source the agent has been shown (kept across runs)
        # messages -> reply text: the independent reviewer of requirement claims (set by the agent runner)
        self.reviewer: Optional[Callable[[List[Dict[str, Any]]], str]] = None
        # (prompt, png) -> text: reads datasheet figures; None = the configured vision model (agent/vision.py)
        self.vision: Optional[Callable[[str, bytes], str]] = None
        # (texts, 'passage' | 'query') -> vectors for search_datasheet; None = the configured embedding model
        self.embedder: Optional[Callable[..., List[List[float]]]] = None
        if not self.reads.path.exists() and (project.root / "conversation.json").exists():
            # a project from before read tracking, or a damaged log (kept aside as reads.json.corrupt)
            self.reads.seed(project.load_conversation())
        # carry the last real build/verify across runs; finish() still compares against the CURRENT source hash
        meta = project.meta()
        lb, lv = meta.get("last_build") or {}, meta.get("last_verify") or {}
        if lb:
            self.state.update(build_hash=lb.get("hash"), build_ok=bool(lb.get("ok")), build_version=lb.get("version"))
        if lv:
            self.state.update(verify_hash=lv.get("hash"), verify_ok=bool(lv.get("passed")), verify_inputs=lv.get("inputs"))
        self._handlers: Dict[str, Callable[..., Any]] = {
            "resolve_target": self.resolve_target, "init_project": self.init_project, "init_betaflight": self.init_betaflight,
            "web_search": self.web_search, "find_documents": self.find_documents,
            "fetch_document": self.fetch_document, "find_reference_code": self.find_reference_code,
            "read_source": self.read_source, "datasheet_outline": self.datasheet_outline, "read_section": self.read_section,
            "coding_brief": self.coding_brief, "resolve_requirements": self.resolve_requirements,
            "load_skill": self.load_skill, "read_figure": self.read_figure, "search_datasheet": self.search_datasheet,
            "record_fact": self.record_fact,
            "record_user_fact": self.record_user_fact, "record_board_pins": self.record_board_pins,
            "record_sequence": self.record_sequence, "record_facts": self.record_facts, "record_registers": self.record_registers,
            "record_layouts": self.record_layouts, "record_formula": self.record_formula,
            "record_constraint": self.record_constraint,
            "list_facts": self.list_facts, "query_board": self.query_board,
            "search_framework": self.search_framework, "read_framework_file": self.read_framework_file,
            "write_file": self.write_file, "replace_in_file": self.replace_in_file, "read_file": self.read_file,
            "rename_file": self.rename_file,
            "list_files": self.list_files, "build": self.build, "verify": self.verify, "set_plan": self.set_plan,
            "ask_user": self.ask_user, "finish": self.finish,
        }

    # ------------------------------------------------------------ dispatch
    # names models call that mean a real tool with the same arguments (seen in run traces): singular/plural slips
    _ALIASES = {"record_layout": "record_layouts", "record_register": "record_registers", "record_formulas": "record_formula",
                "record_sequences": "record_sequence"}

    def call(self, name: str, args: Dict[str, Any]) -> Any:
        h = self._handlers.get(name) or self._handlers.get(self._ALIASES.get(name, ""))
        if h is None:
            return {"error": f"Unknown tool '{name}'. Available: {', '.join(self._handlers)}"}
        name = name if name in self._handlers else self._ALIASES[name]
        if name in ("record_fact", "record_user_fact") and isinstance(args.get("facts"), (list, str)) and not args.get("quote"):
            items = args["facts"]
            if isinstance(items, str):
                try:
                    import json
                    items = json.loads(items)
                except ValueError:
                    items = []
            if name == "record_user_fact":   # the user's words are the conversation source
                sid = self.store._by_url.get("user://conversation") or ""
                items = [dict(i, source_id=i.get("source_id") or sid) for i in items if isinstance(i, dict)]
            res = self.record_facts(items if isinstance(items, list) else [], str(args.get("source_id") or ""))
            res["note"] = f"{name} records one fact; this list was recorded with record_facts."
            return res
        import inspect
        sig = inspect.signature(h).parameters
        accepted = set(sig)
        ignored = sorted(k for k in args if k not in accepted)
        call_args = {k: v for k, v in args.items() if k in accepted}
        # one parameter under another name (query_board(designator='U3'), read_source(id=...)): use it for the one missing
        spec = next((t["function"]["parameters"] for t in TOOL_SPECS if t["function"]["name"] == name), {})
        for k, v in list(call_args.items()):   # a list/object sent as a JSON (or Python-literal) string
            want = ((spec.get("properties") or {}).get(k) or {}).get("type")
            if want in ("array", "object") and isinstance(v, str) and v.strip()[:1] in "[{":
                import ast
                import json as _json
                for parse in (_json.loads, ast.literal_eval):
                    try:
                        parsed = parse(v)
                    except (ValueError, SyntaxError):
                        continue
                    if isinstance(parsed, list if want == "array" else dict):
                        call_args[k] = parsed
                        break
        missing = [k for k in (spec.get("required") or list(sig)[:1]) if k not in call_args]
        ptype = ((spec.get("properties") or {}).get(missing[0]) or {}).get("type") if len(missing) == 1 else None
        val = args[ignored[0]] if len(ignored) == 1 else None
        if len(missing) == 1 and len(ignored) == 1 and ((ptype == "string" and isinstance(val, str) and val.strip())
                                                        or (ptype == "integer" and isinstance(val, int) and not isinstance(val, bool))):
            call_args[missing[0]] = args[ignored[0]]
            ignored = [f"{ignored[0]} (used as {missing[0]})"]
        limit = 14000 if name in ("query_board", "read_section", "coding_brief", "search_datasheet") else 7000
        import json as _json
        sig = name + _json.dumps(call_args, sort_keys=True, default=str)
        if name.startswith("record_") and self._failed_calls.get(sig, 0) >= 2:
            return {"ok": False, "error": (f"This exact {name} call was already rejected {self._failed_calls[sig]} times "
                                           f"({self._failed_errors.get(sig, '')[:200]}). Sending it again cannot succeed: change "
                                           "the quote (copy one of the candidate_quotes) or the value, or skip it if the firmware "
                                           "does not need it, and move on to the NEXT STEP.")}
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
        if name.startswith("record_") and isinstance(res, dict) and res.get("ok") is False and not res.get("recorded"):
            self._failed_calls[sig] = self._failed_calls.get(sig, 0) + 1
            self._failed_errors[sig] = str(res.get("error") or (res.get("rejected") or [{}])[0].get("error") or "")
        return res

    def source_hash(self) -> str:
        """Hash of every build input (sources, linker/CMake/make files, platformio.ini)."""
        return build_inputs_hash(self.project.root)

    def verify_inputs_hash(self) -> str:
        """Build inputs plus what verify judges them against (facts, sequences, schematic, the datasheet sections read): a
        new fact, a section read or a re-imported board after verify makes the old verify stale."""
        h = hashlib.sha256(self.source_hash().encode())
        for n in ("ledger.json", "sequences.json", "registers.json", "board.json", "sources/reads.json", "requirements.json"):
            p = self.project.root / n
            h.update(n.encode())
            if p.exists():
                h.update(p.read_bytes())
        # a stored PDF or a figure read changes what FIGURE_UNREAD says
        for p in sorted(self.store.dir.glob("*.pdf")) + sorted((self.store.dir / "figures").glob("*.md")):
            h.update(p.name.encode())
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
        # the latest substantive request: after the task, the user's 'continue' / 'yes' does not change what was asked
        latest = next((m for m in reversed(msgs) if len(m.strip()) >= 40), msgs[-1] if msgs else "")
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
        if self._tavily_unavailable or self._tavily_budget_spent():
            return self._tavily_unavailable
        try:
            res = self.store.search(query, domains)
        except Exception as e:
            if not self._tavily_out(e):
                raise
            return self._tavily_unavailable
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
            if ex:
                self.reads.add(r["source_id"], ex[0]["offset"], ex[0]["offset"] + min(len(ex[0]["text"]), 900))
            r["is_datasheet"] = bool(src and _looks_like_datasheet(src.url))
            r.pop("snippet", None)
            self.session.emit("source", **{k: v for k, v in r.items() if k != "excerpt"},
                              via=src.via if src else "", tavily_request=src.tavily_request if src else "")
        self._searches[key] = res
        if not res:
            return {"results": [], "note": "No results. Try the vendor's own part number / register names, or drop the domain filter."}
        return {"results": res,
                "note": "Each result's `excerpt` is the passage that best matches your query. If it contains the value you need, "
                        "record_fact now with a verbatim quote copied from it; use read_source(source_id, query) for other passages."}

    def _tavily_budget_spent(self) -> bool:
        """Count one Tavily search/map for this run; past EMBEDFORGE_TAVILY_MAX_CALLS the agent works from its sources."""
        if self._tavily_calls >= config.tavily_max_calls:
            stored = [f"{s.id} ({s.title[:50]})" for s in self.store.sources.values() if s.kind != "user"][:12]
            self._tavily_unavailable = {
                "error": f"This run's Tavily budget ({config.tavily_max_calls} searches/maps) is used up. "
                         + (f"Work from the stored sources with read_source: {', '.join(stored)}. " if stored else "")
                         + "find_reference_code still works (GitHub); otherwise ask_user.",
                "tavily_unavailable": True}
            return True
        self._tavily_calls += 1
        return False

    def _tavily_out(self, e: Exception) -> bool:
        """Tavily refused for good (credits used up, key rejected): remember it for the rest of the run, so the agent is
        pointed at the sources it already has instead of retrying searches."""
        from tavily.errors import InvalidAPIKeyError, MissingAPIKeyError, UsageLimitExceededError
        if not isinstance(e, (UsageLimitExceededError, InvalidAPIKeyError, MissingAPIKeyError)):
            return False
        stored = [f"{s.id} ({s.title[:50]})" for s in self.store.sources.values() if s.kind != "user"][:12]
        self._tavily_unavailable = {
            "error": f"Web search is unavailable for this run (Tavily: {str(e)[:120]}). "
                     + (f"Use the stored sources with read_source: {', '.join(stored)}. " if stored else "")
                     + "find_reference_code still works (GitHub) and fetch_document can download a datasheet URL you know; "
                       "otherwise ask_user to upload the datasheet.",
            "tavily_unavailable": True}
        self.session.emit("status", text="Tavily search unavailable (credits or key) — using stored sources")
        return True

    def find_documents(self, url: str, part: str = "") -> Dict[str, Any]:
        if not str(url).lower().startswith(("http://", "https://")):
            url = "https://" + str(url).strip().lstrip("/")   # 'www.bosch-sensortec.com/products/...' -> https://...
        if self._tavily_unavailable or self._tavily_budget_spent():
            return self._tavily_unavailable
        self.session.emit("status", text=f"Mapping {url} for official documents (Tavily Map)…")
        try:
            res = self.store.find_documents(url, part)
        except Exception as e:
            if not self._tavily_out(e):
                raise
            return self._tavily_unavailable
        if not res["documents"]:
            res["note"] = ("No document links found on that page. Try the part's product page, the vendor's documentation "
                           "root, or web_search with the vendor domain.")
        else:
            res["next"] = "fetch_document the datasheet (or reference manual) URL, then read_source for the values you need."
        return res

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
        self.session.emit("source", source_id=src.id, title=src.title, url=src.url, tier=src.tier, chars=src.chars,
                          via=src.via, tavily_request=src.tavily_request)
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
        for e in ex:
            self.reads.add(source_id, e["offset"], e["offset"] + len(e["text"]))
        note = ""
        if not ex:
            note = "No passage matched. Try the vendor's own names (e.g. TMR vs TIM, CHxC vs CHxN) or register names."
            if src.chars < 20_000 and src.url.startswith("http"):
                note += f" This source is only a {src.chars}-char snippet; fetch_document('{src.url}') downloads the full document."
        elif len(self._outline(source_id)) >= 8:
            note = ("Excerpts are fragments: for a datasheet, read whole sections with read_section (datasheet_outline lists "
                    "them) — modes, timing and readout rules often sit in sections a keyword does not reach.")
        return {"source_id": source_id, "url": src.url, "tier": src.tier, "chars": src.chars, "excerpts": ex, "note": note}

    # ------------------------------------------------------------ datasheet structure
    def _outline(self, source_id: str):
        from embedforge.truth.outline import sections
        cache = self.__dict__.setdefault("_outlines", {})
        src = self.store.get(source_id)
        if src is None:
            return []
        key = (source_id, src.chars)
        if key not in cache:
            cache[key] = sections(src.text) if src.chars > 3000 and not src.url.startswith("user://board") else []
        return cache[key]

    def datasheet_outline(self, source_id: str) -> Dict[str, Any]:
        source_id = self._sid(source_id)
        secs = self._outline(source_id)
        if not secs:
            return {"error": f"{source_id} has no section structure (or is not a stored document). Use read_source with the "
                             "names you need."}
        need = {s.num: why for s, why in self._required_for(source_id)}
        lines = []
        for s in secs:
            pct = self.reads.seen(source_id, s.start, s.end)
            lines.append(f"{s.num} {s.title[:70]} ({s.end - s.start} chars, {pct:.0%} read)"
                         + (f"  << needed: {need[s.num]}" if s.num in need and pct < 0.5 else ""))
        unread = [n for n in need if self.reads.seen(source_id, *self._span(source_id, n)) < 0.5]
        out: Dict[str, Any] = {"source_id": source_id, "sections": lines}
        if unread:
            out["next"] = (f"read_section('{source_id}', {unread[:6]}) — the sections marked << your code depends on and "
                           "you have not read; record what they require (record_fact / record_sequence).")
        return out

    def _span(self, source_id: str, num: str):
        s = next((x for x in self._outline(source_id) if x.num == num), None)
        return (s.start, s.end) if s else (0, 0)

    def read_section(self, source_id: str, section: Any = "", part: int = 1) -> Dict[str, Any]:
        """Whole sections of a datasheet (one, or a list), with the sentences in each that state a rule."""
        from embedforge.truth.outline import find_section, requirements, subtree_end
        source_id = self._sid(source_id)
        src = self.store.get(source_id)
        secs = self._outline(source_id)
        if src is None or not secs:
            return {"error": f"{source_id} has no section structure. Use read_source with the names you need."}
        keys = section if isinstance(section, list) else [x for x in re.split(r"\s*[,;]\s*", str(section or "")) if x]
        if not keys:
            return {"error": "section: a number ('3.3.3') or title ('Normal mode'), or a list of them; datasheet_outline lists them."}
        page, budget = 4500, 11000   # a section is read in pages of `page` chars; one call returns up to `budget`
        part = max(1, int(part or 1))
        out, missing, later, total = [], [], [], 0
        for key in keys[:10]:
            s = find_section(secs, str(key))
            if s is None:
                missing.append(str(key))
                continue
            end = subtree_end(secs, s)
            a = s.start + (part - 1) * page
            b = min(end, a + page)
            if out and total + (b - a) > budget:
                later.append(s.num)
                continue
            # PDF math letters shown as the plain letters (one character each, so offsets hold; quotes match either form)
            text = src.text[a:b].translate(MATH_FOLD)
            total += len(text)
            self.reads.add(source_id, a, b)
            # offset: where the text sits in the source, so the read log can be rebuilt from the conversation
            item = {"section": s.label, "offset": a, "text": text, "requirements": requirements(text)}
            figs = [x for x in self._figure_index(source_id) if s.start <= x.start < end]
            if figs:   # drawings the text extraction cannot carry
                item["figures"] = [f"{x.label}: {x.caption} — read_figure('{source_id}', '{x.label}')" for x in figs[:8]]
            if b < end:
                item["more"] = f"read_section('{source_id}', '{s.num}', part={part + 1}) for the rest ({end - b} chars)"
            out.append(item)
        res: Dict[str, Any] = {"source_id": source_id, "sections": out}
        if later:
            res["not_read_yet"] = f"read_section('{source_id}', {later}) — they did not fit in this reply"
        if missing:
            res["not_found"] = missing
            res["hint"] = "datasheet_outline lists the section numbers and titles."
        if out:
            res["next"] = ("Each `requirements` line is a rule the datasheet states: record the ones your code depends on "
                           "(record_fact for values and times, record_sequence for orders), quoting the text exactly.")
        return res

    def _compiler_findings(self) -> List[Any]:
        """GCC's warnings from the build of the current sources: the bug classes (uninitialized use, array bounds, shift
        overflow, missing return, format mismatch...) are errors, the rest warnings."""
        from embedforge.truth.verifier import Finding
        lb = self.project.meta().get("last_build") or {}
        if lb.get("hash") != self.source_hash():
            return []
        out = []
        st = lb.get("static") or {}
        for d in st.get("defects") or []:
            ident = str(d.get("id") or "")
            misra = ident.startswith("misra-c2012-")
            defect = not misra and (d.get("severity") == "high" or d.get("category") in ("error", "warning"))
            msg = (f"MISRA C:2012 Rule {ident[len('misra-c2012-'):]} is not followed here" if misra
                   else f"cppcheck {ident}: {d.get('message')}")
            out.append(Finding("ERROR" if defect else "WARNING", "MISRA_C" if misra else "STATIC_ANALYSIS",
                               str(d.get("file") or ""), int(d.get("line") or 0), msg,
                               suggestion=("Fix the defect cppcheck points at (a value used before it is set, an index out of "
                                           "range, a leak...)." if defect else
                                           "Follow the rule, or keep the code and explain the deviation in a comment "
                                           "(// cppcheck-suppress <id>: why) — MISRA C is advisory for C++ code.")))
        if st and not st.get("ok"):
            out.append(Finding("WARNING", "STATIC_ANALYSIS_NOT_RUN", "", 0, f"cppcheck did not run: {st.get('error')}",
                               suggestion="Report it to the user; the other checks still ran."))
        for w in lb.get("warnings") or []:
            msg = str(w.get("message") or "")
            bug = bool(pio.BUG_WARNINGS.search(msg))
            out.append(Finding("ERROR" if bug else "WARNING", "COMPILER_WARNING", str(w.get("file") or ""), int(w.get("line") or 0),
                               f"GCC: {msg}", suggestion=("This warning class marks code that can misbehave at run time: fix "
                                                          "the code it points at." if bug else
                                                          "Clean it up, or explain in a comment why it is intended.")))
        return out

    def _code_uses(self, rep: Any) -> Dict[str, str]:
        """What the code does, as the datasheet topics it makes required reading (from verify's view of the code and
        the recorded truth): the modes it sets, the registers it touches, the data it reads, its formulas and bus."""
        from embedforge.truth.registers import RegisterBook
        from embedforge.truth.sequences import SequenceBook
        regs = rep.registers or {}
        writes = regs.get("writes") or []
        uses: Dict[str, str] = {}
        names = {m.group(1) for w in writes for m in [re.search(r":\d+ ([A-Za-z_]\w*) \(0x", w)] if m}
        modes = {}
        for w in writes:
            reg = re.search(r":\d+ ([A-Za-z_]\w*) \(0x", w)
            for fm in re.finditer(r"(\w*mode\w*)=\S+ \(([\w-]+)\)", w, re.I):
                modes[fm.group(2).lower()] = f"the code sets {reg.group(1) if reg else 'a register'}.{fm.group(1)} = {fm.group(2)}"
            for fm in re.finditer(r"(\w+)=", w):
                f = fm.group(1).lower()
                if f.startswith("osr") and "oversampling" not in uses:
                    uses["oversampling"] = f"the code sets {f}"
                if ("iir" in f or "filter" in f) and "filter" not in uses:
                    uses["filter"] = f"the code sets {f}"
                if f.startswith("odr") and "timing" not in uses:
                    uses["timing"] = f"the code sets {f} (the output data rate)"
                if "fifo" in f:
                    uses.setdefault("fifo", f"the code sets {f}")
                if re.match(r"(int|irq)(_|\d|$)", f) or f.startswith("drdy_en"):
                    uses.setdefault("interrupts", f"the code sets {f}")
        if modes:
            uses["modes"] = next(iter(modes.values()))
            uses["mode_words"] = ",".join(modes)
        if any(k in uses for k in ("oversampling", "filter", "timing")):
            # the datasheet's recommended settings per use case: the choice should fit what the user is building
            uses["settings_choice"] = "the code chooses oversampling / filter / data rate: match the recommendation for the user's use case"
        book = RegisterBook(self.project.root)
        data = [l.name for l in book.layouts.values() if not re.search(r"nvm|calib|par_|trim|coef|dig_", l.name, re.I)]
        if data or book.bursts:
            why = f"the code reads measurement data ({(data or ['a burst'])[0]})"
            uses.setdefault("timing", why)
            uses["ready"] = why
            uses["readout"] = why
        if book.formulas:
            uses["formulas"] = f"the code implements {len(book.formulas)} recorded formulas"
        if SequenceBook(self.project.root).items:
            uses["reset"] = "the code runs a recorded init sequence"
        names |= {r.name for r in book.registers.values() if re.search(rf"\b{re.escape(r.name)}\b", " ".join(writes))}
        if names:
            uses["register_names"] = ",".join(sorted(names))
        code = " ".join(p.read_text(encoding="utf-8", errors="ignore") for p in collect_sources(self.project.root))
        if re.search(r"\bWire1?\s*\.|\bTwoWire\b|\bi2c_\w+\(|HAL_I2C_|i2c_master_", code):
            uses["i2c"] = "the code talks I2C"
        if re.search(r"\bSPI1?\s*\.|\bspi_\w+\(|HAL_SPI_|spi_device_", code):
            uses["spi"] = "the code talks SPI"
        return uses

    def _unread_sections(self, rep: Any) -> List[Any]:
        """DATASHEET_UNREAD: sections of a datasheet the code relies on that the agent was never shown."""
        from embedforge.truth.verifier import Finding
        self.state["uses"] = self._code_uses(rep)
        cited = {f.source_id for f in self.ledger.facts.values()}
        try:
            from embedforge.truth.registers import RegisterBook
            book = RegisterBook(self.project.root)
            cited |= {x.source_id for x in list(book.registers.values()) + list(book.layouts.values()) + list(book.formulas.values())}
        except Exception:
            pass
        out = []
        for sid in sorted(cited):
            src = self.store.get(sid)
            if src is None or src.url.startswith("user://") or len(self._outline(sid)) < 8:
                continue
            need = self._required_for(sid)
            unread = [(s, why) for s, why in need if self.reads.seen(sid, s.start, s.end) < 0.5]
            self.state.setdefault("unread", {})[sid] = [f"{s.num} {s.title[:50]}" for s, _ in unread]
            out += self._open_requirements(sid, [s for s, _ in need if (s, _) not in unread])
            out += self._unbacked_claims(sid)
            # figures in the sections that limit what the code sets (timing, modes, settings): often the only place a rule is
            from embedforge.truth.outline import STRICT_TOPICS
            strict = [s for s, _w, t in self._needed(sid) if t in STRICT_TOPICS]
            figs = [x for x in self._figure_index(sid) if x.label.startswith("Figure")
                    and any(s.start <= x.start < s.end for s in strict)
                    and not (self.store.dir / "figures" / f"{sid}-{re.sub(r'[^a-z0-9]+', '-', x.label.lower())}.md").exists()]
            if figs:
                stored = self.store.pdf_path(sid).exists()
                out.append(Finding("WARNING", "FIGURE_UNREAD", "", 0,
                                   f"{len(figs)} figure(s) in sections about what this code sets were not read: "
                                   + "; ".join(f"{x.label}: {x.caption[:50]} (section {x.section})" for x in figs[:6]) + ".",
                                   suggestion=(f"read_figure('{sid}', '<Figure n>') for each; order and timing rules are often "
                                               "drawn, not written." if stored else
                                               f"The original PDF of {sid} is not stored: fetch_document its URL again, or ask "
                                               "the user to upload the PDF, so its figures can be read.")))
            if unread:
                listed = "; ".join(f"{s.label[:60]} ({why})" for s, why in unread[:8])
                out.append(Finding(
                    "ERROR", "DATASHEET_UNREAD", "", 0,
                    f"{len(unread)} section(s) of {sid} describe what this code does but were never read, so the rules in them "
                    f"(order, timing, valid settings) are unchecked: {listed}.",
                    suggestion=(f"read_section('{sid}', {[s.num for s, _ in unread[:8]]}) and record what they require "
                                "(record_fact for values and times, record_sequence for orders); then fix the code if it breaks one.")))
        return out

    def _open_requirements(self, sid: str, read_sections: List[Any]) -> List[Any]:
        """REQUIREMENT_OPEN: rule sentences of the sections read (the ones the code depends on) that no record quotes and
        the agent has not resolved (truth/knowledge.py)."""
        from embedforge.truth.knowledge import Requirements, truth_spans
        from embedforge.truth.ledger import anchor
        from embedforge.truth.outline import requirements as rule_sentences
        from embedforge.truth.verifier import Finding
        src = self.store.get(sid)
        if src is None:
            return []
        found = []
        for s in read_sections:
            body = src.text[s.start:s.end]
            for sent in rule_sentences(body, limit=40):
                sp = anchor(sent, body)
                if sp:
                    found.append((s.label[:80], sent, s.start + sp[0], s.start + sp[1]))
        reqs = Requirements(self.project.root)
        mine = reqs.sync(sid, found)
        reqs.cover(truth_spans(self.project.root, self.store, self.ledger))
        mine = [reqs.items[r.id] for r in mine]
        out = []
        for r in [x for x in reqs.disputed() if x.source_id == sid]:
            stuck = r.disputes >= 3   # author and reviewer keep disagreeing: a person decides
            out.append(Finding("WARNING" if stuck else "ERROR", "REQUIREMENT_DISPUTED", r.review.get("where", "").split(":")[0], 0,
                               f"{r.id} [{r.section.split(' ')[0]}] \"{r.text[:120]}\": you said {r.status} ({r.reason[:100]}); the "
                               f"independent reviewer disagrees: {r.review.get('why', '')}"
                               + (f" ({r.review['where']})" if r.review.get("where") else "")
                               + (" — disputed 3 times: left for a person to decide." if stuck else ""),
                               suggestion=("Fix the code where the reviewer points, or record the rule (record_constraint / "
                                           "record_fact / record_sequence) so verify checks it instead of a claim, or "
                                           "resolve_requirements again with a reason that answers the reviewer.")))
        open_ = [r for r in mine if r.status == "open"]
        if not open_:
            return out
        listed = "; ".join(f"{r.id} [{r.section.split(' ')[0]}] {r.text[:110]}" for r in open_[:10])
        return out + [Finding("ERROR", "REQUIREMENT_OPEN", "", 0,
                        f"{len(open_)} rule(s) the datasheet states in sections this code depends on are not settled: {listed}"
                        + (f" (+{len(open_) - 10} more: coding_brief lists all)" if len(open_) > 10 else "."),
                        suggestion=("load_skill('datasheet-rules') says what to record for each kind of rule. For each: record "
                                    "what it requires (record_fact / record_sequence / record_constraint, quoting it), or "
                                    "resolve_requirements([{id, status: 'implemented', reason: <where the code does it>} | "
                                    "{id, status: 'not_applicable', reason: <why>}]) — several per call; an independent "
                                    "reviewer checks each claim against the code."))]

    def resolve_requirements(self, items: Any) -> Dict[str, Any]:
        from embedforge.truth.knowledge import Requirements
        reqs = Requirements(self.project.root)
        items = items if isinstance(items, list) else [items] if isinstance(items, dict) else []
        # rules about what the code itself sets need a record behind the claim (see _unbacked_claims)
        from embedforge.truth.knowledge import truth_spans
        truth = truth_spans(self.project.root, self.store, self.ledger)
        early: List[Dict[str, Any]] = []
        keep = []
        for it in items:
            it = it if isinstance(it, dict) else {}
            rid = re.sub(r"^\s*REQ[-_ ]?0*(\d+)\s*$", lambda m: f"REQ-{int(m.group(1)):03d}", str(it.get("id") or ""), flags=re.I)
            r = reqs.items.get(rid)
            topic, spans = self._strict_topic(r.source_id, r.section) if r else ("", [])
            ref = str(it.get("ref") or "").strip().upper()
            status = str(it.get("status") or "").strip().lower().replace(" ", "_")
            if topic and status == "not_applicable":
                early.append({"id": rid, "error": f"this rule is about {topic.replace('_', ' ')} the code itself sets, so it "
                                                  "applies: record it (record_constraint / record_fact / record_sequence) "
                                                  "and resolve it as implemented with ref = that record's id"})
            elif topic and not self._backed(r.source_id, ref, spans, truth, topic):
                quoted = bool(ref) and self._backed(r.source_id, ref, spans, truth)
                early.append({"id": rid, "error": (
                    f"a claim about {topic.replace('_', ' ')} needs ref = the id of a record quoting this section that verify "
                    "checks against the code" + (
                        f"; {ref} quotes it, but nothing in the code uses it, so it checks nothing: "
                        + ("record_constraint the limit (e.g. when <MODE_REG>.mode == normal: conversion time <= sampling "
                           "period), which verify evaluates after every register write"
                           if topic == "timing" else
                           "cite that fact on the constant the code sets (/* FACT-nnn */), or record_constraint the rule")
                        if quoted else (f"; {ref} does not quote it" if ref else "")))})
            else:
                keep.append(dict(it, ref=ref))
        res = reqs.resolve(keep)
        if early:
            res.setdefault("rejected", []).extend(early)
            res["ok"] = bool(res.get("resolved"))
        res["next"] = "verify again: the requirements are part of what it judges."
        ids = [s.split(" ")[0] for s in res.get("resolved") or []]
        if ids and self.reviewer is not None:
            # an independent reviewer (agent/review.py) checks each claim against the code
            from embedforge.agent.review import review_claims
            files = {str(p.relative_to(self.project.root)).replace("\\", "/"): p.read_text(encoding="utf-8", errors="ignore")
                     for p in collect_sources(self.project.root)}
            self.session.emit("status", text=f"Reviewer checking {len(ids)} requirement claim(s) against the code…")
            try:
                verdicts = review_claims(self.reviewer, [reqs.items[i] for i in ids], files)
            except Exception as e:   # the reviewer is a second opinion: its failure must not cost the claim
                verdicts = {}
                self.session.emit("status", text=f"Reviewer unavailable: {type(e).__name__}: {str(e)[:120]}")
            reqs.set_reviews(verdicts)
            dis = [{"id": i, **v} for i, v in verdicts.items() if v["verdict"] == "disagree"]
            res["review"] = {"agreed": sum(1 for v in verdicts.values() if v["verdict"] == "agree"), "disagreed": dis,
                             "unreviewed": [i for i in ids if i not in verdicts]}
            if dis:
                res["next"] = ("The reviewer disagrees with the claims listed under review.disagreed. Fix the code where it "
                               "points, or record the rule (record_constraint / record_fact / record_sequence) so verify "
                               "checks it, or resolve again with a reason that answers the reviewer.")
        return res

    def search_datasheet(self, source_id: str, question: str, k: int = 5) -> Dict[str, Any]:
        """The passages of a stored source that best answer a question (Nemotron embeddings, cached per source)."""
        from embedforge.agent import retrieval
        sid = self._sid(source_id)
        src = self.store.get(sid)
        if src is None:
            return {"error": f"Unknown source_id {sid}."}
        if not str(question or "").strip():
            return {"error": "question: what you want to find, in words (e.g. 'measurement time for x8 oversampling')."}
        k = max(1, min(int(k or 5), 8))
        try:
            hits = retrieval.search(self.store.dir, sid, src.text, self._outline(sid), str(question), k, self.embedder)
        except Exception as e:   # no key, provider down: the agent still has read_section / read_source
            return {"error": f"Datasheet search is unavailable ({type(e).__name__}: {str(e)[:200]}). Use datasheet_outline "
                             "and read_section, or read_source with a query."}
        for h in hits:
            self.reads.add(sid, h["start"], h["end"])
            h["text"] = h["text"].translate(MATH_FOLD)
        return {"source_id": sid, "passages": hits,
                "next": "Quote from these passages (record_fact / record_constraint); read_section the sections they come "
                        "from when you need the whole rule."}

    def _figure_index(self, sid: str):
        from embedforge.truth.outline import figures
        src = self.store.get(sid)
        return figures(src.text, self._outline(sid)) if src is not None else []

    def read_figure(self, source_id: str, figure: str) -> Dict[str, Any]:
        """A datasheet figure or table, transcribed by a vision model from the stored PDF (cached per figure)."""
        sid = self._sid(source_id)
        src = self.store.get(sid)
        if src is None:
            return {"error": f"Unknown source_id {sid}."}
        figs = self._figure_index(sid)
        key = re.sub(r"\s+", " ", re.sub(r"^fig(?:ure)?\.?\s*", "figure ", str(figure or "").strip().lower()))
        if re.fullmatch(r"\d+", key):   # a bare number is a figure; 'Table 3' and 'Figure 3' are different things
            key = "figure " + key
        lead = re.match(r"(figure|table) (\d+)\b", key)   # 'Table 41: Register 0x1C “OSR”' names Table 41
        f = next((x for x in figs if x.label.lower() == key), None) \
            or next((x for x in figs if lead and x.label.lower() == f"{lead.group(1)} {lead.group(2)}"), None) \
            or next((x for x in figs if key and key in x.caption.lower()), None)
        if f is None:
            return {"error": f"No figure {figure!r} in {sid}. Captions: " + "; ".join(f"{x.label}: {x.caption[:40]}" for x in figs[:30])}
        context = re.sub(r"[ \t]+", " ", src.text[max(0, f.start - 600):f.start + 400]).strip()
        slug = re.sub(r"\W+", "-", f.label.lower())
        cache = self.store.dir / "figures" / f"{sid}-{slug}.md"
        if cache.exists():
            text, model = cache.read_text(encoding="utf-8"), "cached"
        else:
            pdf = self.store.pdf_path(sid)
            if not pdf.exists():
                return {"figure": f"{f.label}: {f.caption}", "section": f.section, "text_around_caption": context,
                        "error": (f"The original PDF of {sid} is not stored (it came in as text), so the figure cannot be "
                                  "rendered. fetch_document its URL again or ask the user to upload the PDF; until then "
                                  "the text around the caption is all there is.")}
            from embedforge.agent import vision
            reader, model = (self.vision, "custom") if self.vision is not None else vision.default_reader()
            self.session.emit("status", text=f"Reading {f.label} ({f.caption[:50]}) with {model}…")
            try:
                text, page = vision.transcribe(pdf, f.label, f.caption, f.page, reader)
            except ImportError:
                return {"error": "Rendering PDF pages needs PyMuPDF (pip install 'embedforge[pdf]'); ask the user to install "
                                 "it. Until then the text around the caption is all there is.", "text_around_caption": context}
            except Exception as e:
                return {"error": f"The vision model could not read {f.label}: {type(e).__name__}: {str(e)[:200]}",
                        "text_around_caption": context}
            if not text:
                return {"error": f"{f.label} was not found on any page of the stored PDF.", "text_around_caption": context}
            f.page = page
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(text, encoding="utf-8")
        derived = self.store.add(f"figure://{sid}/{slug}", f"{f.label}: {f.caption} (page {f.page}, transcribed by a vision "
                                 f"model from {sid})", text, kind="figure")
        self.reads.add(sid, f.start, f.start + len(f.caption) + 20)
        return {"figure": f"{f.label}: {f.caption}", "page": f.page, "section": f.section, "source_id": derived.id,
                "transcription": text,
                "note": (f"A vision model's transcription of the drawing, not the datasheet's own words: use it to understand "
                         f"the order and the values, and quote the datasheet text ({sid}) where it says the same. A fact "
                         f"quoting {derived.id} is accepted with a warning that it comes from a figure.")}

    def load_skill(self, name: str) -> Dict[str, Any]:
        from embedforge.skills import list_skills, load_skill
        body = load_skill(name)
        if body is None:
            return {"error": f"No skill {name!r}. Skills: " + ", ".join(n for n, _ in list_skills())}
        return {"skill": name, "playbook": body}

    def coding_brief(self) -> Dict[str, Any]:
        """Everything the code must do, from what is on record: the user's request, wiring, init order, settings, the
        rules that must hold after every write, readout, conversions, the datasheet's requirements and the gates verify
        applies. Read it before writing or fixing code."""
        from embedforge.truth.knowledge import Requirements
        from embedforge.truth.registers import RegisterBook, register_derived
        from embedforge.truth.sequences import SequenceBook
        brief: Dict[str, Any] = {}
        conv = self.store._by_url.get("user://conversation")
        if conv and self.store.get(conv):
            brief["user_asked"] = self.store.get(conv).text[-1500:]
        facts = list(self.ledger.facts.values())
        brief["wiring_and_ids"] = [f"{f.id} {f.subject}.{f.property} = {f.value}" + (f" {f.unit}" if f.unit else "") for f in facts][:40]
        book = RegisterBook(self.project.root)
        brief["registers"] = [f"{r.id} {r.name} 0x{r.addr:02X}" + (f" reset 0x{r.reset:02X}" if r.reset is not None else "") + ": "
                              + ", ".join(f.label() + (" {" + ", ".join(f"{n}={'|'.join(map(str, vs))}" for n, vs in f.values.items()) + "}" if f.values else "")
                                          for f in r.fields) for r in book.registers.values()][:30]
        seqs = SequenceBook(self.project.root).items.values()
        brief["sequences"] = [f"{s.id} {s.subject}.{s.name} in {s.entry}(): " + " -> ".join(
            f"{st.op} " + ", ".join(f"{k}={v}" for k, v in st.public().items() if k != "op") for st in s.steps) for s in seqs]
        if book.constraints:
            brief["rules_after_every_write"] = [f"{c.id} {c.name}: " + (f"when {c.when}: " if c.when else "") + c.require
                                                + (f" [{c.unit}]" if c.unit else "") for c in book.constraints.values()]
        if book.bursts or book.layouts:
            brief["readout"] = ([f"{b.id} {b.name}: read 0x{b.start:02X}..0x{b.end:02X} in ONE burst" for b in book.bursts.values()]
                                + [l.describe() for l in book.layouts.values()])[:30]
        if book.formulas:
            derived = register_derived(book)
            brief["conversions"] = [f"{f.id} {f.name}: {f.text()}" for f in book.formulas.values() if f.id in derived][:30]
            consts = [f"{f.name} = {f.text().split('=', 1)[-1].strip()}" for f in book.formulas.values() if f.id not in derived]
            if consts:
                brief["constants"] = consts[:20]
        reqs = Requirements(self.project.root)
        if reqs.items:
            from embedforge.truth.knowledge import truth_spans
            reqs.cover(truth_spans(self.project.root, self.store, self.ledger))   # records made since the last verify count
            brief["datasheet_requirements"] = {
                "open": [f"{r.id} [{r.section.split(' ')[0]}] {r.text}" for r in reqs.items.values() if r.status == "open"][:25],
                "implemented (claimed)": [f"{r.id} {r.text[:120]} — {r.reason}" for r in reqs.items.values() if r.status == "implemented"][:20],
                "covered by records": [f"{r.id} {r.text[:100]} ({', '.join(r.refs)})" for r in reqs.items.values() if r.status == "covered"][:20],
                "counts": reqs.counts()}
        lv = self.project.meta().get("last_verify") or {}
        if lv.get("findings"):
            brief["last_verify"] = {"summary": lv.get("summary"), "first_findings": lv.get("findings")}
        brief["gates_verify_applies"] = [
            "every hardware constant and every pin carries /* FACT-nnn */ (pins: the schematic's fact for that role, SDA vs SCL)",
            "register writes decode to recorded fields and named values; multi-byte values follow their layouts; bursts stay whole",
            "each conversion computes the recorded formula exactly; sequences happen in order with their delays",
            "each rule in rules_after_every_write holds after EVERY register write (write settings before the mode)",
            "no wait on hardware without a timeout or retry limit; check every status a call returns; a failure branch never "
            "returns success",
            "compiles warning-free at -Wall -Wextra; cppcheck finds no defect (initialise every member and variable)",
            "every datasheet requirement above is covered, implemented or not applicable; then a mutation test measures the checks"]
        brief["next"] = ("Write or fix the code so it does all of this; then build and verify. Resolve each open requirement "
                         "(record it, or resolve_requirements) — verify fails while one is open.")
        return brief

    def _needed(self, source_id: str) -> List[Tuple[Any, str, str]]:
        """(section, why, topic) for the sections this source's code depends on, plus the sections they refer to."""
        from embedforge.truth.outline import required_topics, with_xrefs
        uses = self.state.get("uses") or {}
        src = self.store.get(source_id)
        if not uses or src is None:
            return []
        secs = self._outline(source_id)
        return with_xrefs(src.text, secs, required_topics(secs, uses))

    def _required_for(self, source_id: str):
        """Sections of this source the code depends on (from what the last verify saw the code do)."""
        return [(s, why) for s, why, _t in self._needed(source_id)]

    def _strict_topic(self, sid: str, section_label: str) -> Tuple[str, List[Tuple[int, int]]]:
        """The topic of a requirement's section when it is one whose rules limit what the code sets (timing, the mode it
        uses, the recommended settings), and the spans of every section of that topic: a record quoting any of them
        backs a claim about it. ('', []) otherwise."""
        from embedforge.truth.outline import STRICT_TOPICS
        tops = self._needed(sid)
        topic = next((t for s, _w, t in tops if s.label[:80] == section_label), "")
        if topic not in STRICT_TOPICS:
            return "", []
        return topic, [(s.start, s.end) for s, _w, t in tops if t == topic]

    def _backed(self, sid: str, ref: str, spans: List[Tuple[int, int]], truth: Dict[str, List[Tuple[int, int, str]]],
                topic: str = "") -> bool:
        return (bool(ref) and any(r == ref and any(a < e and s < b for s, e in spans) for a, b, r in truth.get(sid, []))
                and self._checks_code(ref, topic))

    def _cited_facts(self) -> set:
        """FACT ids the project's own source files cite (/* FACT-012 */): facts verify traces into the code."""
        out: set = set()
        for d in ("src", "include", "lib"):
            for p in (self.project.root / d).rglob("*"):
                if p.is_file() and p.suffix.lower() in (".c", ".cc", ".cpp", ".h", ".hpp", ".ino"):
                    out |= set(re.findall(r"FACT-\d+", p.read_text(encoding="utf-8", errors="ignore").upper()))
        return out

    def _checks_code(self, ref: str, topic: str) -> bool:
        """For timing and the recommended settings, the record behind a claim must be one verify evaluates on the code:
        a constraint (checked after every register write), a sequence (order and delays), or a fact the code cites on
        the constant it sets. The BMP388 fix-up run settled 'the minimum achievable frequency is determined by the
        maximum measurement time' with a fact whose value was the heading '3.9.1 Measurement time', and verify passed
        a driver that enters normal mode before its settings. Rules about modes keep any record: many of them only say
        what the device does by itself."""
        if topic not in ("timing", "settings_choice"):
            return True
        if ref.startswith(("CON-", "SEQ-")):
            return True
        return ref.startswith("FACT-") and ref in self._cited_facts()

    def _unbacked_claims(self, sid: str) -> List[Any]:
        """REQUIREMENT_NEEDS_RECORD: a rule about timing, the mode the code uses or the recommended settings settled by
        a claim alone. The BMP388 rehearsal showed why: the agent called the measurement-time limit 'informational' and
        the settings-before-mode rule 'implemented', and an LLM reviewer agreed; only a record verify checks holds."""
        from embedforge.truth.knowledge import Requirements, truth_spans
        from embedforge.truth.verifier import Finding
        reqs = Requirements(self.project.root)
        truth = truth_spans(self.project.root, self.store, self.ledger)
        bad = []
        for r in reqs.items.values():
            if r.source_id != sid or r.status not in ("implemented", "not_applicable"):
                continue
            topic, spans = self._strict_topic(sid, r.section)
            if topic and not self._backed(sid, r.ref, spans, truth, topic):
                bad.append((r, topic, spans))
        if not bad:
            return []
        listed = "; ".join(f"{r.id} [{r.section.split(' ')[0]}] {r.text[:90]} ({r.status})" for r, _t, _s in bad[:8])
        # what to do for each, concretely: in the BMP388 fix-up run Nemotron re-read the sections for 25 steps, not seeing
        # that FACT-019 already quoted the mode-transition section and could back two of these rules
        make = {"timing": "record_constraint(when='<MODE_REG>.mode == <the mode the code uses>', require='<conversion time "
                          "from the formula, REGISTER.field names> <= <sampling period from the ODR table>', quotes=[the "
                          "formula line, the table row])",
                "settings_choice": "record_fact for the row of the recommended-settings table that fits the user's use "
                                   "case (its oversampling, filter, data rate), quoting that row; then use those values "
                                   "and cite the fact on the constants (/* FACT-nnn */)",
                "modes": "record_sequence for the order the section gives (e.g. settings while asleep, then the mode), "
                         "or record_fact for a time or value it states"}
        groups: Dict[Tuple[str, Tuple[str, ...]], List[str]] = {}
        for r, topic, spans in bad[:8]:
            # records quoting this topic's sections, the ones nearest the rule's own sentence first
            near = sorted({(max(0, a - r.end, r.start - b), ref) for a, b, ref in truth.get(sid, [])
                           if any(a < e and s < b for s, e in spans) and self._checks_code(ref, topic)})
            have = tuple(dict.fromkeys(ref for _d, ref in near))[:3]
            groups.setdefault((topic, have), []).append(r.id)
        steps = []
        for (topic, have), ids in groups.items():
            steps.append(f"{', '.join(ids)}: " + (f"{', '.join(have)} already quote the {topic.replace('_', ' ')} sections (nearest "
                                                  f"first) — where one states the rule, resolve_requirements with status "
                                                  f"'implemented' and ref '{have[0]}'; otherwise " if have else "")
                         + make.get(topic, "record the rule") + ", then resolve with ref = the new record's id")
        return [Finding("ERROR", "REQUIREMENT_NEEDS_RECORD", "", 0,
                        f"{len(bad)} rule(s) about {', '.join(sorted({t.replace('_', ' ') for _, t, _s in bad}))} — what the code "
                        f"itself sets — are settled by a claim, which nothing checks: {listed}.",
                        suggestion=("Record each rule so verify checks it on every build (reading the section again does not "
                                    "settle it): " + " | ".join(steps) + ". load_skill('datasheet-rules') explains each kind."))]

    def record_fact(self, subject: str, property: str, value: Any, source_id: str, quote: str, unit: str = "") -> Dict[str, Any]:
        # a quote copied from a clipped line may end in an ellipsis; the text before it is still verbatim
        quote = re.sub(r"\s*(…|\.\.\.)\s*$", "", quote or "")
        m = re.fullmatch(r"\s*SRC[-_ ]?0*(\d+)\s*", str(source_id or ""), re.I)   # 'SRC001', 'src-1' -> 'SRC-001'
        if m:
            source_id = f"SRC-{int(m.group(1)):03d}"
        res = self.ledger.record(subject, property, value, source_id, quote, unit)
        if not res.get("ok") and "does not appear inside the quoted text" in str(res.get("error")):
            # '111011x (6 MSB fixed, LSB from SDO)' -> '111011x': the model's own explanation is not part of the value
            core = re.split(r"\s*[(\[;]|\s+-\s+|,\s+(?=[A-Za-z])", str(value).strip(), maxsplit=1)[0].strip()
            if core and core != str(value).strip():
                again = self.ledger.record(subject, property, core, source_id, quote, unit)
                if again.get("ok"):
                    res = again
                    res.setdefault("warnings", []).append(f"Recorded the value as {core!r}: the explanation after it is not in the quote.")
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
            cands = self._candidate_quotes(source_id, str(value), quote=quote)
            if cands:
                res["candidate_quotes"] = cands
                res["hint"] = ("These passages from the source contain the value verbatim, closest to your quote first. If one "
                               "states the fact, retry with it as `quote`.")
        return res

    def _candidate_quotes(self, source_id: str, value: str, limit: int = 3, quote: str = "") -> List[str]:
        """Verbatim windows of the source that contain `value`, to help the model copy an exact quote; with the quote the
        model tried, the windows sharing most of its words come first (the passage it meant, not the first mention)."""
        from embedforge.truth.ledger import value_variants
        src = self.store.get(source_id)
        if not src or not value.strip():
            return []
        text = src.text
        low = text.lower()
        found: List[str] = []
        for _form, rx, conf in value_variants(value):
            if conf != "STRICT":
                continue
            for m in re.finditer(rx, low):
                a, b = max(0, m.start() - 110), min(len(text), m.end() + 110)
                # trim partial words at both ends without cutting into the matched value
                a = next((i for i in range(a, m.start()) if text[i].isspace()), a) if a > 0 else a
                b = next((i for i in range(b, m.end(), -1) if text[i - 1].isspace()), b) if b < len(text) else b
                w = re.sub(r"\s+", " ", text[a:b]).strip()
                if w and w not in found:
                    found.append(w)
                if len(found) >= (40 if quote else limit):
                    break
        words = set(re.findall(r"[a-z0-9_]{3,}", (quote or "").lower()))
        if words:   # stable: equally close windows keep document order
            found.sort(key=lambda w: -len(words & set(re.findall(r"[a-z0-9_]{3,}", w.lower()))))
        return found[:limit]

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
        src = self.store.get(source_id)
        if not res.get("ok") and src is not None and "order_quote" in str(res.get("error")):
            # sentences of the source about order and timing, to quote instead of a paraphrase
            words = re.compile(r"soft.?reset|power.?(?:on|up)|start.?up|after|before|wait|sleep mode|normal mode|must|first|then", re.I)
            sents = [re.sub(r"\s+", " ", s).strip() for s in re.split(r"(?<=[.;])\s+", src.text) if 30 < len(s) < 400]
            ranked = sorted(sents, key=lambda s: -len(words.findall(s)))[:3]
            if ranked:
                res["candidate_quotes"] = ranked
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

    def record_constraint(self, device: str, source_id: str, name: str, require: str, quotes: Any = None, when: str = "",
                          unit: str = "", quote: str = "") -> Dict[str, Any]:
        qs = quotes if isinstance(quotes, list) else [quotes] if quotes else []
        if quote:
            qs.append(quote)
        res = self._register_book().record_constraint(self.store, device, self._sid(source_id), name, require, qs, when, unit)
        if not res.get("ok") and "not verbatim" in str(res.get("error")):
            res["hint"] = ("Copy each quote exactly from read_section / read_source (several short passages are fine: the "
                           "formula's line and the table rows).")
        return res

    def record_layouts(self, device: str, source_id: str, layouts: List[Dict[str, Any]]) -> Dict[str, Any]:
        lays = layouts if isinstance(layouts, list) else []
        for l in lays:
            if isinstance(l, dict) and l.get("source_id"):
                l["source_id"] = self._sid(l["source_id"])
        res = self._register_book().record_layouts(self.store, device, self._sid(source_id), lays)
        retry = []
        for rej in res.get("rejected") or []:
            raw = next((l for l in lays if isinstance(l, dict) and l.get("name") == rej.get("layout")), None)
            alt = self._source_with(str(raw.get("quote") or ""), str(raw.get("source_id") or self._sid(source_id))) \
                if raw and "not verbatim" in str(rej.get("error")) else None
            if alt:
                retry.append(dict(raw, source_id=alt))
        if retry:   # quoted from another stored source than the one cited
            again = self._register_book().record_layouts(self.store, device, self._sid(source_id), retry)
            done = {str(x).split(" ", 2)[1] for x in again.get("recorded") or []}
            res["recorded"] = (res.get("recorded") or []) + (again.get("recorded") or [])
            res["rejected"] = [r for r in res.get("rejected") or [] if r.get("layout") not in done] + (again.get("rejected") or [])
            res["ok"] = bool(res["recorded"])
            if not res["rejected"]:
                res.pop("rejected", None)
                res.pop("hint", None)
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

    def _source_with(self, quote: str, exclude: str) -> Optional[str]:
        """Another stored (non-user) source in which the quote is verbatim: models often cite the wrong SRC id."""
        from embedforge.truth.registers import verbatim
        if not quote or len(quote) < 12:
            return None
        for s in self.store.sources.values():
            if s.id != exclude and not s.url.startswith("user://") and verbatim(quote, s.text):
                return s.id
        return None

    def record_formula(self, device: str, source_id: str = "", name: str = "", steps: Any = None, quote: str = "", unit: str = "",
                       inputs: Optional[Dict[str, str]] = None, formulas: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        if isinstance(formulas, list) and formulas and not steps:
            # several in one call, in order (a later one may use an earlier one); device and source_id are defaults
            recorded: List[str] = []
            rejected: List[Dict[str, Any]] = []
            for item in formulas[:20]:
                item = item if isinstance(item, dict) else {}
                res = self.record_formula(str(item.get("device") or device), str(item.get("source_id") or source_id),
                                          str(item.get("name") or ""), item.get("steps"), str(item.get("quote") or ""),
                                          str(item.get("unit") or ""), item.get("inputs") if isinstance(item.get("inputs"), dict) else None)
                if res.get("ok"):
                    recorded.append(res["formula"])
                else:
                    rejected.append({"item": item.get("name") or "?", "error": str(res.get("error"))[:500],
                                     **{k: res[k] for k in ("candidate_quotes", "where_defined", "hint") if res.get(k)}})
            out: Dict[str, Any] = {"ok": bool(recorded), "recorded": recorded}
            if rejected:
                out["rejected"] = rejected
                out["note"] = "Recorded formulas are kept; resend only the rejected ones."
            return out
        res = self._register_book().record_formula(self.store, device, self._sid(source_id), name, steps, quote, unit,
                                                   inputs if isinstance(inputs, dict) else None)
        if not res.get("ok") and "not verbatim" in str(res.get("error")):
            alt = self._source_with(str(quote or ""), self._sid(source_id))
            if alt:
                res = self._register_book().record_formula(self.store, device, alt, name, steps, quote, unit,
                                                           inputs if isinstance(inputs, dict) else None)
                res["note"] = f"The quote is not in {self._sid(source_id)} but is verbatim in {alt}; recorded with {alt}."
        unknown = re.search(r"unknown name\(s\) ([\w, ]+)\.", str(res.get("error") or ""))
        if not res.get("ok") and unknown:
            # where the stored sources assign those names (a converted coefficient is defined somewhere: quote that)
            found = []
            for n in [x.strip() for x in unknown.group(1).split(",") if x.strip()][:3]:
                hits = []
                for s in self.store.sources.values():
                    if s.url.startswith("user://"):
                        continue
                    for m in re.finditer(rf"(?:->|\.|\b){re.escape(n)}\s*=(?!=)([^;\n]*)", s.text, re.I):
                        rhs = m.group(1)
                        # a computed value (a conversion) ranks before a plain copy of register bytes
                        score = (2 if re.search(r"[/*]", rhs) else 0) + (1 if re.search(r"\d\.\d|temp_var|pow|<<|>>", rhs) else 0)
                        a = m.start()
                        for _ in range(4):   # a few lines of context above (constants such as temp_var = 0.00390625f)
                            a = s.text.rfind("\n", 0, max(0, a - 1))
                        b = s.text.find("\n", m.end())
                        hits.append((-score, s.id, s.text[a + 1:b if b > 0 else m.end() + 200].strip()[:500]))
                for sc, sid, passage in sorted(hits)[:2]:
                    found.append({"name": n, "source_id": sid, "passage": passage})
            if found:
                res["where_defined"] = found
                res["hint"] = ("These names are assigned in the stored sources shown. A number the code sets there "
                               "(temp_var = 0.00390625f;) needs no formula of its own: quote that line with this formula. A "
                               "computed value is its own record_formula first (quote its passage exactly), then this formula.")
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
        path = re.sub(r"^betaflight-src:", "bf:", str(path).strip().replace("\\", "/"))  # the form build diagnostics use
        if (self._is_betaflight() and not path.startswith("bf:") and not (self.project.root / path).is_file()
                and re.match(r"(?:\./)?(?:betaflight/)?(?:src|lib)/", path)):
            path = "bf:" + re.sub(r"^(?:\./)?(?:betaflight/)?", "", path)   # 'src/main/...' / 'betaflight/src/...' -> 'bf:src/...'
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
        if (pk not in p.parents or not p.is_file()) and "packages/" in path:   # an absolute or ~/.platformio/packages/... path
            p = (pk / re.sub(r"^.*?(?:^|/)packages/", "", path)).resolve()
        if pk not in p.parents or not p.is_file():
            installed = sorted(d.name for d in pk.iterdir() if d.is_dir())[:12] if pk.is_dir() else []
            return {"error": (f"No SDK file '{path}'. Paths are relative to the packages directory, exactly as search_framework "
                              "prints them before ':<line>' (e.g. 'framework-xyz/include/foo.h')"
                              + (f"; installed packages: {', '.join(installed)}" if installed else "")
                              + ". Use search_framework to find the file first.")}
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

    def rename_file(self, path: str, new_path: str) -> Dict[str, Any]:
        """Rename a project source (e.g. bmp388.c -> bmp388.cpp when it uses Arduino C++ objects)."""
        src = (self.project.root / str(path).replace("\\", "/").lstrip("/")).resolve()
        dst_rel = str(new_path).replace("\\", "/").lstrip("/")
        root = self.project.root.resolve()
        if root not in src.parents or not src.is_file():
            return {"error": f"{path} is not a project file"}
        text = src.read_text(encoding="utf-8", errors="ignore")
        problem = check_agent_write(dst_rel, text) or check_agent_write(str(src.relative_to(root)).replace("\\", "/"), text)
        if problem:
            return {"error": problem}
        if (self.project.root / dst_rel).exists():
            return {"error": f"{dst_rel} already exists"}
        res = self.project.write_file(dst_rel, text)
        src.unlink()
        self.session.emit("file", path=res["path"], content=text)
        return {"ok": True, "renamed": f"{path} -> {dst_rel}", "next": "build"}

    def replace_in_file(self, path: str, old: str, new: str) -> Dict[str, Any]:
        if old == new:
            return {"error": "'old' and 'new' are identical, so nothing would change. Write the corrected text in 'new'."}
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
                c_files = {str(d.get("file")) for d in out["diagnostics"] if str(d.get("file") or "").endswith(".c")}
                cpp_api = [f for f in c_files if (self.project.root / f).is_file() and re.search(
                    r"\b(Wire|Serial|SPI|Wire1)\s*\.\s*\w+\s*\(|#include\s*[<\"](Wire|Arduino|SPI)\.h", (self.project.root / f).read_text(encoding="utf-8", errors="ignore"))]
                if cpp_api:
                    out["next"] = (f"{', '.join(sorted(cpp_api))} is compiled as C, but it uses Arduino's C++ objects (Wire, Serial, SPI): "
                                   f"rename_file it to .cpp (e.g. rename_file('{sorted(cpp_api)[0]}', '{sorted(cpp_api)[0][:-2]}.cpp')), "
                                   "then build.")
            else:
                out["log_tail"] = "\n".join((out.get("log_tail") or "").splitlines()[-25:])
        own = ("src/", "include/", "lib/")
        self.project.update_meta(last_build={
            "hash": h, "ok": res.ok, "source_errors": bool(out["diagnostics"]),
            "diagnostics": [f"{d.get('file')}:{d.get('line')}: {str(d.get('message'))[:160]}" for d in out["diagnostics"][:5]]
                           or ([out.get("error")] if out.get("error") else []),
            # the compiler's warnings on the project's own files, for verify (it judges the build of the current sources)
            "warnings": [{"file": d.file, "line": d.line, "message": d.message[:300]} for d in res.diagnostics
                         if d.severity == "warning" and d.file.replace("\\", "/").lstrip("./").startswith(own)][:60],
        })
        if res.ok and not self._is_betaflight() and config.static_analysis:
            # cppcheck + MISRA C:2012 on the project's own code; verify reports it for the build of the current sources
            self.session.emit("status", text="Static analysis (cppcheck, MISRA C:2012)…")
            st = pio.check(self.project.root)
            self.project.update_meta(last_build={**(self.project.meta().get("last_build") or {}),
                                                 "static": {"ok": st["ok"], "defects": st["defects"][:80], "error": st.get("error", "")}})
            if st["ok"]:
                out["static_analysis"] = f"cppcheck: {len(st['defects'])} finding(s); verify reports them"
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
        chip = f"{meta.get('mcu') or ''} {meta.get('board') or ''} {meta.get('bf_target') or ''}"
        pico = bool(re.search(r"RP2[03]", chip, re.I))
        rep = verify_project(self.project.root, self.ledger, pico=pico, chip=chip.strip())
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
        rep.findings += self._unread_sections(rep)
        rep.findings += self._compiler_findings()
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
                write_atomic(mf_path, json.dumps(mf, indent=2))
                self.session.emit("build_manifest", manifest=mf)
        pub = rep.public()
        self.session.emit("verify", report=pub, summary=rep.summary())
        import json
        # the whole report, so the IDE shows the evidence (findings, traceability, sequences) when the project is reopened
        (self.project.root / "verify_report.json").write_text(
            json.dumps({"hash": h, "summary": rep.summary(), "report": pub}, default=str), encoding="utf-8")
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
            if regs.get("constraints"):
                out["meaning"]["constraints"] = regs["constraints"][:8]
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
                # a finding about one line gets a short hint; one about the whole project (what to read, which rules to
                # record) is few and its steps are the point: cut at 200 characters, the BMP388 runs never saw 'then
                # resolve_requirements with ref = that record' and re-read the datasheet instead
                g["suggestion"] = str(f.get("suggestion") or "")[:200 if f.get("file") else 1500]
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
