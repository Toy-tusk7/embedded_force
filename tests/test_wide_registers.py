"""Registers wider than a byte (TI's TMP117: 16-bit registers, most significant byte first): recorded with their width
and the datasheet's byte order, then checked on the code as 16-bit values — fields, reserved bits, masks, formulas — plus
the bugs only wide registers have: bytes assembled swapped, a register written or read in part."""

from pathlib import Path

from embedforge.agent.workspace import Project
from embedforge.truth import FactLedger, SourceStore
from embedforge.truth.flow import FlowModel
from embedforge.truth.registers import RegisterBook, check_registers, coverage_findings, stated_byte_order
from embedforge.truth.sequences import SequenceBook, check_flows

DS = """7.5.3 Bus overview
Register bytes are sent with the most significant byte first, followed by the least significant byte.
The 7-bit I2C address is 0x48 when ADD0 is connected to Ground.
Table 7-5. Register Map
ADDRESS TYPE RESET ACRONYM REGISTER NAME
00h R 8000h Temp_Result Temperature result register: 16 bits, two's complement format, resolution 7.8125 m°C
01h R/W 0220h Configuration Configuration register
0Fh R 0117h Device_ID Device ID register
Configuration Register Field Descriptions
BIT FIELD TYPE RESET DESCRIPTION
15 HIGH_Alert R 0 High alert flag
13 Data_Ready R 0 Data ready flag
11:10 MOD[1:0] R/W 0 Set conversion mode. 00: continuous conversion 01: shutdown 11: one-shot conversion
9:7 CONV[2:0] R/W 100 Conversion cycle bit.
6:5 AVG[1:0] R/W 01 Conversion averaging modes.
Temperature: T = Temp_Result * 0.0078125
One LSB equals 7.8125 m°C. One count is 7.8125 degrees.
Start-up: wait 2 ms after power-up, then write the configuration register.
"""

CODE = r"""
#include <Arduino.h>
#include <Wire.h>
class TMPX {
public:
    static constexpr uint8_t ADDRESS = 0x48;
    static constexpr uint8_t REG_TEMP = 0x00;
    static constexpr uint8_t REG_CONFIG = 0x01;
    static constexpr uint8_t REG_ID = 0x0F;
    static constexpr uint16_t ID = 0x0117;
    static constexpr uint16_t DATA_READY = DRDY;
    TMPX(uint8_t address = ADDRESS) : _address(address) {}
    bool begin();
    bool read(float &t);
private:
    uint8_t _address;
    bool wr(uint8_t reg, uint16_t v);
    bool rd(uint8_t reg, uint16_t &v);
};
bool TMPX::wr(uint8_t reg, uint16_t v) {
    Wire.beginTransmission(_address);
    Wire.write(reg);
    Wire.write((uint8_t)(v >> 8));
    Wire.write((uint8_t)(v & 0xFF));
    return Wire.endTransmission() == 0;
}
bool TMPX::rd(uint8_t reg, uint16_t &v) {
    Wire.beginTransmission(_address);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) return false;
    if (Wire.requestFrom(_address, (uint8_t)2) != 2) return false;
    uint8_t b[2];
    b[0] = Wire.read();
    b[1] = Wire.read();
    v = (uint16_t)((b[0] << 8) | b[1]);
    return true;
}
bool TMPX::begin() {
    Wire.begin();
    delay(2);
    uint16_t id = 0;
    if (!rd(REG_ID, id) || id != ID) return false;
    return wr(REG_CONFIG, CONFIG);
}
bool TMPX::read(float &t) {
    uint16_t cfg = 0;
    if (!rd(REG_CONFIG, cfg) || !(cfg & DATA_READY)) return false;
    uint16_t raw = 0;
    if (!rd(REG_TEMP, raw)) return false;
    t = (int16_t)raw * 0.0078125f;
    return true;
}
TMPX sensor;
float temperature;
void setup() { sensor.begin(); }
void loop() {
    if (sensor.read(temperature)) Serial.println(temperature);
    delay(1000);
}
"""

