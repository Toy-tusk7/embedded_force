"""Context building, the ground-truth state block, build/verify feedback and the loop changes that keep a mid-size
model on track (no LLM or toolchain needed)."""

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from embedforge.agent import context as ctx
from embedforge.agent import loop as loop_mod
from embedforge.agent.events import RunSession
from embedforge.agent.tools import ToolBelt
from embedforge.agent.workspace import Project
from embedforge.toolchain import betaflight
from embedforge.toolchain.pio import BuildResult, Diagnostic
from embedforge.truth import FactLedger, SourceStore, verify_project

from test_agent_loop import _call, _events, _run


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project("t2", tmp_path / "t2")


def _belt(project: Project) -> ToolBelt:
    store = SourceStore(project.root)
    return ToolBelt(project, RunSession("r", project.id), store, FactLedger(project.root, store))


def _history(turns: int, body: int = 3000) -> List[Dict[str, Any]]:
    h: List[Dict[str, Any]] = [{"role": "user", "content": "Write the Betaflight config for my board\n\n[Board imported from x: 3 parts]"}]
    for i in range(turns):
        h.append({"role": "assistant", "content": "", "tool_calls": [_call(i, "write_file", {"path": "src/a.c", "content": "x" * body})]})
        h.append({"role": "tool", "tool_call_id": f"c{i}", "content": json.dumps({"path": "src/a.c", "pad": "y" * body}),
                  "_digest": f"write_file src/a.c #{i}"})
        if i == 2:
            h.append(loop_mod.notice("old stall notice"))
    h.append({"role": "user", "content": "continue"})
    return h


# ------------------------------------------------------------------ context
def test_context_keeps_pairs_intact_and_digests_old_results():
    out = ctx.build_context(_history(30))
    ids = [tc["id"] for m in out if m["role"] == "assistant" for tc in m.get("tool_calls", [])]
    answered = [m["tool_call_id"] for m in out if m["role"] == "tool"]
    assert ids == answered                                   # every call has its result, in order, nothing orphaned
    tools = [m for m in out if m["role"] == "tool"]
    assert tools[0]["content"] == "write_file src/a.c #0"     # old result -> digest
    assert tools[-1]["content"].startswith('{"path"')        # recent result -> verbatim
    assert "chars elided" in out[1]["tool_calls"][0]["function"]["arguments"]
    assert not any("_digest" in m or "_ephemeral" in m for m in out)
    assert not any("old stall notice" in str(m.get("content")) for m in out)   # stale notice dropped
    assert out[0]["content"] == "Write the Betaflight config for my board"   # old harness context stripped


def test_context_respects_budget_but_keeps_user_turns():
    out = ctx.build_context(_history(60, body=4000), budget=60_000)
    total = sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or [])) for m in out)
    assert total <= 60_000
    assert out[0]["role"] == "user" and out[-1]["content"] == "continue"
    assert sum(1 for m in out if m["role"] == "assistant") >= ctx.VERBATIM_TURNS


def test_persistence_drops_notices_and_never_starts_with_tool():
    h = _history(5)
    kept = ctx.for_persistence(h, max_messages=6)
    assert kept[0]["role"] != "tool" and not any(ctx.is_notice(m) for m in kept)


# ------------------------------------------------------------------ state block
def _bf_project(project: Project) -> None:
    project.update_meta(kind="betaflight", board="MYFC", bf_target="RP2350B", mcu="RP2350B", framework="2026.6.2")


def test_state_block_next_steps_follow_real_results(project):
    _bf_project(project)
    belt = _belt(project)
    _, nxt = ctx.state_block(project, belt.ledger, belt, [{"role": "user", "content": "build my flight controller config please"}])
    assert nxt.startswith("write_file the config.h")
    cfg = project.root / "betaflight" / "configs" / "CUST" / "MYFC" / "config.h"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("#pragma once\n", encoding="utf-8")
    assert ctx.state_block(project, belt.ledger, belt, [])[1].startswith("build")
    project.update_meta(last_build={"hash": belt.source_hash(), "ok": False, "diagnostics": ["config.h:9: 'I2C0' undeclared"]})
    text, nxt = ctx.state_block(project, belt.ledger, belt, [])
    assert "I2C0" in nxt and "Last build: FAILED (current sources)" in text
    project.update_meta(last_build={"hash": belt.source_hash(), "ok": True}, last_verify={"hash": "old", "passed": True})
    assert ctx.state_block(project, belt.ledger, belt, [])[1] == "verify"
    project.update_meta(last_verify={"hash": belt.source_hash(), "passed": True, "summary": "PASS"})
    assert ctx.state_block(project, belt.ledger, belt, [])[1].startswith("finish")


