"""cppcheck + MISRA C:2012 through `pio check`: the report is parsed (framework files and repeats dropped) and verify
reports defects as errors, style and MISRA as warnings, for the build of the current sources only."""

import json

from embedforge.agent.events import RunSession
from embedforge.agent.tools import ToolBelt
from embedforge.agent.workspace import Project
from embedforge.toolchain import pio
from embedforge.truth import FactLedger, SourceStore


def test_pio_check_report_is_parsed(tmp_path, monkeypatch):
    root = tmp_path / "p"
    (root / "src").mkdir(parents=True)
    defects = [
        {"severity": "medium", "category": "warning", "id": "uninitMemberVar", "file": str(root / "src" / "a.cpp"), "line": 3,
         "message": "Member variable 'A::x' is not initialized in the constructor."},
        {"severity": "low", "category": "style", "id": "noExplicitConstructor", "file": str(root / "src" / "a.h"), "line": 9, "message": "m"},
        {"severity": "low", "category": "style", "id": "noExplicitConstructor", "file": str(root / "src" / "a.h"), "line": 9, "message": "m"},
        {"severity": "high", "category": "error", "id": "nullPointer", "file": str(tmp_path / "framework" / "Wire.cpp"), "line": 1, "message": "x"},
    ]
    seen = {}

    def fake_run(cmd, cwd, on_line, timeout):
        seen["cmd"] = cmd
        return 0, ["Checking weact_rp2350b > cppcheck", json.dumps([{"env": "e", "tool": "cppcheck", "defects": defects}])], False
    monkeypatch.setattr(pio, "_run_streaming", fake_run)
    res = pio.check(root)
    assert res["ok"] and "--addon=misra" in " ".join(seen["cmd"]) and "--skip-packages" in seen["cmd"]
    assert [(d["id"], d["file"]) for d in res["defects"]] == [("uninitMemberVar", "src/a.cpp"), ("noExplicitConstructor", "src/a.h")]


def test_verify_reports_static_findings_of_the_current_build(tmp_path):
    p = Project("t", tmp_path / "t")
    (p.root / "src").mkdir(exist_ok=True)
    (p.root / "src" / "main.cpp").write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
    store = SourceStore(p.root)
    belt = ToolBelt(p, RunSession("r", p.id), store, FactLedger(p.root, store))
    static = {"ok": True, "defects": [
        {"severity": "medium", "category": "warning", "id": "uninitvar", "file": "src/main.cpp", "line": 1, "message": "Uninitialized variable: x"},
        {"severity": "low", "category": "style", "id": "misra-c2012-12.3", "file": "src/main.cpp", "line": 2, "message": "misra violation"}]}
    p.update_meta(last_build={"hash": belt.source_hash(), "ok": True, "static": static})
    found = {(f["code"], f["severity"]): f["message"] for f in belt.verify()["findings"]}
    assert found[("STATIC_ANALYSIS", "ERROR")] == "cppcheck uninitvar: Uninitialized variable: x"
    assert found[("MISRA_C", "WARNING")] == "MISRA C:2012 Rule 12.3 is not followed here"
    (p.root / "src" / "main.cpp").write_text("void setup() {}\nvoid loop() { }\n", encoding="utf-8")   # sources changed
    assert not [f for f in belt.verify()["findings"] if f["code"] in ("STATIC_ANALYSIS", "MISRA_C")]