ORDER = "Register bytes are sent with the most significant byte first, followed by the least significant byte."
FIELDS = DS[DS.index("Configuration Register Field Descriptions"):DS.index("Conversion averaging modes.") + len("Conversion averaging modes.")]
REGISTERS = [
    {"name": "Temp_Result", "addr": "0x00", "width": "16", "byte_order_quote": ORDER,
     "quote": "00h R 8000h Temp_Result Temperature result register", "fields": [{"name": "Temp_Result", "bits": "15:0"}]},
    {"name": "Configuration", "addr": "0x01", "width": "16", "quote": FIELDS, "reset": "0x0220",
     "reset_quote": "01h R/W 0220h Configuration Configuration register",
     "fields": [{"name": "HIGH_Alert", "bits": "15"}, {"name": "Data_Ready", "bits": "13"},
                {"name": "MOD", "bits": "11:10", "values": {"continuous": "00", "shutdown": "01", "one-shot": "11"}},
                {"name": "CONV", "bits": "9:7"}, {"name": "AVG", "bits": "6:5"}]},
    {"name": "Device_ID", "addr": "0x0F", "width": "16", "id_value": "0x0117", "quote": "0Fh R 0117h Device_ID Device ID register",
     "fields": [{"name": "Device_ID", "bits": "15:0"}]},
]


def code(**subs):
    out = CODE.replace("DRDY", "0x2000").replace("CONFIG)", "0x0220)")
    for old, new in subs.items():
        assert old in out, old
        out = out.replace(old, new)
    return out


def _setup(tmp_path: Path, src_code: str, registers=None):
    p = Project("w1", tmp_path / "w1")
    store = SourceStore(p.root)
    src = store.add("https://www.ti.com/lit/ds/x.pdf", "datasheet", DS, kind="pdf")
    ledger = FactLedger(p.root, store)
    assert ledger.record("TMPX", "i2c_address", "0x48", src.id, "The 7-bit I2C address is 0x48 when ADD0 is connected to Ground.")["ok"]
    book = RegisterBook(p.root)
    r = book.record_registers(store, ledger, "TMPX", src.id, registers or REGISTERS)
    assert r["ok"] and "rejected" not in r, r
    r = book.record_layouts(store, "TMPX", src.id, [{"name": "Temp_Result", "bytes": ["0x00"], "signed": True,
                                                     "quote": DS[DS.index("00h R 8000h"):DS.index("m°C") + 3]}])
    assert r["ok"] and "rejected" not in r, r
    assert book.record_formula(store, "TMPX", src.id, "T", ["T = Temp_Result * 0.0078125"],
                               "Temperature: T = Temp_Result * 0.0078125")["ok"]
    (p.root / "src").mkdir(exist_ok=True)
    (p.root / "src" / "main.cpp").write_text(src_code, encoding="utf-8")
    return p, store, ledger, src


def _codes(p, ledger):
    model = FlowModel.from_dir(p.root)
    summary, found = check_registers(p.root, ledger, model)
    found += coverage_findings(p.root, ledger, model)
    return summary, sorted({f.code for f in found if f.severity == "ERROR"}), found


def test_byte_order_is_read_from_the_sentence_that_states_it():
    assert stated_byte_order(ORDER) == "msb_first"
    assert stated_byte_order("The LSB byte is transmitted first, then the MSB byte.") == "lsb_first"
    assert stated_byte_order("The first byte is the most significant byte of the register.") == "msb_first"
    # bit order, not byte order: says nothing about which byte of a register comes first
    assert stated_byte_order("Data is shifted out MSB first on SDO.") == ""
    assert stated_byte_order("The pointer register is the first byte after the address.") == ""


def test_a_correct_16_bit_driver_passes_and_its_write_decodes_as_one_value(tmp_path):
    p, store, ledger, src = _setup(tmp_path, code())
    summary, codes, found = _codes(p, ledger)
    assert codes == [], [(f.code, f.message) for f in found]
    assert any("Configuration (0x01) <- 0x0220: HIGH_Alert=0, Data_Ready=0, MOD=0 (continuous), CONV=4, AVG=1" in w
               for w in summary["writes"]), summary["writes"]
    assert all("matches the code" in s for s in summary["formulas"]), summary["formulas"]
    reg = RegisterBook(p.root).register_at(0x01)
    assert (reg.width, reg.byte_order, reg.reset) == (16, "msb_first", 0x0220)
    # the other registers took the part's byte order from the first one
    assert {r.byte_order for r in RegisterBook(p.root).registers.values()} == {"msb_first"}


def test_bytes_assembled_swapped_are_caught(tmp_path):
    p, store, ledger, src = _setup(tmp_path, code(**{"(b[0] << 8) | b[1]": "(b[1] << 8) | b[0]"}))
    summary, codes, found = _codes(p, ledger)
    assert "REG_BYTE_ORDER" in codes, [(f.code, f.message) for f in found]
    msg = next(f.message for f in found if f.code == "REG_BYTE_ORDER")
    assert "most significant byte first" in msg and "swapped" in msg, msg


