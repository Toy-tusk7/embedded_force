"""Register meaning: fields, byte layouts, burst reads and formulas recorded from datasheet text, checked statically
against driver code (truth/registers.py), plus the C semantics of the tracer they rely on."""

from pathlib import Path

import pytest

from embedforge.agent.events import RunSession
from embedforge.agent.tools import ToolBelt
from embedforge.agent.workspace import Project
from embedforge.truth import FactLedger, SourceStore, verify_project
from embedforge.truth.flow import FlowModel, c_convert
from embedforge.truth.registers import RegisterBook, check_registers, coverage_findings
from embedforge.truth.sequences import SequenceBook

DS = """Table 1: Register 0x00 CHIP_ID
0x00 CHIP_ID 0x50 chip_id
Table 2: Register 0x03 STATUS
Bit 4 cmd_rdy Command decoder ready
Bit 5 drdy_press Pressure data ready
Bit 6 drdy_temp Temperature data ready
Table 3: Register 0x1B PWR_CTRL
Bit 0 press_en 0 press_off 1 press_on
Bit 1 temp_en 0 temp_off 1 temp_on
Bit 5..4 mode 00 sleep mode 01/10 forced mode 11 normal mode
Table 4: Register 0x1C OSR
Bit 2..0 osr_p 000 x1 001 x2 010 x4 011 x8
Bit 5..3 osr_t 000 x1 001 x2 010 x4 011 x8
Table 5: Register 0x7E CMD
Bit 7..0 cmd 0xB6 softreset Triggers a reset
Table 6: Data registers
Register 0x06 DATA_2 0x05 DATA_1 0x04 DATA_0 name PRESS_MSB_23_16 PRESS_LSB_15_8 PRESS_XLSB_7_0
Register 0x09 DATA_5 0x08 DATA_4 0x07 DATA_3 name TEMP_MSB_23_16 TEMP_LSB_15_8 TEMP_XLSB_7_0
Shadowing only works if all data registers are read in a single burst read.
Table 7: Trimming coefficients
50 0x32 NVM_PAR_T1<15:8> NVM_PAR_T1 16 Bit unsigned 49 0x31 NVM_PAR_T1<7:0>
52 0x34 NVM_PAR_T2<15:8> NVM_PAR_T2 16 Bit signed 51 0x33 NVM_PAR_T2<7:0>
Coefficients: PAR_T1 = NVM_PAR_T1 / 2^-8 and PAR_T2 = NVM_PAR_T2 / 2^30
Temperature: partial = UT - PAR_T1; T = partial * PAR_T2
The 7-bit I2C address is 0x76 when SDO is connected to GND.
Power-up: write softreset to CMD, wait 2 ms, then set PWR_CTRL.
Reference code: partial = (float)(uncomp_temp - calib_data->par_t1); T = partial * calib_data->par_t2;
Vendor code: par_t1 = (double)reg_calib_data->par_t1 / 0.00390625;
"""

GOOD = r"""
#include <Arduino.h>
#include <Wire.h>
#define ADDR 0x76
#define REG_ID 0x00
#define REG_STATUS 0x03
#define REG_DATA 0x04
#define REG_PWR 0x1B
#define REG_OSR 0x1C
#define REG_CALIB 0x31
#define REG_CMD 0x7E
#define DRDY_MASK 0x60
static double par_t1, par_t2, temperature;
static void wr(uint8_t r, uint8_t v) { Wire.beginTransmission(ADDR); Wire.write(r); Wire.write(v); Wire.endTransmission(); }
static bool rd(uint8_t r, uint8_t *b, uint8_t n) {
  Wire.beginTransmission(ADDR); Wire.write(r);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((uint8_t)ADDR, n) != n) return false;
  for (uint8_t i = 0; i < n; i++) b[i] = Wire.read();
  return true;
}
void sensor_init() {
  wr(REG_CMD, 0xB6);
  delay(3);
  uint8_t c[4];
  rd(REG_CALIB, c, 4);
  uint16_t nvm_t1 = (uint16_t)((c[1] << 8) | c[0]);
  int16_t nvm_t2 = (int16_t)((c[3] << 8) | c[2]);
  par_t1 = nvm_t1 / pow(2, -8);
  par_t2 = nvm_t2 / pow(2, 30);
  wr(REG_OSR, 0x0B);
  wr(REG_PWR, PWR_VALUE);
}
void setup() {
  Wire.begin();
  uint8_t id = 0;
  rd(REG_ID, &id, 1);
  if (id != 0x50) return;
  sensor_init();
}
void loop() {
  uint8_t st = 0;
  rd(REG_STATUS, &st, 1);
  if ((st & DRDY_MASK) != DRDY_MASK) return;
  READ_DATA
  temperature = ((double)ut - par_t1) * par_t2;
  Serial.println(temperature);
  delay(100);
}
"""
GOOD_READ = ("uint8_t d[6]; rd(REG_DATA, d, 6); "
             "uint32_t ut = ((uint32_t)d[5] << 16) | ((uint32_t)d[4] << 8) | d[3];")


def driver(pwr="0x33", read=GOOD_READ, **subs):
    code = GOOD.replace("PWR_VALUE", pwr).replace("READ_DATA", read)
    for old, new in subs.items():
        assert old in code, old
        code = code.replace(old, new)
    return code


def _q(start, end):
    i = DS.index(start)
    return DS[i:DS.index(end, i) + len(end)]


REGISTERS = [
    {"name": "CHIP_ID", "addr": "0x00", "id_value": "0x50", "quote": "0x00 CHIP_ID 0x50 chip_id"},
    {"name": "STATUS", "addr": "0x03", "quote": _q("Table 2", "Temperature data ready"),
     "fields": [{"name": "cmd_rdy", "bits": "4"}, {"name": "drdy_press", "bits": "5"}, {"name": "drdy_temp", "bits": "6"}]},
    {"name": "PWR_CTRL", "addr": "0x1B", "quote": _q("Table 3", "11 normal mode"),
     "fields": [{"name": "press_en", "bits": "0"}, {"name": "temp_en", "bits": "1"},
                {"name": "mode", "bits": "5:4", "values": {"sleep": "00", "forced": ["0b01", "0b10"], "normal": "0b11"}}]},
    {"name": "OSR", "addr": "0x1C", "quote": _q("Table 4", "011 x8\nBit 5..3 osr_t 000 x1 001 x2 010 x4 011 x8"),
     "fields": [{"name": "osr_p", "bits": "2:0"}, {"name": "osr_t", "bits": "5:3"}]},
    {"name": "CMD", "addr": "0x7E", "quote": _q("Table 5", "Triggers a reset"),
     "fields": [{"name": "cmd", "bits": "7:0", "values": {"softreset": "0xB6"}}]},
]
LAYOUTS = [
    {"name": "NVM_PAR_T1", "bytes": ["0x31", "0x32"], "signed": False, "quote": "50 0x32 NVM_PAR_T1<15:8> NVM_PAR_T1 16 Bit unsigned 49 0x31 NVM_PAR_T1<7:0>"},
    {"name": "NVM_PAR_T2", "bytes": ["0x33", "0x34"], "signed": True, "quote": "52 0x34 NVM_PAR_T2<15:8> NVM_PAR_T2 16 Bit signed 51 0x33 NVM_PAR_T2<7:0>"},
    {"name": "UP", "bytes": ["0x04", "0x05", "0x06"], "quote": "Register 0x06 DATA_2 0x05 DATA_1 0x04 DATA_0"},
    {"name": "UT", "bytes": ["0x07", "0x08", "0x09"], "quote": "Register 0x09 DATA_5 0x08 DATA_4 0x07 DATA_3"},
]


