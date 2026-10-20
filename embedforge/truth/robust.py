"""Robustness rules a field failure teaches, checked on the code (comments stripped, one string per line):

  UNBOUNDED_WAIT   a loop that waits on hardware (a status bit, a bus call, Serial, a pin) with no timeout or retry
                   limit: when the condition never comes true (no USB host, sensor unplugged, bus stuck) the firmware
                   hangs there forever.
  RESULT_IGNORED   a call that reports failure (a bus transfer, or one of the project's own functions that returns an
                   error) whose result is thrown away (MISRA C:2012 Rule 17.7, CERT C ERR33-C): a failed transfer goes
                   unnoticed and stale bytes are used as data. A deliberate discard is written (void)f(...).
"""

import re
from typing import Dict, List, Optional, Set, Tuple

# bus transfers that return a status or a byte count (SPI blocking transfers on the pico-sdk cannot fail and return
# the length; ESP-IDF's i2c_master_read/write only queue a command whose result comes from i2c_master_cmd_begin)
READ_CALLS = ("requestFrom", "i2c_read_blocking", "i2c_read_timeout_us", "i2c_read_blocking_until", "HAL_I2C_Mem_Read",
              "HAL_I2C_Master_Receive", "i2c_master_read_from_device", "i2c_master_write_read_device",
              "i2c_master_transmit_receive", "i2c_master_receive", "i2c_burst_read_dt", "i2c_read_dt", "i2c_write_read_dt",
              "HAL_SPI_Receive", "HAL_SPI_TransmitReceive")
WRITE_CALLS = ("endTransmission", "i2c_write_blocking", "i2c_write_timeout_us", "i2c_write_blocking_until", "HAL_I2C_Mem_Write",
               "HAL_I2C_Master_Transmit", "i2c_master_write_to_device", "i2c_master_transmit", "i2c_master_cmd_begin",
               "i2c_reg_write_byte_dt", "i2c_burst_write_dt", "i2c_write_dt", "HAL_SPI_Transmit")

_TIME_BOUND = re.compile(r"\b(millis|micros|time_us_\d+|get_absolute_time|absolute_time_diff_us|time_reached|HAL_GetTick|"
                         r"xTaskGetTickCount|k_uptime_get\w*|esp_timer_get_time|timeout\w*|deadline|elapsed|expired?)\b", re.I)
_COUNT_WORDS = re.compile(r"\b\w*(tries|retries|retry|attempts?|count|counter|iterations?|loops?|spins?|guard)\w*\b", re.I)
_HW_WORDS = re.compile(r"\bSerial\d?\b|\b(digitalRead|gpio_get|gpio_get_all)\s*\(|\w*(read|Read|available|status|Status|ready|"
                       r"Ready|busy|Busy|transfer|Transfer|receive|Receive)\w*\s*\(|\b\w*(STATUS|status|DRDY|drdy|RDY|rdy|BUSY|"
                       r"busy|ready|READY|done|DONE|flag|FLAG)\w*\b")


def _block(code: str, i: int) -> Tuple[str, int]:
    """The balanced (...) or {...} starting at code[i], and the index after it."""
    open_, close = code[i], {"(": ")", "{": "}"}[code[i]]
    depth = 0
    for j in range(i, len(code)):
        if code[j] == open_:
            depth += 1
        elif code[j] == close:
            depth -= 1
            if depth == 0:
                return code[i + 1:j], j + 1
    return code[i + 1:], len(code)


def _body(code: str, i: int) -> Tuple[str, int]:
    """The statement or block after a loop header ending at code[i]."""
    while i < len(code) and code[i] in " \t\n":
        i += 1
    if i < len(code) and code[i] == "{":
        return _block(code, i)
    j = code.find(";", i)
    return (code[i:j], j + 1) if j >= 0 else (code[i:], len(code))


