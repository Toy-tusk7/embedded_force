"""What the model sees each step: a budgeted view of the full history plus a ground-truth state block.

The full history is kept (and persisted, minus harness notices). Each step the model gets:
  * the system prompt and a STATE block regenerated from disk, the Fact Ledger and the last build/verify, so its
    memory of the project can never be lost to compaction;
  * the last VERBATIM_TURNS assistant turns exactly, older tool results as one-line digests and older large
    tool arguments (file bodies) elided — tool-call/result pairs always stay intact;
  * user messages with stale harness context removed, exact duplicates merged.
Nothing here creates facts or decides outcomes; it only summarises real tool results.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

CONTEXT_BUDGET_CHARS = 120_000
VERBATIM_TURNS = 10
PERSIST_MAX_MESSAGES = 800
_BIG_ARGS = ("content", "old", "new", "markdown")
_POWER_NET = re.compile(r"^(\+?\d+(\.\d+)?V\d*|\d+V\d+|V(CC|DD|BAT|BUS|IN|SYS|REG)\w*|AGND|DGND|PGND|GND\w*|VSS\w*|3V3|5V|1V1|1V8)$", re.I)
_CONTEXT_MARKERS = re.compile(r"\n\n\[(?:Requirement Engine|Board imported)")


def visible_user_text(content: str) -> str:
    """The user's own words, without the context blocks the runner used to append for the model."""
    return _CONTEXT_MARKERS.split(content or "", maxsplit=1)[0]


def strip_private(m: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in m.items() if not k.startswith("_")}


def is_notice(m: Dict[str, Any]) -> bool:
    return m.get("role") == "user" and (m.get("_ephemeral") or str(m.get("content") or "").startswith("[System Notice]"))


# ------------------------------------------------------------------ digests
def digest(name: str, args: Dict[str, Any], result: Any) -> str:
    """One line that preserves what a tool call established (used once the full result leaves the window)."""
    r = result if isinstance(result, dict) else {}
    err = r.get("error")
    if err and not (name == "build" and r.get("diagnostics")):  # a failed build's first diagnostic says more than its error
        return f"{name} ERROR: {str(err)[:160]}"
    if name == "record_fact" or name == "record_user_fact":
        f = r.get("fact") or {}
        return (f"{name} -> {f.get('id')} {f.get('subject')}.{f.get('property')}={f.get('value')}" if r.get("ok")
                else f"{name} REJECTED: {str(r.get('error'))[:120]}")
    if name == "record_board_pins":
        rec = ", ".join(str(x) for x in r.get("recorded") or [])
        extra = [f"{len(r[k])} {k.replace('_', ' ')}" for k in ("tagged", "mismatched", "not_in_config", "rejected") if r.get(k)]
        return f"record_board_pins -> {rec or 'none'}" + (f"; {', '.join(extra)}" if extra else "")
    if name == "record_sequence":
        s = r.get("sequence") or {}
        return (f"record_sequence -> {s.get('id')} {s.get('subject')}.{s.get('name')} in {s.get('entry')}() ({len(s.get('steps') or [])} steps)"
                if r.get("ok") else f"record_sequence REJECTED: {str(r.get('error'))[:140]}")
    if name in ("record_facts", "record_registers", "record_layouts") or (name == "record_formula" and "recorded" in r):
        rec =[str(x).split(":")[0] for x in (r.get("recorded") or [])] + [str(x).split(":")[0] for x in (r.get("bursts") or [])]
        rej = r.get("rejected") or []
        return (f"{name} -> {', '.join(rec[:14]) or 'none'}" + (f" (+{len(rec) - 14})" if len(rec) > 14 else "")
                + (f"; {len(rej)} rejected, e.g. {rej[0].get('item') or rej[0].get('register') or rej[0].get('layout') or rej[0].get('burst')}: "
                   f"{str(rej[0].get('error'))[:90]}" if rej else ""))
    if name == "record_formula":
        return f"record_formula -> {str(r.get('formula'))[:150]}" if r.get("ok") else f"record_formula REJECTED: {str(r.get('error'))[:140]}"
    if name == "coding_brief":
        reqs = (r.get("datasheet_requirements") or {}).get("counts") or {}
        return (f"coding_brief: {len(r.get('sequences') or [])} sequence(s), {len(r.get('rules_after_every_write') or [])} rule(s), "
                f"{len(r.get('conversions') or [])} conversion(s), {reqs.get('open', 0)} open requirement(s)")
    if name == "resolve_requirements":
        return (f"resolve_requirements -> {', '.join(r.get('resolved') or []) or 'none'}"
                + (f"; {len(r['rejected'])} rejected: {str(r['rejected'][0].get('error'))[:80]}" if r.get("rejected") else ""))
    if name == "record_constraint":
        return (f"record_constraint -> {str(r.get('constraint'))[:150]}" if r.get("ok")
                else f"record_constraint REJECTED: {str(r.get('error'))[:140]}")
    if name == "read_file":
        return f"read_file {args.get('path')} lines {r.get('start')}-{r.get('end')}/{r.get('total_lines')}"
    if name in ("write_file", "replace_in_file"):
        lint = r.get("lint") or []
        return f"{name} {args.get('path')} ({r.get('bytes')} B)" + (f"; lint: {lint[0]}" if lint else "")
    if name == "build":
        if r.get("ok"):
            return f"build OK v{r.get('version')} flash {r.get('flash_used')}/{r.get('flash_total')}"
        diags = r.get("diagnostics") or []
        if not diags:
            return f"build FAILED: {str(r.get('error') or 'see log')[:160]}"
        d = diags[0]
        more = max(0, len(diags) - 1)
        return f"build FAILED: {d.get('file')}:{d.get('line')} {str(d.get('message'))[:120]}" + (f" (+{more} more)" if more else "")
    if name == "verify":
        f0 = (r.get("findings") or [{}])[0]
        return f"verify {r.get('summary')}" + (f"; first: {f0.get('code')} {f0.get('file')}:{f0.get('line')}" if f0 else "")
    if name == "query_board":
        lines = r.get("lines") or []
        return f"query_board {args.get('query')!r}: {len(lines)} lines" + (f"; e.g. {lines[0][:120]}" if lines else "")
    if name in ("init_betaflight", "init_project"):
        board = r.get("board") if isinstance(r.get("board"), dict) else {}
        return f"{name} -> {r.get('config_path') or board.get('id') or 'ok'}"
    if name == "web_search":
        return f"web_search {args.get('query')!r}: " + ", ".join(x.get("source_id", "") for x in (r.get("results") or []))
    if name in ("read_framework_file",):
        return f"read_framework_file {args.get('path')} lines {r.get('start')}-{r.get('end')}/{r.get('total_lines')}"
    if name == "search_framework":
        return f"search_framework {args.get('pattern')!r}: {len(r.get('matches') or [])} matches"
    s = json.dumps(r, default=str)
    return f"{name}: {s[:160]}"


