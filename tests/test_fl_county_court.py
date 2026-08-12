"""
FL county-court (HOA-lien) phantom-surplus guard — ACCEPTANCE TEST.

A county-court foreclosure is filed by an HOA/condo association over a small
maintenance-fee lien; the SENIOR MORTGAGE SURVIVES the sale, so sale − opening
bid is NOT a real surplus. This flags such leads (case-type-gated, per county),
drops the phantom from the KPI totals, and NEVER touches a docket-confirmed lead.

Proves specifically (per spec):
  • per-county prefix detection (Broward CO** vs CACE; -CC- for the others);
  • CC leads → real_surplus_source='fl_hoa_unverified', amount None (out of totals);
  • the confirmed circuit lead (ratio 0.030, -CA-, certificate of disbursement) is
    UNAFFECTED — the rule keys on TYPE, not ratio;
  • a docket-confirmed CC case (free-and-clear HOA) can still confirm.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.loader import is_fl_county_court_case
from core.dashboard_data import _surplus_for_payload

_checks = []


def check(name, cond, detail=""):
    _checks.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def main():
    print("=" * 78)
    print("  FL county-court (HOA-lien) phantom-surplus guard — acceptance")
    print("=" * 78)

    print("\n── per-county prefix detection (real case numbers) ──")
    # county court → True
    for cid, cn in [
        ("broward-fl", "CONO-25-020084"), ("broward-fl", "COCE-25-088956"),
        ("broward-fl", "COWE-26-005819"), ("broward-fl", "COSO-25-077811"),
        ("orange-fl", "2025-CC-017428-O"), ("duval-fl", "16-2024-CC-010262-AXXX-MA"),
        ("miami-dade-fl", "2026-011938-CC-25"), ("lee-fl", "23-CC-003560"),
    ]:
        check(f"{cid} {cn} → county court", is_fl_county_court_case(cid, cn) is True)
    # circuit → False
    for cid, cn in [
        ("broward-fl", "CACE-22-002423"), ("orange-fl", "2025-CA-000648-O"),
        ("duval-fl", "16-2025-CA-002820-AXXX-MA"), ("miami-dade-fl", "2026-004417-CA-01"),
        ("lee-fl", "25-CA-004356"),
    ]:
        check(f"{cid} {cn} → circuit (NOT county court)", is_fl_county_court_case(cid, cn) is False)
    # OH never matches
    check("OH case never matches the FL detector",
          is_fl_county_court_case("summit-oh", "CV2025105075") is False)

    print("\n── surplus payload: CC flagged, phantom dropped from totals ──")
    # A CC lead, apparent tier, county-court flag set → fl_hoa_unverified, None amount.
    cc = {"money_status": "apparent_surplus", "state": "FL", "fl_county_court": True,
          "gross_surplus": 420336.0, "true_surplus": 420336.0, "debt_source": "fl_opening_bid"}
    amt, bucket = _surplus_for_payload(cc)
    check("CC lead → bucket 'fl_hoa_unverified', amount None (out of KPI totals)",
          bucket == "fl_hoa_unverified" and amt is None, f"{bucket}/{amt}")

    print("\n── the confirmed circuit lead is UNAFFECTED (type, not ratio) ──")
    # Miami-Dade 2026-004417-CA-01: ratio 0.030 (LOWER than any CC case), circuit,
    # docket-confirmed. fl_county_court=False → still confirmed_surplus.
    confirmed = {"money_status": "confirmed_surplus", "state": "FL", "fl_county_court": False,
                 "true_surplus": 97051.34, "gross_surplus": 97051.34, "debt_source": "fl_opening_bid"}
    amt, bucket = _surplus_for_payload(confirmed)
    check("confirmed circuit lead (ratio 0.030) stays confirmed_surplus $97,051.34",
          bucket == "confirmed_surplus" and abs(amt - 97051.34) < 0.01, f"{bucket}/{amt}")

    print("\n── a docket-confirmed CC case can STILL confirm (free-and-clear HOA) ──")
    # Hypothetical: an HOA foreclosure on a free-and-clear property that cleared
    # the docket proof gate → money_status confirmed_surplus. The confirmed return
    # precedes the fl_county_court gate, so it confirms despite fl_county_court=True.
    cc_confirmed = {"money_status": "confirmed_surplus", "state": "FL", "fl_county_court": True,
                    "true_surplus": 50000.0, "gross_surplus": 50000.0, "debt_source": "fl_opening_bid"}
    amt, bucket = _surplus_for_payload(cc_confirmed)
    check("docket-confirmed CC case still confirms (flag = auction math only)",
          bucket == "confirmed_surplus" and abs(amt - 50000.0) < 0.01, f"{bucket}/{amt}")

    passed, total = sum(_checks), len(_checks)
    print("\n" + "=" * 78)
    print(f"  RESULT: {passed}/{total} checks passed")
    print("=" * 78)
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
