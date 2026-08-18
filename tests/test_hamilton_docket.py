"""
SurplusIQ — Hamilton docket classifier acceptance test (standalone, no pytest).

    python -m tests.test_hamilton_docket

Network-free: drives core.dockets.hamilton_classify over 6 REAL captured dockets
(data/samples/hamilton/ci/, genuine-Chrome recon 2026-08-17). Hamilton exposes NO
debt figure — this asserts the kill/no-kill decisions and owner extraction only.

Acceptance (client spec):
  * excess-funds claim kills (A2201245), and the withdrawn vacate + vacated
    bankruptcy stay on that same case do NOT contribute kill signals;
  * Rule 41(A)(2) dismissal kills (A2104251);
  * bankruptcy stay with no termination and no confirmation kills (A2202173);
  * a confirmed-but-not-yet-processed case (A2201677) with a superseded vacate and
    terminated bankruptcy stays stays reviewed_no_kill — and is NOT elevated/clean;
  * a confirmed case whose only "dismissal" text is an HOA cross-claim motion does
    NOT kill (A2500255);
  * owners extract from the caption, excluding plaintiff lenders / junior creditors.
"""
from __future__ import annotations
import json
import glob
import os
import sys

from core.dockets.hamilton_classify import classify_hamilton, owner_from_caption

SAMPLE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "samples", "hamilton", "ci"))

_PASS, _FAIL = 0, 0


def _check(cond, msg):
    global _PASS, _FAIL
    if cond:
        _PASS += 1; print(f"  [PASS] {msg}")
    else:
        _FAIL += 1; print(f"  [FAIL] {msg}")


