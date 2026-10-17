"""A server stopped mid-run (a closed terminal, a killed process) keeps the project: records are written whole or not at
all, a file that does not parse is kept aside instead of being overwritten, the conversation is saved after every step,
and a damaged read log is rebuilt from that conversation."""

import json

from embedforge.agent import loop as loop_mod
from embedforge.agent.events import RunSession
from embedforge.agent.tools import ToolBelt
from embedforge.agent.workspace import Project
from embedforge.core.files import load_json, write_atomic
from embedforge.truth import FactLedger, SourceStore

from tests.test_agent_loop import ScriptedLLM, _call
from tests.test_outline import DS


def test_writes_are_whole_and_a_damaged_file_is_kept_aside(tmp_path):
    p = tmp_path / "ledger.json"
    write_atomic(p, json.dumps([{"id": "FACT-001"}]))
    assert load_json(p, []) == [{"id": "FACT-001"}] and not list(tmp_path.glob(".*.tmp"))
    p.write_bytes(b"\x00" * 316)   # what a write cut off by a killed process left behind
    assert load_json(p, []) == [] and not p.exists()
    assert (tmp_path / "ledger.json.corrupt").read_bytes() == b"\x00" * 316
    assert load_json(tmp_path / "missing.json", {"x": 1}) == {"x": 1}


def test_a_damaged_ledger_is_not_overwritten_by_the_next_fact(tmp_path):
    p = Project("t", tmp_path / "t")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/ds.pdf", "ds", "The 7-bit I2C address is 0x76.", kind="pdf")
    (p.root / "ledger.json").write_text('[{"id": "FACT-001", "subject": "BMP388", "pro', encoding="utf-8")   # cut off
    ledger = FactLedger(p.root, store)
    assert ledger.record("BMP388", "i2c_address", "0x76", src.id, "The 7-bit I2C address is 0x76.")["ok"]
    assert (p.root / "ledger.json.corrupt").read_text(encoding="utf-8").startswith('[{"id": "FACT-001"')


def test_the_conversation_is_saved_after_every_step(tmp_path, monkeypatch):
    project = Project("t", tmp_path / "t")
    saved = []

    class Watching(ScriptedLLM):
        def chat(self, messages, tools=None, **kw):
            if len(self.seen) == 1:   # the second call: step 1 is over, the run is not
                saved.append(project.load_conversation())
            return super().chat(messages, tools, **kw)

    session = RunSession("r1", project.id)
    runner = loop_mod.AgentRunner.__new__(loop_mod.AgentRunner)
    runner.project, runner.session = project, session
    runner.llm = Watching([[_call(0, "write_file", {"path": "src/main.c", "content": "int x;\n"})], "done"])
    runner.run("write a file")
    tool_msgs = [m for m in saved[0] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1 and "src/main.c" in tool_msgs[0]["content"]


def test_a_damaged_read_log_is_rebuilt_from_the_conversation(tmp_path):
    p = Project("t", tmp_path / "t")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/dm388.pdf", "DM388 datasheet", DS, kind="pdf")
    belt = ToolBelt(p, RunSession("r", p.id), store, FactLedger(p.root, store))
    res = belt.call("read_section", {"source_id": src.id, "section": "2.1.2"})
    sec = res["sections"][0]
    assert DS[sec["offset"]:sec["offset"] + len(sec["text"])] == sec["text"]
    p.save_conversation([{"role": "assistant", "content": "", "tool_calls": [_call(1, "read_section", {"source_id": src.id, "section": "2.1.2"})]},
                         {"role": "tool", "tool_call_id": "c1", "content": json.dumps(res)}])
    (p.root / "sources" / "reads.json").write_bytes(b"\x00" * 316)
    again = ToolBelt(p, RunSession("r2", p.id), store, FactLedger(p.root, store))
    assert again.reads.seen(src.id, sec["offset"], sec["offset"] + len(sec["text"])) == 1.0
    assert (p.root / "sources" / "reads.json.corrupt").exists()