def _setup(tmp_path: Path, code: str, formulas: bool = True):
    p = Project("r1", tmp_path / "r1")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/x-datasheet.pdf", "datasheet", DS, kind="pdf")
    ledger = FactLedger(p.root, store)
    facts = {}
    for key, prop, val, unit, quote in [("addr", "i2c_address", "0x76", "", "The 7-bit I2C address is 0x76 when SDO is connected to GND."),
                                        ("cmd", "cmd_reg", "0x7E", "", "Table 5: Register 0x7E CMD"),
                                        ("rst", "softreset", "0xB6", "", "Bit 7..0 cmd 0xB6 softreset"),
                                        ("t", "startup_time", "2", "ms", "write softreset to CMD, wait 2 ms"),
                                        ("pwr", "pwr_ctrl_reg", "0x1B", "", "Table 3: Register 0x1B PWR_CTRL")]:
        res = ledger.record("BMPX", prop, val, src.id, quote, unit)
        assert res["ok"], res
        facts[key] = res["fact"]["id"]
    book = RegisterBook(p.root)
    r = book.record_layouts(store, "BMPX", src.id, LAYOUTS)
    assert r["ok"] and "rejected" not in r, r
    r = book.record_registers(store, ledger, "BMPX", src.id, REGISTERS,
                              bursts=[{"name": "data", "start": "0x04", "end": "0x09", "quote": "all data registers are read in a single burst read"}])
    assert r["ok"] and "rejected" not in r, r
    if formulas:
        fq = "Coefficients: PAR_T1 = NVM_PAR_T1 / 2^-8 and PAR_T2 = NVM_PAR_T2 / 2^30"
        assert book.record_formula(store, "BMPX", src.id, "PAR_T1", ["PAR_T1 = NVM_PAR_T1 / 2^-8"], fq)["ok"]
        assert book.record_formula(store, "BMPX", src.id, "PAR_T2", ["PAR_T2 = NVM_PAR_T2 / 2^30"], fq)["ok"]
        assert book.record_formula(store, "BMPX", src.id, "T", ["partial = UT - PAR_T1", "T = partial * PAR_T2"],
                                   "Temperature: partial = UT - PAR_T1; T = partial * PAR_T2")["ok"]
    (p.root / "src").mkdir(exist_ok=True)
    (p.root / "src" / "main.cpp").write_text(code, encoding="utf-8")
    return p, store, ledger, src, facts


def _codes(p, ledger):
    model = FlowModel.from_dir(p.root)
    summary, found = check_registers(p.root, ledger, model)
    found += coverage_findings(p.root, ledger, model)
    return summary, sorted({f.code for f in found if f.severity == "ERROR"}), found


# ------------------------------------------------------------------ the checks on code


def test_correct_driver_has_no_meaning_errors(tmp_path):
    p, store, ledger, src, f = _setup(tmp_path, driver())
    summary, codes, found = _codes(p, ledger)
    assert codes == [], [(f.code, f.message) for f in found]
    assert all("matches the code" in s for s in summary["formulas"]), summary["formulas"]
    assert any("PWR_CTRL (0x1B) <- 0x33: mode=3 (normal), temp_en=1, press_en=1" in w for w in summary["writes"]), summary["writes"]


def test_a_constant_formula_is_not_compared_with_the_code(tmp_path):
    from embedforge.truth.registers import FormulaDef, register_derived
    p, store, ledger, src, f = _setup(tmp_path, driver())
    book = RegisterBook(p.root)
    # recorded before constants were refused (a project from an older run): the code may fold it into a literal
    book.formulas["FRM-090"] = FormulaDef("FRM-090", "BMPX", "SCALE_T1", [["SCALE_T1", "2 ** -8"]], src.id, "2^-8")
    book._save()
    assert "FRM-090" not in register_derived(book) and {"FRM-001", "FRM-002", "FRM-003"} <= register_derived(book)
    summary, codes, found = _codes(p, ledger)
    assert codes == [], [(x.code, x.message) for x in found]
    assert "FRM-090 SCALE_T1: a constant, not register data" in summary["formulas"]
    # a coefficient built on the constant: the mismatch message gives the constant's value, not 'fix SCALE_T1 first'
    book.formulas["FRM-091"] = FormulaDef("FRM-091", "BMPX", "PAR_T1S", [["PAR_T1S", "NVM_PAR_T1 * SCALE_T1"]], src.id, "x")
    book._save()
    msg = next(x.message for x in _codes(p, ledger)[2] if x.code == "FORMULA_MISMATCH" and "PAR_T1S" in x.message)
    assert "(with SCALE_T1 = 0.00390625)" in msg and "fix those first" not in msg, msg
    res = book.record_formula(store, "BMPX", src.id, "SCALE_T2", ["SCALE_T2 = 2^30"], "PAR_T2 = NVM_PAR_T2 / 2^30")
    assert not res["ok"] and "uses no register value" in res["error"] and "record_fact" in res["error"], res


RULES = ("Memory map: 0x1C OSR 0x05 osr_t osr_p (default value). 0x1B PWR_CTRL 0x00 mode temp_en press_en.\n"
         "In normal mode the pressure oversampling osr_p must not exceed 3 (x8).\n")


def _constraint_setup(tmp_path, code):
    from embedforge.truth.registers import RegisterBook
    p, store, ledger, src, f = _setup(tmp_path, code)
    rules = store.add("https://www.bosch-sensortec.com/x-app-note.pdf", "app note", RULES, kind="pdf")
    book = RegisterBook(p.root)
    regs = [dict(r) for r in REGISTERS if r["name"] in ("PWR_CTRL", "OSR")]
    regs[0].update(reset="0x00", reset_quote="0x1B PWR_CTRL 0x00 mode temp_en press_en", source_id=src.id)
    regs[1].update(reset="0x05", reset_quote="0x1C OSR 0x05 osr_t osr_p", source_id=src.id)
    for r in regs:   # the reset rows are in the app note, the fields in the datasheet
        r["reset_quote_source"] = rules.id
    res = book.record_registers(store, ledger, "BMPX", src.id, regs)
    return p, store, ledger, src, rules, book, res


def test_a_rule_over_settings_is_checked_after_every_write(tmp_path):
    from embedforge.truth.registers import RegisterBook
    p, store, ledger, src, rules, book, res = _constraint_setup(tmp_path, driver())
    assert res["ok"] and any("reset value not kept" in n for n in res.get("notes", [])), res   # rows are in another source
    book = RegisterBook(p.root)
    regs = [dict(r) for r in REGISTERS if r["name"] in ("PWR_CTRL", "OSR")]
    store.add("https://www.bosch-sensortec.com/x-map.pdf", "memory map", RULES, kind="pdf")
    for r, q, v in ((regs[0], "0x1B PWR_CTRL 0x00 mode temp_en press_en", "0x00"), (regs[1], "0x1C OSR 0x05 osr_t osr_p", "0x05")):
        r.update(reset=v, reset_quote=q, quote=r["quote"])
    # reset rows and fields from one source: the datasheet text plus its memory map
    src2 = store.add("https://www.bosch-sensortec.com/x-full.pdf", "datasheet + map", DS + "\n" + RULES, kind="pdf")
    res = book.record_registers(store, ledger, "BMPX", src2.id, regs)
    assert res["ok"] and not res.get("notes"), res
    assert book.register_at(0x1C).reset == 0x05
    r = book.record_constraint(store, "BMPX", src2.id, "osr_in_normal_mode", "OSR.osr_p <= 3",
                               ["In normal mode the pressure oversampling osr_p must not exceed 3 (x8)."], when="PWR_CTRL.mode == normal")
    assert r["ok"] and "CON-001" in r["constraint"], r
    assert "not in the quotes" in book.record_constraint(store, "BMPX", src2.id, "x", "OSR.osr_p <= 4",
                                                         ["In normal mode the pressure oversampling osr_p must not exceed 3 (x8)."])["error"]
    assert "no field" in book.record_constraint(store, "BMPX", src2.id, "x", "OSR.osr_q <= 3",
                                                ["In normal mode the pressure oversampling osr_p must not exceed 3 (x8)."])["error"]
    summary, codes, found = _codes(p, ledger)
    assert "CONSTRAINT_VIOLATED" not in codes and any("holds after each of" in s for s in summary["constraints"]), summary
    # the same values written in the other order: normal mode starts while OSR still holds its reset value 0x05
    swapped = driver(**{"  wr(REG_OSR, 0x0B);\n  wr(REG_PWR, 0x33);": "  wr(REG_PWR, 0x33);\n  wr(REG_OSR, 0x0B);"})
    assert swapped != driver()
    (p.root / "src" / "main.cpp").write_text(swapped, encoding="utf-8")
    summary, codes, found = _codes(p, ledger)
    bad = next(x for x in found if x.code == "CONSTRAINT_VIOLATED")
    assert "OSR.osr_p=5" in bad.message and "mode=3 (normal)" in bad.message and bad.line == swapped.split("\n").index("  wr(REG_PWR, 0x33);") + 1, bad
    always = book.record_constraint(store, "BMPX", src2.id, "osr_limit", "OSR.osr_p <= 3",   # models write when='true'
                                    ["In normal mode the pressure oversampling osr_p must not exceed 3 (x8)."], when="true")
    assert always["ok"] and "when" not in always["constraint"], always


