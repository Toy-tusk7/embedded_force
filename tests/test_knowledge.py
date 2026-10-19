"""The requirements memory: rule sentences of the sections the code depends on are numbered and kept, covered by the
records that quote them or settled explicitly, and verify fails while one is open. coding_brief compiles it all."""

from embedforge.agent.events import RunSession
from embedforge.agent.tools import ToolBelt
from embedforge.agent.workspace import Project
from embedforge.truth import FactLedger, SourceStore
from embedforge.truth.knowledge import Requirements
from test_outline import DRIVER, DS as BASE

# the normal-mode section also states a timing rule a fact can quote
DS = BASE.replace("Changing OSR in normal mode is not allowed. ", "Changing OSR in normal mode is not allowed. Wait at least 2 ms after setting normal mode. ", 1)


def test_sync_cover_and_resolve(tmp_path):
    reqs = Requirements(tmp_path)
    got = reqs.sync("SRC-001", [("2.2 Measurement time", "Wait at least 9 ms before the first read.", 100, 140),
                                ("2.1.2 Normal mode", "Changing OSR in normal mode is not allowed.", 200, 243)])
    assert [r.id for r in got] == ["REQ-001", "REQ-002"]
    assert [r.id for r in reqs.sync("SRC-001", [("2.2 Measurement time", "Wait at least 9 ms before the first read.", 100, 140)])] == ["REQ-001"]
    reqs.cover({"SRC-001": [(120, 128, "FACT-004")]})
    assert reqs.items["REQ-001"].status == "covered" and reqs.items["REQ-001"].refs == ["FACT-004"]
    res = reqs.resolve([{"id": "req-2", "status": "not applicable", "reason": "the code never changes OSR after init"},
                        {"id": "REQ-001", "status": "implemented", "reason": "short"}])
    assert res["resolved"] == ["REQ-002 not_applicable"] and "reason" in res["rejected"][0]["error"]
    assert Requirements(tmp_path).counts() == {"open": 0, "covered": 1, "implemented": 0, "not_applicable": 1}   # kept on disk


def test_a_timing_rule_needs_a_record_verify_checks_on_the_code(tmp_path):
    """The BMP388 fix-up run settled 'the minimum achievable frequency is determined by the maximum measurement time'
    with a fact whose value was the section heading; nothing in the code used it, and verify passed a driver that
    enters normal mode before its settings."""
    from test_outline import _project
    from embedforge.truth.outline import find_section, sections
    belt, store, src = _project(tmp_path)
    p = belt.project
    sec = find_section(sections(src.text), "2.2")
    # Measurement time (2.2) as a timing section the code depends on (a driver that reads data or sets the ODR)
    belt._strict_topic = lambda sid, label: ("timing", [(sec.start, sec.end)]) if label.startswith("2.2") else ("", [])
    reqs = Requirements(p.root)
    sentence = "Wait at least 9 ms before the first read."
    start = src.text.index(sentence, sec.start)
    reqs.sync(src.id, [(sec.label[:80], sentence, start, start + len(sentence))])
    rid = next(r.id for r in Requirements(p.root).items.values() if r.section.startswith("2.2"))
    heading = belt.call("record_fact", {"subject": "DM388", "property": "measurement_time_section", "value": "Measurement time",
                                        "source_id": src.id,
                                        "quote": "2.2. Measurement time\nThe conversion takes at most 8.9 ms with x2 oversampling."})
    assert heading["ok"], heading
    claim = {"id": rid, "status": "implemented", "reason": "the code waits for the conversion before reading"}
    refused = belt.call("resolve_requirements", {"items": [dict(claim, ref=heading["fact"]["id"])]})
    assert not refused["ok"] and "nothing in the code uses it" in refused["rejected"][0]["error"], refused
    # the same fact cited on a constant of the code is checked by verify, so it backs the claim
    code = (p.root / "src" / "main.cpp").read_text(encoding="utf-8")
    assert "Wire.write(0x33);" in code
    (p.root / "src" / "main.cpp").write_text(code.replace("Wire.write(0x33);", f"Wire.write(0x33);  /* {heading['fact']['id']} */"),
                                             encoding="utf-8")
    asked = []
    belt.reviewer = lambda messages: asked.append(messages) or "[]"
    assert belt.call("resolve_requirements", {"items": [dict(claim, ref=heading["fact"]["id"])]})["ok"]
    # verify checks that record on the code, so the reviewer (who sees only the code) is not asked
    assert asked == [] and Requirements(p.root).items[rid].review["verdict"] == "agree"


