"""EasyEDA / netlist / BOM import: parsers, merging, upload flow, agent tool and fact quoting."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from embedforge import eda
from embedforge.agent.events import RunSession
from embedforge.agent.tools import ToolBelt, check_agent_write
from embedforge.agent.workspace import Project
from embedforge.truth import FactLedger, SourceStore

# --- a tiny ESC front-end: MCU U1 (PA8 -> HIN_A -> gate driver U2.HIN1), GND flag on VSS
LIB_U1 = ("LIB~100~100~package`QFN-32`BOM_Manufacturer Part`AT32F421G8U7`BOM_Supplier Part`C2869578`~~0~gge1"
          "#@$T~P~90~80~0~#000080~Arial~~~~~comment~U1~1~start~gge2"
          "#@$T~N~90~90~0~#000080~Arial~~~~~comment~AT32F421G8U7~1~start~gge3"
          "#@$P~show~0~29~200~100~180~gge4^^200~100^^M 200 100 h -10~#880000^^1~185~103~0~PA8~end~~~#0000FF^^1~195~99~0~29~start~~~#0000FF^^0~192~100^^0~M 0 0"
          "#@$P~show~0~16~200~120~180~gge5^^200~120^^M 200 120 h -10~#880000^^1~185~123~0~VSS~end~~~#0000FF^^1~195~119~0~16~start~~~#0000FF^^0~192~120^^0~M 0 0"
          "#@$P~show~0~10~200~140~180~gge6^^200~140^^M 200 140 h -10~#880000^^1~185~143~0~PA2~end~~~#0000FF^^1~195~139~0~10~start~~~#0000FF^^0~192~140^^0~M 0 0")
LIB_U2 = ("LIB~400~100~package`QFN-24`~~0~gge10"
          "#@$T~P~390~80~0~#000080~Arial~~~~~comment~U2~1~start~gge11"
          "#@$T~N~390~90~0~#000080~Arial~~~~~comment~FD6288Q~1~start~gge12"
          "#@$P~show~0~3~300~100~0~gge13^^300~100^^M 300 100 h 10~#880000^^1~315~103~0~HIN1~start~~~#0000FF^^1~305~99~0~3~end~~~#0000FF^^0~308~100^^0~M 0 0")
STD_SHEET = {
    "head": {"docType": "1"}, "canvas": "CA~1000~1000",
    "shape": [LIB_U1, LIB_U2,
              "W~200 100 250 100 300 100~#008800~1~0~none~gge20",
              "N~250~100~0~#FF0000~HIN_A~gge21~start~252~100~Arial~",
              "W~200 120 220 120 220 140~#008800~1~0~none~gge22",
              "F~part_netLabel_gnD~220~140~~gge23^^220~140^^GND~#000080~0~0~0~start~1~Arial~9pt^^"],
}
PROTEL = "[\nU1\nQFN-32\nAT32F421G8U7\n]\n[\nU2\nQFN-24\nFD6288Q\n]\n(\nHIN_A\nU1-29\nU2-3\n)\n(\nGND\nU1-16\n)\n(\nNetU1_10\nU1-10\n)\n"
PADS = "*PADS-PCB*\n*PART*\nU1 AT32F421G8U7@QFN-32\nU2 QFN-24\n*NET*\n*SIGNAL* HIN_A\nU1.29 U2.3\n*SIGNAL* GND\nU1.16\n*END*\n"
KICAD = ('(export (version "E") (components (comp (ref "U1") (value "AT32F421") (footprint "Pkg:QFN-32") '
         '(fields (field (name "MPN") "AT32F421G8U7")))) (nets (net (code "1") (name "/HIN_A") '
         '(node (ref "U1") (pin "29") (pinfunction "PA8") (pintype "bidirectional")))))')
BOM_UTF16 = ("ID\tName\tDesignator\tFootprint\tQuantity\tManufacturer Part\tManufacturer\tSupplier\tSupplier Part\n"
             "1\tAT32F421G8U7\tU1\tQFN-32\t1\tAT32F421G8U7\tARTERY\tLCSC\tC2869578\n"
             "2\tFD6288Q\tU2\tQFN-24\t1\tFD6288Q\tFortior\tLCSC\tC2847614\n").encode("utf-16")


def _parse(name: str, data: bytes) -> eda.Board:
    kind = eda.detect(name, data)
    assert kind, f"{name} not detected"
    return eda.parse(kind, name, data)


def test_easyeda_standard_geometry_connectivity():
    b = _parse("esc.json", json.dumps(STD_SHEET).encode())
    assert b.parts["U1"].pins["29"].name == "PA8"
    assert b.parts["U1"].pins["29"].net == "HIN_A"          # label sits mid-wire
    assert b.parts["U2"].pins["3"].net == "HIN_A"           # pin on the wire's far end
    assert b.parts["U1"].pins["16"].net == "GND"            # power flag at a bent wire's end
    assert b.parts["U1"].pins["10"].net == ""               # unconnected pin
    assert b.parts["U1"].mpn == "AT32F421G8U7" and b.parts["U1"].lcsc == "C2869578"
    assert b.query("PA8")["lines"] == ["U1 pin 29 (PA8) -> net HIN_A -> U2.3 (HIN1)"]


def test_easyeda_standard_multisheet_labels_join_across_sheets():
    sheet2 = {"head": {"docType": "1"}, "canvas": "CA", "shape": [
        LIB_U2.replace("U2", "U3").replace("gge1", "hh1"), "N~300~100~0~#FF0000~HIN_A~x1~start~0~0~Arial~"]}
    doc = {"schematics": [{"title": "mcu", "dataStr": STD_SHEET}, {"title": "drv", "dataStr": json.dumps(sheet2)}]}
    b = _parse("project.json", json.dumps(doc).encode())
    assert ("U3", "3") in b.nets()["HIN_A"] and ("U1", "29") in b.nets()["HIN_A"]


@pytest.mark.parametrize("name, data, fmt", [
    ("esc.net", PROTEL.encode(), "protel"), ("esc.asc", PADS.encode(), "pads"),
    ("esc_kicad.net", KICAD.encode(), "kicad"), ("BOM_esc.csv", BOM_UTF16, "bom"),
])
def test_format_detection(name, data, fmt):
    assert eda.detect(name, data) == fmt


def test_netlists_and_bom_parse():
    assert _parse("esc.net", PROTEL.encode()).parts["U2"].pins["3"].net == "HIN_A"
    pads = _parse("esc.asc", PADS.encode())
    assert pads.parts["U1"].value == "AT32F421G8U7" and pads.parts["U1"].footprint == "QFN-32"
    kicad = _parse("k.net", KICAD.encode())
    assert kicad.parts["U1"].pins["29"].name == "PA8" and kicad.parts["U1"].mpn == "AT32F421G8U7"
    bom = _parse("bom.csv", BOM_UTF16)
    assert bom.parts["U2"].manufacturer == "Fortior" and bom.parts["U2"].lcsc == "C2847614"


def test_merge_netlist_names_and_bom_without_spurious_warnings():
    b = eda.Board(name="esc")
    for name, data in [("esc.net", PROTEL.encode()), ("esc.json", json.dumps(STD_SHEET).encode()), ("bom.csv", BOM_UTF16)]:
        b.merge(_parse(name, data))
    u1 = b.parts["U1"]
    assert u1.pins["29"].name == "PA8" and u1.pins["29"].net == "HIN_A" and u1.manufacturer == "ARTERY"
    assert b.warnings == []  # Protel's auto name NetU1_10 vs no net in the sheet is not a conflict
    assert "U1 pin 29 (PA8) -> net HIN_A -> U2.3 (HIN1)" in b.report()


PROTEL2_PRO = """PROTEL NETLIST 2.0
[
DESIGNATOR
U1
FOOTPRINT
QFN-80_L10.0-W10.0-P0.40-TL-EP3.4
PARTTYPE
RP2354B_C39843328
DESCRIPTION
MCU
Reuse Block

Supplier Part
C39843328
Manufacturer Part
RP2354B
]
[
DESIGNATOR
U6
FOOTPRINT
SD-CARD-SMD_SD-CARD-11P
PARTTYPE
SD-CARD 11P
Name
SD-CARD 11P
]
(
FLASH_SCL
U1-53 RP2354B_C39843328-GPIO42_ADC2 Passive
U6-5 SD-CARD 11P-CLK Passive
)
(
GND
U6-12 SD-CARD 11P-GND Passive
)
"""


def test_easyeda_pro_protel2_keyvalue_variant_with_pin_names():
    """EasyEDA Pro writes key/value component blocks and 'REF-PIN PARTTYPE-PINNAME TYPE' nodes."""
    b = _parse("Netlist_Schematic1.net", PROTEL2_PRO.encode())
    assert set(b.parts) == {"U1", "U6"}
    u1 = b.parts["U1"]
    assert (u1.mpn, u1.lcsc, u1.footprint) == ("RP2354B", "C39843328", "QFN-80_L10.0-W10.0-P0.40-TL-EP3.4")
    assert (u1.pins["53"].name, u1.pins["53"].net) == ("GPIO42_ADC2", "FLASH_SCL")
    assert b.parts["U6"].pins["5"].name == "CLK"          # part type with a space and a dash
    assert b.query("GPIO42_ADC2")["lines"] == ["U1 pin 53 (GPIO42_ADC2) -> net FLASH_SCL -> U6.5 (CLK)"]


def test_rp2354_is_recognised_as_mcu():
    b = _parse("n.net", PROTEL2_PRO.encode())
    for i in range(10):
        b.parts["U1"].pin(str(100 + i))
    assert [m.ref for m in b.mcus()] == ["U1"]


def test_non_eda_files_are_not_detected():
    assert eda.detect("notes.txt", b"just some notes") is None
    assert eda.detect("data.csv", b"time,value\n1,2\n") is None
    assert eda.detect("config.json", b'{"a": 1}') is None


def test_upload_builds_board_and_agent_can_quote_it(tmp_path, monkeypatch):
    client = TestClient(__import__("embedforge.server", fromlist=["app"]).app)
    pid = client.post("/api/projects", json={"name": "esc-board"}).json()["id"]
    files = [("files", ("esc.json", json.dumps(STD_SHEET).encode(), "application/json")),
             ("files", ("esc.net", PROTEL.encode(), "text/plain")),
             ("files", ("BOM_esc.csv", BOM_UTF16, "text/csv"))]
    res = client.post(f"/api/projects/{pid}/upload", files=files).json()
    assert [u["kind"] for u in res["uploaded"]] == ["eda", "eda", "eda"]
    assert all(u["path"].startswith("schematics/") for u in res["uploaded"])
    assert res["board"]["parts"] == 2 and res["board"]["pin_names_known"]
    board = client.get(f"/api/projects/{pid}/board").json()["board"]
    assert board["nets"]["HIN_A"] == [["U1", "29"], ["U2", "3"]]

    # the agent's query_board tool returns lines that the Fact Ledger accepts as verbatim quotes
    from embedforge.agent.workspace import ProjectManager
    project = ProjectManager.open(pid)
    store = SourceStore(project.root)
    belt = ToolBelt(project, RunSession("r", pid), store, FactLedger(project.root, store))
    q = belt.query_board("PA8")
    line = q["lines"][0]
    fact = belt.record_fact("U1", "PA8_net", "HIN_A", q["source_id"], line)
    assert fact["ok"] and fact["fact"]["tier"] == "USER" and fact["fact"]["authoritative"]


def test_pin_defines_must_trace_to_the_schematic(tmp_path):
    """A pin number in code is a wiring claim: verify it against a fact quoted from the imported board."""
    from embedforge.truth import verify_project
    store = SourceStore(tmp_path)
    board = _parse("Netlist_Schematic1.net", PROTEL2_PRO.encode())
    src = store.add(eda.BOARD_SOURCE_URL, "board", board.report(), kind="schematic")
    ledger = FactLedger(tmp_path, store)
    line = board.query("GPIO42_ADC2")["lines"][0]
    fid = ledger.record("U1", "SD_CLK_gpio", "GPIO42", src.id, line)["fact"]["id"]
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pins.h").write_text(
        f"#define SD_CLK_PIN 42 /* {fid} */\n#define SD_CMD_PIN 43 /* {fid} */\n#define LED_PIN 12\n", encoding="utf-8")
    rep = verify_project(tmp_path, ledger)
    codes = [(f.code, f.line) for f in rep.findings]
    assert ("FACT_VALUE_MISMATCH", 2) in codes        # 43 is not the GPIO the cited schematic line says
    assert ("UNSOURCED_CONSTANT", 3) in codes         # an untraced pin assignment
    assert rep.traceability[0]["fact_id"] == fid and rep.traceability[0]["line"] == 1


def test_betaflight_pico_pin_names_map_to_gpio(tmp_path):
    """Betaflight's RP2350 platform names GPIO n 'PAn'; on STM32 'PA14' is a different pin than GPIO14."""
    from embedforge.truth import verify_project
    store = SourceStore(tmp_path)
    board = _parse("n.net", PROTEL2_PRO.encode())
    src = store.add(eda.BOARD_SOURCE_URL, "board", board.report(), kind="schematic")
    ledger = FactLedger(tmp_path, store)
    fid = ledger.record("U1", "SD_CLK_gpio", "GPIO42", src.id, board.query("GPIO42_ADC2")["lines"][0])["fact"]["id"]
    cfg = tmp_path / "betaflight" / "configs" / "MYFC"
    cfg.mkdir(parents=True)
    (cfg / "config.h").write_text(f"#define SDCARD_SPI_SCK_PIN PA42 /* {fid} */\n", encoding="utf-8")
    assert verify_project(tmp_path, ledger, pico=True).passed
    assert not verify_project(tmp_path, ledger, pico=False).passed


def test_agent_cannot_plant_a_schematic():
    assert check_agent_write("schematics/fake.net", "[\nU1\n]\n") is not None


def test_board_only_scans_upload_folder(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "fake.net").write_text(PROTEL, encoding="utf-8")
    assert eda.rebuild_project_board(tmp_path, SourceStore(tmp_path)) is None
