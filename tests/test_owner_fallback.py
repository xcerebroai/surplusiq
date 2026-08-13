"""
Docket owner-name derivation (loader fallback) — ACCEPTANCE TEST.

Phase B+C fix. The auction feed rarely carries an owner, PropertyRadar often
doesn't match, and each county's docket scraper stores the party data
differently:
  • Miami-Dade / Summit / Montgomery: a role-filtered "defendants" list
    (owner first, plaintiff excluded), but owner_name is blanked when the
    defendant of record is a company.
  • Cuyahoga: no defendants list at all — only a clean "PLAINTIFF vs. DEFENDANT,
    ET AL." case_title.
  • Broward: a NON-role-separated party list that mixes the plaintiff bank/HOA
    in (sometimes token-reversed) — a bare defendants[0] would put a bank in the
    owner column.

core.loader._apply_docket_to_lead resolves owner_name from this data when the
auction scrape left it blank, with two guards:
  • _GENERIC_DEFENDANT — skip true non-parties (John Doe, tenants, unknown heirs).
  • _PLAINTIFF_ORG    — skip institutional plaintiffs (bank / mortgage / HOA /
    association / servicing) so they never land in the owner column, WITHOUT
    dropping a legitimate investor LLC/INC owner.
It never overwrites an owner the auction already provided.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import loader as L

_checks = []


def check(name, cond, detail=""):
    _checks.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


class _Lead:
    """Minimal stand-in with the attributes _apply_docket_to_lead touches."""
    def __init__(self, owner_name=""):
        self.owner_name = owner_name
        self.prayer_amount = 0.0
        self.final_sale_price = 0.0
        self.true_surplus = None
        self.debt_source = ""
        self.foreclosure_type = ""
        self.docket_evidence_level = ""
        self.lead_status = ""
        self.claim_filed = False
        self.claim_type = ""
        self.state = ""
        self.opening_bid = 0.0


def owner_after(docket, county_id="miami-dade-fl", start=""):
    lead = _Lead(owner_name=start)
    L._apply_docket_to_lead(lead, docket, county_id)
    return lead.owner_name


def main():
    print("=" * 72)
    print("  Docket owner-name derivation (loader fallback) — acceptance")
    print("=" * 72)

    print("\n── Miami-Dade confirmed lead: corporate owner of record surfaces ──")
    # Real captured record: VERABELLA FALLS CONDO ASSN vs LOYAL CIMA LLC.
    md = {"owner_name": "", "defendants": ["LOYAL CIMA LLC"]}
    check("LOYAL CIMA LLC (investor-LLC defendant) → owner",
          owner_after(md) == "LOYAL CIMA LLC", owner_after(md))

    print("\n── owner_name from scraper wins; defendants only a fallback ──")
    check("scraper-provided owner_name is used verbatim",
          owner_after({"owner_name": "PEREZ, PEDRO", "defendants": ["X LLC"]}) == "PEREZ, PEDRO")

    print("\n── never overwrite an owner the auction already provided ──")
    check("existing lead.owner_name preserved",
          owner_after({"defendants": ["LOYAL CIMA LLC"]}, start="AUCTION OWNER") == "AUCTION OWNER")

    print("\n── generic non-parties skipped, real owner taken next ──")
    check("UNKNOWN TENANT skipped → next real defendant",
          owner_after({"defendants": ["UNKNOWN TENANT #1", "MARIA GONZALEZ"]}) == "MARIA GONZALEZ")

    print("\n── Summit/Montgomery: owner is first defendant, banks/agencies after ──")
    summit = {"defendants": ["JAMES, MICHAEL", "MORTGAGE ELECTRONIC REGISTRATION SYSTEMS, INC.",
                             "STATE OF OHIO DEPARTMENT OF TAXATION"]}
    check("first (owner) defendant taken, not the MERS/agency co-defendants",
          owner_after(summit, "summit-oh") == "JAMES, MICHAEL", owner_after(summit, "summit-oh"))

    print("\n── Cuyahoga: parse owner from 'PLAINTIFF vs. DEFENDANT, ET AL.' caption ──")
    cuy_ind = {"case_title": "U.S. BANK NATIONAL ASSOCIATION vs. ROSEALEE SHORT, ET AL"}
    check("individual defendant from caption",
          owner_after(cuy_ind, "cuyahoga-oh") == "ROSEALEE SHORT", owner_after(cuy_ind, "cuyahoga-oh"))
    cuy_llc = {"case_title": "SHNIZEL OHIO, LLC vs. 2912 CLINTON AVE LLC, ET AL."}
    check("corporate owner from caption (plaintiff LLC excluded, defendant LLC kept)",
          owner_after(cuy_llc, "cuyahoga-oh") == "2912 CLINTON AVE LLC", owner_after(cuy_llc, "cuyahoga-oh"))

    print("\n── THE CRITICAL GUARD: a plaintiff bank/HOA never becomes the owner ──")
    # Broward's non-role-separated list — plaintiff mixed in, token-reversed.
    bro = {"defendants": ["bank national us", "farms franklin homeowners", "Jackson, Anthony D"]}
    check("bank + HOA skipped → the actual homeowner surfaces",
          owner_after(bro, "broward-fl") == "Jackson, Anthony D", owner_after(bro, "broward-fl"))
    check("MIDFIRST BANK alone → blank, NEVER surfaced as owner",
          owner_after({"defendants": ["MIDFIRST BANK"]}, "broward-fl") == "")
    check("HOA plaintiff in caption is on the plaintiff side → defendant taken",
          owner_after({"case_title": "PHEASANT RIDGE ASSOCIATION INC vs. JEREMY DEE HARPER"},
                      "montgomery-oh") == "JEREMY DEE HARPER")

    print("\n── heirs case: deceased owner's estate is a real target, kept ──")
    heirs = {"case_title": "FEDERAL HOME LOAN MORTGAGE CORPORATION vs. "
                           "UNK. HEIRS, ETC. OF ENGLEBE G. ALEXANDER, ET AL"}
    # "UNK." is not the spelled-out "unknown"; this stays as informative docket text
    # (the estate of a named decedent), never blanked into nothing useful.
    got = owner_after(heirs, "cuyahoga-oh")
    check("estate-of-decedent caption kept (names the decedent)",
          "ENGLEBE G. ALEXANDER" in got, got)

    print("\n── nothing to derive → stays blank (never fabricated) ──")
    check("empty defendants + no caption → blank",
          owner_after({"defendants": [], "case_title": ""}) == "")

    passed, total = sum(_checks), len(_checks)
    print("\n" + "=" * 72)
    print(f"  RESULT: {passed}/{total} checks passed")
    print("=" * 72)
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
