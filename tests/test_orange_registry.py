"""
SurplusIQ — Orange Court Registry lifecycle classifier acceptance test.

    python -m tests.test_orange_registry

Network-free: drives core.dockets.orange_registry_classify over 6 REAL captured
registry lookups (data/samples/orange/registry/, genuine-Chrome recon 2026-08-17),
covering all three lifecycle stages plus the no-registry case:
  • distributed CA (clean owner surplus)          — 2025-CA-003157-O
  • distributed CC (real held funds, HOA caution)  — 2023-CC-006558-O
  • pending (balance ≈ full bid) CC / CA / CA      — 017428 / 002399 / 009840
  • no-registry (KILL)                             — 2012-CA-123456-O

Asserts the stage, the kill flag, confirmed-eligibility, the HOA caution, and the
critical anti-inflation rules (pending is never a surplus; a pending balance never
becomes registry_surplus).
"""
from __future__ import annotations
import json
import glob
import os
import sys

from core.dockets.orange_registry_classify import (
    classify_registry, case_type,
    STAGE_KILL, STAGE_PENDING, STAGE_DISTRIBUTED,
)

SAMPLE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "samples", "orange", "registry"))

_PASS, _FAIL = 0, 0


def _check(cond, msg):
    global _PASS, _FAIL
    if cond:
        _PASS += 1; print(f"  [PASS] {msg}")
    else:
        _FAIL += 1; print(f"  [FAIL] {msg}")


