"""Pins the code passes to calls (Wire.setSDA(16), pinMode(LED_PIN, OUTPUT), gpio_init(25)) are wiring claims, like a
`#define X_PIN`: verify traces each to a schematic fact, checks the function the call gives the pin (SDA, SCL, SCK, TX,
RX, CS) against the role the fact records, and on RP2040/RP2350 checks that the peripheral instance can reach that GPIO
(an I2C0 SDA must sit on a GPIO with number % 4 == 0; the chip silently routes nothing otherwise).

Also: a bus read whose result is never looked at (a failed transfer then delivers stale buffer bytes as data)."""

import re
from typing import Dict, List, Optional, Tuple

# call -> the role it gives its first argument ('' = a pin, no particular function)
PIN_CALLS: Dict[str, str] = {
    "setSDA": "SDA", "setSCL": "SCL", "setSCK": "SCK", "setCS": "CS", "setMISO": "RX", "setMOSI": "TX",
    "setRX": "RX", "setTX": "TX",
    "pinMode": "", "digitalWrite": "", "digitalRead": "", "analogRead": "", "analogWrite": "", "digitalPinToInterrupt": "",
    "tone": "", "noTone": "",
    "gpio_init": "", "gpio_put": "", "gpio_get": "", "gpio_set_dir": "", "gpio_set_function": "", "gpio_pull_up": "",
    "gpio_pull_down": "", "gpio_set_irq_enabled": "",
}
_CALL_RE = re.compile(r"(?:\b([A-Za-z_]\w*)\s*(?:\.|->)\s*)?\b(" + "|".join(PIN_CALLS) + r")\s*\(")

# the words a fact's property or a define's name uses for a pin's function, one role per family
_ROLE_WORDS = {"SDA": "SDA", "SCL": "SCL", "SCK": "SCK", "SCLK": "SCK", "CLK": "SCK", "MOSI": "TX", "SDO": "TX", "COPI": "TX",
               "TX": "TX", "TXD": "TX", "MISO": "RX", "SDI": "RX", "CIPO": "RX", "RX": "RX", "RXD": "RX", "CS": "CS",
               "NSS": "CS", "SS": "CS", "CSN": "CS"}

# Arduino-Pico (earlephilhower) objects -> RP2040/RP2350 peripheral instance
INSTANCES = {"I2C": {"Wire": 0, "Wire1": 1}, "SPI": {"SPI": 0, "SPI1": 1}, "UART": {"Serial1": 0, "Serial2": 1}}

# bus reads that return a status or a byte count (ESP-IDF's i2c_master_read only queues a command: its result comes
# from i2c_master_cmd_begin)
_READ_CALLS = ("requestFrom", "i2c_read_blocking", "i2c_read_timeout_us", "i2c_read_blocking_until", "HAL_I2C_Mem_Read",
               "HAL_I2C_Master_Receive", "i2c_master_read_from_device", "i2c_master_write_read_device",
               "i2c_master_transmit_receive", "i2c_master_receive", "i2c_burst_read_dt", "i2c_read_dt", "i2c_write_read_dt")
_READ_RE = re.compile(r"(?:\b[A-Za-z_]\w*\s*(?:\.|->)\s*)?\b(" + "|".join(_READ_CALLS) + r")\s*\(")


def role_of(name: str) -> str:
    """'BMP388_SDA_PIN' -> 'SDA', 'GYRO_MISO_PIN' -> 'RX', 'LED_PIN' -> ''."""
    for w in re.split(r"[^A-Za-z0-9]+", str(name or "").upper()):
        if w in _ROLE_WORDS:
            return _ROLE_WORDS[w]
    return ""


