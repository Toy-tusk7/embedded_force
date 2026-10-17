"""The independent reviewer of the agent's requirement claims, and the skills (playbooks) it can load."""

import json

from embedforge.agent.events import RunSession
from embedforge.agent.prompts import get_active_prompt_text
from embedforge.agent.review import review_claims
from embedforge.agent.tools import ToolBelt
from embedforge.agent.workspace import Project
from embedforge.skills import list_skills, load_skill
from embedforge.truth import FactLedger, SourceStore
from embedforge.truth.knowledge import Requirement, Requirements
from test_knowledge import DS
from test_outline import DRIVER


def test_review_parses_verdicts_and_survives_noise():
    claims = [Requirement("REQ-001", "SRC-001", "2.1.2 Normal mode", "Set the mode only after OSR and ODR.", 0, 10,
                          status="implemented", reason="begin() sets the mode")]
    seen = []

    def ask(messages):
        seen.append(messages)
        return '```json\n[{"id": "req-1", "verdict": "Disagree", "where": "src/main.cpp:6", "why": "PWR_CTRL is written first"}]\n```'
    out = review_claims(ask, claims, {"src/main.cpp": "void setup() {\n  wr(PWR, 0x33);\n}\n"})
    assert out == {"REQ-001": {"verdict": "disagree", "where": "src/main.cpp:6", "why": "PWR_CTRL is written first"}}
    assert "   2:   wr(PWR, 0x33);" in seen[0][1]["content"] and "author: implemented" in seen[0][1]["content"]
    assert review_claims(lambda m: "I think it is fine.", claims, {}) == {}   # unreadable: no verdict, not a pass


def _belt(tmp_path, reviewer=None):
    p = Project("t", tmp_path / "t")
    store = SourceStore(p.root)
    src = store.add("https://www.bosch-sensortec.com/dm388.pdf", "DM388 datasheet", DS, kind="pdf")
    ledger = FactLedger(p.root, store)
    (p.root / "src").mkdir(exist_ok=True)
    (p.root / "src" / "main.cpp").write_text(DRIVER, encoding="utf-8")
    Requirements(p.root).sync(src.id, [("2.1.2 Normal mode", "Set the mode only after OSR and ODR.", 10, 50)])
    belt = ToolBelt(p, RunSession("r", p.id), store, ledger)
    belt.reviewer = reviewer
    return p, belt, src


def test_a_disputed_claim_fails_verify_until_settled(tmp_path):
    replies = [[{"id": "REQ-001", "verdict": "disagree", "where": "src/main.cpp:5", "why": "the mode is written before OSR"}]]
    p, belt, src = _belt(tmp_path, reviewer=lambda m: json.dumps(replies[-1]))
    res = belt.call("resolve_requirements", {"items": [{"id": "REQ-001", "status": "implemented", "reason": "setup() configures the sensor"}]})
    assert res["review"]["disagreed"][0]["why"] == "the mode is written before OSR" and "Fix the code" in res["next"]
    reqs = Requirements(p.root)
    assert [r.id for r in reqs.disputed()] == ["REQ-001"] and reqs.items["REQ-001"].disputes == 1
    findings = belt._open_requirements(src.id, [])   # disputes are reported even without new sections read
    assert [f.code for f in findings] == ["REQUIREMENT_DISPUTED"]
    from embedforge.truth.outline import sections
    found = belt._open_requirements(src.id, sections(DS)[:1])
    disputed = [f for f in found if f.code == "REQUIREMENT_DISPUTED"]
    assert disputed and disputed[0].severity == "ERROR" and "reviewer disagrees" in disputed[0].message
    replies.append([{"id": "REQ-001", "verdict": "agree", "where": "", "why": "settings are written first now"}])
    belt.call("resolve_requirements", {"items": [{"id": "REQ-001", "status": "implemented", "reason": "setup() writes OSR, ODR, then PWR_CTRL"}]})
    assert Requirements(p.root).disputed() == []


def test_three_disputes_leave_it_to_a_person(tmp_path):
    p, belt, src = _belt(tmp_path, reviewer=lambda m: '[{"id": "REQ-001", "verdict": "disagree", "why": "no"}]')
    for k in range(3):
        belt.call("resolve_requirements", {"items": [{"id": "REQ-001", "status": "not_applicable", "reason": f"attempt {k}: does not apply here"}]})
    from embedforge.truth.outline import sections
    f = next(x for x in belt._open_requirements(src.id, sections(DS)[:1]) if x.code == "REQUIREMENT_DISPUTED")
    assert f.severity == "WARNING" and "left for a person" in f.message


def test_without_a_reviewer_claims_stand(tmp_path):
    p, belt, src = _belt(tmp_path)
    res = belt.call("resolve_requirements", {"items": [{"id": "REQ-001", "status": "implemented", "reason": "setup() configures the sensor"}]})
    assert "review" not in res and Requirements(p.root).disputed() == []


def test_skills_are_listed_and_loaded(tmp_path):
    names = [n for n, _ in list_skills()]
    assert {"sensor-driver", "datasheet-rules", "rp2-arduino-pico"} <= set(names)
    assert "settings first, the mode last" in load_skill("sensor-driver").lower()
    assert "SKILLS (playbooks" in get_active_prompt_text() and "- sensor-driver:" in get_active_prompt_text()
    p, belt, src = _belt(tmp_path)
    assert "Wire.setSDA(pin)" in belt.call("load_skill", {"name": "rp2-arduino-pico"})["playbook"]
    assert "No skill" in belt.call("load_skill", {"name": "nope"})["error"]