def test_probe_uses_the_id_the_code_expects_when_none_is_recorded(tmp_path):
    p, store, ledger, src, f = _setup(tmp_path, driver())
    book = RegisterBook(p.root)
    reg = book.register_at(0x00)
    reg.id_value = None   # recorded without id_value: the probe must still get past `if (id != 0x50) return;`
    book._save()
    summary, codes, found = _codes(p, ledger)
    assert codes == [] and all("matches the code" in s for s in summary["formulas"]), summary["formulas"]


@pytest.mark.parametrize("code, expected", [
    (driver(read="uint8_t d[6]; rd(REG_DATA, d, 6); uint32_t ut = ((uint32_t)d[3] << 16) | ((uint32_t)d[4] << 8) | d[5];"), "LAYOUT_SHIFT"),
    (driver(read="uint8_t d[3]; rd(0x07, d, 3); uint32_t ut = ((uint32_t)d[2] << 16) | ((uint32_t)d[1] << 8) | d[0];"), "REG_BURST_SPLIT"),
    (driver(**{"#define DRDY_MASK 0x60": "#define DRDY_MASK 0x0C"}), "REG_MASK_UNDEFINED"),
    (driver(**{"par_t1 = nvm_t1 / pow(2, -8);": "par_t1 = nvm_t1;"}), "FORMULA_MISMATCH"),
    (driver(**{"par_t1 = nvm_t1 / pow(2, -8);": "par_t1 = nvm_t1 * pow(2, -8);"}), "FORMULA_MISMATCH"),
    (driver(**{"int16_t nvm_t2": "uint16_t nvm_t2"}), "FORMULA_MISMATCH"),                       # signedness
    (driver(**{"(c[1] << 8) | c[0]": "(c[0] << 8) | c[1]"}), "LAYOUT_SHIFT"),                    # byte order
    (driver(**{"wr(REG_OSR, 0x0B);": "wr(REG_OSR, 0x0B); wr(0x1D, 0x02);"}), "REG_UNMAPPED"),   # a register nobody looked up
])
def test_each_meaning_bug_is_caught(tmp_path, code, expected):
    p, store, ledger, src, f = _setup(tmp_path, code)
    _summary, codes, found = _codes(p, ledger)
    assert expected in codes, [(x.code, x.message) for x in found]


def test_unscaled_coefficient_message_names_the_raw_value(tmp_path):
    p, store, ledger, src, f = _setup(tmp_path, driver(**{"par_t1 = nvm_t1 / pow(2, -8);": "par_t1 = nvm_t1;"}))
    _s, _c, found = _codes(p, ledger)
    msg = next(x.message for x in found if x.code == "FORMULA_MISMATCH" and "PAR_T1" in x.message)
    assert "raw NVM_PAR_T1" in msg and "conversion is missing" in msg, msg


def test_sequence_fields_compose_the_value_and_catch_sleep_mode(tmp_path):
    p, store, ledger, src, f = _setup(tmp_path, driver(pwr="0x03"))
    steps = [{"op": "write", "reg": f["cmd"], "value": f["rst"]}, {"op": "delay_min", "value": f["t"]},
             {"op": "write", "reg": f["pwr"], "fields": {"press_en": 1, "temp_en": 1, "mode": "normal"}}]
    res = SequenceBook(p.root).record(ledger, "BMPX", "power_up", "sensor_init", steps, src.id,
                                      "Power-up: write softreset to CMD, wait 2 ms, then set PWR_CTRL.", device=f["addr"])
    assert res["ok"], res
    assert res["sequence"]["steps"][2]["fields"] == {"press_en": 1, "temp_en": 1, "mode": 3}
    rep = verify_project(p.root, ledger)
    bad = [x for x in rep.findings if x.code == "FLOW_VALUE_MISMATCH"]
    assert bad and "mode=0 (sleep)" in bad[0].message and "0x33" in bad[0].message, [x.message for x in rep.findings]
    # the same sequence passes once the code writes the composed value
    (p.root / "src" / "main.cpp").write_text(driver(pwr="0x33"), encoding="utf-8")
    assert not [x for x in verify_project(p.root, ledger).findings if x.code.startswith("FLOW_") and x.severity == "ERROR"]


def test_sequence_steps_may_use_values_the_register_map_grounds(tmp_path):
    p, store, ledger, src, f = _setup(tmp_path, driver())
    steps = [{"op": "write", "reg": "0x7E", "value": "0xB6"}, {"op": "delay_min", "value": "2", "unit": "ms"},
             {"op": "read", "reg": "0x31", "value": "4"}, {"op": "write", "reg": "0x1B", "value": "0x33", "mask": "0xFF"}]
    res = SequenceBook(p.root).record(ledger, "BMPX", "power_up", "sensor_init", steps, src.id,
                                      "Power-up: write softreset to CMD, wait 2 ms, then set PWR_CTRL.", device="BMPX")
    assert res["ok"], res
    st = res["sequence"]["steps"]
    assert st[0]["fields"] == {"cmd": 0xB6} and st[1]["value"] == f["t"] and st[3]["fields"] == {"press_en": 1, "temp_en": 1, "mode": 3}
    assert res["sequence"]["device"] == f["addr"]
    rep = verify_project(p.root, ledger)
    assert not [x for x in rep.findings if x.code.startswith("FLOW_") and x.severity == "ERROR"], [x.message for x in rep.findings]
    # still refused: an address nobody recorded, bits no field defines, a delay that is no fact, a read past the recorded bytes
    for bad, why in [({"op": "write", "reg": "0x55", "value": "0x01"}, "neither a FACT id"),
                     ({"op": "write", "reg": "0x1B", "value": "0x83"}, "no field"),
                     ({"op": "delay_min", "value": "5", "unit": "ms"}, "not a recorded time fact"),
                     ({"op": "read", "reg": "0x31", "value": "21"}, "must be recorded")]:
        r = SequenceBook(p.root).record(ledger, "BMPX", "x", "sensor_init", [bad], src.id,
                                        "Power-up: write softreset to CMD, wait 2 ms, then set PWR_CTRL.")
        assert not r["ok"] and why in r["error"], (bad, r)


def test_value_fact_contradicting_fields_is_rejected(tmp_path):
    p, store, ledger, src, f = _setup(tmp_path, driver())
    wrong = ledger.record("BMPX", "pwr_normal", "0x03", src.id, "Table 2: Register 0x03 STATUS")["fact"]["id"]
    res = SequenceBook(p.root).record(ledger, "BMPX", "power_up", "sensor_init",
                                      [{"op": "write", "reg": f["pwr"], "value": wrong, "fields": {"mode": "normal", "press_en": 1, "temp_en": 1}}],
                                      src.id, "Power-up: write softreset to CMD, wait 2 ms, then set PWR_CTRL.")
    assert not res["ok"] and "decodes as" in res["error"] and "mode=0 (sleep)" in res["error"], res


