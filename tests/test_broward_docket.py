"""
SurplusIQ — Broward docket classifier acceptance test (standalone, no pytest).

    python -m tests.test_broward_docket

Network-free: drives core.dockets.broward.parse_docket / _apply_evidence_level
over REAL committed docket JSON (data/samples/broward/ci/<CASE>_docket.json,
captured by Actions run 27312374787 — see SURPLUS_VOCAB_FINDINGS.md). This is the
exact Phase-2 path the live scraper runs after fetch_docket().

Acceptance (build spec):
  * the 9 ground-truthed surplus-claim cases MUST classify killed
  * the 3 traps — homeowner (Merritt), purchaser (NASINNYA), HOA (Manor Grove) —
    MUST NOT be flagged recovery firms; the HOA-only case MUST stay pursuable
  * clean cases MUST stay pursuable (not killed)
"""
from __future__ import annotations
import json
import glob
import os
import sys

from core.dockets.base import DocketResult
from core.dockets.broward import (
    BrowardDocketScraper,
    classify_appearance,
    collect_party_and_purchaser_names,
    collect_defendant_names,
    owner_from_defendants,
    NOA_BENIGN,
    NOA_RECOVERY_KILL,
)
from core.loader import derive_owner_from_docket

SAMPLE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "samples", "broward", "ci")
)

# 9 ground-truthed surplus-claim cases (SURPLUS_VOCAB_FINDINGS.md) — must KILL.
CONFIRMED_KILL = [
    "CACE-24-008631", "CACE-25-005168", "CACE-24-012541", "CACE-23-015282",
    "CACE-25-002358", "CONO-25-048381", "CACE-25-004451", "COCE-25-085528",
    "CACE-24-007420",
]

KNOWN_FIRMS = [
    "GET LIQUID FUNDING, LLC", "PRIORITY SURPLUS LLC", "The Recovery Agents, LLC",
    "AMERIFUND EQUITY GROUP", "New Beginnings Trustee, LLC as Assignee for J. Roux",
    "Capital Crafter Inc.",
]
BENIGN_APPEARANCES = [
    "Party: Plaintiff Nationstar Mortgage Llc",
    "Party: Defendant Karagic, Muhamed",
    "ANTHONY J. ALONEFTIS, ESQ.",
    "JUAN C MARTINEZ AS COUNSEL FOR CANBY BUSINESS PARK, LLC (PURCHASER)",
    # Regression: live CI false-killed CACE-13-021361 ($327K) on the "assignee"
    # keyword — this is the PLAINTIFF's law firm, not a surplus firm. Must be benign.
    "GHIDOTTI /BERGER LLP Attorneys for the Plaintiffs Assignee AND REQUEST FOR SERVICE",
]

_PASS, _FAIL = 0, 0


def _check(cond: bool, msg: str):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {msg}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {msg}")


def _rows(case_nodash: str) -> list:
    with open(os.path.join(SAMPLE_DIR, f"{case_nodash}_docket.json"), encoding="utf-8") as f:
        return json.load(f)["rows"]


def _classify(case: str) -> DocketResult:
    s = BrowardDocketScraper()
    r = DocketResult(county_id="broward-fl", case_number=case)
    s.parse_docket(_rows(case.replace("-", "")), r)
    s._apply_evidence_level(r)
    return r


def _appearance(case: str, name: str) -> str:
    party, purchaser = collect_party_and_purchaser_names(_rows(case.replace("-", "")))
    return classify_appearance(name, party, purchaser)[0]


