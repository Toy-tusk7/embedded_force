"""Datasheet structure: numbered sections (a table of contents and body lines that look like headings do not break
them), read coverage, the sections the code depends on, and verify's DATASHEET_UNREAD."""

from embedforge.agent.events import RunSession
from embedforge.agent.tools import ToolBelt
from embedforge.agent.workspace import Project
from embedforge.truth import FactLedger, SourceStore
from embedforge.truth.outline import ReadLog, find_section, required, requirements, sections, subtree_end

BODY = {
    "1. Specification": "Supply voltage 1.7 V to 3.6 V. " * 8,
    "2. Functional description": "The sensor measures pressure and temperature. " * 4,
    "2.1. Power modes": "The device has three power modes. " * 5,
    "2.1.1. Sleep mode": "In sleep mode no measurements are performed. " * 4,
    "2.1.2. Normal mode": ("Write the measurement settings first; the mode must be set to normal only after OSR and ODR "
                           "are configured. Changing OSR in normal mode is not allowed. " * 2),
    "2.2. Measurement time": "The conversion takes at most 8.9 ms with x2 oversampling. Wait at least 9 ms before the first read. " * 2,
    "2.3. Data ready": "The drdy bit is set after a measurement ends. Poll drdy before reading the data registers. " * 2,
    "2.4. FIFO read out": "The FIFO is read out in frames. " * 6,
    "3. Register description": "All registers are 8 bit wide. " * 5,
    "3.1. Register 0x1B “PWR_CTRL”": "Bit 5..4 mode 00 sleep 11 normal. " * 5,
}
TOC = "Table of contents\n" + "".join(f"{h} {'.' * 20} {i + 3}\n" for i, h in enumerate(BODY))
DS = TOC + "\n" + "".join(f"{h}\n{text}\n\n1 Write the register first, then wait.\n\n" for h, text in BODY.items())


def test_sections_follow_the_numbering_and_skip_the_table_of_contents():
    secs = sections(DS)
    assert [s.num for s in secs] == ["1", "2", "2.1", "2.1.1", "2.1.2", "2.2", "2.3", "2.4", "3", "3.1"]
    normal = find_section(secs, "2.1.2")
    assert normal.title == "Normal mode" and "must be set to normal only after" in DS[normal.start:normal.end]
    assert find_section(secs, "normal mode") is normal and find_section(secs, "Section 2.1.2.") is normal
    modes = find_section(secs, "2.1")
    assert DS[modes.start:subtree_end(secs, modes)].count("Normal mode") == 1   # 2.1 includes its subsections
    assert any("not allowed" in r for r in requirements(DS[normal.start:normal.end]))


def test_markdown_headings_without_numbers():
    md = "# DM388\nintro\n## Power modes\n" + "x " * 80 + "\n## Data readout\n" + "y " * 80 + "\n## Registers\n" + "z " * 80
    assert [s.title for s in sections(md)] == ["DM388", "Power modes", "Data readout", "Registers"]


def test_required_reading_follows_what_the_code_does():
    secs = sections(DS)
    uses = {"modes": "the code sets PWR_CTRL.mode = normal", "mode_words": "normal", "timing": "reads data",
            "ready": "reads data", "readout": "reads data", "register_names": "PWR_CTRL"}
    got = {s.num: why for s, why in required(secs, uses)}
    assert set(got) == {"2.1", "2.1.2", "2.2", "2.3", "3.1"}, got   # not Sleep mode, not the FIFO (unused)
    assert got["3.1"] == "the code uses register PWR_CTRL"
    assert "2.4" in {s.num for s, _ in required(secs, dict(uses, fifo="the code sets fifo_en"))}


def test_read_log_adds_up_and_persists(tmp_path):
    log = ReadLog(tmp_path)
    log.add("SRC-001", 0, 100)
    log.add("SRC-001", 50, 200)
    assert ReadLog(tmp_path).spans == {"SRC-001": [[0, 200]]} and log.seen("SRC-001", 100, 300) == 0.5


DRIVER = """#include <Wire.h>
#define REG_PWR_CTRL 0x1B  /* {reg} */
void setup() {
    Wire.begin();
    Wire.beginTransmission(0x76);  /* {addr} */
    Wire.write(REG_PWR_CTRL);
    Wire.write(0x33);
    Wire.endTransmission();
}
void loop() {}
"""


def test_verify_requires_the_sections_the_code_depends_on(tmp_path):
    p = Project("t", tmp_path / "t")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/dm388.pdf", "DM388 datasheet", DS, kind="pdf")
    ledger = FactLedger(p.root, store)
    store.add("https://www.bosch-sensortec.com/dm388-addr.pdf", "addr", "The 7-bit I2C address is 0x76.", kind="pdf")
    addr = ledger.record("DM388", "i2c_address", "0x76", "SRC-002", "The 7-bit I2C address is 0x76.")["fact"]["id"]
    reg = ledger.record("DM388", "pwr_ctrl_reg", "0x1B", src.id, "3.1. Register 0x1B “PWR_CTRL”")["fact"]["id"]
    (p.root / "src").mkdir(exist_ok=True)
    (p.root / "src" / "main.cpp").write_text(DRIVER.replace("{reg}", reg).replace("{addr}", addr), encoding="utf-8")
    belt = ToolBelt(p, RunSession("r", p.id), store, ledger)
    from embedforge.truth.registers import RegisterBook
    book = RegisterBook(p.root)
    q = DS[DS.index("3.1. Register 0x1B"):DS.index("normal. ", DS.index("3.1. Register 0x1B")) + 7]
    assert book.record_registers(store, ledger, "DM388", src.id, [
        {"name": "PWR_CTRL", "addr": "0x1B", "quote": q,
         "fields": [{"name": "mode", "bits": "5:4", "values": {"sleep": "00", "normal": "11"}}]}])["ok"]
    first = belt.call("verify", {})
    unread = [f for f in first["findings"] if f["code"] == "DATASHEET_UNREAD"]
    assert unread and "2.1.2 Normal mode (the code sets PWR_CTRL.mode = normal)" in unread[0]["message"], first["findings"]
    outline = belt.call("datasheet_outline", {"source_id": src.id})
    assert any("2.1.2 Normal mode" in line and "<< needed" in line for line in outline["sections"])
    res = belt.call("read_section", {"source_id": src.id, "section": "2.1, Register 0x1B"})
    assert [s["section"] for s in res["sections"]] == ["2.1 Power modes", "3.1 Register 0x1B “PWR_CTRL”"]
    assert any("not allowed" in r for r in res["sections"][0]["requirements"])
    again = belt.call("verify", {})
    assert not [f for f in again["findings"] if f["code"] == "DATASHEET_UNREAD"] and not again.get("unchanged"), again