def test_device_without_register_map_fails_verify(tmp_path):
    p = Project("r2", tmp_path / "r2")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/y.pdf", "datasheet", DS, kind="pdf")
    ledger = FactLedger(p.root, store)
    a = ledger.record("BMPX", "i2c_address", "0x76", src.id, "The 7-bit I2C address is 0x76 when SDO is connected to GND.")["fact"]["id"]
    c = ledger.record("BMPX", "cmd_reg", "0x7E", src.id, "Table 5: Register 0x7E CMD")["fact"]["id"]
    v = ledger.record("BMPX", "softreset", "0xB6", src.id, "Bit 7..0 cmd 0xB6 softreset")["fact"]["id"]
    (p.root / "src").mkdir(exist_ok=True)
    (p.root / "src" / "main.cpp").write_text(driver(), encoding="utf-8")
    assert SequenceBook(p.root).record(ledger, "BMPX", "reset", "sensor_init", [{"op": "write", "reg": c, "value": v}], src.id,
                                       "Power-up: write softreset to CMD, wait 2 ms", device=a)["ok"]
    rep = verify_project(p.root, ledger)
    assert "REG_MAP_MISSING" in {x.code for x in rep.findings} and not rep.passed


# ------------------------------------------------------------------ what can be recorded


def _book(tmp_path):
    p = Project("r3", tmp_path / "r3")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/z.pdf", "datasheet", DS, kind="pdf")
    return p, store, FactLedger(p.root, store), src, RegisterBook(p.root)


@pytest.mark.parametrize("field, why", [
    ({"name": "mode", "bits": "4:3"}, "bits 4..3 are not stated"),
    ({"name": "power_mode", "bits": "5:4"}, "name is not in the quote"),
    ({"name": "mode", "bits": "5:4", "values": {"normal": "0b111"}}, "does not fit"),
])
def test_register_fields_must_match_the_table(tmp_path, field, why):
    p, store, ledger, src, book = _book(tmp_path)
    res = book.record_registers(store, ledger, "BMPX", src.id, [{"name": "PWR_CTRL", "addr": "0x1B", "quote": _q("Table 3", "11 normal mode"), "fields": [field]}])
    assert not res["ok"] and why in res["rejected"][0]["error"], res


def test_register_quoted_by_name_must_be_at_the_datasheet_address(tmp_path):
    p, store, ledger, src, book = _book(tmp_path)
    reg = {"name": "PWR_CTRL", "addr": "0x1C", "quote": "Bit 5..4 mode 00 sleep mode 01/10 forced mode 11 normal mode",
           "fields": [{"name": "mode", "bits": "5:4"}]}
    res = book.record_registers(store, ledger, "BMPX", src.id, [dict(reg, quote=_q("Table 3", "11 normal mode"))])
    assert not res["ok"] and "gives PWR_CTRL at 0x1B, not 0x1C" in res["rejected"][0]["error"], res
    # the quote names the field only: the address comes from where the datasheet states the register
    reg["fields"][0]["quote"] = reg["quote"]
    res = book.record_registers(store, ledger, "BMPX", src.id, [dict(reg, quote="Table 3: Register 0x1B PWR_CTRL")])
    assert not res["ok"], res
    res = book.record_registers(store, ledger, "BMPX", src.id, [dict(reg, addr="0x1B", quote="Table 3: Register 0x1B PWR_CTRL")])
    assert res["ok"], res


def test_unstated_value_names_are_dropped_not_the_register(tmp_path):
    p, store, ledger, src, book = _book(tmp_path)
    q = _q("Table 3", "11 normal mode").replace("“", '"')
    res = book.record_registers(store, ledger, "BMPX", src.id, [{"name": "PWR_CTRL", "addr": "0x1B", "quote": q, "fields": [
        {"name": "mode", "bits": "5:4", "values": {"normal": "0b10", "sleep": "00", "1": "01"}}]}])
    assert res["ok"] and "mode.normal" in res["notes"][0] and "mode.1" in res["notes"][0], res
    assert book.register_at(0x1B).field("mode").values == {"sleep": [0]}
    # a whole-register field needs no bit numbers, and quote marks do not have to match
    res = book.record_registers(store, ledger, "BMPX", src.id, [{"name": "CHIP_ID", "addr": "0x00", "quote": '0x00 "CHIP_ID" 0x50 chip_id',
                                                                  "id_value": "0x50", "fields": [{"name": "chip_id", "bits": "7:0"}]}])
    assert res["ok"] and "rejected" not in res, res


def test_layouts_must_be_lsb_first_and_signed_as_stated(tmp_path):
    p, store, ledger, src, book = _book(tmp_path)
    q = LAYOUTS[0]["quote"]
    res = book.record_layouts(store, "BMPX", src.id, [{"name": "NVM_PAR_T1", "bytes": ["0x32", "0x31"], "quote": q}])
    assert not res["ok"] and "least significant first" in res["rejected"][0]["error"], res
    res = book.record_layouts(store, "BMPX", src.id, [{"name": "NVM_PAR_T1", "bytes": ["0x31", "0x32"], "signed": True, "quote": q}])
    assert not res["ok"] and "unsigned" in res["rejected"][0]["error"], res
    res = book.record_layouts(store, "BMPX", src.id, [{"name": "NVM_PAR_T1", "bytes": ["0x31", "0x35"], "quote": q}])
    assert not res["ok"] and "0x35 is not in the quote" in res["rejected"][0]["error"], res


def test_formulas_need_their_constants_and_inputs_on_record(tmp_path):
    p, store, ledger, src, book = _book(tmp_path)
    assert book.record_layouts(store, "BMPX", src.id, LAYOUTS[:1])["ok"]
    fq = "Coefficients: PAR_T1 = NVM_PAR_T1 / 2^-8 and PAR_T2 = NVM_PAR_T2 / 2^30"
    assert "2^-6" in book.record_formula(store, "BMPX", src.id, "PAR_T1", ["PAR_T1 = NVM_PAR_T1 / 2^-6"], fq)["error"]
    assert "unknown name" in book.record_formula(store, "BMPX", src.id, "PAR_T2", ["PAR_T2 = NVM_PAR_T2 / 2^30"], fq)["error"]
    assert "not verbatim" in book.record_formula(store, "BMPX", src.id, "PAR_T1", ["PAR_T1 = NVM_PAR_T1 / 2^-8"], "PAR_T1 = NVM_PAR_T1 / 256 exactly")["error"]
    # a quote that skips a line says where it departs from the source and what the source has there
    err = book.record_formula(store, "BMPX", src.id, "PAR_T1", ["PAR_T1 = NVM_PAR_T1 / 2^-8"],
                              "Coefficients: PAR_T1 = NVM_PAR_T1 / 2^-8 and PAR_T3 = NVM_PAR_T3 / 2^48")["error"]
    assert "up to 'Coefficients: PAR_T1 = NVM_PAR_T1 / 2^-8 and PAR_T'" in err and "the source has '2 = NVM_PAR_T2" in err
    assert "write … where you leave text out" in err, err
    ok = book.record_formula(store, "BMPX", src.id, "PAR_T1", ["PAR_T1 = (float)calib->nvm_par_t1 / 2^-8;"], fq)
    assert ok["ok"] and "nvm_par_t1 / 2**-8" in ok["formula"], ok


BMP3_C = """static void parse_calib_data(const uint8_t *reg_data, struct bmp3_calib_data *calib_data)
{
    double temp_var;

    /* 1 / 2^8 */
    temp_var = 0.00390625f;
    reg_calib_data->par_t1 = BMP3_CONCAT_BYTES(reg_data[1], reg_data[0]);
    quantized_calib_data->par_t1 = ((double)reg_calib_data->par_t1 / temp_var);
    reg_calib_data->par_t2 = BMP3_CONCAT_BYTES(reg_data[3], reg_data[2]);
    temp_var = 1073741824.0f;
    quantized_calib_data->par_t2 = ((double)reg_calib_data->par_t2 / temp_var);
"""


