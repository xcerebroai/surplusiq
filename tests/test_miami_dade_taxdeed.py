"""
Miami-Dade tax-deed claim-status layer (RealTDM) — ACCEPTANCE TEST.

Runs the exact production logic (core.dockets.miami_dade_taxdeed) against the
REAL captured RealTDM vocabulary (data/samples/miami_dade_taxdeed/ci/) — status
strings and document-type lists pulled from real cases, plus the real Surplus
Letter PDF text.

Proves (per spec):
  • each KILL status kills, with its cited reason;
  • Surplus Court Order present → killed (disbursed);
  • SURPLUS_LETTER present (underscore OR space) → surplus_confirmed;
  • the real Surplus Letter PDF text extracts the clerk-stated $58,504.73 pool
    + 90-day claim window;
  • 2026A00192 (ACTIVE - SOLD BIDDER, no surplus doc) stays a labeled pool
    (pool_pending), NOT killed — the assertion that matters most (absence of a
    document is the normal in-window state, never a penalty).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.dockets.miami_dade_taxdeed import classify_tax_deed, extract_surplus_pool

SAMPLES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                       "data", "samples", "miami_dade_taxdeed", "ci"))
_checks = []


def check(name, cond, detail=""):
    _checks.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


# Real captured cases (status + representative document types from the fixtures).
FRESH_DOCS = ["Returned Certified Mail", "Publication", "Sheriff's Return Of Service", "Recorded NOA"]
REDEEMED_DOCS = ["Publication Tax Deeds", "Release NOA", "Sheriff's Return Of Service", "Recorded NOA"]


def main():
    print("=" * 78)
    print("  Miami-Dade tax-deed claim-status (RealTDM) — acceptance")
    print("=" * 78)

    print("\n── KILL statuses (each real, one-L 'CANCELED') ──")
    for status, sig in [
        ("COMPLETED - REDEEMED", "taxdeed_redeemed"),
        ("CANCELED - VACATE SALE", "taxdeed_vacated"),
        ("CANCELED - PER BANKRUPTCY", "taxdeed_bankruptcy"),
        ("COMPLETED - ESCHEATMENT", "taxdeed_escheated"),
    ]:
        v = classify_tax_deed(status, REDEEMED_DOCS if "REDEEM" in status else [])
        check(f"{status} → killed ({sig})",
              v["verdict"] == "killed" and v["kill_signal"] == sig, f"{v['verdict']}/{v['kill_signal']}")
        check(f"{status}: reason cited", bool(v["reason"]))

    print("\n── Surplus Court Order → killed (disbursed) ──")
    v = classify_tax_deed("COMPLETED - SOLD BIDDER", ["SURPLUS_LETTER", "Surplus Court Order", "Publication"])
    check("Surplus Court Order present → killed (disbursed), overrides the letter",
          v["verdict"] == "killed" and v["kill_signal"] == "taxdeed_surplus_disbursed", v["verdict"])

    print("\n── SURPLUS_LETTER (no order yet) → surplus_confirmed ──")
    for label in ["SURPLUS_LETTER", "SURPLUS LETTER"]:
        v = classify_tax_deed("COMPLETED - SOLD BIDDER", [label, "Publication", "Title Search"])
        check(f"{label!r} present → surplus_confirmed", v["verdict"] == "surplus_confirmed", v["verdict"])
    # supporting-only mail must NOT confirm
    v = classify_tax_deed("COMPLETED - SOLD BIDDER", ["RETURNED CERT SURPLUS MAIL", "Publication"])
    check("RETURNED CERT SURPLUS MAIL alone → NOT confirmed (pool_pending)",
          v["verdict"] == "pool_pending", v["verdict"])

    print("\n── Surplus Letter PDF extraction (real 2014A00429 text) ──")
    with open(os.path.join(SAMPLES, "2014A00429_surplus_letter.txt"), encoding="utf-8") as f:
        letter = f.read()
    ext = extract_surplus_pool(letter)
    check("extracts clerk-stated pool $58,504.73",
          ext["pool_amount"] is not None and abs(ext["pool_amount"] - 58504.73) < 0.01,
          f"${ext['pool_amount']}")
    check("extracts 90-day claim window", ext["claim_deadline_days"] == 90, str(ext["claim_deadline_days"]))
    # anti-fabrication: no anchor → None
    ext2 = extract_surplus_pool("no surplus figure here, just prose about the sale")
    check("no anchor → pool None (never guessed)", ext2["pool_amount"] is None)

    print("\n── 2014A00429 full real doc set → killed (it IS disbursed) ──")
    v = classify_tax_deed("COMPLETED - SOLD BIDDER",
                          ["SURPLUS_LETTER", "Surplus Court Order", "RETURNED CERT SURPLUS MAIL", "PUBLICATION"])
    check("2014A00429 (letter + 2020 court order) → killed/disbursed",
          v["verdict"] == "killed", v["verdict"])

    print("\n── THE CRITICAL ONE: fresh in-window sale is NOT penalized ──")
    v = classify_tax_deed("ACTIVE - SOLD BIDDER", FRESH_DOCS)
    check("2026A00192 (ACTIVE - SOLD BIDDER, no surplus doc) → pool_pending (NOT killed)",
          v["verdict"] == "pool_pending", v["verdict"])
    check("fresh case NOT killed and NOT falsely confirmed",
          v["verdict"] not in ("killed", "surplus_confirmed"))
    v_empty = classify_tax_deed("ACTIVE - SOLD BIDDER", [])
    check("no documents at all → still pool_pending (absence is not a signal)",
          v_empty["verdict"] == "pool_pending", v_empty["verdict"])

    passed, total = sum(_checks), len(_checks)
    print("\n" + "=" * 78)
    print(f"  RESULT: {passed}/{total} checks passed")
    print("=" * 78)
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