def main() -> int:
    print("=" * 70)
    print("Broward docket classifier — acceptance against 22 real cases")
    print("=" * 70)

    print("\nfull 22-case classification")
    for path in sorted(glob.glob(os.path.join(SAMPLE_DIR, "*_docket.json"))):
        case = json.load(open(path, encoding="utf-8"))["case"]
        r = _classify(case)
        print(f"    {case:<16} {r.classification:<8} {r.lead_status:<22} "
              f"{r.classification_reason[:64]}")

    print("\n1. the 9 ground-truthed surplus-claim cases must KILL")
    for case in CONFIRMED_KILL:
        _check(_classify(case).classification == "killed", f"{case} killed")

    print("\n2. traps must NOT cause a wrong kill")
    _check(_appearance("CACE-24-012541", "RANDOLPH MERRITT") == NOA_BENIGN,
           "homeowner Merritt (pro se) classified benign")
    _check(_appearance("COWE-26-005819", "NASINNYA LLC") != NOA_RECOVERY_KILL,
           "auction purchaser NASINNYA not a recovery kill")
    _check(_appearance("CACE-25-009885", "MANOR GROVE VILLAGE ONE, INC.") == NOA_BENIGN,
           "HOA Manor Grove classified benign")
    _check(_classify("CACE-25-009885").classification != "killed",
           "HOA-only case CACE-25-009885 stays pursuable")

    print("\n3. known recovery firms must KILL when residual")
    for firm in KNOWN_FIRMS:
        _check(classify_appearance(firm, set(), set())[0] == NOA_RECOVERY_KILL,
               f"recovery firm killed: {firm[:40]}")

    print("\n4. benign party/counsel appearances must NOT kill")
    for ap in BENIGN_APPEARANCES:
        _check(classify_appearance(ap, set(), set())[0] == NOA_BENIGN,
               f"benign: {ap[:45]}")

    print("\n5. clean sold cases must stay pursuable (not killed)")
    for case in ["CACE-21-019437", "CACE-25-003189", "COCE-25-009070"]:
        _check(_classify(case).classification in {"green", "yellow"},
               f"{case} pursuable")

    # ── Regression: generic-keyword tokens must NOT override a benign party/counsel
    #    appearance (the GHIDOTTI-class bug, now generalized). The pre-fix code
    #    gated Stage 1/2 on is_recovery, so a legit "Party: Plaintiff X Funding LLC"
    #    was false-killed on the bare "funding"/"consulting"/"equity group" keyword.
    #    The fix gates Stage 1/2 on a KNOWN-FIRM name only. These cases were a blind
    #    spot for the old 22-case suite — no benign party carried a generic token.
    print("\n6. generic-keyword benign parties must NOT false-kill (was blind spot)")
    for ap in [
        "Notice of Appearance Party: Plaintiff Pennymac Loan Funding LLC",
        "Party: Defendant Sunrise Consulting LLC",
        "Party: Plaintiff Coastal Capital Consulting, LLC",
        "GREENBERG TRAURIG, P.A. Attorneys for Plaintiff Riverside Equity Group",
    ]:
        _check(classify_appearance(ap, set(), set())[0] == NOA_BENIGN,
               f"benign (generic token): {ap[:48]}")

    # ── Counterpart: the fix must NOT open a false-NEGATIVE hole. A real recovery
    #    firm appearing in RESIDUAL (no party/counsel token) must still KILL — by
    #    known name AND by bare generic keyword on an unknown firm.
    print("\n7. residual recovery firms must STILL kill (no false-negative hole)")
    for firm in ["PRIORITY SURPLUS LLC", "Get Liquid Funding, LLC"]:
        _check(classify_appearance(firm, set(), set())[0] == NOA_RECOVERY_KILL,
               f"known firm residual kills: {firm[:40]}")
    for firm in ["Apex Surplus Funding LLC", "Statewide Asset Recovery Group"]:
        _check(classify_appearance(firm, set(), set())[0] == NOA_RECOVERY_KILL,
               f"unknown generic-keyword firm residual kills: {firm[:40]}")

    # ── Owner extraction: multi-word surname + government-party exclusion.
    #    COCE-25-060300 (live 2026-08-17): the individual defendant is "LEON
    #    ALVARADO, PAULINA" (two-word surname). The old single-word-surname regex
    #    missed it → owner_name blank → the loader fallback put the co-defendant
    #    HUD ("Secretary of Housing and Urban Development") in the owner column.
    print("\n8. owner extraction — multi-word surname + govt-party exclusion")
    _hud_rows = [
        {"description": "Motion for Default", "additional":
         "Party: Defendant THE SECRETARY OF HOUSING AND URBAN DEVELOPMENT"},
        {"description": "Answer to Complaint", "additional":
         "Party: Defendant LEON ALVARADO, PAULINA"},
        {"description": "eSummons", "additional":
         "Party: Plaintiff REPUBLIC SQUARE CONDOMINIUM ASSOCIATION, INC"},
    ]
    _names = collect_defendant_names(_hud_rows)
    _check("LEON ALVARADO, PAULINA" in _names,
           "two-word surname 'LEON ALVARADO, PAULINA' now parses")
    _check(owner_from_defendants(_names) == "LEON ALVARADO, PAULINA",
           "owner_from_defendants returns the individual, not the HUD co-defendant")
    _check(collect_defendant_names(
        [{"additional": "Party: Defendant THE SECRETARY OF HOUSING AND URBAN DEVELOPMENT"}]) == [],
        "HUD (no comma) is never captured as a defendant name")
    _check(collect_defendant_names([{"additional": "Party: Defendant Merritt, Randolph"}])
           == ["Merritt, Randolph"], "single-surname regression still parses")
    # loader fallback: a blank owner_name with Broward's sorted token-set defendants
    # must skip the HUD party (govt exclusion), never surfacing a federal agency.
    _tokset = {"owner_name": "", "case_title": "",
               "defendants": ["development housing secretary urban",
                              "alvarado leon paulina", "condominium republic square"]}
    _derived = derive_owner_from_docket(_tokset)
    _check("secretary" not in _derived.lower() and "housing" not in _derived.lower(),
           f"loader fallback excludes the HUD govt party (got '{_derived}')")
    _check(_derived == "alvarado leon paulina",
           "loader fallback returns the individual defendant token-set")

    print("\n" + "=" * 70)
    print(f"  RESULT: {_PASS}/{_PASS + _FAIL} checks passed")
    print("=" * 70)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