def test_a_constant_the_quoted_code_sets_needs_no_step_of_its_own(tmp_path):
    p, store, ledger, src, book = _book(tmp_path)
    assert book.record_layouts(store, "BMPX", src.id, LAYOUTS[:1])["ok"]
    code = store.add("https://github.com/boschsensortec/BMP3_SensorAPI/blob/master/bmp3.c", "bmp3.c", BMP3_C)
    one = BMP3_C[BMP3_C.index("/* 1 / 2^8"):BMP3_C.index("reg_calib_data->par_t2")]
    res = book.record_formula(store, "BMPX", code.id, "PAR_T1", ["par_t1 = reg_calib_data->par_t1 / temp_var"], one,
                              inputs={"reg_calib_data->par_t1": "NVM_PAR_T1"})
    assert res["ok"] and "temp_var = 0.00390625; par_t1 = NVM_PAR_T1 / temp_var" in res["formula"], res
    # a quote that sets temp_var to two values does not say which one a step means
    both = BMP3_C[BMP3_C.index("/* 1 / 2^8"):]
    res = book.record_formula(store, "BMPX", code.id, "PAR_T1B", ["par_t1 = reg_calib_data->par_t1 / temp_var"], both,
                              inputs={"reg_calib_data->par_t1": "NVM_PAR_T1"})
    assert not res["ok"] and "unknown name(s) temp_var" in res["error"], res
    res = book.record_formula(store, "BMPX", code.id, "PAR_T1C", ["t1 = reg_par_t1 / temp_var"], one, inputs={"reg_par_t1": "NVM_PAR_T1"})
    assert "Names in this quote: temp_var, reg_calib_data->par_t1, reg_data, quantized_calib_data->par_t1." in res["error"], res


def test_a_vendor_power_helper_is_pow(tmp_path):
    p, store, ledger, src, book = _book(tmp_path)
    assert book.record_layouts(store, "BMPX", src.id, LAYOUTS)["ok"]
    code = store.add("https://github.com/boschsensortec/BMP3_SensorAPI/blob/master/bmp3.c", "bmp3.c",
                     "    partial_data1 = quantized_calib_data->par_t2 * quantized_calib_data->t_lin;\n"
                     "    partial_data2 = quantized_calib_data->par_t1 * pow_bmp3(quantized_calib_data->t_lin, 2);\n"
                     "    partial_out1 = partial_data1 + partial_data2;\n")
    quote = code.text.strip()
    fq = "Coefficients: PAR_T1 = NVM_PAR_T1 / 2^-8 and PAR_T2 = NVM_PAR_T2 / 2^30"
    for name in ("PAR_T1", "PAR_T2"):
        assert book.record_formula(store, "BMPX", src.id, name, [f"{name} = NVM_{name} / 2^{'-8' if name == 'PAR_T1' else '30'}"], fq)["ok"]
    inputs = {"quantized_calib_data->par_t1": "PAR_T1", "quantized_calib_data->par_t2": "PAR_T2", "quantized_calib_data->t_lin": "UT"}
    as_written = [s.strip() for s in quote.split(";") if s.strip()]
    assert book.record_formula(store, "BMPX", code.id, "OUT1", as_written, quote, inputs=inputs)["ok"]
    squared = ["partial_data1 = par_t2 * t_lin", "partial_data2 = par_t1 * t_lin ** 2", "partial_out1 = partial_data1 + partial_data2"]
    assert book.record_formula(store, "BMPX", code.id, "OUT1B", squared, quote, inputs=inputs)["ok"]
    cubed = ["partial_data1 = par_t2 * t_lin", "partial_data2 = par_t1 * t_lin ** 3", "partial_out1 = partial_data1 + partial_data2"]
    assert "does not compute what the quote states" in book.record_formula(store, "BMPX", code.id, "OUT1C", cubed, quote, inputs=inputs)["error"]


def test_formula_must_compute_what_the_quote_states(tmp_path):
    p, store, ledger, src, book = _book(tmp_path)
    assert book.record_layouts(store, "BMPX", src.id, LAYOUTS)["ok"]
    fq = "Coefficients: PAR_T1 = NVM_PAR_T1 / 2^-8 and PAR_T2 = NVM_PAR_T2 / 2^30"
    # every one of these reuses the quote's numbers, and every one is a different formula
    for wrong in ("PAR_T1 = NVM_PAR_T1 / 2^8", "PAR_T1 = NVM_PAR_T1 * 2^-8", "PAR_T1 = NVM_PAR_T1 / 2^30"):
        r = book.record_formula(store, "BMPX", src.id, "PAR_T1", [wrong], fq)
        assert not r["ok"] and "does not compute what the quote states" in r["error"], (wrong, r)
    assert book.record_formula(store, "BMPX", src.id, "PAR_T1", ["PAR_T1 = NVM_PAR_T1 * 256"], fq)["ok"]   # the same value


def test_formula_structure_reads_lost_superscripts_and_ignores_what_does_not_parse():
    from embedforge.truth.registers import formula_structure_problem
    q = "PAR_P1 = (NVM_PAR_P1 - 214) / 220"   # a PDF text layer without superscripts
    assert formula_structure_problem(["PAR_P1 = (NVM_PAR_P1 - 2^14) / 2^20"], q, "PAR_P1") is None
    assert "does not compute" in formula_structure_problem(["PAR_P1 = NVM_PAR_P1 - 2^14 / 2^20"], q, "PAR_P1")
    assert formula_structure_problem(["X = (A - B) / C"], "X = A - B\nC", "X") is None        # fraction bar lost: no claim
    assert formula_structure_problem(["T = 2^osr * 2000"], "T = 2<sup>osr</sup> * 2000", "T") is None
    # steps are compared in order with the quote's own sequence
    seq = "partial_data1 = uncomp_temp - par_t1; partial_data2 = partial_data1 * par_t2; t_lin = partial_data2 + partial_data1 * partial_data1 * par_t3"
    good = ["partial_data1 = uncomp_temp - par_t1", "partial_data2 = partial_data1 * par_t2",
            "calib_data->t_lin = partial_data2 + (partial_data1 * partial_data1) * par_t3"]
    assert formula_structure_problem(good, seq, "T_LIN") is None
    assert "step 3" in formula_structure_problem(good[:2] + ["t_lin = partial_data2 + partial_data1 * par_t3"], seq, "T_LIN")


def test_value_names_take_only_the_adjacent_value():
    from embedforge.truth.registers import _value_near
    row = "Bit 5..4 mode 00 sleep mode 01/10 forced mode 11 normal mode"
    assert _value_near(row, "sleep", 0, 2) and not _value_near(row, "sleep", 1, 2)   # 01 is forced's
    assert _value_near(row, "forced", 1, 2) and _value_near(row, "forced", 2, 2) and not _value_near(row, "forced", 3, 2)
    md = "| Bit 5..4 | mode | 01/10 | forced mode |  |\n|  |  | 11 normal | mode |  |"
    assert _value_near(md, "forced", 2, 2) and _value_near(md, "normal", 3, 2) and not _value_near(md, "normal", 2, 2)
    text = "normal mode = 0b11, sleep: 00"
    assert _value_near(text, "normal", 3, 2) and _value_near(text, "sleep", 0, 2)


def test_register_written_without_fields_is_an_error(tmp_path):
    p, store, ledger, src, f = _setup(tmp_path, driver())
    r = RegisterBook(p.root).record_registers(store, ledger, "BMPX", src.id, [{"name": "PWR_CTRL", "addr": "0x1B", "quote": _q("Table 3", "11 normal mode")}])
    assert r["ok"], r
    summary, codes, found = _codes(p, ledger)
    assert "REG_FIELDS_MISSING" in codes, codes


def test_a_check_that_crashes_fails_verify(tmp_path, monkeypatch):
    import embedforge.truth.registers as regs
    p, store, ledger, src, f = _setup(tmp_path, driver())

    def boom(*a, **k):
        raise RuntimeError("tracer bug")
    monkeypatch.setattr(regs, "check_registers", boom)
    rep = verify_project(p.root, ledger)
    assert not rep.passed and any(x.code == "REGISTER_CHECK_ERROR" and x.severity == "ERROR" for x in rep.findings)


