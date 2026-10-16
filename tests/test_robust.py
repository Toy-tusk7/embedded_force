"""Robustness rules: every wait on hardware is bounded, and every status that reports a failure is checked."""

import pytest

from embedforge.truth import robust


def _lines(body: str):
    return ("bool f(void) {\n    int n = 0; uint8_t s = 0;\n" + body + "\n    return true;\n}\n").split("\n")


@pytest.mark.parametrize("loop, flagged", [
    ("    while (!Serial) {\n        delay(10);\n    }", True),
    ("    while (!Serial && millis() < 3000) {\n        delay(10);\n    }", False),
    ("    uint32_t t0 = millis();\n    while (!Serial) {\n        if (millis() - t0 > 3000) break;\n    }", False),
    ("    while (!(read_status() & DRDY)) {\n    }", True),
    ("    while (!(read_status() & DRDY)) {\n        if (++n > 100) return false;\n        delay(1);\n    }", False),
    ("    while (!(read_status() & DRDY) && n++ < 100) {\n    }", False),
    ("    while (Wire.available()) buf[n++] = Wire.read();", False),
    ("    while (n < 6) {\n        buf[n] = 0;\n        n++;\n    }", False),
    ("    for (;;) {\n        if (read_status() & 1) break;\n    }", True),
    ("    for (;;) {\n        do_work();\n        delay(1000);\n    }", False),
    ("    for (int i = 0; i < 6; i++) buf[i] = Wire.read();", False),
    ("    do {\n        s = read_status();\n    } while (!(s & DRDY));", True),
    ("    do {\n        s = read_status();\n    } while (!(s & DRDY) && ++n < 50);", False),
    ("    while (1) {\n        delay(1000);\n    }", False),     # a deliberate halt, not a wait on hardware
])
def test_waits_on_hardware_must_be_bounded(loop, flagged):
    assert bool(robust.unbounded_waits(_lines(loop))) == flagged


OWN = """static int write_reg(uint8_t r, uint8_t v) { return i2c_write_blocking(i2c0, 0x76, b, 2, false) == 2 ? 0 : -1; }
static int read_regs(uint8_t r, uint8_t *b, size_t n) { return i2c_burst_read_dt(&dev, r, b, n); }
static bool ok_read(uint8_t *b) { if (read_regs(0x04, b, 6) != 0) return false; return true; }
static int plain(int x) { return x + 1; }
"""


def test_a_same_named_method_of_another_object_is_not_the_projects_function():
    cls = ("class Baro { public: bool begin(); };\n"
           "bool Baro::begin() {\n    _wire.begin();\n    if (!probe()) return false;\n    return true;\n}\n"
           "Baro baro;\n"
           "void setup() {\n    Serial.begin(115200);\n    baro.begin();\n}\n")
    found = robust.ignored_results({"src/main.cpp": cls.split("\n")})
    assert [(ln, call) for _f, ln, call, _m in found] == [(10, "begin")], found   # baro.begin(), not Serial / _wire

@pytest.mark.parametrize("call, flagged", [
    ("write_reg(0x7E, 0xB6);", True),               # ? 0 : -1
    ("read_regs(0x04, b, 6);", True),               # passes the SDK's status through
    ("ok_read(b);", True),                          # bool that returns false
    ("plain(3);", False),                           # cannot fail
    ("(void)write_reg(0x7E, 0xB6);", False),        # discarded on purpose
    ("if (write_reg(0x7E, 0xB6) != 0) return;", False),
    ("int rc = write_reg(0x7E, 0xB6);", True),      # stored, never read
    ("int rc = write_reg(0x7E, 0xB6);\n    if (rc) return;", False),
    ("Serial.write_reg(1, 2);", False),             # another object's method with the same name
])
def test_statuses_that_report_failure_must_be_checked(call, flagged):
    texts = {"src/main.c": (OWN + "void g(uint8_t *b) {\n    " + call + "\n}\n").split("\n")}
    assert bool(robust.ignored_results(texts)) == flagged, robust.ignored_results(texts)


@pytest.mark.parametrize("branch, flagged", [
    ("if (!write_reg(0x7E, 0xB6)) {\n        return true;\n    }", True),
    ("if (!write_reg(0x7E, 0xB6)) {\n        return false;\n    }", False),
    ("if (read_regs(0x04, b, 6) != 0) return true;", True),
    ("if (read_regs(0x04, b, 6) == 0) return true;", False),      # the success branch
    ("if (n != 3) return true;", False),                          # not a call that reports failure
])
def test_a_failure_must_not_be_reported_as_success(branch, flagged):
    code = OWN.replace("static bool ok_read", "static bool wr2").replace("bool wr2", "bool ok_read") + \
        "bool setup_dev(uint8_t *b) {\n    int n = 0;\n    " + branch + "\n    if (!ok_read(b)) return false;\n    return true;\n}\n"
    found = robust.failures_reported_as_success({"src/main.c": code.split("\n")})
    assert bool(found) == flagged, found