def test_latest_request_skips_short_replies():
    h = [{"role": "user", "content": "Make the Betaflight config for the RP2354B flight controller from my schematic"},
         loop_mod.notice("x" * 80), {"role": "user", "content": "continue"}]
    assert ctx.latest_request(h).startswith("Make the Betaflight config")


# ------------------------------------------------------------------ Betaflight config feedback
GOOD_CFG = """#pragma once
#define FC_TARGET_MCU     RP2350B
#define BOARD_NAME        MYFC
#define MANUFACTURER_ID   CUST
#define USE_BARO
#define BARO_I2C_INSTANCE I2CDEV_0
#define I2C0_SDA_PIN      PA44 /* FACT-001 */
#define I2C0_SCL_PIN      PA45 /* FACT-002 */
#define SPI0_SCK_PIN      PA2  /* FACT-003 */
#define SPI0_SDI_PIN      PA4  /* FACT-004 */
#define SPI0_SDO_PIN      PA3  /* FACT-005 */
#define GYRO_1_SPI_INSTANCE SPI0
"""


def test_lint_accepts_reference_style_config():
    assert betaflight.lint_config(GOOD_CFG, "MYFC", "RP2350B") == {"errors": [], "warnings": []}


def test_lint_catches_common_mistakes():
    bad = GOOD_CFG.replace("I2CDEV_0", "I2C0").replace("#pragma once\n", "#ifndef X\n") + "#define HSE_VALUE 12000000\n// FACT-SRC001\n"
    lint = betaflight.lint_config(bad, "MYFC", "RP2350B")
    msgs = " | ".join(lint["errors"])
    assert "I2CDEV_n (here I2CDEV_0)" in msgs and "HSE_VALUE" in msgs and "without a matching #endif" in msgs
    assert any("pragma once" in w for w in lint["warnings"])
    assert any("'FACT-SRC001' is not a fact id" in w for w in lint["warnings"])   # verify's job; must not block the build


def test_write_file_returns_lint_and_pin_map(project):
    _bf_project(project)
    res = _belt(project).write_file("betaflight/configs/CUST/MYFC/config.h", GOOD_CFG.replace("I2CDEV_0", "I2C0"))
    assert "I2C0_SDA_PIN = PA44 [FACT-001]" in res["pins"]
    assert any("I2CDEV_0" in x for x in res["lint"])
    assert "lint" not in _belt(project).write_file("src/main.c", "int main(void){return 0;}\n")


def test_replace_miss_points_at_the_line(project):
    belt = _belt(project)
    belt.write_file("src/main.c", "int a;\n#define LED_PIN 13\nint b;\n")
    err = belt.replace_in_file("src/main.c", "#define LED_PIN 13 \n", "x")["error"]
    assert "2: #define LED_PIN 13" in err


# ------------------------------------------------------------------ build feedback
def test_failed_build_shows_source_lines_and_unchanged_rebuild_is_refused(project, monkeypatch):
    from embedforge.toolchain import pio
    calls = []

    def fake_build(root, on_line=None):
        calls.append(1)
        return BuildResult(ok=False, diagnostics=[Diagnostic("error", "src/main.c", 2, "'x' undeclared")] * 2,
                           log_tail="lots of log", error="build failed")

    monkeypatch.setattr(pio, "build", fake_build)
    belt = _belt(project)
    belt.write_file("src/main.c", "int a;\nint b = x;\nint c;\n")
    out = belt.build()
    assert len(out["diagnostics"]) == 1 and ">2: int b = x;" in out["diagnostics"][0]["context"]
    assert "log_tail" not in out and "FIRST" in out["next"]
    assert project.meta()["last_build"]["diagnostics"] == ["src/main.c:2: 'x' undeclared"]
    again = _belt(project).build()                  # a fresh ToolBelt (next run) still knows the build failed
    assert again.get("unchanged") and "'x' undeclared" in again["error"] and len(calls) == 1
    belt.replace_in_file("src/main.c", "int b = x;", "int b = 0;")
    belt.build()
    assert len(calls) == 2