def _first_arg(code: str, start: int) -> Tuple[str, int]:
    """The first argument of the call whose '(' is at code[start-1], and where the call ends."""
    depth, i = 0, start
    while i < len(code):
        c = code[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif c == "," and depth == 0:
            break
        i += 1
    return code[start:i].strip(), i


def pin_args(code: str) -> List[Tuple[str, str, str]]:
    """(call, receiver, first argument) for each pin-taking call in one line of code (comments stripped)."""
    out = []
    for m in _CALL_RE.finditer(code):
        arg, _ = _first_arg(code, m.end())
        out.append((m.group(2), m.group(1) or "", arg))
    return out


def literal_pin(arg: str) -> Optional[int]:
    m = re.fullmatch(r"\(?\s*(?:\(\s*\w+\s*\)\s*)?(\d{1,3})[uU]?\s*\)?", arg)
    return int(m.group(1)) if m else None


def family(call: str, receiver: str) -> str:
    if call in ("setSDA", "setSCL"):
        return "I2C"
    if call in ("setSCK", "setCS", "setMISO", "setMOSI"):
        return "SPI"
    if call in ("setRX", "setTX"):
        r = receiver.lower()
        return "SPI" if "spi" in r else "UART" if re.search(r"serial|uart", r) else ""
    return ""


def rp_function(fam: str, gpio: int, rp2350: bool) -> Optional[Tuple[int, str]]:
    """(instance, role) that GPIO `gpio` carries for an I2C, SPI or UART on RP2040/RP2350 (datasheet GPIO function
    table; the pattern repeats over GPIO0-47 on the RP2350B)."""
    if fam == "I2C":
        return (gpio // 2) % 2, "SDA" if gpio % 2 == 0 else "SCL"
    if fam == "SPI":
        return (gpio // 8) % 2, {0: "RX", 1: "CS", 2: "SCK", 3: "TX"}[gpio % 4]
    if fam == "UART":
        if gpio % 4 >= 2 and not rp2350:   # the RP2350 adds TX/RX on the other two pins of each group (UART_AUX)
            return None
        return ((gpio // 4) ^ (gpio // 8)) & 1, "TX" if gpio % 2 == 0 else "RX"
    return None


def legal_pins(fam: str, inst: int, role: str, rp2350: bool, max_gpio: int) -> List[int]:
    return [g for g in range(max_gpio + 1) if rp_function(fam, g, rp2350) == (inst, role)]


def unchecked_reads(rel: str, lines: List[str]) -> List[Tuple[int, str, str]]:
    """(line, call, message) for bus reads whose result the code never looks at: the call is a statement of its own and
    no available() check follows, or its result goes into a variable that is never read."""
    code = "\n".join(lines)
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln) + 1)
    out = []
    for m in _READ_RE.finditer(code):
        line = next(i for i in range(len(starts) - 1) if starts[i] <= m.start() < starts[i + 1]) + 1
        # the rest of the enclosing block
        depth, j = 0, m.end()
        while j < len(code):
            if code[j] == "{":
                depth += 1
            elif code[j] == "}":
                if depth == 0:
                    break
                depth -= 1
            j += 1
        _, call_end = _first_arg(code, m.end())
        while call_end < len(code) and code[call_end] != ")":   # the rest of the argument list
            _, call_end = _first_arg(code, call_end + 1)
        rest = code[call_end + 1:j]
        before = code[starts[line - 1]:m.start()]
        assigned = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*=\s*(?:\([^()]*\)\s*)?$", before)
        stmt = re.fullmatch(r"\s*(?:\(void\)\s*)?", before)
        if assigned and not re.search(r"(==|!=|<=|>=|return|\bif\b|\?)", before):
            var = assigned.group(1)
            if not re.search(rf"\b{re.escape(var)}\b", rest):
                out.append((line, m.group(1), f"{m.group(1)}() puts its result in {var}, which is never read: a failed "
                                              "transfer is not noticed and the buffer's old bytes are used as data."))
        elif stmt and not re.search(r"\bavailable\s*\(", rest):
            out.append((line, m.group(1), f"The result of {m.group(1)}() is ignored: a failed transfer is not noticed and "
                                          "the buffer's old bytes are used as data."))
    return out
