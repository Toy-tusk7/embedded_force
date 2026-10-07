"""EasyEDA Pro (.epro V2 / .epro2 V3) importer on small synthetic projects.

The importer was also validated against two real projects from KiCad's QA data: every schematic pin's net
matched the project's own PCB pad nets (V2: 407/407, V3: 892/892). Those files are not redistributed here.
"""

import io
import json
import zipfile

from embedforge import eda

SYM_MCU, SYM_DRV, SYM_GND, SYM_SHORT = "symmcu", "symdrv", "symgnd", "symshort"


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def _lines(*recs):
    return "\n".join(json.dumps(r) for r in recs) + "\n"


def make_v2():
    project = {
        "schematics": {"sch1": {"name": "ESC", "sheets": [{"name": "P1", "id": 1, "uuid": "u1"}]}},
        "devices": {
            "devmcu": {"title": "AT32F421G8U7", "attributes": {"Symbol": SYM_MCU, "Footprint": "fpqfn", "Designator": "U?",
                                                           "Manufacturer Part": "AT32F421G8U7", "Supplier Part": "C2869578"}},
            "devdrv": {"title": "FD6288Q", "attributes": {"Symbol": SYM_DRV, "Designator": "U?", "Manufacturer Part": "FD6288Q"}},
            "devgnd": {"title": "GND", "attributes": {"Symbol": SYM_GND, "Global Net Name": "GND"}},
        },
        "footprints": {"fpqfn": {"title": "QFN-32_L4.0-W4.0"}},
        "symbols": {}, "boards": {},
    }
    mcu = _lines(["DOCTYPE", "SYMBOL", "1.0"], ["HEAD", {"symbolType": 2}], ["PART", "AT32F421G8U7.1", {}],
                 ["PIN", "e1", 1, 3, 10, 0, 10, 180, None, 0, 0], ["ATTR", "a1", "e1", "NAME", "PA8"], ["ATTR", "a2", "e1", "NUMBER", "29"],
                 ["PIN", "e2", 1, 3, 10, -10, 10, 180, None, 0, 0], ["ATTR", "a3", "e2", "NAME", "VSS"], ["ATTR", "a4", "e2", "NUMBER", "16"],
                 ["PIN", "e3", 1, 3, 10, -20, 10, 180, None, 0, 0], ["ATTR", "a5", "e3", "NAME", "PA2"], ["ATTR", "a6", "e3", "NUMBER", "10"])
    drv = _lines(["DOCTYPE", "SYMBOL", "1.0"], ["HEAD", {"symbolType": 2}], ["PART", "FD6288Q.1", {}],
                 ["PIN", "e1", 1, 1, -10, 0, 10, 0, None, 0, 0], ["ATTR", "a1", "e1", "NAME", "HIN1"], ["ATTR", "a2", "e1", "NUMBER", "3"])
    gnd = _lines(["DOCTYPE", "SYMBOL", "1.0"], ["HEAD", {"symbolType": 18}], ["PIN", "e1", 1, 0, 0, 0, 0, 90, None, 0, 0],
                 ["ATTR", "a1", "", "Global Net Name", "GND"])
    # U1 at (100,100) rotated 90 (y-up): pin (10,0)->(100,110), (10,-10)->(110,110), (10,-20)->(120,110)
    sheet = _lines(["DOCTYPE", "SCH", "1.1"], ["HEAD", {"originX": 0, "originY": 0}],
                   ["COMPONENT", "c1", "AT32F421G8U7.1", 100, 100, 90, 0, {}, 0],
                   ["ATTR", "t1", "c1", "Device", "devmcu", 0, 0, None, None, 0, "", 0], ["ATTR", "t2", "c1", "Designator", "U1", 0, 1, 0, 0, 0, "", 0],
                   ["COMPONENT", "c2", "FD6288Q.1", 200, 110, 0, 0, {}, 0],      # HIN1 at (190,110)
                   ["ATTR", "t3", "c2", "Device", "devdrv", 0, 0, None, None, 0, "", 0], ["ATTR", "t4", "c2", "Designator", "U2", 0, 1, 0, 0, 0, "", 0],
                   ["COMPONENT", "c3", "", 110, 130, 0, 0, {}, 0],
                   ["ATTR", "t5", "c3", "Device", "devgnd", 0, 0, None, None, 0, "", 0],
                   ["WIRE", "e100", [[100, 110, 150, 110], [150, 110, 190, 110]], "st1", 0],
                   ["ATTR", "t6", "e100", "NET", "HIN_A", 0, 1, 150, 110, 0, "", 0],
                   ["WIRE", "e101", [[110, 110, 110, 130]], "st1", 0])       # VSS down to the GND flag at (110,130)
    return _zip({"project.json": json.dumps(project), f"SYMBOL/{SYM_MCU}.esym": mcu, f"SYMBOL/{SYM_DRV}.esym": drv,
                 f"SYMBOL/{SYM_GND}.esym": gnd, "SHEET/sch1/1.esch": sheet})


