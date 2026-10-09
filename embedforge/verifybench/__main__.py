"""python -m embedforge.verifybench [framework ...] [-v]: score verify on the DM388 drivers and planted bugs."""

import sys

from embedforge.verifybench.bench import run, summary


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "-v" in sys.argv
    cases = run(args or None)
    by_fw = {}
    for c in cases:
        by_fw.setdefault(c.framework, []).append(c)
    for fw, cs in by_fw.items():
        if cs[0].mutation == "must_fail":
            c = cs[0]
            print(f"{fw:28} unchecked code {'blocked' if c.caught else 'PASSED VERIFY'}" +
                  (f"  [{c.errors[0][:110]}]" if verbose and c.errors else ""))
            continue
        correct = cs[0]
        muts = cs[1:]
        caught = sum(bool(c.caught) for c in muts)
        print(f"{fw:28} correct driver {'PASS' if correct.passed else 'FAIL'}   bugs caught {caught}/{len(muts)}")
        if not correct.passed:
            for e in correct.errors[:6]:
                print(f"      false error: {e}")
        for c in muts:
            if not c.caught or verbose:
                print(f"      {'caught' if c.caught else 'MISSED'} {c.mutation}: {c.what}" +
                      (f"  [{c.errors[0][:110]}]" if verbose and c.errors else ""))
    s = summary(cases)
    print(f"\ncorrect drivers passing: {s['correct_passed']}/{s['correct_total']}   bugs caught: {s['caught']}/{s['mutants']}"
          f"   unchecked code blocked: {s['unchecked_blocked']}/{s['unchecked_total']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