# ------------------------------------------------------------------ verifier
def test_verifier_flags_made_up_fact_tags(tmp_path):
    store = SourceStore(tmp_path)
    ledger = FactLedger(tmp_path, store)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.c").write_text("#define LED_PIN 13 /* FACT-SRC001 */\n", encoding="utf-8")
    codes = [f.code for f in verify_project(tmp_path, ledger).findings]
    assert "MALFORMED_FACT_TAG" in codes


# ------------------------------------------------------------------ loop behaviour
def test_bad_args_and_guard_blocks_are_not_failures(project, monkeypatch):
    turns = [[{"id": "c0", "type": "function", "function": {"name": "list_files", "arguments": "{not json"}}],
             [_call(1, "list_files", {})], [_call(2, "list_files", {})], [_call(3, "list_files", {})], "ok"]
    session = _run(project, turns, monkeypatch)
    stats = _events(session, "run_stats")[-1]
    assert stats["failures"] == {} and stats["blocked"] == {"list_files": 2}


def test_build_failures_between_edits_are_never_blocked(project, monkeypatch):
    from embedforge.toolchain import pio
    monkeypatch.setattr(pio, "build", lambda root, on_line=None: BuildResult(ok=False, error="boom",
                        diagnostics=[Diagnostic("error", "src/main.c", 1, "bad")]))
    turns = []
    for i in range(5):
        turns.append([_call(2 * i, "write_file", {"path": "src/main.c", "content": f"int v{i};\n"})])
        turns.append([_call(2 * i + 1, "build", {})])
    session = _run(project, turns + ["stop"], monkeypatch)
    builds = [r for r in _events(session, "tool_result") if r["name"] == "build"]
    assert len(builds) == 5 and not any(g["kind"] == "repeated_failure" for g in _events(session, "guard"))


def test_truncated_reply_is_retried_not_treated_as_the_answer(project, monkeypatch):
    turns = [{"content": "The user wants me to", "finish_reason": "length"}, "Final answer."]
    session = _run(project, turns, monkeypatch, message="hello there, what can you do for me today")
    msgs = [e["text"] for e in _events(session, "message")]
    assert msgs == ["Final answer."]
    assert any("cut off" in str(m.get("content")) for m in session.llm.seen[1])


def test_text_reply_mid_task_gets_one_reminder(project, monkeypatch):
    _bf_project(project)
    session = _run(project, ["Shall I go ahead and write the config?", "Waiting for you."], monkeypatch,
                   message="Write the Betaflight config for this board")
    assert [g["kind"] for g in _events(session, "guard")] == ["continue"]
    assert len(session.llm.seen) == 2 and "NEXT STEP" in session.llm.seen[1][0]["content"]


def test_model_sees_state_block_and_betaflight_prompt(project, monkeypatch):
    _bf_project(project)
    session = _run(project, ["?"], monkeypatch, message="What pins does the gyro use?")
    system = session.llm.seen[0][0]["content"]
    assert "Betaflight flight-controller board config" in system and "PROJECT STATE" in system


# ------------------------------------------------------------------ batch pin facts
BOARD_REPORT = ("Board connectivity: fc\nU1 pin 21 (GPIO14) -> net GYRO_SCK -> U4.13 (SCLK)\n"
                "U1 pin 22 (GPIO15) -> net GYRO_MOSI -> U4.14 (SDI)\nU1 pin 52 (GPIO41_ADC1) -> net ADC_VBAT -> R8.1\n"
                "U1 pin 2 (GPIO4) -> net LED1 -> D1.1\nU1 pin 77 (GPIO0) -> not connected\n")


def _board_belt(project: Project) -> ToolBelt:
    store = SourceStore(project.root)
    store.add("user://board", "Board connectivity (fc)", BOARD_REPORT, kind="schematic", replace=True)
    return ToolBelt(project, RunSession("r", project.id), store, FactLedger(project.root, store))