def test_fact_quotes_may_skip_text_between_close_verbatim_pieces_and_match_script_digits(tmp_path):
    p = Project("r7", tmp_path / "r7")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/y.pdf", "datasheet",
                    "Table 3: Register 0x00 “CHIP_ID”\n|  | Name | Description |\n|---|---|---|\n"
                    "| Bit 7..0 | chip_id | Chip identification code, fixed to 0x50 |\nThe I²C interface runs at 400 kHz.",
                    kind="pdf")
    ledger = FactLedger(p.root, store)
    # a table caption and its row, the layout between left out
    assert ledger.record("BMPX", "chip_id", "0x50", src.id, "Register 0x00 CHIP_ID … Chip identification code, fixed to 0x50")["ok"]
    # the value must stand in a quoted piece, not in the text the '…' leaves out
    r = ledger.record("BMPX", "chip_id_reg", "0x00", src.id, "Table 3: Register … identification code, fixed to 0x50")
    assert not r["ok"] and "does not appear inside the quoted text" in r["error"], r
    assert not ledger.record("BMPX", "chip_id2", "0x50", src.id, "Table 3 … 0x50")["ok"]   # pieces too short to anchor
    # I2C for I²C, and a quote double-escaped by the model (a literal backslash-u sequence)
    assert ledger.record("BMPX", "i2c_speed", "400", src.id, "The I2C interface runs at 400 kHz.", "kHz")["ok"]
    assert ledger.record("BMPX", "i2c_speed_b", "400", src.id, "The I\\u00b2C interface runs at 400 kHz.", "kHz")["ok"]


def test_quotes_are_anchored_to_whole_words_and_the_value_and_unit_are_read_from_the_source(tmp_path):
    p = Project("r6", tmp_path / "r6")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/z.pdf", "datasheet",
                    "After reset wait 12 ms after power-on. Operating range -40 to 85 °C. Start-up time | 2 | ms |", kind="pdf")
    ledger = FactLedger(p.root, store)
    r = ledger.record("BMPX", "startup", "2", src.id, "2 ms after power-on")          # a substring of '12 ms'
    assert not r["ok"] and "inside a word or number" in r["error"], r
    assert not ledger.record("BMPX", "t_min", "40", src.id, "Operating range -40 to 85")["ok"]   # minus forty
    assert ledger.record("BMPX", "t_max", "85", src.id, "Operating range -40 to 85")["ok"]
    r = ledger.record("BMPX", "startup", "2", src.id, "Start-up time | 2 | ms", unit="us")
    assert not r["ok"] and "gives 2 ms" in r["error"], r
    assert ledger.record("BMPX", "startup", "2", src.id, "Start-up time | 2 | ms", unit="ms")["ok"]
    from embedforge.truth.registers import verbatim
    assert verbatim("0x1B PWR_CTRL", DS) and not verbatim("1B PWR_CTRL", DS)


def test_delay_unit_comes_from_the_quote_before_the_steps_hint():
    from types import SimpleNamespace
    from embedforge.truth.sequences import delay_us
    fact = SimpleNamespace(value="2", unit="", quote="write softreset to CMD, wait 2 ms")
    assert delay_us(fact, "us") == 2000


def test_a_quote_running_into_the_next_register_does_not_lend_its_fields(tmp_path):
    p, store, ledger, src, book = _book(tmp_path)
    q = _q("Table 3", "Bit 5..3 osr_t 000 x1")   # PWR_CTRL's table, then OSR's
    res = book.record_registers(store, ledger, "BMPX", src.id, [{"name": "PWR_CTRL", "addr": "0x1B", "quote": q,
                                                                 "fields": [{"name": "osr_t", "bits": "5:3"}]}])
    assert not res["ok"] and "osr_t" in res["rejected"][0]["error"], res


def test_two_parts_with_a_register_at_the_same_address_are_kept_apart(tmp_path):
    p, store, ledger, src, f = _setup(tmp_path, driver(read=GOOD_READ + " wr2(0x1B, 0x18);"), formulas=False)
    imu = store.add("https://invensense.tdk.com/imux-datasheet.pdf", "IMU datasheet",
                    "The I2C address of the IMUX is 0x68.\nRegister 0x1B GYRO_CONFIG\nBit 4..3 fs_sel full scale\n", kind="pdf")
    assert ledger.record("IMUX", "i2c_address", "0x68", imu.id, "The I2C address of the IMUX is 0x68.")["ok"]
    r = RegisterBook(p.root).record_registers(store, ledger, "IMUX", imu.id, [
        {"name": "GYRO_CONFIG", "addr": "0x1B", "quote": "Register 0x1B GYRO_CONFIG\nBit 4..3 fs_sel", "fields": [{"name": "fs_sel", "bits": "4:3"}]}])
    assert r["ok"], r
    code = driver(read=GOOD_READ + " wr2(0x1B, 0x18);").replace(
        "void sensor_init() {", "static void wr2(uint8_t r, uint8_t v) { Wire.beginTransmission(0x68); Wire.write(r); Wire.write(v); "
                                "Wire.endTransmission(); }\nvoid sensor_init() {")
    (p.root / "src" / "main.cpp").write_text(code, encoding="utf-8")
    summary, codes, found = _codes(p, ledger)
    assert any("GYRO_CONFIG (0x1B) <- 0x18" in w for w in summary["writes"]), summary["writes"]
    assert not any("PWR_CTRL (0x1B) <- 0x18" in w for w in summary["writes"]), summary["writes"]


SPI_DRIVER = r"""
#include "hardware/spi.h"
static double up;
void setup() {
  uint8_t tx[5] = {0x84, 0, 0, 0, 0};   /* read from 0x04: command byte, dummy byte, then 3 data bytes */
  uint8_t rx[5];
  spi_write_read_blocking(spi0, tx, rx, 5);
  ASSEMBLE
}
"""


@pytest.mark.parametrize("assemble, wrong", [
    ("up = (double)((uint32_t)rx[2] | ((uint32_t)rx[3] << 8) | ((uint32_t)rx[4] << 16));", False),   # BMP388 SPI: dummy byte
    ("up = (double)((uint32_t)rx[1] | ((uint32_t)rx[2] << 8) | ((uint32_t)rx[3] << 16));", False),   # a part without one
    ("up = (double)(((uint32_t)rx[2] << 8) | (uint32_t)rx[3]);", True),                                # wrong either way
])
def test_spi_dummy_byte_is_judged_both_ways(tmp_path, assemble, wrong):
    p, store, ledger, src, f = _setup(tmp_path, SPI_DRIVER.replace("ASSEMBLE", assemble), formulas=False)
    summary, codes, found = _codes(p, ledger)
    assert ("LAYOUT_SHIFT" in codes) == wrong, [(x.code, x.message) for x in found]


def test_final_sigma_does_not_switch_anchoring_off(tmp_path):
    p = Project("r7", tmp_path / "r7")
    store = SourceStore(p.root)
    src = store.add("https://www.ti.com/lit/ds/ads1115.pdf", "datasheet",
                    "The ADS1115 is a precision delta-sigma (ΔΣ) ADC. Data rate 860 SPS. Gain 128 V/V.", kind="pdf")
    ledger = FactLedger(p.root, store)
    assert not ledger.record("ADS", "rate", "128", src.id, "Data rate 860 SPS.")["ok"]   # 128 is elsewhere in the text
    assert ledger.record("ADS", "rate", "860", src.id, "Data rate 860 SPS.")["ok"]


def test_units_and_ranges_read_every_occurrence(tmp_path):
    from types import SimpleNamespace
    from embedforge.truth.ledger import value_in_text
    from embedforge.truth.sequences import delay_us
    p = Project("r8", tmp_path / "r8")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/y.pdf", "datasheet", "| tPOR | power-on time | VDD = 3 V | 3 | ms |", kind="pdf")
    ledger = FactLedger(p.root, store)
    assert ledger.record("BMPX", "t_por", "3", src.id, "power-on time | VDD = 3 V | 3 | ms", unit="ms")["ok"]
    assert delay_us(SimpleNamespace(value="3", unit="", quote="VDD = 3 V | 3 | ms"), "ms") == 3000
    assert delay_us(SimpleNamespace(value="3", unit="", quote="VDD = 3 V"), "ms") == 3000   # a voltage is no time unit
    assert value_in_text("3.6", "Operating range 1.71 V -3.6 V") and not value_in_text("40", "range: -40 to 85")