def _line_of(starts: List[int], pos: int) -> int:
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= pos:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def _bounded(cond: str, body: str) -> bool:
    text = cond + " " + body
    if _TIME_BOUND.search(text):
        return True
    if _COUNT_WORDS.search(text) and re.search(r"(<|<=|>|>=|==|!=)", text):
        return True
    # a counter stepped and compared: while (!ready() && n++ < 100), if (--n == 0) break;
    return bool(re.search(r"(\+\+|--)\s*\w+\s*(<|<=|>|>=|==|!=)|\w+\s*(\+\+|--)\s*(<|<=|>|>=|==|!=)|(<|<=|>|>=|==|!=)\s*\w+\s*(\+\+|--)", text))


def unbounded_waits(lines: List[str]) -> List[Tuple[int, str]]:
    """(line, message) for each loop waiting on hardware without a bound."""
    code = "\n".join(lines)
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln) + 1)
    out = []
    for m in re.finditer(r"\b(while|for)\s*\(", code):
        if m.group(1) == "while" and re.search(r"\}\s*$", code[:m.start()]) and \
                re.search(r"\bdo\s*\{[^{}]*(\{[^{}]*\}[^{}]*)*\}\s*$", code[max(0, m.start() - 4000):m.start()]):
            continue   # the tail of a do { } while (...): handled with its do
        cond, end = _block(code, m.end() - 1)
        if m.group(1) == "for":
            parts = cond.split(";")
            if len(parts) != 3 or parts[1].strip():
                continue                   # a counted for loop
            cond = ""
        body, _ = _body(code, end)
        if cond.strip() and cond.strip() not in ("1", "true", "TRUE"):
            waits = _HW_WORDS.search(cond)
            if waits and re.fullmatch(r"\s*[\w.\->]*available\s*\(\s*\)\s*(>\s*0\s*)?", cond) and re.search(r"\bread\s*\(", body):
                continue                   # while (Wire.available()) buf[i++] = Wire.read(): drains what arrived
        else:                              # while (1) / for (;;): a wait only if it breaks out on hardware state
            brk = re.search(r"\bif\s*\(([^;{]*)\)\s*(\{[^}]*)?\b(break|return)\b", body)
            waits = brk and _HW_WORDS.search(brk.group(1))
        if not waits or _bounded(cond, body):
            continue
        out.append((_line_of(starts, m.start()), _wait_message(cond)))
    for m in re.finditer(r"\bdo\s*\{", code):   # do { s = read_status(); } while (!(s & DRDY));
        body, end = _block(code, m.end() - 1)
        tail = re.match(r"\s*while\s*\(", code[end:])
        if not tail:
            continue
        cond, _ = _block(code, end + tail.end() - 1)
        hw = _HW_WORDS.search(cond) or (_HW_WORDS.search(body) and re.search(r"\b(" + "|".join(
            re.findall(r"([A-Za-z_]\w*)\s*=[^=]", body)) + r")\b", cond) if re.search(r"[A-Za-z_]\w*\s*=[^=]", body) else None)
        if hw and not _bounded(cond, body):
            out.append((_line_of(starts, m.start()), _wait_message(cond)))
    return sorted(out)


def _wait_message(cond: str) -> str:
    what = re.sub(r"\s+", " ", cond.strip() or "an endless loop")[:80]
    return (f"This loop waits on hardware ({what}) with no timeout or retry limit: if that never happens (no USB host "
            "attached, sensor missing or unpowered, bus stuck) the firmware hangs here forever.")


