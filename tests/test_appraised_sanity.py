"""
Appraised-value sanity signal — ACCEPTANCE TEST.

Runs the exact production flag (core.loader._flag_mispriced_opener) against the
REAL current OH leads with their REAL appraised values (fetched from RealAuction
2026-08-11). Proves:
  • OH-no-debt leads whose opener is far below the 2/3-appraised floor are
    flagged (Franklin 25CV9562, Hamilton A2505625).
  • OH-no-debt leads with a normal 2/3 opener are NOT flagged.
  • Leads WITH real docket debt (oh_mortgage_computed/_uncertain) are NEVER
    flagged, even with appraised present — their number is trustworthy.
  • Inert when appraised_value == 0 (not yet scraped).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.loader import Lead, _flag_mispriced_opener, MISPRICED_OPENER_FLOOR

_checks = []


def check(name, cond, detail=""):
    _checks.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def mk(case, state, debt_source, opening, sale, appraised):
    return Lead(
        county_id="x-oh", county_name="X", state=state, case_number=case,
        address="", parcel_id="", auction_type="", opening_bid=opening,
        final_sale_price=sale, gross_surplus=sale - opening, assessed_value=0.0,
        sale_date="2026-07-24", sale_datetime="", sold_to="3rd Party Bidder",
        is_third_party=True, source_url="", auction_status="Sold",
        scraped_at="", source_file="",
        appraised_value=appraised, debt_source=debt_source,
    )


# (case, debt_source, opening, sale, appraised, expect_flagged)
CASES = [
    # OH-no-debt, mispriced openers → FLAG
    ("25CV9562",     "", 18208.0,  240100.0, 270000.0, True),   # Franklin 0.067
    ("A2505625",     "", 45776.0,  95300.0,  150000.0, True),   # Hamilton 0.305
    # OH-no-debt, normal 2/3 openers → NOT flagged
    ("CV2024062692", "", 142000.0, 215200.0, 213000.0, False),  # Summit 0.667
    ("CV2025062537", "", 38000.0,  65700.0,  57000.0,  False),  # Summit 0.667
    # OH WITH real docket debt → NEVER flagged (trustworthy number), even if the
    # opener were low: use Franklin's numbers but with a docket debt source.
    ("computed",     "oh_mortgage_computed",  18208.0, 240100.0, 270000.0, False),
    ("uncertain",    "oh_mortgage_uncertain", 18208.0, 240100.0, 270000.0, False),
    # FL lead (opening IS the debt) → skip (state gate)
    ("fl-case",      "fl_opening_bid",        18208.0, 240100.0, 270000.0, False),
]


def main():
    print("=" * 78)
    print(f"  Appraised-value sanity signal — acceptance (floor = opening < "
          f"{MISPRICED_OPENER_FLOOR:.2f} × appraised)")
    print("=" * 78)
    for case, ds, op, sale, ap, want in CASES:
        state = "FL" if ds == "fl_opening_bid" else "OH"
        l = mk(case, state, ds, op, sale, ap)
        _flag_mispriced_opener(l)
        ratio = op / ap
        print(f"  {case:14} debt={ds or '(none)':22} open/appr={ratio:.3f} "
              f"sale/appr={l.sale_vs_appraised:.3f} flagged={l.mispriced_opener}")
        check(f"{case}: mispriced_opener == {want}", l.mispriced_opener == want)

    # Inert when appraised is 0 (current published data, pre-scrape).
    l0 = mk("no-appr", "OH", "", 18208.0, 240100.0, 0.0)
    _flag_mispriced_opener(l0)
    check("appraised==0 → inert (not flagged)", l0.mispriced_opener is False)

    # sale_vs_appraised is computed for context even when not flagged.
    lc = mk("ctx", "OH", "", 142000.0, 215200.0, 213000.0)
    _flag_mispriced_opener(lc)
    check("sale_vs_appraised computed for context", abs(lc.sale_vs_appraised - 1.010) < 0.002,
          f"{lc.sale_vs_appraised}")

    passed, total = sum(_checks), len(_checks)
    print("\n" + "=" * 78)
    print(f"  RESULT: {passed}/{total} checks passed")
    print("=" * 78)
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