def _row(t, rid, inner, ticket=1):
    return json.dumps({"type": t, "ticket": ticket, "id": rid}) + "||" + (json.dumps(inner) if inner is not None else "") + "|"


def _doc(doc_type, uuid, *rows):
    return [json.dumps({"type": "DOCHEAD", "ticket": 1}) + "||" + json.dumps({"docType": doc_type, "uuid": uuid}) + "|", *rows]


def make_v3():
    lines = []
    lines += _doc("SYMBOL", SYM_MCU, _row("META", "m", {"title": "MCU", "docType": 2}),
                  _row("PART", "pmcu", {"title": "AT32F421G8U7.1"}),
                  _row("PIN", "e1", {"partId": "pmcu", "x": 10, "y": 0, "length": 10, "rotation": 180}),
                  _row("ATTR", "a1", {"parentId": "e1", "key": "Pin Name", "value": "PA8"}),
                  _row("ATTR", "a2", {"parentId": "e1", "key": "Pin Number", "value": "29"}),
                  _row("PIN", "e2", {"partId": "pmcu", "x": 10, "y": 10, "length": 10, "rotation": 180}),
                  _row("ATTR", "a3", {"parentId": "e2", "key": "Pin Name", "value": "PB1"}),
                  _row("ATTR", "a4", {"parentId": "e2", "key": "Pin Number", "value": "15"}))
    lines += _doc("SYMBOL", SYM_DRV, _row("META", "m", {"title": "DRV", "docType": 2}),
                  _row("PIN", "e1", {"partId": "pdrv", "x": -10, "y": 0}),
                  _row("ATTR", "a1", {"parentId": "e1", "key": "Pin Name", "value": "HIN1"}),
                  _row("ATTR", "a2", {"parentId": "e1", "key": "Pin Number", "value": "3"}),
                  _row("PIN", "e2", {"partId": "pdrv", "x": -10, "y": 20}),
                  _row("ATTR", "a3", {"parentId": "e2", "key": "Pin Name", "value": "LIN3"}),
                  _row("ATTR", "a4", {"parentId": "e2", "key": "Pin Number", "value": "8"}))
    lines += _doc("SYMBOL", SYM_SHORT, _row("META", "m", {"title": "Short", "docType": 22}),
                  _row("PIN", "e1", {"partId": "", "x": 0, "y": 0}), _row("PIN", "e2", {"partId": "", "x": 10, "y": 0}))
    lines += _doc("DEVICE", "devmcu", _row("META", "m", {"title": "AT32F421G8U7", "attributes": {"Symbol": SYM_MCU, "Manufacturer Part": "AT32F421G8U7"}}))
    lines += _doc("DEVICE", "devdrv", _row("META", "m", {"title": "FD6288Q", "attributes": {"Symbol": SYM_DRV, "Manufacturer Part": "FD6288Q"}}))
    lines += _doc("DEVICE", "devshort", _row("META", "m", {"title": "Short", "attributes": {"Symbol": SYM_SHORT}}))
    lines += _doc("SCH", "sch1", _row("META", "m", {"title": "ESC"}))
    # U1 at (100,100) rotated 90 with y-down: pin (10,0)->(100,90); pin (10,10)->(110,90)
    lines += _doc("SCH_PAGE", "pg1", _row("META", "m", {"title": "P1", "schematic": "sch1", "zIndex": 2}),
                  _row("COMPONENT", "c1", {"partId": "AT32F421G8U7.1", "x": 100, "y": 100, "rotation": 90, "isMirror": False}),
                  _row("ATTR", "t1", {"parentId": "c1", "key": "Device", "value": "devmcu"}),
                  _row("ATTR", "t2", {"parentId": "c1", "key": "Designator", "value": "U9"}, ticket=1),
                  _row("ATTR", "t2", {"parentId": "c1", "key": "Designator", "value": "U1"}, ticket=5),   # later ticket wins
                  _row("COMPONENT", "c2", {"partId": "pdrv", "x": 200, "y": 90, "rotation": 0, "isMirror": False}),
                  _row("ATTR", "t3", {"parentId": "c2", "key": "Device", "value": "devdrv"}),
                  _row("ATTR", "t4", {"parentId": "c2", "key": "Designator", "value": "U2"}),
                  _row("WIRE", "e3868", {"zIndex": 1}),
                  _row("LINE", "l1", {"lineGroup": "e3868", "startX": 100, "startY": 90, "endX": 190, "endY": 90}),
                  _row("WIRE", "e3869", {"zIndex": 1}),
                  _row("LINE", "l2", {"lineGroup": "e3869", "startX": 110, "startY": 90, "endX": 110, "endY": 60}),
                  _row("ATTR", "t9", {"parentId": "e3869", "key": "NET", "value": "AGND"}),
                  # a short between AGND (110,60) and GND (160,60); GND wire continues to U2.LIN3 at (190,110)
                  _row("COMPONENT", "c3", {"partId": "", "x": 110, "y": 60, "rotation": 0, "isMirror": False}),
                  _row("ATTR", "t10", {"parentId": "c3", "key": "Device", "value": "devshort"}),
                  _row("WIRE", "e4000", {"zIndex": 1}),
                  _row("LINE", "l3", {"lineGroup": "e4000", "startX": 120, "startY": 60, "endX": 190, "endY": 60}),
                  _row("LINE", "l4", {"lineGroup": "e4000", "startX": 190, "startY": 60, "endX": 190, "endY": 110}),
                  _row("ATTR", "t11", {"parentId": "e4000", "key": "NET", "value": "GND"}),
                  _row("WIRE", "e5000", {"zIndex": 1}),
                  _row("WIRE", "e5000", None, ticket=9))                                              # tombstone
    return _zip({"project2.json": json.dumps({"title": "ESC"}), "ESC.epru": "\n".join(lines) + "\n"})


