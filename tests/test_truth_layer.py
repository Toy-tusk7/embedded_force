"""Anti-hallucination tests: the truth layer must make it impossible to smuggle in unsourced facts."""

from pathlib import Path

import pytest

from embedforge.truth import FactLedger, SourceStore, classify_url, verify_project, TrustTier
from embedforge.truth.ledger import value_in_text, parse_int

DATASHEET = """
MPU-6000/MPU-6050 Register Map and Descriptions
Register 107 – Power Management 1
Register (Hex) 6B  Register (Decimal) 107
Bit6 SLEEP: When set to 1, the MPU-60X0 is put into sleep mode.
Register 117 – Who Am I
Register (Hex) 75 Register (Decimal) 117
This register is used to verify the identity of the device. The contents of WHO_AM_I are the upper 6 bits of
the MPU-60X0’s 7-bit I2C address. The default value is 0x68.
The slave address of the MPU-60X0 is b110100X which is 7 bits long. The LSB bit of the 7 bit address is
determined by the logic level on pin AD0.
"""


@pytest.fixture
def env(tmp_path):
    store = SourceStore(tmp_path)
    src = store.add("https://invensense.tdk.com/download-pdf/mpu-6000-register-map/", "MPU6050 Register Map", DATASHEET, kind="pdf")
    return tmp_path, store, FactLedger(tmp_path, store), src


def test_trust_tiers():
    assert classify_url("https://invensense.tdk.com/x.pdf") == TrustTier.OFFICIAL_MANUFACTURER
    assert classify_url("https://www.st.com/resource/en/datasheet/x.pdf") == TrustTier.OFFICIAL_MANUFACTURER
    assert classify_url("https://github.com/ArduPilot/ardupilot/blob/x") == TrustTier.OFFICIAL_REPO
    assert classify_url("https://github.com/randomuser/mpu6050") == TrustTier.UNOFFICIAL
    assert classify_url("https://stackoverflow.com/q/1") == TrustTier.UNOFFICIAL
    assert classify_url("https://evil-st.com.attacker.io/x") == TrustTier.UNOFFICIAL


def test_real_quote_is_accepted(env):
    _, _, ledger, src = env
    r = ledger.record("MPU6050", "who_am_i_default", "0x68", src.id, "The default value is 0x68.")
    assert r["ok"], r
    assert r["fact"]["authoritative"] is True
    assert r["fact"]["id"] == "FACT-001"


def test_fabricated_quote_is_rejected(env):
    _, _, ledger, src = env
    r = ledger.record("MPU6050", "i2c_address", "0x69", src.id, "The I2C address of the MPU6050 is always 0x69 on this board.")
    assert not r["ok"] and "verbatim" in r["error"]
    assert ledger.facts == {}


def test_value_must_be_inside_quote(env):
    _, _, ledger, src = env
    r = ledger.record("MPU6050", "pwr_mgmt_1", "0x6C", src.id, "Register (Hex) 6B  Register (Decimal) 107")
    assert not r["ok"] and "does not appear inside" in r["error"]


def test_pdf_whitespace_and_hex_forms_are_tolerated(env):
    _, _, ledger, src = env
    # datasheet writes the address as bare hex "6B" -> accepted but flagged LOOSE
    r = ledger.record("MPU6050", "PWR_MGMT_1", "0x6B", src.id, "Register (Hex) 6B Register (Decimal) 107")
    assert r["ok"] and r["fact"]["confidence"] == "LOOSE"
    assert any("loosely" in w for w in r["warnings"])


def test_unknown_source_and_short_quote_rejected(env):
    _, _, ledger, src = env
    assert not ledger.record("X", "y", "0x68", "SRC-999", "whatever quote text here")["ok"]
    assert not ledger.record("X", "y", "0x68", src.id, "0x68")["ok"]


def test_unofficial_source_marked_non_authoritative(env):
    tmp, store, ledger, _ = env
    s = store.add("https://random-blog.example/mpu", "blog", "Our blog says the slave address is 0x68 for MPU6050 boards.")
    r = ledger.record("MPU6050", "i2c_address", "0x68", s.id, "the slave address is 0x68 for MPU6050 boards")
    assert r["ok"] and r["fact"]["authoritative"] is False
    assert any("NOT authoritative" in w for w in r["warnings"])