def test_record_board_pins_takes_values_from_verbatim_lines(project):
    belt = _board_belt(project)
    res = belt.record_board_pins([
        {"define": "SPI0_SCK_PIN", "line": "U1 pin 21 (GPIO14) -> net GYRO_SCK -> U4.13 (SCLK)"},
        {"define": "spi0_sdo_pin", "line": "U1 pin 22 (GPIO15) -> net GYRO_MOSI -> U4.14 (SDI) …"},
        {"define": "GYRO_1_CS_PIN", "line": "U1 pin 23 (GPIO16) -> net GYRO_CS -> U4.1 (CS)"},     # not in the schematic
        {"define": "BEEPER_PIN", "line": "beeper is on GPIO6"},
        {"define": "ADC_VBAT_PIN", "line": "U1 pin 52 (GPIO41_ADC1) -> net ADC_VBAT -> R8.1"},
        {"define": "FLASH_CS_PIN", "line": "U1 pin 77 (GPIO0) -> not connected"},
        {"function": "led", "line": "U1 pin 2 (GPIO4) -> net LED1 -> D1.1"},                    # older form still works
    ])
    assert [r.rsplit(" ", 1)[0] for r in res["recorded"]] == ["SPI0_SCK_PIN=GPIO14", "SPI0_SDO_PIN=GPIO15",
                                                               "ADC_VBAT_PIN=GPIO41", "led=GPIO4"]
    rejected = {r["define"]: r["error"] for r in res["rejected"]}
    assert set(rejected) == {"GYRO_1_CS_PIN", "BEEPER_PIN", "FLASH_CS_PIN"} and "not connected" in rejected["FLASH_CS_PIN"]
    fact = next(f for f in belt.ledger.facts.values() if f.property == "SPI0_SCK_PIN")
    assert fact.subject == "U1" and fact.value == "GPIO14" and fact.source_url == "user://board"
    again = belt.record_board_pins([{"define": "SPI0_SCK_PIN", "line": "U1 pin 21 (GPIO14) -> net GYRO_SCK -> U4.13 (SCLK)"}])
    assert again.get("all_duplicates") and loop_mod._no_op(again)
    clash = belt.record_board_pins([{"define": "SPI0_SCK_PIN", "line": "U1 pin 22 (GPIO15) -> net GYRO_MOSI -> U4.14 (SDI)"}])
    assert "one define has one pin" in clash["rejected"][0]["error"]


def test_record_board_pins_tags_matching_config_lines_only(project):
    _bf_project(project)
    belt = _board_belt(project)
    cfg = "#pragma once\n#define SPI0_SCK_PIN PA14 // FACT-SRC001: gyro clock\n#define LED0_PIN PA5\n#define SPI0_SDO_PIN PA15\n"
    belt.write_file("betaflight/configs/CUST/MYFC/config.h", cfg)
    res = belt.record_board_pins([{"define": "SPI0_SCK_PIN", "line": "U1 pin 21 (GPIO14) -> net GYRO_SCK -> U4.13 (SCLK)"},
                                  {"define": "LED0_PIN", "line": "U1 pin 2 (GPIO4) -> net LED1 -> D1.1"},
                                  {"define": "ADC_VBAT_PIN", "line": "U1 pin 52 (GPIO41_ADC1) -> net ADC_VBAT -> R8.1"}])
    text = (project.root / "betaflight/configs/CUST/MYFC/config.h").read_text(encoding="utf-8")
    sck = next(f.id for f in belt.ledger.facts.values() if f.property == "SPI0_SCK_PIN")
    assert f"#define SPI0_SCK_PIN PA14 /* {sck} */" in text and "FACT-SRC001" not in text
    assert "#define LED0_PIN PA5\n" in text                         # pin differs from the fact: never touched
    assert res["mismatched"] and "LED0_PIN is PA5" in res["mismatched"][0]
    assert res["not_in_config"] == ["ADC_VBAT_PIN"] and res["tagged"] == ["SPI0_SCK_PIN line 2"]
    assert not loop_mod._no_op(res)


