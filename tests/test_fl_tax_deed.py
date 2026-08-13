"""
FL tax-deed (FS 197.582) treatment — ACCEPTANCE TEST.

A tax-deed sale is a DIFFERENT mechanism from the county-court HOA case: the
surplus POOL (sale − tax opening bid) is REAL and clerk-held (the tax deed
extinguishes junior liens incl. the mortgage), but it is distributed to
lienholders before the former owner, so owner-net is unknown. A REDEEMED tax
deed is a non-sale — no surplus at all. Neither may be tagged fl_opening_bid.

Proves:
  • detection: auction_type='TAXDEED' + per-county case-format backup;
  • redeemed guard (status 'Redeemed'/'Redeemed After Sale', sale==assessed
    backup) → non-sale, no surplus figure — runs BEFORE any surplus math;
  • tax-deed sale → bucket 'fl_taxdeed_pool', amount None (out of owner-surplus
    totals), NOT tagged fl_opening_bid;
  • the HOA rule is NOT applied to tax deeds (distinct state/label);
  • foreclosure leads (circuit CA, county-court CC) unaffected.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.loader import (
    is_fl_tax_deed, is_tax_deed_redeemed, is_fl_county_court_case, _parse_lead,
)
from core.dashboard_data import _surplus_for_payload

_checks = []


def check(name, cond, detail=""):
    _checks.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def main():
    print("=" * 78)
    print("  FL tax-deed (FS 197.582) treatment — acceptance")
    print("=" * 78)

    print("\n── detection (auction_type + per-county case format) ──")
    check("Miami-Dade 2026A00192 + TAXDEED → tax deed",
          is_fl_tax_deed("miami-dade-fl", "2026A00192", "TAXDEED") is True)
    check("Miami-Dade 2026A00192 by case format alone (no type) → tax deed",
          is_fl_tax_deed("miami-dade-fl", "2026A00192", "") is True)
    check("Duval 2024-2001TD by case format → tax deed",
          is_fl_tax_deed("duval-fl", "2024-2001TD", "") is True)
    check("Miami-Dade circuit 2026-004417-CA-01 → NOT tax deed",
          is_fl_tax_deed("miami-dade-fl", "2026-004417-CA-01", "") is False)
    check("Duval county-court 16-2024-CC-010262 → NOT tax deed",
          is_fl_tax_deed("duval-fl", "16-2024-CC-010262-AXXX-MA", "") is False)

    print("\n── redeemed guard (status primary, sale==assessed backup) ──")
    check("status 'Redeemed' → redeemed",
          is_tax_deed_redeemed({"auction_status": "Redeemed", "final_sale_price": 822177, "assessed_value": 822177}) is True)
    check("status 'Redeemed After Sale' → redeemed",
          is_tax_deed_redeemed({"auction_status": "Redeemed After Sale", "final_sale_price": 3519.33, "assessed_value": 3135}) is True)
    check("sale==assessed with no redeem status → redeemed (backup)",
          is_tax_deed_redeemed({"auction_status": "Sold", "final_sale_price": 5000, "assessed_value": 5000}) is True)
    check("real sale (sale != assessed, status Sold) → NOT redeemed",
          is_tax_deed_redeemed({"auction_status": "Sold", "final_sale_price": 151100, "assessed_value": 48623}) is False)

    print("\n── _parse_lead: the live lead 2026A00192 (real tax-deed sale) ──")
    rec = {"case_number": "2026A00192", "auction_type": "TAXDEED", "auction_status": "Sold",
           "opening_bid": 11946.82, "final_sale_price": 151100.0, "assessed_value": 48623.0,
           "gross_surplus": 139153.18, "sold_to": "3rd Party Bidder", "is_third_party": True,
           "address": "", "parcel_id": "x", "sale_date": "2026-08-06", "source_url": ""}
    lead = _parse_lead(rec, "miami-dade-fl", "src")
    check("2026A00192 flagged fl_tax_deed (not redeemed)",
          lead.fl_tax_deed and not lead.fl_tax_deed_redeemed)
    check("2026A00192 NOT tagged fl_opening_bid (the mislabel is corrected)",
          lead.debt_source != "fl_opening_bid", f"debt_source={lead.debt_source!r}")
    check("2026A00192 true_surplus None (owner-net unknown)", lead.true_surplus is None)
    check("2026A00192 gross_surplus preserved for the pool display",
          abs(lead.gross_surplus - 139153.18) < 1)

    print("\n── _surplus_for_payload: pool + redeemed buckets ──")
    pool = {"money_status": "apparent_surplus", "state": "FL", "fl_tax_deed": True,
            "gross_surplus": 139153.18, "true_surplus": None, "debt_source": ""}
    amt, bucket = _surplus_for_payload(pool)
    check("tax-deed sale → bucket 'fl_taxdeed_pool', amount None (out of totals)",
          bucket == "fl_taxdeed_pool" and amt is None, f"{bucket}/{amt}")

    redeemed = {"money_status": "apparent_surplus", "state": "FL", "fl_tax_deed_redeemed": True,
                "gross_surplus": 753586.0, "true_surplus": None, "debt_source": ""}
    amt, bucket = _surplus_for_payload(redeemed)
    check("redeemed tax deed → bucket 'fl_taxdeed_redeemed', amount None (no surplus)",
          bucket == "fl_taxdeed_redeemed" and amt is None, f"{bucket}/{amt}")

    print("\n── the HOA rule is NOT applied to tax deeds; foreclosures unaffected ──")
    check("a tax-deed case is NOT flagged as county-court HOA",
          is_fl_county_court_case("miami-dade-fl", "2026A00192") is False)
    confirmed = {"money_status": "confirmed_surplus", "state": "FL", "fl_tax_deed": False,
                 "true_surplus": 97051.34, "gross_surplus": 97051.34, "debt_source": "fl_opening_bid"}
    amt, bucket = _surplus_for_payload(confirmed)
    check("confirmed foreclosure lead unaffected by tax-deed logic",
          bucket == "confirmed_surplus" and abs(amt - 97051.34) < 0.01)

    passed, total = sum(_checks), len(_checks)
    print("\n" + "=" * 78)
    print(f"  RESULT: {passed}/{total} checks passed")
    print("=" * 78)
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