def _elide_calls(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for tc in tool_calls:
        fn = dict(tc.get("function") or {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (ValueError, TypeError):
            args = None
        if isinstance(args, dict):
            for k in _BIG_ARGS:
                if isinstance(args.get(k), str) and len(args[k]) > 300:
                    args[k] = f"<{len(args[k])} chars elided>"
            fn["arguments"] = json.dumps(args)
        out.append({**tc, "function": fn})
    return out


def _size(m: Dict[str, Any]) -> int:
    return len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or []))


# ------------------------------------------------------------------ context building
def build_context(history: List[Dict[str, Any]], budget: int = CONTEXT_BUDGET_CHARS,
                  verbatim_turns: int = VERBATIM_TURNS) -> List[Dict[str, Any]]:
    """Budgeted, well-formed message list for the LLM (system prompt and state block are added by the caller)."""
    # group into units: a user message, or an assistant message with the tool results that answer it
    units: List[List[Dict[str, Any]]] = []
    for m in history:
        if m.get("role") == "tool" and units and units[-1][0].get("role") == "assistant":
            units[-1].append(m)
        elif m.get("role") == "tool":
            continue  # orphan tool result (can happen in old saved histories) — providers reject it
        else:
            units.append([m])

    assistant_idx = [i for i, u in enumerate(units) if u[0].get("role") == "assistant"]
    verbatim_from = assistant_idx[-verbatim_turns] if len(assistant_idx) >= verbatim_turns else 0
    last_user = max((i for i, u in enumerate(units) if u[0].get("role") == "user" and not is_notice(u[0])), default=-1)

    out_units: List[List[Dict[str, Any]]] = []
    seen_user: Dict[str, int] = {}
    for i, u in enumerate(units):
        head = u[0]
        if head.get("role") == "user":
            if is_notice(head) and i < verbatim_from:
                continue  # stale harness notice
            text = str(head.get("content") or "")
            if i != last_user and not is_notice(head):
                text = visible_user_text(text)
            key = text.strip()
            if key in seen_user:  # exact duplicate request: keep only the latest copy
                out_units[seen_user[key]] = []
            seen_user[key] = len(out_units)
            out_units.append([{"role": "user", "content": text}])
            continue
        if i >= verbatim_from:
            out_units.append([strip_private(m) for m in u])
            continue
        a = strip_private(head)
        if a.get("tool_calls"):
            a["tool_calls"] = _elide_calls(a["tool_calls"])
        group = [a]
        for t in u[1:]:
            group.append({"role": "tool", "tool_call_id": t.get("tool_call_id"),
                          "content": t.get("_digest") or str(t.get("content") or "")[:200]})
        out_units.append(group)

    out_units = [u for u in out_units if u]
    total = sum(_size(m) for u in out_units for m in u)
    # still too big: drop the oldest assistant groups (never the verbatim window, never user turns)
    keep_from = next((k for k, u in enumerate(out_units) if u and u[0].get("role") == "assistant" and
                      sum(1 for x in out_units[k:] if x and x[0].get("role") == "assistant") <= verbatim_turns), len(out_units))
    k = 0
    while total > budget and k < keep_from:
        u = out_units[k]
        if u and u[0].get("role") == "assistant":
            total -= sum(_size(m) for m in u)
            out_units[k] = []
        k += 1
    # the verbatim window alone is over budget (many huge results): digest it from the oldest, keeping the last 3 turns
    asst = [k for k, u in enumerate(out_units) if u and u[0].get("role") == "assistant"]
    for k in asst[:-3]:
        if total <= budget:
            break
        before = sum(_size(m) for m in out_units[k])
        digests = {m.get("tool_call_id"): m.get("_digest") for m in history if m.get("role") == "tool"}
        a = dict(out_units[k][0])
        if a.get("tool_calls"):
            a["tool_calls"] = _elide_calls(a["tool_calls"])
        out_units[k] = [a] + [{"role": "tool", "tool_call_id": t.get("tool_call_id"),
                               "content": digests.get(t.get("tool_call_id")) or str(t.get("content") or "")[:200]} for t in out_units[k][1:]]
        total -= before - sum(_size(m) for m in out_units[k])
    return [m for u in out_units for m in u]


def for_persistence(history: List[Dict[str, Any]], max_messages: int = PERSIST_MAX_MESSAGES) -> List[Dict[str, Any]]:
    """Full history minus harness notices, capped at a clean boundary (never starting with a tool result)."""
    kept = [m for m in history if not is_notice(m)]
    if len(kept) > max_messages:
        kept = kept[-max_messages:]
        while kept and kept[0].get("role") == "tool":
            kept.pop(0)
    return kept


def latest_request(history: List[Dict[str, Any]], min_chars: int = 40) -> str:
    """The most recent substantive user request ('continue' / 'ok' are not requests)."""
    for m in reversed(history):
        if m.get("role") == "user" and not is_notice(m):
            t = visible_user_text(str(m.get("content") or "")).strip()
            if len(t) >= min_chars:
                return t
    return ""


# ------------------------------------------------------------------ ground-truth state block
def uncovered_pins(board: Any, facts: List[Any], limit: int = 40) -> Tuple[List[str], Optional[str]]:
    """Wired MCU GPIO pins (signal nets) that no recorded fact's quote names yet."""
    from embedforge.eda.board import _AUTO_NET
    from embedforge.truth.verifier import quote_pins
    if board is None:
        return [], None
    mcus = board.mcus()
    if not mcus:
        return [], None
    mcu = mcus[0]
    nets = board.nets()
    covered = set()
    for f in facts:
        covered |= quote_pins(f.quote)
    out = []
    for pin in sorted(mcu.pins.values(), key=lambda p: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.number)]):
        m = re.match(r"GPIO(\d+)", pin.name or "", re.I)
        if not m or not pin.net or _POWER_NET.match(pin.net) or _AUTO_NET.match(pin.net):
            continue
        if len(nets.get(pin.net, [])) < 2:
            continue
        g = f"GPIO{int(m.group(1))}"
        if g not in covered:
            out.append(f"{g} ({pin.net})")
    return out[:limit], mcu.ref