def test_state_block_lists_untraced_config_pins_and_points_at_record_board_pins(project):
    _bf_project(project)
    belt = _board_belt(project)
    from embedforge.eda.board import Board, Part, Pin
    b = Board(name="fc")
    u1 = b.part("U1"); u1.value = "RP2354B"; u1.mpn = "RP2354B"
    for num, name, net in (("21", "GPIO14", "GYRO_SCK"), ("2", "GPIO4", "LED1"), ("77", "GPIO0", "")):
        u1.pins[num] = Pin(num, name, net)
    for i in range(8):  # a real MCU symbol has many pins
        u1.pins[str(60 + i)] = Pin(str(60 + i), "GND", "")
    for ref in ("U4", "D1"):
        b.part(ref).pins["1"] = Pin("1", "", {"U4": "GYRO_SCK", "D1": "LED1"}[ref])
    (project.root / "board.json").write_text(json.dumps(b.to_json()), encoding="utf-8")
    belt.write_file("betaflight/configs/CUST/MYFC/config.h", "#pragma once\n#define SPI0_SCK_PIN PA14\n#define FLASH_CS_PIN PA0 /* FACT-SRC001 */\n")
    text, nxt = ctx.state_block(project, belt.ledger, belt, [])
    assert "SPI0_SCK_PIN=PA14 -> GPIO14 net GYRO_SCK (no fact tag)" in text
    assert "FLASH_CS_PIN=PA0 -> GPIO0 NOT CONNECTED (remove) (invalid tag)" in text
    assert nxt.startswith("record_board_pins")


# ------------------------------------------------------------------ truth details
def test_pin_fact_value_does_not_match_a_longer_gpio(tmp_path):
    store = SourceStore(tmp_path)
    src = store.add("user://board", "board", "U1 pin 52 (GPIO41_ADC1) -> net ADC_VBAT -> R8.1\n", kind="schematic")
    ledger = FactLedger(tmp_path, store)
    assert not ledger.record("U1", "LED0_PIN", "GPIO4", src.id, "U1 pin 52 (GPIO41_ADC1) -> net ADC_VBAT -> R8.1").get("ok")
    assert ledger.record("U1", "ADC_VBAT_PIN", "GPIO41", src.id, "U1 pin 52 (GPIO41_ADC1) -> net ADC_VBAT -> R8.1").get("ok")


def test_verifier_checks_define_name_of_pin_facts(tmp_path):
    store = SourceStore(tmp_path)
    src = store.add("user://board", "board", "U1 pin 21 (GPIO14) -> net GYRO_SCK -> U4.13 (SCLK)\n", kind="schematic")
    ledger = FactLedger(tmp_path, store)
    fid = ledger.record("U1", "SPI0_SCK_PIN", "GPIO14", src.id, "U1 pin 21 (GPIO14) -> net GYRO_SCK -> U4.13 (SCLK)")["fact"]["id"]
    (tmp_path / "betaflight").mkdir()
    (tmp_path / "betaflight" / "config.h").write_text(f"#define SPI0_SCK_PIN PA14 /* {fid} */\n#define LED0_PIN PA14 /* {fid} */\n",
                                                      encoding="utf-8")
    codes = [(f.code, f.line) for f in verify_project(tmp_path, ledger, pico=True).findings]
    assert codes == [("FACT_DEFINE_MISMATCH", 2)]


def test_truncate_keeps_status_keys():
    from embedforge.agent.tools import _truncate
    big = {"passed": False, "summary": "FAIL: 40 errors", "findings": [{"message": "x" * 400}] * 40}
    out = _truncate(big, 7000)
    assert out["passed"] is False and out["summary"].startswith("FAIL") and out["truncated"] and out["findings_omitted"] > 0
    assert len(json.dumps(out)) <= 7000 and len(big["findings"]) == 40      # the original is untouched


def test_board_query_matches_pin_tokens_and_function_words():
    from embedforge.eda.board import Board, Pin
    b = Board(name="fc")
    u1 = b.part("U1"); u1.value = u1.mpn = "RP2354B"
    for num, name, net in (("54", "GPIO43_ADC3", "FLASH_SDI"), ("21", "GPIO14", "SPI0_CS")):
        u1.pins[num] = Pin(num, name, net)
    for i in range(8):
        u1.pins[str(60 + i)] = Pin(str(60 + i), "GND", "")
    imu = b.part("U5"); imu.value = imu.mpn = "BMI270"
    imu.pins["1"] = Pin("1", "CSB", "SPI0_CS")
    sd = b.part("U6"); sd.value = "SD-CARD 11P"
    sd.pins["2"] = Pin("2", "CMD", "FLASH_SDI")
    assert b.query("GPIO43")["lines"] == ["U1 pin 54 (GPIO43_ADC3) -> net FLASH_SDI -> U6.2 (CMD)"]
    assert b.query("PA14")["lines"][0].startswith("U1 pin 21 (GPIO14)")
    assert any(ln.startswith("U5 pin 1") for ln in b.query("gyro")["lines"])
    assert b.mcu_pin_lines(43) == ["U1 pin 54 (GPIO43_ADC3) -> net FLASH_SDI -> U6.2 (CMD)"]
    miss = b.query("U11")
    assert "error" in miss and "GPIO43_ADC3" not in miss["error"] and "FLASH_SDI" in miss["available_nets"]


