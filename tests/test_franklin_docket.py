"""
Franklin docket classifier — ACCEPTANCE TEST.

Runs the exact production classifier (core/dockets/franklin_classify) against the
13 REAL committed Franklin docket fixtures (data/samples/franklin/ci/*.txt,
captured 2026-08-11) with their REAL auction sale dates, plus synthetic cases
that prove the temporal rule and the excess-funds/bankruptcy variants not
present post-sale in the fixtures.

Proves specifically (per spec):
  • post-sale vacate/withdraw MUST kill      — 25CV7609 (real): sold 07-17,
        WITHDRAWING PROPERTY 07-22 → KILLED.
  • pre-sale vacate + completed sale must NOT kill — 25CV6087 (real): ORDER TO
        VACATE 06-22, sold 08-07 → reviewed_no_kill; and 24CV9172 (real).
  • excess-funds ORDER = kill (distributed); MOTION = kill (competing claimant).
  • bankruptcy kills only when it post-dates the sale (latest controlling).
  • NO debt figure is ever produced.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.dockets.franklin_classify import (
    parse_docket_events, classify_franklin,
    _is_vacate, _is_excess, _is_bankruptcy,
)

SAMPLES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                       "data", "samples", "franklin", "ci"))

# real auction sale dates for the 13 fixtures (from data/raw/franklin-oh)
SALE_DATES = {
    "18CV4329": date(2026, 7, 17), "24CV8022": date(2026, 7, 31),
    "24CV8527": date(2026, 7, 24), "24CV9172": date(2026, 8, 7),
    "25CV3720": date(2026, 7, 24), "25CV5573": date(2026, 7, 17),
    "25CV5784": date(2026, 7, 24), "25CV6087": date(2026, 8, 7),
    "25CV7609": date(2026, 7, 17), "25CV7993": date(2026, 7, 31),
    "25CV8583": date(2026, 7, 17), "25CV9562": date(2026, 7, 24),
    "26CV838":  date(2026, 7, 17),
}
# The ONLY fixture with a post-sale kill (withdrawn 07-22 after sale 07-17).
EXPECT_KILLED = {"25CV7609"}

_checks = []


def check(name, cond, detail=""):
    _checks.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def load(slug):
    with open(os.path.join(SAMPLES, f"{slug}.txt"), encoding="utf-8") as f:
        return f.read()


def main():
    print("=" * 78)
    print("  Franklin docket classifier — acceptance (13 real fixtures + temporal)")
    print("=" * 78)

    print("\n── per-case classification on real fixtures ──")
    results = {}
    for slug, sd in sorted(SALE_DATES.items()):
        events = parse_docket_events(load(slug))
        r = classify_franklin(events, sd)
        results[slug] = r
        post = [(d, desc) for d, desc in events if d > sd]
        tag = "KILLED" if r["classification"] == "killed" else "docket-checked"
        print(f"  {slug:10} sold {sd}  → {tag:14} "
              f"{('· ' + r['classification_reason']) if r['classification']=='killed' else ''}")
        if post and r["classification"] != "killed":
            print(f"             (post-sale events: {[(str(d),x[:30]) for d,x in post]})")

    for slug in SALE_DATES:
        want_killed = slug in EXPECT_KILLED
        got_killed = results[slug]["classification"] == "killed"
        check(f"{slug}: {'KILLED' if want_killed else 'not killed'}",
              got_killed == want_killed, results[slug]["classification"])

    # 25CV7609 — the real post-sale withdraw kill, verify the reason + signal.
    r7609 = results["25CV7609"]
    check("25CV7609: kill reason cites withdrawn-from-sale",
          "withdrawn_from_sale" in r7609["kill_signals"], str(r7609["kill_signals"]))

    # 25CV6087 + 24CV9172 — pre-sale vacates that MUST be superseded.
    for slug in ("25CV6087", "24CV9172"):
        evs = parse_docket_events(load(slug))
        had_vacate = any(_is_vacate(d) for _, d in evs)
        check(f"{slug}: has a (pre-sale) vacate but NOT killed",
              had_vacate and results[slug]["classification"] == "reviewed_no_kill",
              f"vacate_present={had_vacate}")

    print("\n── synthetic temporal + variant proofs ──")
    S = date(2026, 7, 1)
    before, after = date(2026, 6, 1), date(2026, 8, 1)

    r = classify_franklin([(before, "MOTION TO VACATE ORDER OF SALE")], S)
    check("pre-sale vacate → NOT killed", r["classification"] == "reviewed_no_kill")

    r = classify_franklin([(after, "ORDER TO VACATE")], S)
    check("post-sale vacate → KILLED", r["classification"] == "killed" and "sale_vacated" in r["kill_signals"])

    r = classify_franklin([(after, "ORDER OF DISTRIBUTION OF EXCESS FUNDS")], S)
    check("post-sale excess-funds ORDER → KILLED (distributed)",
          r["classification"] == "killed" and "excess_funds_distributed" in r["kill_signals"])

    r = classify_franklin([(after, "MOTION FOR ORDER OF DISTRIBUTION OF EXCESS FUNDS")], S)
    check("post-sale excess-funds MOTION → KILLED + competing claimant",
          r["classification"] == "killed" and "excess_funds_claimant" in r["competing_filers"])

    r = classify_franklin([(after, "BANKRUPTCY STAY - CASE INACTIVATED")], S)
    check("post-sale bankruptcy → KILLED", r["classification"] == "killed" and "bankruptcy_stay" in r["kill_signals"])

    r = classify_franklin([(before, "BANKRUPTCY STAY - CASE INACTIVATED")], S)
    check("pre-sale bankruptcy (resolved, sale completed) → NOT killed",
          r["classification"] == "reviewed_no_kill")

    r = classify_franklin([(after, "DISMISS CROSS CLAIM - DEFENDANT")], S)
    check("post-sale DISMISS CROSS CLAIM → NOT a case dismissal (not killed)",
          r["classification"] == "reviewed_no_kill")

    # No debt figure anywhere in the classifier output.
    check("classifier never emits a debt/prayer figure",
          all(k not in results["25CV7609"] for k in ("prayer_amount", "debt", "total_debt")))

    passed, total = sum(_checks), len(_checks)
    print("\n" + "=" * 78)
    print(f"  RESULT: {passed}/{total} checks passed")
    print("=" * 78)
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
