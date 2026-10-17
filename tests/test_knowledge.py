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
    assert belt.call("record_fact", {"subject": "DM388", "property": "normal_mode_wait", "value": "2", "unit": "ms",
                                     "source_id": src.id, "quote": "Wait at least 2 ms after setting normal mode."})["ok"]
    brief = belt.call("coding_brief", {})
    reqs = brief["datasheet_requirements"]
    assert any("Wait at least 2 ms" in r for r in reqs["covered by records"]), reqs
    assert brief["registers"][0].startswith("REG-001 PWR_CTRL 0x1B") and brief["gates_verify_applies"]
    ids = [r.split(" ")[0] for r in reqs["open"]]
    assert belt.call("resolve_requirements", {"items": [{"id": i, "status": "implemented", "reason": "setup() writes OSR and ODR before PWR_CTRL"}
                                                        for i in ids]})["ok"]
    v = belt.call("verify", {})
    assert not [f for f in v["findings"] if f["code"] == "REQUIREMENT_OPEN"] and not v.get("unchanged"), v["findings"]