def _results(code: str, starts: List[int], names: Tuple[str, ...],
             owners: Optional[Dict[str, Tuple[str, Set[str]]]] = None) -> List[Tuple[int, str, str]]:
    """(line, call, how) for calls of `names` whose result is thrown away or put in a variable never read. `owners`:
    the project's own functions -> (class or '', that class's objects): `_wire.begin()` is not BMP388::begin()."""
    out = []
    rx = re.compile(r"(?:\b([A-Za-z_]\w*)\s*(?:\.|->)\s*)?\b(" + "|".join(map(re.escape, names)) + r")\s*\(")
    for m in rx.finditer(code):
        if m.start() > 0 and code[m.start() - 1] in ".>" and not m.group(1):
            continue
        cls, objs = (owners or {}).get(m.group(2), (None, set()))
        recv = m.group(1) or ""
        if cls is not None:
            if recv and recv != "this" and recv not in objs:
                continue                   # a same-named method of another object (Serial.begin, Wire.read)
            if not recv and cls and not re.search(rf"\b{re.escape(cls)}::\w+\s*\([^;{{]*\)\s*(const\s*)?\{{", code[:m.start()]):
                continue                   # a bare call outside that class's methods
        line = _line_of(starts, m.start())
        args, after = _block(code, m.end() - 1)
        before = code[starts[line - 1]:m.start()]
        if re.search(r"\(\s*void\s*\)\s*$", before):
            continue                       # (void)f(...): discarded on purpose
        # the rest of the enclosing block
        depth, j = 0, after
        while j < len(code):
            if code[j] == "{":
                depth += 1
            elif code[j] == "}":
                if depth == 0:
                    break
                depth -= 1
            j += 1
        rest = code[after:j]
        assigned = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*=\s*(?:\([^()]*\)\s*)?$", before)
        if assigned and not re.search(r"(==|!=|<=|>=|\breturn\b|\bif\b|\?)", before):
            if not re.search(rf"\b{re.escape(assigned.group(1))}\b", rest):
                out.append((line, m.group(2), f"puts its result in {assigned.group(1)}, which is never read"))
        elif re.fullmatch(r"\s*", before) and re.match(r"\s*;", code[after:]):
            if m.group(2) == "requestFrom" and re.search(r"\bavailable\s*\(", rest):
                continue                   # Wire.requestFrom(...); while (Wire.available()) ...: checked by the count
            out.append((line, m.group(2), "result is ignored"))
    return out


_FUNC_DEF = re.compile(r"(?m)^[ \t]*(?:static\s+|inline\s+)*(bool|int|int8_t|int16_t|int32_t|esp_err_t|HAL_StatusTypeDef|"
                       r"status_t|err_t)\s+(?:([A-Za-z_]\w*)::)?([A-Za-z_]\w*)\s*\([^;{)]*\)\s*(?:const\s*)?\{")


def _status_functions(texts: Dict[str, List[str]]) -> Dict[str, Tuple[str, str]]:
    """The project's own functions that report failure -> (how, class or '' for a free function): bool functions that
    can return false, int/status functions that can return a negative number or an error constant, and functions that
    return such a status as is (return i2c_burst_read_dt(&dev, reg, buf, len);)."""
    code = "\n".join("\n".join(lines) for lines in texts.values())
    funcs = []
    for m in _FUNC_DEF.finditer(code):
        body, _ = _block(code, m.end() - 1)
        funcs.append((m.group(1), m.group(2) or "", m.group(3), body))
    out: Dict[str, Tuple[str, str]] = {}
    for kind, cls, name, body in funcs:
        if kind == "bool" and re.search(r"\breturn\s+false\b|\breturn\s*\([^;]*(==|!=|<|>)", body):
            out[name] = ("returns false on failure", cls)
        elif kind != "bool" and re.search(r"\breturn\s+(-\s*\d+|[A-Z_]*(ERR|FAIL|ERROR)\w*)|\breturn\b[^;]*:\s*-\s*\d+\s*;", body):
            out[name] = ("returns an error code on failure", cls)
    changed = True
    while changed:
        changed = False
        known = READ_CALLS + WRITE_CALLS + tuple(out)
        for kind, cls, name, body in funcs:
            if name not in out and re.search(r"\breturn\s+(?:[A-Za-z_]\w*\s*(?:\.|->)\s*)*(" + "|".join(map(re.escape, known))
                                             + r")\s*\(", body):
                out[name] = ("returns the status of the transfer", cls)
                changed = True
    return out