def test_conflicting_facts_are_flagged(env):
    tmp, store, ledger, src = env
    ledger.record("MPU6050", "i2c_address", "0x68", src.id, "The default value is 0x68.")
    s2 = store.add("https://invensense.tdk.com/other.pdf", "other", "Strapped high the address becomes 0x69 on some boards.")
    r = ledger.record("MPU6050", "i2c_address", "0x69", s2.id, "the address becomes 0x69 on some boards")
    assert r["ok"] and any("CONFLICT" in w for w in r["warnings"])
    assert len(ledger.conflicts()) == 1


def test_value_matching_helpers():
    assert value_in_text("0x68", "default 0x68")[1] == "STRICT"
    assert value_in_text("400", "max clock 400kHz")[0] == "number"
    assert value_in_text("0x68", "no numbers here") is None
    assert parse_int("0x6B") == 107


def _project(tmp_path: Path, body: str) -> Path:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "main.c").write_text(body, encoding="utf-8")
    return tmp_path


def test_verifier_flags_unsourced_constant(env):
    tmp, _, ledger, _ = env
    _project(tmp, "#define MPU_ADDR 0x68\nint main(void){return 0;}\n")
    rep = verify_project(tmp, ledger)
    assert not rep.passed
    assert rep.findings[0].code == "UNSOURCED_CONSTANT"


def test_verifier_accepts_tagged_constant_and_traces_it(env):
    tmp, _, ledger, src = env
    f = ledger.record("MPU6050", "who_am_i_default", "0x68", src.id, "The default value is 0x68.")["fact"]
    _project(tmp, f"#define MPU_ID 0x68 /* {f['id']} */\nint main(void){{return 0;}}\n")
    rep = verify_project(tmp, ledger)
    assert rep.passed, rep.findings
    assert rep.traceability[0]["fact_id"] == f["id"]
    assert "0x68" in rep.traceability[0]["quote"]


def test_verifier_rejects_tag_with_wrong_value(env):
    """The LLM cites a real fact but writes a different number — must be caught."""
    tmp, _, ledger, src = env
    f = ledger.record("MPU6050", "who_am_i_default", "0x68", src.id, "The default value is 0x68.")["fact"]
    _project(tmp, f"#define MPU_ID 0x69 /* {f['id']} */\n")
    rep = verify_project(tmp, ledger)
    assert not rep.passed and rep.findings[0].code == "FACT_VALUE_MISMATCH"


def test_verifier_rejects_unknown_fact_tag(env):
    tmp, _, ledger, _ = env
    _project(tmp, "#define MPU_ID 0x68 /* FACT-042 */\n")
    rep = verify_project(tmp, ledger)
    codes = {f.code for f in rep.findings}
    assert "UNKNOWN_FACT_TAG" in codes and not rep.passed


def test_verifier_checks_decimal_hw_defines_and_ignores_comments(env):
    tmp, _, ledger, _ = env
    _project(tmp, "// register 0x6B is mentioned only in a comment\n#define SENSOR_REG_X 107\n")
    rep = verify_project(tmp, ledger)
    assert [f.code for f in rep.findings] == ["UNSOURCED_CONSTANT"]  # decimal define flagged, comment ignored


def test_verifier_warns_on_non_authoritative_backing(env):
    tmp, store, ledger, _ = env
    s = store.add("https://random-blog.example/mpu", "blog", "Our blog says the slave address is 0x68 for MPU6050 boards.")
    f = ledger.record("MPU6050", "i2c_address", "0x68", s.id, "the slave address is 0x68 for MPU6050 boards")["fact"]
    _project(tmp, f"#define MPU_ADDR 0x68 /* {f['id']} */\n")
    rep = verify_project(tmp, ledger)
    assert rep.passed  # not an error...
    assert any(x.code == "NON_AUTHORITATIVE_SOURCE" for x in rep.findings)  # ...but surfaced