def test_own_section_keeps_rows_the_pdf_put_after_the_next_header():
    from embedforge.truth.registers import own_section
    q = ("Table 41: Register 0x1C OSR\n4.3.18 Register 0x1D ODR\nTable 42: Register 0x1D ODR\n"
         "Bit 2..0 osr_p 000 x1\nBit 5..3 osr4_t 000 x1")
    assert "osr_p" in own_section(q, 0x1C)


def test_value_lists_with_and_and_name_first_rows():
    from embedforge.truth.registers import _value_near
    row = "00 Sleep mode 01 and 10 Forced mode 11 Normal mode"
    assert _value_near(row, "forced", 1, 2) and _value_near(row, "forced", 2, 2) and not _value_near(row, "sleep", 1, 2)
    assert _value_near("Normal mode 11", "normal", 3, 2)


def test_formula_structure_checks_the_result_and_accepts_integer_division_and_inputs():
    from embedforge.truth.registers import formula_structure_problem
    q = "var1 = a + b; var2 = a - b; T = (var1 + var2) / 5120.0"
    wrong = ["var1 = a + b", "var2 = a - b", "temp_c = (var1 - var2) / 5120.0"]
    assert "not what the quote computes for T" in formula_structure_problem(wrong, q, "T")
    assert "none of the values" in formula_structure_problem(wrong, q, "TEMP")
    assert formula_structure_problem(wrong[:2] + ["temp_c = (var1 + var2) / 5120.0"], q, "TEMP") is None
    assert formula_structure_problem(["partial_data6 = (x * y) // 1000"], "partial_data6 = (x * y) / 1000;", "P") is None
    assert formula_structure_problem(["partial_data1 = uncomp_temp - PAR_T1"],
                                     "PAR_T1 = NVM_PAR_T1 / 2^-8; partial_data1 = uncomp_temp - PAR_T1;", "P") is None
    assert formula_structure_problem(["y = x**-8"], "y = x**-8", "Y") is None


def test_formula_binds_the_quotes_own_names(tmp_path):
    p, store, ledger, src, book = _book(tmp_path)
    assert book.record_layouts(store, "BMPX", src.id, LAYOUTS)["ok"]
    fq = "Coefficients: PAR_T1 = NVM_PAR_T1 / 2^-8 and PAR_T2 = NVM_PAR_T2 / 2^30"
    assert book.record_formula(store, "BMPX", src.id, "PAR_T1", ["PAR_T1 = NVM_PAR_T1 / 2^-8"], fq)["ok"]
    assert book.record_formula(store, "BMPX", src.id, "PAR_T2", ["PAR_T2 = NVM_PAR_T2 / 2^30"], fq)["ok"]
    steps = ["partial = (float)(uncomp_temp – calib_data->par_t1)", "T = partial * calib_data->par_t2"]
    q = "partial = (float)(uncomp_temp - calib_data->par_t1); T = partial * calib_data->par_t2;"
    bad = book.record_formula(store, "BMPX", src.id, "T", steps, q)
    assert not bad["ok"] and "inputs={'uncomp_temp'" in bad["error"], bad
    ok = book.record_formula(store, "BMPX", src.id, "T", steps, q, inputs={"uncomp_temp": "UT"})
    assert ok["ok"] and "partial = (ut - par_t1)" in ok["formula"].lower(), ok


def test_formula_cannot_swap_a_converted_coefficient_for_the_raw_register(tmp_path):
    p, store, ledger, src, book = _book(tmp_path)
    assert book.record_layouts(store, "BMPX", src.id, LAYOUTS)["ok"]
    q = "partial = (float)(uncomp_temp - calib_data->par_t1); T = partial * calib_data->par_t2;"
    # the model writes the raw names itself: they are not what the quote says
    swapped = book.record_formula(store, "BMPX", src.id, "T", ["partial = UT - NVM_PAR_T1", "T = partial * NVM_PAR_T2"], q)
    assert not swapped["ok"] and "NVM_PAR_T1" in swapped["error"] and "do not appear" in swapped["error"].replace("do(es)", "do"), swapped
    # or binds the quote's par_t1 to the raw register content
    bound = book.record_formula(store, "BMPX", src.id, "T", ["partial = uncomp_temp - calib_data->par_t1", "T = partial * calib_data->par_t2"],
                                q, inputs={"uncomp_temp": "UT", "par_t1": "NVM_PAR_T1", "par_t2": "NVM_PAR_T2"})
    assert not bound["ok"] and "converted coefficient" in bound["error"], bound
    # a name the quote marks as raw register content may be bound to it
    raw = book.record_formula(store, "BMPX", src.id, "PAR_T1", ["temp = reg_calib_data->par_t1 / 0.00390625"],
                              "par_t1 = (double)reg_calib_data->par_t1 / 0.00390625;", inputs={"reg_calib_data->par_t1": "NVM_PAR_T1"})
    assert raw["ok"], raw


def test_several_formulas_in_one_call(tmp_path):
    from embedforge.agent.context import digest
    p = Project("r6", tmp_path / "r6")
    store = SourceStore(p.root)
    store.add("https://www.bosch-sensortec.com/x.pdf", "datasheet", DS, kind="pdf")
    code = store.add("https://github.com/boschsensortec/BMP3_SensorAPI/blob/master/bmp3.c", "bmp3.c", BMP3_C)
    belt = ToolBelt(p, RunSession("r", p.id), store, FactLedger(p.root, store))
    assert belt.call("record_layouts", {"device": "BMPX", "source_id": "SRC-001", "layouts": LAYOUTS})["ok"]
    t1 = BMP3_C[BMP3_C.index("/* 1 / 2^8"):BMP3_C.index("reg_calib_data->par_t2")]
    t2 = BMP3_C[BMP3_C.index("temp_var = 1073741824"):]
    res = belt.call("record_formula", {"device": "BMPX", "source_id": code.id, "formulas": [
        {"name": "PAR_T1", "steps": ["par_t1 = reg_calib_data->par_t1 / temp_var"], "quote": t1, "inputs": {"reg_calib_data->par_t1": "NVM_PAR_T1"}},
        {"name": "PAR_T2", "steps": ["par_t2 = reg_calib_data->par_t2 / temp_var"], "quote": t2, "inputs": {"reg_calib_data->par_t2": "NVM_PAR_T9"}}]})
    assert res["ok"] and res["recorded"][0].startswith("FRM-001 PAR_T1:") and res["rejected"][0]["item"] == "PAR_T2", res
    assert "resend only the rejected" in res["note"]
    assert digest("record_formula", {}, res).startswith("record_formula -> FRM-001 PAR_T1; 1 rejected, e.g. PAR_T2")


def test_formula_quoted_from_another_stored_source_is_recorded_with_it(tmp_path):
    p = Project("r5", tmp_path / "r5")
    store = SourceStore(p.root)
    store.add("https://github.com/boschsensortec/BMP3_SensorAPI/blob/master/bmp3.c", "bmp3.c", "int x = 1; /* other code */", kind="code")
    ds = store.add("https://www.bosch-sensortec.com/x.pdf", "datasheet", DS, kind="pdf")
    belt = ToolBelt(p, RunSession("r", p.id), store, FactLedger(p.root, store))
    assert belt.call("record_layouts", {"device": "BMPX", "source_id": "SRC-001", "layouts": LAYOUTS[:1]})["ok"]   # cites the wrong SRC
    r = belt.call("record_formula", {"device": "BMPX", "source_id": "SRC-001", "name": "PAR_T1", "steps": ["PAR_T1 = NVM_PAR_T1 / 2^-8"],
                                     "quote": "PAR_T1 = NVM_PAR_T1 / 2^-8"})
    assert r["ok"] and ds.id in r["note"], r


def test_garbled_pdf_exponents_count_as_the_constant():
    from embedforge.truth.registers import _const_in_quote
    garbled = "𝑃𝑃𝑃𝑃𝑃𝑃_𝑇𝑇1 = 𝑁𝑁𝑁𝑁𝑁𝑁_𝑃𝑃𝑃𝑃𝑃𝑃_𝑇𝑇1\n2−8  𝑃𝑃𝑃𝑃𝑃𝑃_𝑇𝑇2 = 𝑁𝑁𝑁𝑁𝑁𝑁_𝑃𝑃𝑃𝑃𝑃𝑃_𝑇𝑇2\n230"
    assert _const_in_quote(garbled, 2.0 ** -8, -8) and _const_in_quote(garbled, 2.0 ** 30, 30)
    assert _const_in_quote("temp_var = 0.00390625f;", 2.0 ** -8, -8)
    assert not _const_in_quote(garbled, 2.0 ** 31, 31)


