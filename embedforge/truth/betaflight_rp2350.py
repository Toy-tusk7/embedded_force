"""Pin-capability rules for Betaflight board configs on RP2350A/RP2350B (and RP2354A/B, the same dies).

A Betaflight config can name any pin, but the RP2350 drivers only route a peripheral to specific GPIOs and silently
ignore the rest (the board then boots without a gyro, SD card or UART). These tables were read from the pinned
Betaflight 2026.6.2 source and are checked deterministically against config.h:

  src/platform/PICO/bus_spi_pico.c         SPI SCK/SDI(MISO)/SDO(MOSI) pin lists
  src/platform/PICO/bus_i2c_pico.c         I2C: SDA % 4 == 0 + 2*n, SCL % 4 == 1 + 2*n
  src/platform/PICO/uart/uart_hw.c         UART TX/RX pin lists
  src/platform/PICO/adc_pico.c             ADC: GPIO26-29 (RP2350A), GPIO40-47 (RP2350B)
  src/platform/PICO/dshot_pico.c           DShot on one PIO block: 4 motors, all within GPIO0-31 or GPIO16-47
"""

import re
from typing import Dict, List, Optional, Tuple

SOURCE = "Betaflight 2026.6.2 src/platform/PICO"

SPI_PINS = {
    0: {"SCK": {2, 6, 18, 22, 34, 38}, "SDI": {0, 4, 16, 20, 32, 36}, "SDO": {3, 7, 19, 23, 35, 39}},
    1: {"SCK": {10, 14, 26, 30, 42, 46}, "SDI": {8, 12, 24, 28, 40, 44}, "SDO": {11, 15, 27, 31, 43, 47}},
}
UART_PINS = {
    0: {"TX": {0, 2, 12, 14, 16, 18, 28, 30, 32, 34, 44, 46}, "RX": {1, 3, 13, 15, 17, 19, 29, 31, 33, 35, 45, 47}},
    1: {"TX": {4, 6, 8, 10, 20, 22, 24, 26, 36, 38, 40, 42}, "RX": {5, 7, 9, 11, 21, 23, 25, 27, 37, 39, 41, 43}},
}
ADC_PINS = {"RP2350A": set(range(26, 30)), "RP2350B": set(range(40, 48))}
MAX_GPIO = {"RP2350A": 29, "RP2350B": 47}
MAX_DSHOT_MOTORS = 4

_DEFINE = re.compile(r"^\s*#\s*define\s+([A-Z0-9_]+)\s+(PA(\d{1,2})|NONE)\b", re.M)


def _pins(text: str) -> Dict[str, Tuple[int, int]]:
    """define name -> (gpio, line) for every '#define X PAn' (NONE skipped)."""
    out = {}
    for m in _DEFINE.finditer(text):
        if m.group(3) is not None:
            out[m.group(1)] = (int(m.group(3)), text.count("\n", 0, m.start()) + 1)
    return out


def check_config(text: str, target: str) -> List[Dict[str, object]]:
    """Return findings {severity, code, line, message, suggestion} for pins the RP2350 drivers cannot use."""
    if target not in MAX_GPIO:
        return []
    pins = _pins(text)
    out: List[Dict[str, object]] = []

    def bad(line: int, msg: str, fix: str) -> None:
        out.append({"severity": "ERROR", "code": "BF_PIN_CAPABILITY", "line": line, "message": msg,
                    "suggestion": fix + f" (pin tables: {SOURCE})."})

    for name, (gpio, line) in pins.items():
        if gpio > MAX_GPIO[target]:
            bad(line, f"{name} = PA{gpio}: {target} has GPIO0-{MAX_GPIO[target]} only.", "Use a pin that exists on this package")
            continue
        m = re.fullmatch(r"SPI([01])_(SCK|SDI|SDO|MISO|MOSI)_PIN", name)
        if m:
            role = {"MISO": "SDI", "MOSI": "SDO"}.get(m.group(2), m.group(2))
            legal = {p for p in SPI_PINS[int(m.group(1))][role] if p <= MAX_GPIO[target]}
            if gpio not in legal:
                bad(line, f"{name} = PA{gpio} cannot carry SPI{m.group(1)} {role}.", f"Legal pins: {', '.join(f'PA{p}' for p in sorted(legal))}")
            continue
        m = re.fullmatch(r"UART([01])_(TX|RX)_PIN", name)
        if m:
            legal = {p for p in UART_PINS[int(m.group(1))][m.group(2)] if p <= MAX_GPIO[target]}
            if gpio not in legal:
                bad(line, f"{name} = PA{gpio} cannot carry UART{m.group(1)} {m.group(2)}.", f"Legal pins: {', '.join(f'PA{p}' for p in sorted(legal))}")
            continue
        m = re.fullmatch(r"I2C([01])_(SDA|SCL)_PIN", name)
        if m:
            want = 2 * int(m.group(1)) + (0 if m.group(2) == "SDA" else 1)
            if gpio % 4 != want:
                bad(line, f"{name} = PA{gpio}: I2C{m.group(1)} {m.group(2)} must be on a GPIO with number % 4 == {want}.",
                    "Swap SDA/SCL or use the other I2C instance")
            continue
        if re.fullmatch(r"ADC_\w+_PIN", name) and gpio not in ADC_PINS[target]:
            lo, hi = min(ADC_PINS[target]), max(ADC_PINS[target])
            bad(line, f"{name} = PA{gpio} is not an ADC pin on {target}.", f"Use PA{lo}-PA{hi}")

    motors = sorted((int(re.search(r"\d+", n).group()), g, ln) for n, (g, ln) in pins.items() if re.fullmatch(r"MOTOR\d+_PIN", n))
    if len(motors) > MAX_DSHOT_MOTORS:
        bad(motors[MAX_DSHOT_MOTORS][2], f"{len(motors)} motor pins defined; the RP2350 DShot driver supports {MAX_DSHOT_MOTORS}.",
            "Remove MOTOR5_PIN and above")
    if motors:
        gp = [g for _, g, _ in motors]
        if not (max(gp) <= 31 or min(gp) >= 16):
            bad(motors[0][2], f"Motor pins PA{min(gp)}..PA{max(gp)} span more than one PIO window.",
                "All DShot motor pins must lie within GPIO0-31 or within GPIO16-47")
    return out


def summary(text: str, target: str) -> Optional[str]:
    n = len(_pins(text))
    return f"{n} pin assignments checked against {target} peripheral pin tables" if target in MAX_GPIO else None