_PIN_DEFINE = re.compile(r"^\s*#\s*define\s+(\w+_PIN)\s+PA(\d{1,2})\b(.*)$")


def config_pin_status(text: str, ledger: Any, board: Any, limit: int = 24) -> List[str]:
    """'DEFINE=PAn -> GPIOn net X (problem)' for each active pin define without a valid fact. The net is shown so a wrong
    assignment is visible; it is not a claim that the pin is right."""
    from embedforge.truth.verifier import pin_key, quote_pins
    out = []
    for raw in text.split("\n"):
        m = _PIN_DEFINE.match(raw)
        if not m:
            continue
        name, n, rest = m.group(1), int(m.group(2)), m.group(3)
        gpio = f"GPIO{n}"
        tags = re.findall(r"FACT-\d{3,}", rest)
        facts = [ledger.get(t) for t in tags if ledger.get(t)]
        if not tags:
            problem = "no fact tag" if not re.search(r"FACT", rest) else "invalid tag"
        elif not facts:
            problem = f"{tags[0]} is not in the ledger"
        elif not any(pin_key(f.value, True) == gpio or (pin_key(f.value, True) is None and quote_pins(f.quote) == {gpio}) for f in facts):
            problem = f"{facts[0].id} is {facts[0].value}"
        elif any(re.fullmatch(r"[A-Z][A-Z0-9_]*_PIN", f.property) and f.property != name for f in facts):
            problem = f"{facts[0].id} was recorded for {facts[0].property}"
        else:
            continue
        net = ""
        if board is not None:
            lines = board.mcu_pin_lines(n)
            nm = re.search(r"-> net (\S+)", lines[0]) if lines else None
            net = (f" net {nm.group(1)}" if nm else " NOT CONNECTED (remove)" if lines else " not on the MCU")
        out.append(f"{name}=PA{n} -> {gpio}{net} ({problem})")
    return out[:limit]