def test_verify_fails_until_every_requirement_is_settled(tmp_path):
    p = Project("t", tmp_path / "t")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/dm388.pdf", "DM388 datasheet", DS, kind="pdf")
    store.add("https://www.bosch-sensortec.com/dm388-addr.pdf", "addr", "The 7-bit I2C address is 0x76.", kind="pdf")
    ledger = FactLedger(p.root, store)
    addr = ledger.record("DM388", "i2c_address", "0x76", "SRC-002", "The 7-bit I2C address is 0x76.")["fact"]["id"]
    reg = ledger.record("DM388", "pwr_ctrl_reg", "0x1B", src.id, "3.1. Register 0x1B “PWR_CTRL”")["fact"]["id"]
    (p.root / "src").mkdir(exist_ok=True)
    (p.root / "src" / "main.cpp").write_text(DRIVER.replace("{reg}", reg).replace("{addr}", addr), encoding="utf-8")
    from embedforge.truth.registers import RegisterBook
    q = DS[DS.index("3.1. Register 0x1B"):DS.index("normal. ", DS.index("3.1. Register 0x1B")) + 7]
    assert RegisterBook(p.root).record_registers(store, ledger, "DM388", src.id, [
        {"name": "PWR_CTRL", "addr": "0x1B", "quote": q,
         "fields": [{"name": "mode", "bits": "5:4", "values": {"sleep": "00", "normal": "11"}}]}])["ok"]
    belt = ToolBelt(p, RunSession("r", p.id), store, ledger)
    belt.call("verify", {})
    belt.call("read_section", {"source_id": src.id, "section": "2.1, 2.2, 2.3, 3.1"})
    v = belt.call("verify", {})
    open_ = next(f for f in v["findings"] if f["code"] == "REQUIREMENT_OPEN")
    assert "must be set to normal only after OSR and ODR" in open_["message"] and "Poll drdy" not in open_["message"], open_   # 2.3 is not needed: no data is read
    # a record quoting a requirement covers it
    fact = belt.call("record_fact", {"subject": "DM388", "property": "normal_mode_wait", "value": "2", "unit": "ms",
                                     "source_id": src.id, "quote": "Wait at least 2 ms after setting normal mode."})
    assert fact["ok"]
    brief = belt.call("coding_brief", {})
    reqs = brief["datasheet_requirements"]
    assert any("Wait at least 2 ms" in r for r in reqs["covered by records"]), reqs
    assert brief["registers"][0].startswith("REG-001 PWR_CTRL 0x1B") and brief["gates_verify_applies"]
    ids = [r.split(" ")[0] for r in reqs["open"]]
    # the normal-mode rules are about the mode the code uses: a bare claim is refused, not_applicable too
    claim = {"status": "implemented", "reason": "setup() writes OSR and ODR before PWR_CTRL"}
    bare = belt.call("resolve_requirements", {"items": [dict(claim, id=i) for i in ids]})
    assert not bare["ok"] and all("needs ref" in r["error"] for r in bare["rejected"]), bare
    na = belt.call("resolve_requirements", {"items": [{"id": ids[0], "status": "not_applicable", "reason": "the code never uses it"}]})
    assert "so it applies" in na["rejected"][0]["error"]
    # backed by a record that quotes the normal-mode section, the claims stand
    assert belt.call("resolve_requirements", {"items": [dict(claim, id=i, ref=fact["fact"]["id"]) for i in ids]})["ok"]
    v = belt.call("verify", {})
    assert not [f for f in v["findings"] if f["code"].startswith("REQUIREMENT")] and not v.get("unchanged"), v["findings"]
    # a claim settled before such rules needed a record (requirements.json from an older run): verify names the record
    # that already quotes the section, and the whole of that advice reaches the model, not its first 200 characters
    Requirements(p.root).resolve([dict(claim, id=ids[0])])
    v = belt.call("verify", {})
    need = next(f for f in v["findings"] if f["code"] == "REQUIREMENT_NEEDS_RECORD")
    assert f"{ids[0]}: {fact['fact']['id']} already quote" in need["suggestion"], need
    assert need["suggestion"].endswith("load_skill('datasheet-rules') explains each kind."), need["suggestion"]
