"""
Bankruptcy resolution guard (base.py) — ACCEPTANCE TEST.

A bankruptcy signal alone must NOT kill an OH lead (Miami-Dade parity). It kills
only when it is the LATEST controlling status — no explicit resolution and no
sale progress dated after the bankruptcy event. Vocabulary ground-truthed on
real OH dockets (Cuyahoga CV25122629 events + Franklin phrasings).

Proves:
  • resolved bankruptcy (explicit relief/dismissal OR sale progress after) →
    NOT killed on bankruptcy alone → flagged yellow.
  • active/unresolved bankruptcy (silence after) → still KILLED.
  • bankruptcy signal with no anchorable event → conservative KILL (an active
    bankruptcy never leaks through as live).
  • independent kill signals (vacate) still kill even when bankruptcy is dropped.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.dockets.base import DocketScraper, DocketResult

_S = DocketScraper()
_checks = []


def check(name, cond, detail=""):
    _checks.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def ev(d, desc):
    return {"filing_date": d, "description": desc}


def res(kill_signals, events, title="TEST CASE"):
    return DocketResult(case_title=title, kill_signals=list(kill_signals), events=list(events))


# Real Cuyahoga CV25122629 event excerpt: bankruptcy motion, "never formally
# stayed", then sale progress after it.
CV25122629 = [
    ev("2026-04-27", "SINCE THIS COURT NEVER FORMALLY STAYED THE CASE, PLAINTIFF PROCEEDS"),
    ev("2026-05-06", "PLAINTIFF'S MOTION TO VACATE JUDGMENT DUE TO BANKRUPTCY"),
    ev("2026-05-13", "ORDER OF SALE ISSUED TO SHERIFF WITH APPRAISAL"),
    ev("2026-07-29", "OOS ORDER OF SALE RETURNED 7/29/2026, SOLD DATE 7/27/2026"),
]


def main():
    print("=" * 78)
    print("  Bankruptcy resolution guard — acceptance")
    print("=" * 78)

    # ── _bankruptcy_is_controlling on real + synthetic ──
    check("CV25122629: bankruptcy NOT controlling (never-stayed + sale after)",
          _S._bankruptcy_is_controlling(res(["bankruptcy"], CV25122629)) is False)

    active = [ev("2026-05-06", "SUGGESTION OF BANKRUPTCY FILED — AUTOMATIC STAY")]
    check("active bankruptcy, silence after → controlling (kill)",
          _S._bankruptcy_is_controlling(res(["bankruptcy"], active)) is True)

    relief = active + [ev("2026-06-01", "ORDER GRANTING RELIEF FROM STAY")]
    check("bankruptcy then relief from stay → NOT controlling",
          _S._bankruptcy_is_controlling(res(["bankruptcy"], relief)) is False)

    reinst = [ev("2026-01-29", "BANKRUPTCY STAY - CASE INACTIVATED"),
              ev("2026-05-01", "CASE REINSTATED")]
    check("Franklin phrasing: bankruptcy stay then REINSTATED → NOT controlling",
          _S._bankruptcy_is_controlling(res(["bankruptcy"], reinst)) is False)

    progress = [ev("2026-05-06", "CHAPTER 13 BANKRUPTCY NOTED"),
                ev("2026-07-01", "CONFIRMATION OF SALE")]
    check("bankruptcy then confirmation of sale → NOT controlling",
          _S._bankruptcy_is_controlling(res(["bankruptcy"], progress)) is False)

    # sale progress that PRE-dates the bankruptcy must NOT count as resolution.
    pre = [ev("2026-01-01", "NOTICE OF SALE"),
           ev("2026-06-01", "SUGGESTION OF BANKRUPTCY — AUTOMATIC STAY")]
    check("prior sale progress then bankruptcy → controlling (kill)",
          _S._bankruptcy_is_controlling(res(["bankruptcy"], pre)) is True)

    check("bankruptcy signal, no anchorable event → conservative controlling (kill)",
          _S._bankruptcy_is_controlling(res(["bankruptcy"], [ev("2026-05-01", "ORDER OF SALE ISSUED")])) is True)

    print("\n── classify() end-to-end ──")
    # resolved bankruptcy alone → NOT killed (flagged yellow).
    c, r = _S.classify(res(["bankruptcy"], CV25122629), 200000.0)
    check("resolved bankruptcy ALONE → not killed (flag/yellow)", c != "killed", f"{c}: {r}")

    # active bankruptcy alone → killed.
    c, r = _S.classify(res(["bankruptcy"], active), 200000.0)
    check("active bankruptcy alone → killed", c == "killed", f"{c}: {r}")

    # bankruptcy resolved BUT an independent vacate signal remains → still killed
    # on the vacate (bankruptcy fix never rescues a lead with other live kills).
    c, r = _S.classify(res(["bankruptcy", "motion_to_vacate"], CV25122629), 200000.0)
    check("resolved bankruptcy + independent vacate → still killed on vacate",
          c == "killed" and "vacate" in r, f"{c}: {r}")

    passed, total = sum(_checks), len(_checks)
    print("\n" + "=" * 78)
    print(f"  RESULT: {passed}/{total} checks passed")
    print("=" * 78)
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