def state_block(project: Any, ledger: Any, belt: Any, history: List[Dict[str, Any]]) -> Tuple[str, str]:
    """(text, next_step). Everything comes from disk, the ledger and real tool results."""
    from embedforge import eda
    meta = project.meta()
    kind = meta.get("kind") or ("platformio" if (project.root / "platformio.ini").exists() else "")
    board = eda.load_board(project.root)
    facts = list(ledger.facts.values())
    current = belt.source_hash()
    lb = meta.get("last_build") or {}
    lv = meta.get("last_verify") or {}
    lines = ["PROJECT STATE (generated by EmbedForge from files, the Fact Ledger and real tool results; trust it over memory)"]

    req = latest_request(history)
    if req:
        lines.append(f"- Task (latest request; it replaces older requests): {req[:600]}")
    if board is not None:
        s = board.summary()
        mcu = ", ".join(f"{m['ref']} = {m['mpn'] or m['value']} ({m['pin_count']} pins)" for m in s["mcus"]) or "not identified"
        lines.append(f"- Imported schematic: {s['parts']} parts, {s['nets']} nets; MCU {mcu}. Look up wiring with query_board.")
        ics = [f"{p['ref']} {p['mpn'] or p['value']}"[:40] for p in s.get("ics", []) if not p.get("is_mcu")]
        if ics:
            lines.append("- Parts (all ICs, transistors and connectors on the board — do not probe other designators): " + "; ".join(ics))
    if kind == "betaflight":
        cfg = f"betaflight/configs/CUST/{meta.get('board')}/config.h"
        exists = (project.root / cfg).exists()
        n = len((project.root / cfg).read_text(encoding="utf-8", errors="ignore").splitlines()) if exists else 0
        lines.append(f"- Project: Betaflight {meta.get('framework', '')}, target {meta.get('bf_target')}, board {meta.get('board')} "
                     f"(already initialised — do not call init_betaflight again). Config: {cfg} "
                     + (f"({n} lines)" if exists else "(not written yet)"))
    elif kind == "platformio":
        lines.append(f"- Project: PlatformIO board {meta.get('board')} ({meta.get('mcu')}), framework {meta.get('framework')}")
    else:
        lines.append("- Project: not initialised yet")

    if facts:
        shown = facts[-40:]
        lines.append(f"- Facts recorded ({len(facts)}): " + "; ".join(f"{f.id} {f.subject}.{f.property}={f.value}" for f in shown))
    else:
        lines.append("- Facts recorded: none")
    unc, mcu_ref = uncovered_pins(board, facts)
    if unc:
        lines.append(f"- Wired {mcu_ref} pins with no fact yet (record the ones the firmware uses): " + ", ".join(unc))
    pin_problems: List[str] = []
    if kind == "betaflight":
        cfgp = project.root / "betaflight" / "configs" / "CUST" / str(meta.get("board")) / "config.h"
        if cfgp.exists():
            pin_problems = config_pin_status(cfgp.read_text(encoding="utf-8", errors="ignore"), ledger, board)
            if pin_problems:
                lines.append(f"- config.h pins without a valid fact ({len(pin_problems)}): " + "; ".join(pin_problems))

    changed = lb.get("hash") != current
    if lb:
        state = "OK" if lb.get("ok") else "FAILED"
        lines.append(f"- Last build: {state}" + (" (sources changed since)" if changed else " (current sources)")
                     + "".join(f"\n    {d}" for d in (lb.get("diagnostics") or [])[:3]))
    else:
        lines.append("- Last build: never built")
    from embedforge.truth.sequences import SequenceBook
    seqs = list(SequenceBook(project.root).items.values())
    if seqs:
        lines.append(f"- Datasheet sequences recorded ({len(seqs)}): "
                     + "; ".join(f"{s.id} {s.subject}.{s.name} in {s.entry}() ({len(s.steps)} steps)" for s in seqs[:8]))
    from embedforge.truth.registers import RegisterBook
    book = RegisterBook(project.root)
    if not book.empty():
        regs = sorted(book.registers.values(), key=lambda r: r.addr)
        lines.append(f"- Register map: {len(regs)} registers (" + ", ".join(f"{r.name} 0x{r.addr:02X}" for r in regs[:16])
                     + f"), {len(book.layouts)} layouts, {len(book.bursts)} burst rules, {len(book.formulas)} formulas"
                     + (" (" + ", ".join(f.name for f in list(book.formulas.values())[:12]) + ")" if book.formulas else ""))
    unread = []
    for sid in (belt.state.get("unread") or {}):   # datasheet sections the code depends on (known after a verify)
        unread += [f"{sid} {s.num} {s.title[:40]}" for s, _ in belt._required_for(sid) if belt.reads.seen(sid, s.start, s.end) < 0.5]
    if unread:
        lines.append(f"- Datasheet sections your code depends on, not read yet ({len(unread)}): " + "; ".join(unread[:8])
                     + " — read_section them (several per call) and record the rules they state.")
    from embedforge.truth.knowledge import Requirements
    rq = Requirements(project.root)
    if rq.items:
        c = rq.counts()
        lines.append(f"- Datasheet requirements: {len(rq.items)} ({c['covered']} covered by records, {c['implemented']} implemented, "
                     f"{c['not_applicable']} not applicable, {c['open']} open). coding_brief shows them with everything the code must do.")
    inputs_stale = bool(lv) and lv.get("hash") == current and lv.get("inputs") not in (None, belt.verify_inputs_hash())
    if lv:
        lines.append(f"- Last verify: {lv.get('summary')}"
                     + (" (sources changed since)" if lv.get("hash") != current else " (facts/sequences changed since: verify again)" if inputs_stale else "")
                     + "".join(f"\n    {d}" for d in (lv.get("findings") or [])[:3]))
    else:
        lines.append("- Last verify: never run")
    if inputs_stale:
        lv = {**lv, "hash": None}

    nxt = next_step(project, kind, meta, facts, unc, lb, lv, current, req, pin_problems if board is not None else [])
    lines.append(f"- NEXT STEP: {nxt}")
    return "\n".join(lines), nxt