def test_v2_project_pins_names_nets_and_parts():
    data = make_v2()
    assert eda.detect("ESC.epro", data) == "easyeda_pro"
    b = eda.parse("easyeda_pro", "ESC.epro", data)
    u1 = b.parts["U1"]
    assert (u1.pins["29"].name, u1.pins["29"].net) == ("PA8", "HIN_A")     # rotated pin landed on the named wire
    assert u1.pins["16"].net == "GND"                                       # via wire to the power flag
    assert u1.pins["10"].net == ""                                          # touches nothing
    assert b.parts["U2"].pins["3"].net == "HIN_A"
    assert (u1.mpn, u1.lcsc, u1.footprint) == ("AT32F421G8U7", "C2869578", "QFN-32_L4.0-W4.0")
    assert "U1 pin 29 (PA8) -> net HIN_A -> U2.3 (HIN1)" in b.report()


def test_v3_log_y_down_rotation_tickets_and_auto_names():
    data = make_v3()
    assert eda.detect("ESC.epro2", data) == "easyeda_pro"
    b = eda.parse("easyeda_pro", "ESC.epro2", data)
    assert "U9" not in b.parts and "U1" in b.parts                         # highest ticket wins
    u1 = b.parts["U1"]
    assert u1.pins["29"].name == "PA8"
    # unnamed wire e3868 on page zIndex 2 joins U1.29 and U2.3 -> EasyEDA auto name
    assert u1.pins["29"].net == "$2N3868" and b.parts["U2"].pins["3"].net == "$2N3868"


def test_v3_short_symbol_merges_nets_lowest_name_wins():
    b = eda.parse("easyeda_pro", "ESC.epro2", make_v3())
    assert b.parts["U1"].pins["15"].net == "AGND"     # PB1 -> AGND wire
    assert b.parts["U2"].pins["8"].net == "AGND"      # LIN3 on GND, merged through the Short symbol
    assert not b.warnings
