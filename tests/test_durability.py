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


def test_a_run_cut_off_by_a_server_stop_is_marked_interrupted(tmp_path, monkeypatch):
    from embedforge import server
    from embedforge.agent.workspace import workspace_root
    p = Project("cut", workspace_root() / "cut")
    p.trace({"event": "run_start", "model": "m"})
    p.trace({"step": 0, "results": [{"name": "read_file", "ok": True}]})
    p.trace({"step": 1, "results": [{"name": "build", "ok": True}]})
    done = Project("done", workspace_root() / "done")
    done.trace({"event": "run_start"})
    done.trace({"event": "run_end", "status": "success"})
    started = []
    monkeypatch.setattr(server, "_start_run", lambda pid, msg: started.append((pid, msg)))
    assert server.resume_interrupted_runs() == ["cut"] and started == []   # recorded, not resumed: credits are not spent unasked
    end = json.loads((p.root / "trace.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert end["event"] == "run_end" and end["status"] == "interrupted" and end["steps"] == 2
    assert server.resume_interrupted_runs() == []   # once
    p.trace({"event": "run_start"})
    monkeypatch.setenv("EMBEDFORGE_RESUME_INTERRUPTED", "1")
    assert server.resume_interrupted_runs() == ["cut"] and started == [("cut", "continue")]


def test_a_pass_under_older_verify_rules_does_not_finish_a_run(tmp_path, monkeypatch):
    """A carried-over PASS (project.json last_verify) counts only for the same sources, records and verify rules."""
    from embedforge.agent import tools
    p = Project("t", tmp_path / "t")
    store = SourceStore(p.root)
    belt = ToolBelt(p, RunSession("r", p.id), store, FactLedger(p.root, store))
    p.update_meta(last_build={"hash": belt.source_hash(), "ok": True},
                  last_verify={"hash": belt.source_hash(), "passed": True, "inputs": belt.verify_inputs_hash()})
    again = ToolBelt(p, RunSession("r2", p.id), store, FactLedger(p.root, store))
    assert again.finish("success", "built and verified")["ok"]
    monkeypatch.setattr(tools, "_FINGERPRINT", ["rules-changed"])
    later = ToolBelt(p, RunSession("r3", p.id), store, FactLedger(p.root, store))
    assert "verify rules changed" in later.finish("success", "built and verified")["error"]


def test_runlog_keeps_each_run_to_its_own_events(tmp_path):
    from embedforge import runlog
    p = Project("r", tmp_path / "r")
    p.trace({"event": "run_start", "provider": "nebius", "model": "ultra"})
    p.trace({"step": 0, "usage": {"prompt_tokens": 1000, "completion_tokens": 50}, "llm_seconds": 4.0, "results": []})
    p.trace({"event": "run_start", "provider": "nvidia", "model": "super"})   # the first run was cut off
    p.trace({"step": 0, "usage": {"prompt_tokens": 3000, "completion_tokens": 70}, "llm_seconds": 30.0, "results": []})
    p.trace({"event": "run_end", "status": "completed", "steps": 1, "build_ok": True, "verify_ok": True})
    first, second = runlog.runs(tmp_path)
    assert first["status"] == "interrupted" and not first["verified"] and first["prompt_tokens"] == 1000
    assert second["verified"] and second["model_seconds"] == 30.0
    rows = {r["model"]: r for r in runlog.by_model([first, second])}
    assert rows["ultra"]["verified"] == 0 and rows["super"]["verified"] == 1 and rows["super"]["seconds_per_step"] == 30.0


def test_a_second_server_on_a_busy_port_touches_no_run(monkeypatch):
    """A second `python -m embedforge.server` while one is running marked the live run interrupted, then failed to bind."""
    import socket
    import pytest
    from embedforge import server
    called = []
    monkeypatch.setattr(server, "resume_interrupted_runs", lambda: called.append(1))
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        monkeypatch.setenv("EMBEDFORGE_PORT", str(busy.getsockname()[1]))
        with pytest.raises(SystemExit, match="already in use"):
            server.main()
    assert called == []


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