def test_a_register_written_in_part_is_caught(tmp_path):
    p, store, ledger, src = _setup(tmp_path, code(**{"    Wire.write((uint8_t)(v & 0xFF));\n": ""}))
    summary, codes, found = _codes(p, ledger)
    assert "REG_TRANSFER_WIDTH" in codes, [(f.code, f.message) for f in found]
    assert any("writes 1 byte(s) of Configuration" in f.message for f in found if f.code == "REG_TRANSFER_WIDTH")


def test_a_register_read_in_part_is_caught(tmp_path):
    p, store, ledger, src = _setup(tmp_path, code(**{"(uint8_t)2) != 2": "(uint8_t)1) != 1", "b[1] = Wire.read();": "b[1] = 0;"}))
    summary, codes, found = _codes(p, ledger)
    assert "REG_TRANSFER_WIDTH" in codes, [(f.code, f.message) for f in found]


def test_a_mask_on_an_undefined_bit_of_a_16_bit_register_is_caught(tmp_path):
    p, store, ledger, src = _setup(tmp_path, code().replace("0x2000;", "0x4000;"))   # bit 14: not a recorded field
    summary, codes, found = _codes(p, ledger)
    assert "REG_MASK_UNDEFINED" in codes, [(f.code, f.message) for f in found]
    assert any("0x4000" in f.message for f in found if f.code == "REG_MASK_UNDEFINED")


def test_a_16_bit_measurement_needs_a_layout_and_its_sign_is_checked(tmp_path):
    p, store, ledger, src = _setup(tmp_path, code())
    book = RegisterBook(p.root)
    book.layouts.clear()
    book.formulas.clear()
    book._save()
    summary, codes, found = _codes(p, ledger)
    assert "REG_LAYOUT_MISSING" in codes, [(f.code, f.message) for f in found]
    assert any("Temp_Result (0x00, 16 bits" in f.message for f in found if f.code == "REG_LAYOUT_MISSING")
    # with the layout (two's complement) and formula on record, reading the temperature as unsigned is caught
    p2, store2, ledger2, src2 = _setup(tmp_path / "u", code(**{"(int16_t)raw": "(uint16_t)raw"}))
    summary, codes, found = _codes(p2, ledger2)
    assert "FORMULA_MISMATCH" in codes, [(f.code, f.message) for f in found]


def test_a_unit_prefix_in_the_quote_allows_its_factor(tmp_path):
    """'One LSB equals 7.8125 m°C': / 1000 converts to °C. A factor the quote's units do not imply stays refused."""
    p, store, ledger, src = _setup(tmp_path, code())
    book = RegisterBook(p.root)
    ok = book.record_formula(store, "TMPX", src.id, "T_C", ["T_C = LSB * 7.8125 / 1000"], "One LSB equals 7.8125 m°C.",
                             inputs={"LSB": "Temp_Result"})
    assert ok["ok"], ok
    bad = book.record_formula(store, "TMPX", src.id, "T_X", ["T_X = count * 7.8125 / 1000"], "One count is 7.8125 degrees.",
                              inputs={"count": "Temp_Result"})
    assert not bad["ok"] and "1000" in bad["error"], bad


def test_a_16_bit_register_listed_twice_in_a_layout_is_refused(tmp_path):
    """TMP117 fix-up run: bytes ['0x00', '0x00'] for one 16-bit register recorded a 32-bit value of the same bits twice."""
    from embedforge.truth.registers import LayoutDef, LayoutPart
    p, store, ledger, src = _setup(tmp_path, code())
    book = RegisterBook(p.root)
    quote = DS[DS.index("00h R 8000h"):DS.index("m°C") + 3]
    r = book.record_layouts(store, "TMPX", src.id, [{"name": "Temp_Result", "bytes": ["0x00", "0x00"], "signed": True, "quote": quote}])
    assert not r["ok"] and "one 16-bit register: list it once" in r["rejected"][0]["error"], r
    # a doubled layout already on record (made before the refusal) fails verify until it is recorded again
    book.layouts["LAY-001"] = LayoutDef("LAY-001", "TMPX", "Temp_Result", [LayoutPart(0, 15, 0), LayoutPart(0, 15, 0)], True, src.id, quote)
    book._save()
    summary, codes, found = _codes(p, ledger)
    assert "LAYOUT_INVALID" in codes, [(f.code, f.message) for f in found]


