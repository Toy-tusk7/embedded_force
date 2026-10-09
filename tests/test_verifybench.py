"""The verify benchmark (no LLM): every correct DM388 driver verifies, and every planted bug is caught."""

from embedforge.verifybench import run, summary


def test_verify_passes_correct_drivers_and_catches_every_planted_bug(tmp_path):
    s = summary(run(workdir=tmp_path))
    assert s["correct_passed"] == s["correct_total"], s["false_errors"]
    assert s["caught"] == s["mutants"], s["missed"]