def failures_reported_as_success(texts: Dict[str, List[str]]) -> List[Tuple[str, int, str]]:
    """(file, line, message) for a branch taken because a call failed that returns success (true from a bool function,
    0 from one that returns negative codes): the caller then carries on as if the device were set up."""
    own = _status_functions(texts)
    names = READ_CALLS + WRITE_CALLS + tuple(own)
    call = r"(?:[A-Za-z_]\w*\s*(?:\.|->)\s*)?\b(" + "|".join(map(re.escape, names)) + r")\s*\("
    out = []
    for rel, lines in texts.items():
        code = "\n".join(lines)
        starts = [0]
        for ln in lines:
            starts.append(starts[-1] + len(ln) + 1)
        for f in _FUNC_DEF.finditer(code):
            kind = f.group(1)
            fbody, _ = _block(code, f.end() - 1)
            success = "true" if kind == "bool" else "0" if re.search(r"\breturn\s+-\s*\d", fbody) else None
            if success is None:
                continue
            base = f.end()
            for m in re.finditer(r"\bif\s*\(", fbody):
                cond, end = _block(fbody, m.end() - 1)
                failed = re.match(r"\s*!\s*\(?\s*" + call, cond) or (re.search(call, cond) and re.search(
                    r"\)\s*(!=\s*(0|true|\w*OK\b|\w+)|<\s*0|==\s*(false|-\s*\d))", cond))
                # the device is not the part (its ID read does not match), or the object was never set up: the TMP117
                # driver's mutation test turned both branches' 'return false' into 'return true' unnoticed
                id_bad = re.fullmatch(r"\s*\(?\s*\w*(?:chip_?id|device_?id|dev_?id|part_?id|who_?am_?i|\bid)\w*\s*\)?\s*!=\s*\w+\s*",
                                      cond, re.I)
                not_ready = re.fullmatch(r"\s*!\s*(?:this\s*->\s*)?(\w*(?:initiali[sz]ed|inited|ready|begun|started)\w*)\s*", cond, re.I)
                if not (failed or id_bad or not_ready):
                    continue
                branch, _ = _body(fbody, end)
                if re.search(rf"\breturn\s+{success}\s*;", branch) or re.fullmatch(rf"\s*return\s+{success}\s*", branch):
                    line = _line_of(starts, base + m.start())
                    why = (f"When {failed.group(1)}() fails" if failed else
                           f"When the ID read does not match ({cond.strip()})" if id_bad else
                           f"When {not_ready.group(1)} says the device was never set up")
                    out.append((rel, line, f"{why}, this branch returns {success} (success): the caller carries on as if it "
                                           "had worked."))
    return out


def _objects(code: str, cls: str) -> Set[str]:
    """Names declared as objects (or references / pointers) of a class: 'BMP388 bmp388;' -> {'bmp388'}."""
    return set(re.findall(rf"\b{re.escape(cls)}\s*[&*]?\s+([A-Za-z_]\w*)\s*(?=[;=(\[{{,)])", code))


def ignored_results(texts: Dict[str, List[str]]) -> List[Tuple[str, int, str, str]]:
    """(file, line, call, message) for results that report failure and are thrown away."""
    own = _status_functions(texts)
    every = "\n".join("\n".join(lines) for lines in texts.values())
    owners = {name: (cls, _objects(every, cls) if cls else set()) for name, (_how, cls) in own.items()}
    names = READ_CALLS + WRITE_CALLS + tuple(n for n in own if n not in ("main", "setup", "loop", "app_main"))
    out = []
    for rel, lines in texts.items():
        code = "\n".join(lines)
        starts = [0]
        for ln in lines:
            starts.append(starts[-1] + len(ln) + 1)
        for line, call, how in _results(code, starts, names, owners):
            reads = call in READ_CALLS or re.search(r"read|get|fetch|receive|recv|load", call, re.I)
            what = (f"{call}() {own[call][0]}" if call in own else
                    f"{call}() reports whether the {'read' if call in READ_CALLS else 'write'} worked")
            out.append((rel, line, call, f"{what}, but this call's {how}: a failure goes unnoticed"
                                         + (" and the buffer's old bytes are used as data." if reads else ".")))
    return out