_DRIVER_TASK = re.compile(r"\b(driver|sensor|datasheet|register|barometer|imu|gyro|accelerometer|magnetometer|adc|dac|eeprom)\b", re.I)


def driver_plan(project: Any, facts: List[Any], request: str) -> str:
    """Before writing a device driver: the facts its constants need and the register map it writes, so the first code
    is written from the datasheet. The rest (layouts, formulas, the init sequence) can follow the first build: verify
    fails on anything the code uses that is not on record, and its findings say what to record next."""
    from embedforge.truth.registers import RegisterBook
    ds_facts = [f for f in facts if str(getattr(f, "source_url", "")).startswith("http")]
    if not (_DRIVER_TASK.search(request or "") or ds_facts):
        return ""
    book = RegisterBook(project.root)
    if not ds_facts:
        return ("DRIVER — record_facts from the datasheet (read_source first): bus address for the strap on the schematic, ID "
                "register + value, command values, start-up/conversion times with units; then record_registers; then "
                "write_file the driver and build")
    if not book.registers:
        return ("DRIVER — record_registers: the registers the driver will write, poll or test, with fields (bits, named "
                "values) and bursts for data that must be read in one transaction; then write_file the driver and build")
    return ("write_file the driver from the recorded facts and registers (values composed from their fields), then build "
            "and verify: verify names what else to record (layouts for multi-byte values, formulas for conversions, the "
            "init sequence). Layouts, formulas and sequences recorded now are checked from the first verify")


