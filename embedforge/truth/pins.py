"""Pins the code passes to calls (Wire.setSDA(16), pinMode(LED_PIN, OUTPUT), gpio_init(25)) are wiring claims, like a
`#define X_PIN`: verify traces each to a schematic fact, checks the function the call gives the pin (SDA, SCL, SCK, TX,
RX, CS) against the role the fact records, and on RP2040/RP2350 checks that the peripheral instance can reach that GPIO
(an I2C0 SDA must sit on a GPIO with number % 4 == 0; the chip silently routes nothing otherwise)."""

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
