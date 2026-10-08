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
    ok = book.record_formula(store, "BMPX", src.id, "PAR_T1", ["PAR_T1 = (float)calib->nvm_par_t1 / 2^-8;"], fq)
    assert ok["ok"] and "nvm_par_t1 / 2**-8" in ok["formula"], ok


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
