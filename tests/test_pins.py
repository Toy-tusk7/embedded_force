"""Pins passed to calls are wiring claims: traced to the schematic, checked for their role (SDA vs SCL) and, on
RP2040/RP2350, for the peripheral instance that can reach them; and bus reads must check their result."""

from pathlib import Path

import pytest

from embedforge.agent.workspace import Project
from embedforge.truth import FactLedger, SourceStore, verify_project
from embedforge.truth import betaflight_rp2350 as bf
from embedforge.truth import pins

BOARD = ("U3 pin 4 (SDI) -> net I2C0_SDA -> U1 pin 21 (GPIO16)\n"
         "U3 pin 2 (SCK) -> net I2C0_SCL -> U1 pin 22 (GPIO17)\n")

DRIVER = """#include <Arduino.h>
#include <Wire.h>
void setup() {
    // I2C pins from the schematic
    Wire.setSDA(16);   /* {sda} */
    Wire.setSCL(17);   /* {scl} */
    Wire.begin();
}
void loop() {}
"""


def _project(tmp_path: Path, code: str):
    p = Project("p", tmp_path / "p")
    store = SourceStore(p.root)
    board = store.add("user://board", "Imported schematic", BOARD, kind="user")
    ledger = FactLedger(p.root, store)
    sda = ledger.record("U1", "BMP388_SDA_PIN", "GPIO16", board.id, "U3 pin 4 (SDI) -> net I2C0_SDA -> U1 pin 21 (GPIO16)")["fact"]["id"]
    scl = ledger.record("U1", "BMP388_SCL_PIN", "GPIO17", board.id, "U3 pin 2 (SCK) -> net I2C0_SCL -> U1 pin 22 (GPIO17)")["fact"]["id"]
    (p.root / "src").mkdir(exist_ok=True)
    (p.root / "src" / "main.cpp").write_text(code.replace("{sda}", sda).replace("{scl}", scl), encoding="utf-8")
    return p, ledger, sda, scl


def _errors(p, ledger, chip="RP2350 weact_rp2350b"):
    rep = verify_project(p.root, ledger, pico=True, chip=chip)
    return [(f.code, f.message) for f in rep.findings if f.severity == "ERROR"]


def test_cited_pins_in_calls_pass(tmp_path):
    p, ledger, _, _ = _project(tmp_path, DRIVER)
    assert _errors(p, ledger) == []


def test_a_pin_call_needs_its_schematic_fact(tmp_path):
    p, ledger, _, _ = _project(tmp_path, DRIVER.replace("/* {scl} */", ""))
    assert [c for c, _ in _errors(p, ledger)] == ["UNTAGGED_CONSTANT"]
    p, ledger, _, _ = _project(tmp_path / "b", "void setup() { pinMode(25, OUTPUT); }\n")
    assert [c for c, _ in _errors(p, ledger)] == ["UNSOURCED_CONSTANT"]


def test_swapped_sda_and_scl_are_caught_even_when_both_facts_are_cited(tmp_path):
    code = DRIVER.replace("Wire.setSDA(16);   /* {sda} */", "Wire.setSDA(17);   /* {sda} {scl} */") \
                 .replace("Wire.setSCL(17);   /* {scl} */", "Wire.setSCL(16);   /* {sda} {scl} */")
    p, ledger, _, _ = _project(tmp_path, code)
    errs = _errors(p, ledger)
    role = [m for c, m in errs if c == "PIN_ROLE_MISMATCH"]
    assert len(role) == 2 and "uses 17 as SDA" in role[0] and "BMP388_SCL_PIN (SCL)" in role[0], errs
    assert {c for c, _ in errs} == {"PIN_ROLE_MISMATCH", "PIN_CAPABILITY"}


def test_the_i2c_instance_must_reach_the_pin(tmp_path):
    code = """#include <Wire.h>
class Baro { public: Baro(TwoWire &w = Wire1) : _wire(w) {} void begin(); private: TwoWire &_wire; };
void Baro::begin() {
    _wire.setSDA(16);   /* {sda} */
    _wire.setSCL(17);   /* {scl} */
}
"""
    p, ledger, _, _ = _project(tmp_path, code)
    cap = [m for c, m in _errors(p, ledger) if c == "PIN_CAPABILITY"]
    assert len(cap) == 2 and "_wire.setSDA(16) (_wire is Wire1): GPIO16 cannot be I2C1 SDA on the RP2350B; it is I2C0 SDA" in cap[0], cap


def test_a_gpio_the_package_does_not_have(tmp_path):
    p, ledger, _, _ = _project(tmp_path, DRIVER)
    (p.root / "src" / "led.cpp").write_text("#define LED_PIN 40   // GPIO40\nvoid led() { pinMode(LED_PIN, 1); }\n", encoding="utf-8")
    assert "PIN_NOT_ON_CHIP" not in [c for c, _ in _errors(p, ledger)]                 # RP2350B: GPIO0-47
    assert "PIN_NOT_ON_CHIP" in [c for c, _ in _errors(p, ledger, chip="RP2350 pico2")]  # RP2350A: GPIO0-29


def test_the_gpio_function_rule_matches_the_rp2350_pin_tables():
    for inst, roles in bf.SPI_PINS.items():
        for role, gpios in roles.items():
            r = {"SDI": "RX", "SDO": "TX"}.get(role, role)
            assert set(pins.legal_pins("SPI", inst, r, True, 47)) == gpios, (inst, role)
    for inst, roles in bf.UART_PINS.items():
        for role, gpios in roles.items():
            assert set(pins.legal_pins("UART", inst, role, True, 47)) == gpios, (inst, role)
    assert pins.legal_pins("UART", 0, "TX", False, 29) == [0, 12, 16, 28]       # RP2040: no UART_AUX pins
    assert pins.legal_pins("I2C", 0, "SDA", True, 47) == [g for g in range(48) if g % 4 == 0]


@pytest.mark.parametrize("read, flagged", [
    ("size_t got = Wire.requestFrom(0x76, 6);\n    for (int i = 0; i < 6; i++) b[i] = Wire.read();", True),
    ("Wire.requestFrom(0x76, 6);\n    for (int i = 0; i < 6; i++) b[i] = Wire.read();", True),
    ("Wire.requestFrom(0x76, 6);\n    while (Wire.available()) b[n++] = Wire.read();", False),
    ("if (Wire.requestFrom(0x76, 6) != 6) return false;\n    for (int i = 0; i < 6; i++) b[i] = Wire.read();", False),
    ("size_t got = Wire.requestFrom(0x76, 6);\n    if (got != 6) return false;", False),
    ("return i2c_read_blocking(i2c0, 0x76, b, 6, false) == 6;", False),
    ("i2c_read_blocking(i2c0, 0x76, b, 6, false);", True),
])
def test_a_bus_read_must_check_its_result(read, flagged):
    lines = ("bool rd(uint8_t *b) {\n    int n = 0;\n    " + read + "\n    return true;\n}\n").split("\n")
    assert bool(pins.unchecked_reads("src/x.cpp", lines)) == flagged
