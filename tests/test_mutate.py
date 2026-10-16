"""Mutation testing of a delivered driver: the operators plant realistic faults (never in comments, declarations or
pacing delays), verify runs on each, and the score and survivors are reported."""

from embedforge.truth import mutate

CODE = """#include <Wire.h>
#define REG_PWR 0x1B   /* FACT-001 */
#define STARTUP_MS 2   /* FACT-002 */
struct Calib { int16_t par; uint16_t t1; };
bool write_reg(uint8_t reg, uint8_t val);
bool write_reg(uint8_t reg, uint8_t val) {
    Wire.beginTransmission(0x76);  // FACT-003
    return Wire.endTransmission() == 0;
}
bool init_dev(uint8_t *d) {
    delay(STARTUP_MS);
    if (!write_reg(REG_PWR, 0x33)) {
        return false;
    }
    uint32_t raw = ((uint32_t)d[2] << 16) | ((uint32_t)d[1] << 8) | d[0];
    double t = (double)raw / 256.0;   // see 2 / 3 in the datasheet
    return t > 0;
}
void loop() {
    delay(1000);
}
"""


def test_operators_plant_realistic_faults(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.cpp").write_text(CODE, encoding="utf-8")
    assert mutate.write_functions(tmp_path) == ["write_reg"]
    muts = mutate.candidates(tmp_path, ["write_reg"])
    got = {(m.operator, m.line) for m in muts}
    assert {l for op, l in got if op == "constant"} == {2, 3, 7}                # tagged hex and numbers only
    assert ("write", 12) in got and ("write", 5) not in got and ("write", 6) not in got   # calls, not declarations
    assert ("delay", 11) in got and ("delay", 20) not in got                    # not loop() pacing
    assert ("shift", 15) in got and ("index", 15) in got and ("arith", 16) in got
    assert ("sign", 4) not in got                                               # several members on one line: skipped
    assert ("error_path", 13) in got
    arith = next(m for m in muts if m.operator == "arith")
    assert arith.after.endswith("// see 2 / 3 in the datasheet") and "* 256.0" in arith.after   # never inside a comment


def test_run_scores_caught_and_survivors(tmp_path):
    root = tmp_path / "p"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.cpp").write_text(CODE, encoding="utf-8")
    calls = []

    def verify(copy):   # a stand-in verify that only notices changed constants
        text = (copy / "src" / "main.cpp").read_text(encoding="utf-8")
        calls.append(1)
        return [] if all(c in text for c in ("0x1B", "STARTUP_MS 2 ", "0x76")) else ["FACT_VALUE_MISMATCH src/main.cpp:2"]
    res = mutate.run(root, verify, write_funcs=["write_reg"])
    assert res["by_operator"]["constant"] == "3/3" and res["caught"] == 3 and res["total"] == len(calls) - 1
    assert all(s["operator"] != "constant" for s in res["survivors"]) and res["survivors"]
    assert (root / "src" / "main.cpp").read_text(encoding="utf-8") == CODE      # the project itself is untouched