def test_lookup_misses_never_lock_out_lookups_and_finish_rejections_never_block(project, monkeypatch):
    turns = [[_call(i, "query_board", {"query": f"U{20 + i}"})] for i in range(4)]
    turns += [[_call(10 + i, "finish", {"status": "success", "summary": "done"})] for i in range(4)]
    turns += [[_call(20, "finish", {"status": "blocked", "summary": "cannot build yet"})]]
    session = _run(project, turns, monkeypatch)
    assert not any(g["kind"] == "repeated_failure" for g in _events(session, "guard"))
    assert _events(session, "done")[0]["status"] == "blocked"
    walk = (project.root / "artifacts" / "walkthrough.md").read_text(encoding="utf-8")
    assert "Not delivered" in walk and "compiled and statically verified" not in walk


def test_repeated_cut_offs_end_the_run_without_keeping_partial_text(project, monkeypatch):
    cut = {"content": "The user wants me to", "finish_reason": "length"}
    session = _run(project, [cut, cut, cut, "never reached"], monkeypatch, message="hello there, what can you do for me today")
    assert not _events(session, "message")
    assert any("cut off at the token limit 3 times" in e["message"] for e in _events(session, "error"))
    assert not any(m["role"] == "assistant" for m in project.load_conversation())


def test_betaflight_target_from_part_number():
    t = ["AT32F435G", "RP2350A", "RP2350B", "STM32F405", "STM32F4", "STM32H743"]
    assert betaflight.target_for_part("RP2354B", t) == "RP2350B" and betaflight.target_for_part("rp2354a", t) == "RP2350A"
    assert betaflight.target_for_part("STM32F405RGT6", t) == "STM32F405" and betaflight.target_for_part("RP2350B", t) == "RP2350B"
    assert betaflight.target_for_part("ESP32", t) is None


def test_next_step_for_a_betaflight_request_is_init_betaflight(project):
    belt = _belt(project)
    h = [{"role": "user", "content": "Build Betaflight firmware for my RP2354B flight controller from the schematic."}]
    assert ctx.state_block(project, belt.ledger, belt, h)[1].startswith("init_betaflight")

# ------------------------------------------------------------------ schematic-consistency checks
def _fc_board():
    from embedforge.eda.board import Board, Pin
    b = Board(name="fc")
    u1 = b.part("U1"); u1.value = u1.mpn = "RP2354B"
    wiring = {"18": ("GPIO18", "SPI0_SCK"), "14": ("GPIO14", "SPI0_CS"), "13": ("GPIO13", "INT1_ICM"),
              "42": ("GPIO42_ADC2", "FLASH_SCL"), "46": ("GPIO46_ADC6", "FLASH_CS"), "28": ("GPIO28", "MOTO0")}
    for num, (name, net) in wiring.items():
        u1.pins[num] = Pin(num, name, net)
    for i in range(8):
        u1.pins[str(60 + i)] = Pin(str(60 + i), "GND", "")
    imu = b.part("U5"); imu.value = imu.mpn = "BMI270"
    imu.pins.update({"13": Pin("13", "SCX", "SPI0_SCK"), "12": Pin("12", "CSB", "SPI0_CS"), "4": Pin("4", "INT1", "INT1_ICM")})
    sd = b.part("U6"); sd.value = "SD-CARD 11P"
    sd.pins.update({"5": Pin("5", "CLK", "FLASH_SCL"), "1": Pin("1", "CD/DAT3", "FLASH_CS")})
    cn = b.part("CN1"); cn.value = "BM07B"
    cn.pins["4"] = Pin("4", "", "MOTO0")
    return b