def main() -> int:
    print("=" * 70)
    print("Orange registry lifecycle classifier — acceptance on 6 real lookups")
    print("=" * 70)

    fx = {os.path.basename(p)[:-5]: json.load(open(p, encoding="utf-8"))
          for p in sorted(glob.glob(os.path.join(SAMPLE_DIR, "*.json")))}

    print("\nfull classification of every fixture")
    results = {}
    for case, d in fx.items():
        r = classify_registry(d["lookup"], d["final_sale_price"], d["opening_bid"], case)
        results[case] = r
        bal = r["registry_balance"]
        balstr = "None" if bal is None else f"${bal:,.0f}"
        print(f"    {case:<18} {r['registry_status']:<22} balance={balstr:<12} "
              f"kill={r['kill']} confirmed_eligible={r['confirmed_eligible']} hoa={r['hoa_caution']}")

    print("\n1. expected stage / kill / confirmed-eligibility / HOA caution per fixture")
    for case, d in fx.items():
        exp, r = d["expected"], results[case]
        _check(r["registry_status"] == exp["registry_status"],
               f"{case} stage == {exp['registry_status']} (got {r['registry_status']})")
        _check(r["kill"] == exp["kill"], f"{case} kill == {exp['kill']}")
        _check(r["confirmed_eligible"] == exp["confirmed_eligible"],
               f"{case} confirmed_eligible == {exp['confirmed_eligible']}")
        if "hoa_caution" in exp:
            _check(r["hoa_caution"] == exp["hoa_caution"], f"{case} hoa_caution == {exp['hoa_caution']}")

    print("\n2. STAGE 1 — no registry → KILL (Orange's only verification signal)")
    r = results["2012-CA-123456-O"]
    _check(r["registry_status"] == STAGE_KILL and r["kill"] is True, "no-registry classifies KILL")
    _check(r.get("kill_signal") == "registry_funds_gone", "kill signal = registry_funds_gone")
    _check(r["registry_balance"] is None, "no fabricated balance on a kill")

    print("\n3. STAGE 2 — balance ≈ full bid → PENDING (never a surplus, never confirmed)")
    for case in ["2025-CC-017428-O", "2025-CA-002399-O", "2024-CA-009840-O"]:
        r = results[case]
        _check(r["registry_status"] == STAGE_PENDING, f"{case} pending_distribution")
        _check(r["confirmed_eligible"] is False, f"{case} NOT confirmed-eligible (full bid ≠ surplus)")
        _check("NOT a surplus" in r["reason"], f"{case} reason states it is NOT a surplus")

    print("\n4. STAGE 3 — balance well below bid → DISTRIBUTED = clerk-stated held surplus")
    ca = results["2025-CA-003157-O"]
    _check(ca["registry_status"] == STAGE_DISTRIBUTED, "CA distributed")
    _check(ca["confirmed_eligible"] is True, "CA distributed → confirmed-eligible (clean owner surplus)")
    _check(ca["hoa_caution"] is False, "CA has no HOA caution")
    _check(abs(ca["registry_balance"] - 168709.00) < 0.01, "CA registry surplus = $168,709 (clerk figure, not auction math $172,242)")

    cc = results["2023-CC-006558-O"]
    _check(cc["registry_status"] == STAGE_DISTRIBUTED, "CC distributed (funds genuinely held — not phantom)")
    _check(cc["confirmed_eligible"] is False, "CC distributed → NOT booked as clean owner surplus")
    _check(cc["hoa_caution"] is True, "CC carries the HOA/senior-mortgage owner-recovery caution")
    _check(abs(cc["registry_balance"] - 194881.83) < 0.01, "CC real held funds = $194,881.83 surfaced")
    _check("senior mortgage" in cc["reason"].lower(), "CC reason cites the surviving senior mortgage")

    print("\n5. staging is by balance-vs-bid, not elapsed time — synthetic edges")
    # tiny clerk fee off the full bid → still pending
    _check(classify_registry({"found": True, "balance": 199500.0, "as_of": "x"},
                             200000.0, 5000.0, "2025-CA-000001-O")["registry_status"] == STAGE_PENDING,
           "$500 off a $200k bid → pending (a fee is not a distribution)")
    # a real distribution on a small-judgment CC → distributed even though balance is high
    _check(classify_registry({"found": True, "balance": 180000.0, "as_of": "x"},
                             200000.0, 5000.0, "2025-CC-000002-O")["registry_status"] == STAGE_DISTRIBUTED,
           "$20k off a $200k bid → distributed (real distribution, even a small CC judgment)")
    # unknown bid → never guess distributed/confirmed
    u = classify_registry({"found": True, "balance": 50000.0, "as_of": "x"}, 0.0, 0.0, "2025-CA-000003-O")
    _check(u["registry_status"] == "balance_found_unstaged" and u["confirmed_eligible"] is False,
           "unknown bid → unstaged, never confirmed (fail safe)")

    print("\n6. case-type parse")
    _check(case_type("2025-CA-003157-O") == "CA" and case_type("2025-CC-017428-O") == "CC",
           "CA / CC parsed from the case number")

    # ── 7. LOADER INTEGRATION — prove the money model end-to-end ──
    # _apply_registry_to_lead + assign_status_fields must yield:
    #   distributed CA → confirmed_surplus (clerk balance, not auction math)
    #   distributed CC → apparent_surplus + HOA caution (real funds, NOT booked)
    #   pending        → apparent_surplus, no money change
    #   no_registry    → killed (FP-14 filters it out of the deliverable)
    from core.loader import Lead, _apply_registry_to_lead, assign_status_fields
    from core.dockets.orange_registry import _registry_record

    def _lead(case, sale, opening, surplus):
        return Lead(county_id="orange-fl", county_name="Orange", state="FL",
                    case_number=case, address="123 Test St", parcel_id="P1",
                    auction_type="FORECLOSURE", opening_bid=opening,
                    final_sale_price=sale, gross_surplus=surplus, assessed_value=0.0,
                    sale_date="2026-07-15", sale_datetime="2026-07-15 09:00",
                    sold_to="3rd Party", is_third_party=True,
                    source_url="https://myorangeclerk.com/auction", auction_status="SOLD",
                    scraped_at="2026-08-18T00:00:00", source_file="x.jsonl")

    print("\n7. loader integration — money model end-to-end")
    for case in fx:
        d = fx[case]
        v = results[case]
        auction = {"final_sale_price": d["final_sale_price"], "opening_bid": d["opening_bid"],
                   "gross_surplus": d["apparent_surplus"], "address": "123 Test St",
                   "auction_date": "2026-07-15"}
        rec = _registry_record(case, v, d["lookup"], auction)
        # For CA/FL, _parse_lead would have pre-set true_surplus = sale - opening_bid.
        lead = _lead(case, d["final_sale_price"], d["opening_bid"], d["apparent_surplus"])
        if d["final_sale_price"] > 0:
            lead.true_surplus = d["final_sale_price"] - d["opening_bid"]
            lead.debt_source = "fl_opening_bid"
        _apply_registry_to_lead(lead, rec)
        assign_status_fields(lead)
        exp = d["expected"]
        if exp["registry_status"] == "distributed" and exp["confirmed_eligible"]:
            _check(lead.money_status == "confirmed_surplus",
                   f"{case} distributed CA → money_status=confirmed_surplus (got {lead.money_status})")
            _check(abs((lead.true_surplus or 0) - exp["registry_balance"]) < 0.01,
                   f"{case} true_surplus == clerk balance ${exp['registry_balance']:,.0f} (not auction math)")
            _check(lead.classification == "green" and bool(lead.proof_of_surplus),
                   f"{case} classification=green with proof_of_surplus set")
        elif exp["registry_status"] == "distributed":  # CC
            _check(lead.money_status != "confirmed_surplus",
                   f"{case} distributed CC → NOT confirmed_surplus (got {lead.money_status})")
            _check(lead.registry_hoa_caution is True and abs((lead.registry_balance or 0) - exp["registry_balance"]) < 0.01,
                   f"{case} CC surfaces held ${exp['registry_balance']:,.0f} + HOA caution, unbooked")
        elif exp["registry_status"] == "pending_distribution":
            _check(lead.money_status != "confirmed_surplus",
                   f"{case} pending → NOT confirmed_surplus (got {lead.money_status})")
            _check(lead.registry_status == "pending_distribution",
                   f"{case} pending marker recorded on the lead")
        elif exp["kill"]:
            _check(lead.classification == "killed",
                   f"{case} no_registry → classification=killed (FP-14 filters it out)")
            _check("registry_funds_gone" in (lead.kill_signals or []),
                   f"{case} kill signal recorded")

    print("\n" + "=" * 70)
    print(f"  RESULT: {_PASS}/{_PASS + _FAIL} checks passed")
    print("=" * 70)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