def next_step(project: Any, kind: str, meta: Dict[str, Any], facts: List[Any], unc: List[str],
              lb: Dict[str, Any], lv: Dict[str, Any], current: str, request: str = "",
              pin_problems: Optional[List[str]] = None) -> str:
    if not kind and re.search(r"platform\s*io|arduino|pico[- ]?sdk|esp[- ]?idf|stm32cube", request, re.I) and not re.search(r"beta\s*flight", request, re.I):
        board = re.search(r"\bboard\s+([a-z0-9_]{3,40})\b", request, re.I)
        return ("resolve_target, then init_project" + (f"(board='{board.group(1)}', framework=<the framework the user named>)" if board else "")
                + " — the user asked for a PlatformIO/SDK project (not Betaflight)")
    if not kind and re.search(r"\bbeta\s*flight\b", request, re.I):
        return ("init_betaflight(board_name=<short board name>, target=<in-tree target, e.g. RP2350B for an RP2350B/RP2354B>, "
                "flash_mb=<2 for RP2354A/B>) — Betaflight has its own build; do not use resolve_target/init_project")
    if kind == "betaflight":
        cfg = project.root / "betaflight" / "configs" / "CUST" / str(meta.get("board")) / "config.h"
        if not cfg.exists():
            if unc:
                return ("record_board_pins for the pins the firmware will use (one call, each with the exact query_board "
                        "line), then write_file the config.h")
            return "write_file the config.h (copy define names from a reference config; pins only from recorded facts)"
        if pin_problems and lb.get("hash") != current:  # once these sources are built, move on (verify reports what's left)
            return ("record_board_pins with {define, line} for the defines under 'config.h pins without a valid fact' (lines "
                    "from query_board('U1')); it tags config.h. If a net does not fit its define, fix or remove the define; "
                    "remove defines on not-connected pins. Then build")
    elif kind != "platformio":
        return "set up the target: resolve_target + init_project (or init_betaflight for a Betaflight flight controller)"
    else:
        from embedforge.truth.verifier import collect_sources
        if not any(p.suffix in (".c", ".cpp", ".ino") for p in collect_sources(project.root)):
            plan = driver_plan(project, facts, request)
            if plan:
                return plan
            return ("record the facts the firmware needs (record_user_fact / record_fact), then write_file the sources "
                    "(src/main.c and modules), then build")
    built_now = lb.get("hash") == current
    if not built_now:
        return "build (sources changed since the last build)"
    if not lb.get("ok"):
        d = (lb.get("diagnostics") or ["see the last build result"])[0]
        return f"fix the FIRST build error ({d}) with replace_in_file, then build again"
    verified_now = lv.get("hash") == current
    if not verified_now:
        return "verify"
    if not lv.get("passed"):
        found = lv.get("findings") or []
        codes = {str(x).split(" ", 1)[0] for x in found}
        # what the code uses but nothing records yet: record it (no rebuild needed), every kind in one go
        record = {"REG_MAP_MISSING": "record_registers (+ record_layouts, record_formula)", "REG_FIELDS_MISSING": "record_registers with fields",
                  "REG_UNMAPPED": "record_registers / record_layouts for the unmapped addresses",
                  "FORMULA_MISSING": "record_formula for the conversions", "FLOW_UNSPECIFIED": "record_sequence for the init order"}
        todo = [t for c, t in record.items() if c in codes]
        if todo:
            return (f"record what verify found unrecorded: {'; '.join(todo)} (each finding's suggestion says what to quote), "
                    "then verify again — new records need no rebuild")
        f = (found or ["see the last verify result"])[0]
        return f"fix the verify findings (first: {f}), then build and verify again"
    return "finish(status='success', summary=...) — the current sources built and verified"