def test_board_consistency_catches_copied_and_misrouted_hardware():
    cfg = ("#define USE_ACCGYRO_BMI270\n#define USE_MAX7456\n#define SPI0_SCK_PIN PA18\n#define GYRO_1_SPI_INSTANCE SPI0\n"
           "#define GYRO_1_CS_PIN PA14\n#define USE_SDCARD\n#define SDCARD_SPI_INSTANCE SPI0\n#define MOTOR1_PIN PA28\n")
    found = {(c, ln): msg for sev, c, ln, msg, _ in betaflight.board_consistency(cfg, _fc_board())}
    assert ("DEVICE_NOT_ON_BOARD", 2) in found and "MAX7456" in found[("DEVICE_NOT_ON_BOARD", 2)]
    assert ("BUS_DEVICE_MISMATCH", 7) in found and "SDCARD_SPI_CS_PIN is not defined" in found[("BUS_DEVICE_MISMATCH", 7)]
    unused = next(m for (c, _), m in found.items() if c == "WIRED_PIN_UNUSED")
    assert "GPIO13 (INT1_ICM -> U5)" in unused and "GPIO46 (FLASH_CS -> U6)" in unused
    assert not any(c == "DEVICE_NOT_ON_BOARD" and "BMI270" in m for (c, _), m in found.items())   # the gyro is on the board
    # SD chip select on the right part but the bus clock goes to the gyro
    wrong_bus = cfg.replace("#define USE_MAX7456\n", "") + "#define SDCARD_SPI_CS_PIN PA46\n"
    msgs = [m for _, c, _, m, _ in betaflight.board_consistency(wrong_bus, _fc_board()) if c == "BUS_DEVICE_MISMATCH"]
    assert msgs == ["SDCARD_SPI_CS_PIN PA46 goes to U6, but SPI0_SCK_PIN does not reach that part."]


def test_unknown_defines_are_reported_from_one_source_search(monkeypatch):
    from embedforge.toolchain import wsl
    calls = []
    monkeypatch.setattr(betaflight, "_KNOWN_NAMES", {})
    monkeypatch.setattr(wsl, "run", lambda script, sink, t: (calls.append(script), (0, ["MOTOR1_PIN", "USE_ACCGYRO_BMI270"], ""))[1])
    names = ["MOTOR0_PIN", "MOTOR1_PIN", "USE_ACCGYRO_BMI270", "USE_GYRO_BMI270", "BOARD_NAME"]
    assert betaflight.unknown_defines(names) == ["MOTOR0_PIN", "USE_GYRO_BMI270"]
    assert betaflight.unknown_defines(names) == ["MOTOR0_PIN", "USE_GYRO_BMI270"] and len(calls) == 1   # cached


def test_replace_retires_a_pin_fact_bound_to_the_wrong_define(project):
    belt = _board_belt(project)
    first = belt.record_board_pins([{"define": "SPI0_SDI_PIN", "line": "U1 pin 22 (GPIO15) -> net GYRO_MOSI -> U4.14 (SDI)"}])
    old = first["recorded"][0].split()[-1]
    fixed = belt.record_board_pins([{"define": "SPI0_SDI_PIN", "line": "U1 pin 21 (GPIO14) -> net GYRO_SCK -> U4.13 (SCLK)",
                                     "replace": True}])
    new = fixed["recorded"][0].split()[-1]
    assert fixed["retired"] == [f"{old} (SPI0_SDI_PIN=GPIO15)"] and old not in belt.ledger.facts and new != old
    assert int(new.split("-")[1]) > int(old.split("-")[1])                       # ids are never reused
    retired = json.loads((project.root / "ledger_retired.json").read_text(encoding="utf-8"))
    assert retired[0]["id"] == old and "re-recorded" in retired[0]["retired_reason"]

def test_build_script_isolates_projects_that_share_a_board_name():
    s = betaflight.build_script("/mnt/c/p2/betaflight", "RP2354B", "RP2350B")
    guard = s.index("built from another project")
    assert s.index("rm -rf obj/main/RP2350B_RP2354B") > guard and s.index("make CONFIG=") > s.index(".embedforge_config_dir")
    assert "rm -f obj/main/betaflight_RP2350B_RP2354B.elf" in s                     # always relink: fresh outputs only
    assert f"obj/betaflight_{betaflight.BF_TAG}_RP2350B_RP2354B.uf2" in s and "obj/*_RP2354B.uf2" not in s