def _load(case):
    with open(os.path.join(SAMPLE_DIR, f"{case}.json"), encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    print("=" * 70)
    print("Hamilton docket classifier — acceptance against 6 real dockets")
    print("=" * 70)

    fx = {os.path.basename(p)[:-5]: json.load(open(p, encoding="utf-8"))
          for p in sorted(glob.glob(os.path.join(SAMPLE_DIR, "*.json")))}

    print("\nfull classification of every fixture")
    results = {}
    for case, d in fx.items():
        r = classify_hamilton(d["rows"], d.get("caption", ""), d.get("sale_date"))
        results[case] = r
        print(f"    {case:<10} {r['classification']:<16} "
              f"kills={r['kill_signals']} owner={r['owner_name']!r}")

    print("\n1. expected classification + kill signal per fixture")
    for case, d in fx.items():
        exp = d["expected"]; r = results[case]
        _check(r["classification"] == exp["classification"],
               f"{case} classification == {exp['classification']} (got {r['classification']})")
        if exp.get("kill_signal"):
            _check(exp["kill_signal"] in r["kill_signals"],
                   f"{case} kill signal includes {exp['kill_signal']}")
        else:
            _check(r["kill_signals"] == [],
                   f"{case} has no kill signals (got {r['kill_signals']})")

    print("\n2. excess-funds is the ONLY kill on A2201245 (withdrawn vacate + "
          "vacated bankruptcy must NOT contribute)")
    r = results["A2201245"]
    _check(r["kill_signals"] == ["excess_funds_claim"],
           f"A2201245 kill_signals == ['excess_funds_claim'] (got {r['kill_signals']})")
    _check("sale_vacated" not in r["kill_signals"], "A2201245 withdrawn vacate did NOT kill")
    _check("bankruptcy_stay" not in r["kill_signals"], "A2201245 vacated bankruptcy stay did NOT kill")

    print("\n3. Rule 41(A)(2) dismissal kills (A2104251)")
    _check(results["A2104251"]["classification"] == "killed", "A2104251 killed")
    _check("case_dismissed" in results["A2104251"]["kill_signals"], "A2104251 signal = case_dismissed")

    print("\n4. bankruptcy stay w/ no termination & no confirmation kills (A2202173)")
    _check(results["A2202173"]["kill_signals"] == ["bankruptcy_stay"],
           "A2202173 killed by bankruptcy_stay only")

    print("\n5. ABSENCE IS NOT A SIGNAL — confirmed-but-not-processed stays "
          "reviewed_no_kill and is NOT elevated/clean (A2201677)")
    r = results["A2201677"]
    _check(r["classification"] == "reviewed_no_kill", "A2201677 reviewed_no_kill")
    _check(r["confirmed"] is True, "A2201677 confirmed flag set (a later confirmation exists)")
    _check(r["evidence_level"] == "docket_checked",
           "A2201677 evidence_level docket_checked (NOT elevated to a verified/clean tier)")
    _check(r["classification"] != "green", "A2201677 is never marked green/clean")

    print("\n6. HOA cross-claim / 'dismissal date' motion does NOT kill (A2500255)")
    _check(results["A2500255"]["classification"] == "reviewed_no_kill",
           "A2500255 reviewed_no_kill (HOA cross-claim is not a kill)")

    print("\n7. owner extraction — caption defendant, excluding lenders/creditors")
    _check(results["A2201245"]["owner_name"] == "ASHLEY D MCGRAW", "owner = ASHLEY D MCGRAW")
    _check(results["A2202173"]["owner_name"] == "VIRGIL L LEATH", "owner = VIRGIL L LEATH")
    _check(results["A2500210"]["owner_name"] == "BARBARA C ARMSTRONG", "owner = BARBARA C ARMSTRONG")
    _check(owner_from_caption("FIFTH THIRD BANK NATIONAL ASSOCIATION vs. ASHLEY D MCGRAW")
           == "ASHLEY D MCGRAW", "plaintiff FIFTH THIRD BANK excluded (took defendant)")
    _check(owner_from_caption("SOME BANK vs. PORTFOLIO RECOVERY ASSOCIATES LLC") == "",
           "an institutional/junior-creditor defendant is excluded → blank owner")
    _check(owner_from_caption("US BANK vs. HEIDI J HATCHER et al") == "HEIDI J HATCHER",
           "'et al' stripped from the owner")

    print("\n8. IN REM 'unknown spouse/heirs of <NAME>' captions — extract the "
          "embedded individual, never a non-party, never a guess")
    _check(owner_from_caption(
        "WILMINGTON SAVINGS FUND SOCIETY FSB NOT IN ITS IND vs. UNKNOWN SPOUSE IF ANY OF DAVID PETTIGREW")
        == "DAVID PETTIGREW", "unknown-spouse-of → DAVID PETTIGREW")
    _check(owner_from_caption("SOME BANK vs. UNKNOWN HEIRS OF ROBERT L JOHNSON")
           == "ROBERT L JOHNSON", "unknown-heirs-of → ROBERT L JOHNSON")
    _check(owner_from_caption("US BANK vs. THE UNKNOWN HEIRS DEVISEES OF MARY ANN SMITH")
           == "MARY ANN SMITH", "unknown-heirs-devisees-of → MARY ANN SMITH")
    # never guess / never extract a non-individual:
    _check(owner_from_caption("US BANK vs. UNKNOWN SPOUSE IF ANY OF") == "",
           "no embedded name → blank (never guess)")
    _check(owner_from_caption("US BANK vs. UNKNOWN TENANTS IN POSSESSION") == "",
           "unknown tenants (no 'of <name>') → blank")
    _check(owner_from_caption("US BANK vs. UNKNOWN SPOUSE OF PORTFOLIO RECOVERY ASSOCIATES LLC") == "",
           "embedded name is a junior-creditor company → blank (guards re-applied)")
    _check(owner_from_caption("US BANK vs. UNKNOWN HEIRS OF TD BANK USA NA") == "",
           "embedded name is an institutional creditor → blank")

    print("\n" + "=" * 70)
    print(f"  RESULT: {_PASS}/{_PASS + _FAIL} checks passed")
    print("=" * 70)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