def test_candidate_quotes_find_ti_field_tables():
    """TI prints a page as one line, writes addresses as 01h and field tables as 'BIT FIELD TYPE RESET': the candidates
    offered for a rejected register must be its field table, up to the next table's caption (not 'see Table 7-7')."""
    from embedforge.truth.registers import table_passages
    text = ("x " * 150 + "Table 7-6. Configuration Register Field Descriptions BIT FIELD TYPE RESET DESCRIPTION 15 HIGH_Alert R 0 "
            "High alert flag. 9:7 CONV[2:0] R/W 100 Conversion cycle bit. See Table 7-7 for the standby time. 6:5 AVG[1:0] "
            "R/W 01 Conversion averaging modes. Table 7-7. Conversion Cycle Time 000 15.5 ms 0Fh R 0117h Device_ID R 0h x")
    first = table_passages(text, ["Configuration", "0x01"])[0]
    assert first.startswith("Table 7-6.") and "6:5 AVG" in first and "Conversion Cycle Time" not in first, first
    assert "0Fh R 0117h Device_ID" in table_passages(text, ["Device_ID", "0x0F"], want_bits=False)[0]
    assert not any(p.startswith("0h") for p in table_passages(text, ["0x00"], want_bits=False))


def test_reserved_bits_and_field_values_of_a_16_bit_write(tmp_path):
    p, store, ledger, src = _setup(tmp_path, code().replace("0x0220)", "0x0A21)"))   # MOD=10, bit 0 reserved
    summary, codes, found = _codes(p, ledger)
    warn = {f.code for f in found}
    assert {"REG_RESERVED_BITS", "REG_FIELD_VALUE"} <= warn, [(f.code, f.message) for f in found]


def test_recording_a_wide_register_needs_its_byte_order(tmp_path):
    p = Project("w2", tmp_path / "w2")
    store = SourceStore(p.root)
    src = store.add("https://www.ti.com/lit/ds/x.pdf", "datasheet", DS, kind="pdf")
    ledger = FactLedger(p.root, store)
    book = RegisterBook(p.root)
    no_order = dict(REGISTERS[2])
    r = book.record_registers(store, ledger, "TMPX", src.id, [no_order])
    assert not r["ok"] and "byte_order_quote" in r["rejected"][0]["error"], r
    bad = dict(REGISTERS[2], byte_order_quote="Device ID register")
    r = book.record_registers(store, ledger, "TMPX", src.id, [bad])
    assert "does not" in r["rejected"][0]["error"], r
    wrong = dict(REGISTERS[2], byte_order_quote=ORDER, byte_order="lsb_first")
    assert "contradicts" in book.record_registers(store, ledger, "TMPX", src.id, [wrong])["rejected"][0]["error"]
    # a 16-bit row recorded as a byte register
    as_byte = {k: v for k, v in REGISTERS[0].items() if k not in ("width", "byte_order_quote", "fields")}
    assert "wider than a byte" in book.record_registers(store, ledger, "TMPX", src.id, [as_byte])["rejected"][0]["error"]


def test_tool_offers_the_byte_order_sentence(tmp_path):
    from embedforge.agent.events import RunSession
    from embedforge.agent.tools import ToolBelt
    p = Project("w3", tmp_path / "w3")
    store = SourceStore(p.root)
    src = store.add("https://www.ti.com/lit/ds/x.pdf", "datasheet", DS, kind="pdf")
    belt = ToolBelt(p, RunSession("r", p.id), store, FactLedger(p.root, store))
    res = belt.record_registers("TMPX", src.id, [dict(REGISTERS[2])])
    assert res["rejected"][0]["candidate_quotes"] == [ORDER], res


def test_sequence_steps_compare_16_bit_values(tmp_path):
    p, store, ledger, src = _setup(tmp_path, code())
    t = ledger.record("TMPX", "startup_time", "2", src.id, "wait 2 ms after power-up", "ms")["fact"]["id"]
    seq = SequenceBook(p.root)
    r = seq.record(ledger, "TMPX", "init", "TMPX::begin", [
        {"op": "delay_min", "value": t},
        {"op": "write", "reg": "0x01", "fields": {"MOD": "continuous", "CONV": "4", "AVG": "1"}}],
        src.id, "Start-up: wait 2 ms after power-up, then write the configuration register.")
    assert r["ok"], r
    flows, found = check_flows(p.root, ledger, FlowModel.from_dir(p.root))
    statuses = [s["status"] for s in flows[0]["steps"]]
    assert statuses == ["matched", "matched"], flows