def test_burst_range_must_be_on_record_first(tmp_path):
    p, store, ledger, src, book = _book(tmp_path)
    res = book.record_registers(store, ledger, "BMPX", src.id, [], bursts=[{"start": "0x04", "end": "0x09", "quote": "all data registers are read in a single burst read"}])
    assert not res["ok"] and "record_registers / record_layouts" in res["rejected"][0]["error"], res


def test_tools_record_and_verify_shows_meaning(tmp_path, monkeypatch):
    from embedforge.toolchain import pio
    from embedforge.toolchain.pio import BuildResult
    p = Project("r4", tmp_path / "r4")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/w.pdf", "datasheet", DS, kind="pdf")
    ledger = FactLedger(p.root, store)
    belt = ToolBelt(p, RunSession("r", p.id), store, ledger)
    res = belt.call("record_facts", {"source_id": "src001", "facts": [
        {"subject": "BMPX", "property": "i2c_address", "value": "0x76", "quote": "The 7-bit I2C address is 0x76 when SDO is connected to GND."},
        {"subject": "BMPX", "property": "chip_id", "value": "0x51", "quote": "0x00 CHIP_ID 0x50 chip_id"}]})
    assert res["ok"] and len(res["recorded"]) == 1 and res["rejected"][0]["item"] == "BMPX.chip_id", res
    early = belt.call("record_registers", {"device": "BMPX", "source_id": "SRC-001", "registers": REGISTERS,
                                           "bursts": [{"name": "data", "start": "0x04", "end": "0x09", "quote": "read in a single burst read"}]})
    assert early["ok"] and len(early["recorded"]) == 5 and "record_layouts" in early["rejected"][0]["error"], early   # burst needs the data layout
    assert belt.call("record_layouts", {"device": "BMPX", "source_id": "SRC-001", "layouts": LAYOUTS})["ok"]
    r = belt.call("record_registers", {"device": "BMPX", "source_id": "SRC-001", "registers": REGISTERS,
                                       "bursts": [{"name": "data", "start": "0x04", "end": "0x09", "quote": "read in a single burst read"}]})
    assert r["ok"] and len(r["recorded"]) == 5 and r["bursts"], r
    import json
    again = belt.call("record_layouts", {"device": "BMPX", "source_id": "SRC-001", "layouts": json.dumps(LAYOUTS[:1])})  # list sent as a string
    assert again["ok"] and again["recorded"], again
    f = belt.call("record_formula", {"device": "BMPX", "source_id": "SRC-001", "name": "PAR_T1", "steps": ["PAR_T1 = NVM_PAR_T1 / 2^-8"],
                                     "quote": "PAR_T1 = NVM_PAR_T1 / 2^-8"})
    assert f["ok"], f
    (p.root / "src").mkdir(exist_ok=True)
    (p.root / "src" / "main.cpp").write_text(driver(pwr="0x03"), encoding="utf-8")
    monkeypatch.setattr(pio, "build", lambda root, on_line=None: BuildResult(ok=True))
    belt.build()
    v = belt.verify()
    assert any("mode=0 (sleep)" in w for w in v["meaning"]["register_writes"]), v.get("meaning")
    assert any("PAR_T1: matches the code" in x for x in v["meaning"]["formulas"]), v["meaning"]


def test_fact_value_commentary_is_dropped_and_repeated_rejections_stop(tmp_path):
    p, store, ledger, src, f = _setup(tmp_path, driver())
    belt = ToolBelt(p, RunSession("r", p.id), store, ledger)
    r = belt.call("record_fact", {"subject": "BMPX", "property": "addr_sdo_gnd", "value": "0x76 (SDO tied to GND)",
                                  "source_id": src.id, "quote": "The 7-bit I2C address is 0x76 when SDO is connected to GND."})
    assert r["ok"] and r["fact"]["value"] == "0x76", r
    bad = {"subject": "BMPX", "property": "x", "value": "0x99", "source_id": src.id, "quote": "The 7-bit I2C address is 0x76"}
    assert "does not appear" in belt.call("record_fact", bad)["error"]
    assert "does not appear" in belt.call("record_fact", bad)["error"]
    assert "already rejected 2 times" in belt.call("record_fact", bad)["error"]


def test_rename_file_and_identical_replace(tmp_path):
    p = Project("r6", tmp_path / "r6")
    (p.root / "src").mkdir(parents=True, exist_ok=True)
    (p.root / "src" / "d.c").write_text("#include <Wire.h>\nvoid f(void){ Wire.beginTransmission(1); }\n", encoding="utf-8")
    store = SourceStore(p.root)
    belt = ToolBelt(p, RunSession("r", p.id), store, FactLedger(p.root, store))
    assert "identical" in belt.call("replace_in_file", {"path": "src/d.c", "old": "f", "new": "f"})["error"]
    assert belt.call("rename_file", {"path": "src/d.c", "new_path": "src/d.cpp"})["ok"]
    assert sorted(x.name for x in (p.root / "src").iterdir()) == ["d.cpp"]
    assert "error" in belt.call("rename_file", {"path": "src/d.cpp", "new_path": "../evil.cpp"})


# ------------------------------------------------------------------ the C the tracer computes


@pytest.mark.parametrize("typ, v, out", [("int8_t", 0xF0, -16), ("uint8_t", -1, 255), ("int16_t", 0x8001, -32767), ("uint16_t", 70000, 4464),
                                        ("float", 3, 3.0), ("int32_t", 2.9, 2), ("int", -2.9, -2), ("unsigned int", -1, 0xFFFFFFFF),
                                        ("bool", 7, 1), ("char", 300, 44), ("struct x", 5, 5)])
def test_c_conversions(typ, v, out):
    assert c_convert(typ, v) == out and type(c_convert(typ, v)) is type(out)


def test_probe_run_computes_like_c(tmp_path):
    code = r"""
    #include <Wire.h>
    static int16_t g;
    static float scale;
    static void rd(uint8_t r, uint8_t *b, uint8_t n) { Wire.beginTransmission(0x76); Wire.write(r); Wire.endTransmission(false);
      Wire.requestFrom(0x76, n); for (uint8_t i = 0; i < n; i++) b[i] = Wire.read(); }
    void setup() { uint8_t b[2]; rd(0x10, b, 2); g = (b[1] << 8) | b[0]; int q = g / 7; int m = g % 7; scale = g * 0.5f; }
    """
    m = FlowModel({"src/a.cpp": code})
    w = m.run(["setup"], probe={0x10: 0x01, 0x11: 0xFF})          # 0xFF01 = -255 as int16
    vals = {where: v for v, _, where in w.values}
    assert vals["g"] == -255 and vals["q"] == -36 and vals["m"] == -3 and vals["scale"] == -127.5, vals
    shifts = FlowModel({"src/a.cpp": code}).run(["setup"]).obs_shifts
    assert sorted((rx.reg, s) for rx, s, _f, _l in shifts) == [(0x10, 0), (0x11, 8)]


def test_memcpy_carries_read_bytes(tmp_path):
    code = r"""
    #include <string.h>
    #include "hardware/i2c.h"
    static uint8_t raw[4];
    static int32_t v;
    void main(void) { uint8_t r = 0x20, tmp[4]; i2c_write_blocking(i2c0, 0x76, &r, 1, true); i2c_read_blocking(i2c0, 0x76, tmp, 4, false);
      memcpy(raw, tmp, 4); memset(tmp, 0, 4); v = (raw[3] << 8) | raw[2]; }
    """
    w = FlowModel({"src/a.c": code}).run(["main"], probe={0x22: 0x34, 0x23: 0x12})
    assert ({where: x for x, _, where in w.values})["v"] == 0x1234
