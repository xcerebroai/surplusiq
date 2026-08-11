"""
SurplusIQ — Unified Data Loader (v2 — 28-day cutoff enforced)

Consolidates raw scraped data from all 10 counties into a single clean dataset
ready for Excel export, dashboard rendering, and PropertyRadar enrichment.

CHANGES IN v2:
  • Hard 28-day window: any lead with sale_date older than (today - 28 days)
    is dropped before reaching the dashboard / Excel / enrichment.
    (Widened from 14 days so leads persist long enough for late-filed kill
    signals — claims, vacate motions — to be caught by re-verification.)
  • If sale_date can't be parsed, the lead is dropped as well.
  • Console output reports how many were dropped and why, so we can verify
    the filter is doing what we expect each time.

Usage:
    from core.loader import load_all_leads, get_summary

    leads = load_all_leads()                    # all qualifying leads (last 28 days)
    leads = load_all_leads(min_surplus=25000)   # higher surplus threshold
    leads = load_all_leads(window_days=7)       # tighter date window
    summary = get_summary(leads)                # county totals
"""

from __future__ import annotations
import json
import os
import re
from dataclasses import dataclass, field, asdict, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from config.counties import LEAD_WINDOW_DAYS


# ═══════════════════════════════════════════════════════════════════════
# Project paths
# ═══════════════════════════════════════════════════════════════════════
def _find_project_root() -> Path:
    p = Path(__file__).resolve()
    for parent in [p] + list(p.parents):
        if (parent / "config" / "counties.py").exists():
            return parent
    return Path(__file__).resolve().parent.parent

PROJECT_ROOT = _find_project_root()
RAW_DIR      = PROJECT_ROOT / "data" / "raw"


# ═══════════════════════════════════════════════════════════════════════
# County metadata (ID → display info)
# ═══════════════════════════════════════════════════════════════════════
COUNTY_INFO = {
    "miami-dade-fl": {"name": "Miami-Dade", "state": "FL", "platform": "Florida — RealForeclose"},
    "broward-fl":    {"name": "Broward",    "state": "FL", "platform": "Florida — RealForeclose"},
    "duval-fl":      {"name": "Duval",      "state": "FL", "platform": "Florida — RealForeclose (Tax Deed)"},
    "lee-fl":        {"name": "Lee",        "state": "FL", "platform": "Florida — RealForeclose"},
    "orange-fl":     {"name": "Orange",     "state": "FL", "platform": "Florida — RealForeclose"},
    "cuyahoga-oh":   {"name": "Cuyahoga",   "state": "OH", "platform": "Ohio — SheriffSaleAuction"},
    "franklin-oh":   {"name": "Franklin",   "state": "OH", "platform": "Ohio — SheriffSaleAuction"},
    "montgomery-oh": {"name": "Montgomery", "state": "OH", "platform": "Ohio — SheriffSaleAuction"},
    "summit-oh":     {"name": "Summit",     "state": "OH", "platform": "Ohio — SheriffSaleAuction"},
    "hamilton-oh":   {"name": "Hamilton",   "state": "OH", "platform": "Ohio — SheriffSaleAuction"},
}


# ═══════════════════════════════════════════════════════════════════════
# Lead data structure
# ═══════════════════════════════════════════════════════════════════════


def _load_docket_data() -> dict:
    """
    Load all docket scraper results from data/dockets/ into a lookup dict.
    Returns: { (county_id, normalized_case_number): docket_result_dict }
    """
    import json as _json
    docket_dir = PROJECT_ROOT / "data" / "dockets"
    if not docket_dir.exists():
        return {}

    lookup = {}
    # Walk every .jsonl file in data/dockets/
    for jsonl in sorted(docket_dir.glob("*.jsonl")):
        try:
            with open(jsonl) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    cid = d.get("county_id", "")
                    case = d.get("case_number", "")
                    if not cid or not case:
                        continue
                    # Normalize: strip "(NNNNN)" auction suffix and whitespace
                    norm = re.sub(r"\s*\([^)]*\)\s*$", "", case).strip().upper()
                    lookup[(cid, norm)] = d
        except Exception:
            continue
    return lookup


def _normalize_case_for_lookup(case_number: str) -> str:
    """Strip the auction suffix '(NNNNN)' from case numbers for matching."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", case_number).strip().upper()


def is_oh_tax_case(case_number: str) -> bool:
    """True if an OH case is a TAX foreclosure (RC Chapter 5721), where the
    opening bid IS the statutory Minimum Bid = real tax debt (so opening-bid
    surplus math is valid). FALSE for OH MORTGAGE foreclosures, where the
    opening bid is the 2/3-appraised value and is NOT real debt.

    Detector: Ohio tax/delinquent-land cases carry the 'CVG' designator
    (e.g. Summit '2025CVG01728'); mortgage cases are plain 'CV2025...'.
    Conservative by design — an unrecognized tax marker falls through to
    'mortgage', which shows the lead as debt-unverified (safe) rather than
    crediting a fake opening-bid surplus."""
    return "CVG" in (case_number or "").upper()


def _apply_docket_to_lead(lead, docket: dict, county_id: str) -> None:
    """
    Merge a docket result onto a Lead in place.

    State-aware surplus rule (FP-10, Eric's May 12 call):

      • Ohio   — opening_bid is the statutory 2/3-appraised value, NOT real
                 debt. The only valid OH debt is the docket prayer amount.
                 No prayer ⇒ true_surplus stays None.

      • Florida — opening_bid IS the judgment amount (from the FL auction
                 calendar). _parse_lead already pre-populates true_surplus
                 = sale - opening_bid with debt_source = "fl_opening_bid"
                 for FL leads. A docket prayer (from a future Miami-Dade-
                 style scraper) OVERRIDES the opening-bid figure here.

    Confirmation rule is uniform across states: true_surplus alone never
    promotes a lead to confirmed_surplus. It needs to clear kill signals,
    have proof fields, and survive assign_status_fields downstream.
    """
    lead.classification       = docket.get("classification", "") or ""
    lead.classification_reason = docket.get("classification_reason", "") or ""
    lead.prayer_amount        = float(docket.get("prayer_amount", 0.0) or 0.0)
    lead.kill_signals         = list(docket.get("kill_signals", []) or [])
    lead.proof_of_surplus     = docket.get("proof_of_surplus", "") or ""
    lead.competing_filers     = list(docket.get("competing_filers", []) or [])
    lead.additional_parties   = list(docket.get("additional_parties", []) or [])
    lead.docket_url           = docket.get("case_url", "") or ""

    # Eric's review taxonomy (set by county scrapers that implement it, e.g.
    # Miami-Dade). For OH counties these keys are absent/empty → no-op.
    # NOTE: docket["evidence_level"] is Eric's taxonomy on the DocketResult;
    # it maps to lead.docket_evidence_level, NOT lead.evidence_level (which is
    # the verification-status-model field set later by assign_status_fields).
    lead.foreclosure_type      = docket.get("foreclosure_type", "") or ""
    lead.docket_evidence_level = docket.get("evidence_level", "") or ""
    lead.lead_status           = docket.get("lead_status", "") or ""
    lead.claim_filed           = bool(docket.get("claim_filed", False))
    lead.claim_type            = docket.get("claim_type", "") or ""

    # Owner = defendant homeowner from the docket. Only counties that extract it
    # set docket["owner_name"] (currently Miami-Dade); for others it's absent →
    # no-op. Fill ONLY when the auction scrape left owner_name blank, and never
    # overwrite a name the auction already provided.
    docket_owner = (docket.get("owner_name", "") or "").strip()
    if docket_owner and not (getattr(lead, "owner_name", "") or "").strip():
        lead.owner_name = docket_owner

    # Docket prayer takes precedence over any state-specific default (FL
    # opening-bid math). No docket prayer ⇒ keep whatever _parse_lead set:
    # OH leads stay at None, FL leads keep their fl_opening_bid surplus.
    if lead.prayer_amount > 0:
        lead.true_surplus = round(lead.final_sale_price - lead.prayer_amount, 2)
        # Preserve county-specific debt_source if the docket result carried one
        # (e.g. "pdf_extract:docket_NN:judgment" from Montgomery/Summit);
        # otherwise default to "docket_prayer".
        docket_debt_source = docket.get("debt_source", "") or ""
        lead.debt_source = docket_debt_source or "docket_prayer"
    # else: leave true_surplus + debt_source as _parse_lead set them
    #       (FL: fl_opening_bid math; OH: None / "")



@dataclass
class Lead:
    # Identity
    county_id:      str
    county_name:    str
    state:          str
    case_number:    str

    # Property
    address:        str
    parcel_id:      str
    auction_type:   str

    # Financials
    opening_bid:    float
    final_sale_price: float
    gross_surplus:  float
    assessed_value: float

    # Sale details
    sale_date:      str         # ISO format (YYYY-MM-DD) after normalization
    sale_datetime:  str         # Full readable timestamp e.g. "May 4, 2026 9:02 AM ET"
    sold_to:        str
    is_third_party: bool
    source_url:     str         # Direct link to the county auction page


    # Lead quality
    auction_status: str

    # Source
    scraped_at:     str
    source_file:    str

    # Lead score
    score:          str = ""
    score_reason:   str = ""

    # Enrichment placeholders
    enriched:           bool   = False
    estimated_value:    float  = 0.0
    mortgage_balance:   float  = 0.0
    secondary_liens:    float  = 0.0
    net_surplus:        float  = 0.0
    owner_name:         str    = ""
    owner_address:      str    = ""

    # Claim status
    claim_filed:        bool   = False
    claim_status:       str    = "Unknown"
    appraised_value:    float  = 0.0   # OH RealAuction appraised value (stored only,
                                       # no surplus-math change; opening_bid drives math)

    # Docket-enrichment fields (populated when docket scraper has run on this case)
    classification:   str = ""
    classification_reason: str = ""
    prayer_amount:    float = 0.0
    true_surplus:     Optional[float] = None   # None = NOT debt-backed
    debt_source:      str = ""                 # provenance of the debt figure:
                                                #   ""                — no debt known
                                                #   "docket_prayer"   — OH docket prayer/judgment
                                                #   "fl_opening_bid"  — FL opening bid (Eric: FL opening = judgment)
                                                #   "pdf_extract:…"   — county-specific PDF extraction
    kill_signals:     list = field(default_factory=list)
    proof_of_surplus: str = ""
    competing_filers: list = field(default_factory=list)
    additional_parties: list = field(default_factory=list)
    docket_url:       str = ""

    # ── Eric's review taxonomy (Miami-Dade docket validation) ──
    # Distinct from `evidence_level` below (that is the verification-status
    # model field). docket_evidence_level carries Eric's taxonomy
    # (no_claim_found / claim_filed / bankruptcy_found / sale_issue_found /
    # pursuable_with_caution / ...). Populated only by county docket scrapers
    # that implement the review model (currently Miami-Dade); empty otherwise.
    foreclosure_type:       str = ""   # "mortgage_foreclosure" | "tax_deed" | ""
    docket_evidence_level:  str = ""   # Eric's taxonomy (see above)
    lead_status:            str = ""   # pursuable | pursuable_with_caution | not_pursuable
    claim_type:             str = ""   # matched claim phrase, when claim_filed

    # Verification status model (HARDENING — assigned by assign_status_fields)
    research_status:  str = "unknown"
    lead_quality:     str = "unknown"
    money_status:     str = "unknown"
    evidence_level:   str = "unknown"
    pipeline_ready:   bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════
def _latest_jsonl_for_county(county_id: str) -> Optional[Path]:
    pattern = f"{county_id}_*.jsonl"
    files = sorted(RAW_DIR.glob(pattern))
    return files[-1] if files else None


def _extract_sale_datetime(record: dict) -> str:
    """
    Extract a human-readable timestamp like "May 4, 2026 9:02 AM ET" from the raw_text.
    Returns empty string if not parseable.
    """
    raw = record.get("raw_text", "") or ""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}:\d{2})\s*(AM|PM)?\s*ET", raw, re.IGNORECASE)
    if not m:
        return ""
    try:
        mm, dd, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        time_str = m.group(4)
        ampm = (m.group(5) or "").upper()
        d = date(yyyy, mm, dd)
        # NOTE: use d.day (portable) not strftime('%-d') — the %-d directive is
        # glibc-only and raises ValueError on Windows, which the except below
        # would swallow into "" (silently blanking sale_datetime on non-Linux
        # regen). Output is identical to '%b %-d, %Y' on Linux.
        return f"{d.strftime('%b')} {d.day}, {d.year} {time_str} {ampm} ET".strip()
    except (ValueError, AttributeError):
        return ""


def _normalize_address(raw: str) -> str:
    if not raw:
        return ""
    return raw.replace("Property Address:", "").strip()


def _extract_sale_date(record: dict) -> Optional[date]:
    """
    Try every plausible source for the sale date and return a date object.
    Returns None if no parseable date is found.
    """
    # Direct fields first
    for key in ("sale_date", "sale_datetime", "auction_date", "soldDate", "AUCTIONDATE"):
        v = record.get(key)
        if v:
            iso = str(v)[:10]
            try:
                return date.fromisoformat(iso)
            except ValueError:
                pass

    # Pull from raw_text — most scrapers store the unparsed page text
    raw_text = record.get("raw_text", "") or ""

    patterns = [
        r"(\d{1,2}/\d{1,2}/\d{4})\s+\d{1,2}:\d{2}",
        r"Sold on\s+(\d{1,2}/\d{1,2}/\d{4})",
        r"Sale Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})",
        r"AUCTIONDATE[=:\s]+(\d{1,2}/\d{1,2}/\d{4})",
        r"(\d{1,2}/\d{1,2}/\d{4})",
    ]
    for pat in patterns:
        m = re.search(pat, raw_text)
        if m:
            try:
                return datetime.strptime(m.group(1), "%m/%d/%Y").date()
            except ValueError:
                continue
    return None


def _parse_lead(record: dict, county_id: str, source_file: str) -> Optional[Lead]:
    """Convert a raw scraper record into a Lead dataclass."""
    info = COUNTY_INFO.get(county_id, {})

    try:
        opening   = float(record.get("opening_bid") or 0)
        final     = float(record.get("final_sale_price") or 0)
        assessed  = float(record.get("assessed_value") or 0)
        appraised = float(record.get("appraised_value") or 0)
        surplus   = final - opening if final and opening else 0
    except (ValueError, TypeError):
        return None

    # Normalize sale_date to ISO format if we can extract one
    parsed_date = _extract_sale_date(record)
    sale_date_iso = parsed_date.isoformat() if parsed_date else (record.get("sale_date") or "").strip()

    state = info.get("state", "")

    # ── STATE-AWARE SURPLUS (Eric's May 12 rule) ────────────────────────
    # FL: opening_bid IS the judgment amount (set from the FL auction
    #     calendar). Real-debt surplus = sale - opening_bid. Records this
    #     provenance via debt_source so downstream consumers can audit it.
    # OH: opening_bid is the statutory 2/3-appraised value, NOT real debt.
    #     true_surplus stays None until a docket scraper supplies a real
    #     prayer amount in _merge_docket_data.
    # IMPORTANT: FL true_surplus from opening-bid math is real-debt-backed
    # but is NOT "verified" — confirmation still requires a docket kill-
    # signal check + proof fields per assign_status_fields. A FL lead with
    # debt_source="fl_opening_bid" and no docket data sits in apparent_surplus.
    # OH TAX (RC 5721): opening_bid IS the statutory Minimum Bid = real tax debt,
    # so opening-bid surplus math is valid (kept). OH MORTGAGE: opening_bid is the
    # 2/3-appraised value, NOT debt — true_surplus stays None until a docket prayer
    # is found; the fake opening-bid number is NEVER used for OH-mortgage surplus.
    _case_no = (record.get("case_number") or "").strip()
    if state == "FL" and final > 0 and opening > 0:
        initial_true_surplus = round(final - opening, 2)
        initial_debt_source = "fl_opening_bid"
    elif state == "OH" and is_oh_tax_case(_case_no) and final > 0 and opening > 0:
        initial_true_surplus = round(final - opening, 2)
        initial_debt_source = "oh_tax_minimum_bid"
    else:
        initial_true_surplus = None
        initial_debt_source = ""

    return Lead(
        county_id     = county_id,
        county_name   = info.get("name", county_id),
        state         = state,
        case_number   = (record.get("case_number") or "").strip(),
        address       = _normalize_address(record.get("address") or ""),
        parcel_id     = (record.get("parcel_id") or "").strip(),
        auction_type  = (record.get("auction_type") or "").strip(),
        opening_bid   = opening,
        final_sale_price = final,
        gross_surplus = surplus,
        assessed_value   = assessed,
        appraised_value  = appraised,
        sale_date     = sale_date_iso,
        sale_datetime = _extract_sale_datetime(record),
        sold_to       = (record.get("sold_to") or "").strip(),
        source_url    = (record.get("source_url") or "").strip(),
        is_third_party = bool(record.get("is_third_party", False)),
        auction_status = (record.get("auction_status") or "").strip(),
        scraped_at    = datetime.now().isoformat(timespec="seconds"),
        source_file   = source_file,
        true_surplus  = initial_true_surplus,
        debt_source   = initial_debt_source,
    )


def _score_lead(lead: Lead) -> tuple[str, str]:
    s = lead.gross_surplus
    reasons = []

    if s >= 100_000:
        score = "A+"
        reasons.append(f"${s:,.0f} surplus ≥ $100K")
    elif s >= 50_000:
        score = "A"
        reasons.append(f"${s:,.0f} surplus ≥ $50K")
    elif s >= 25_000:
        score = "B"
        reasons.append(f"${s:,.0f} surplus ≥ $25K")
    elif s >= 10_000:
        score = "C"
        reasons.append(f"${s:,.0f} surplus ≥ $10K")
    else:
        score = "—"
        reasons.append("below threshold")

    if lead.is_third_party:
        reasons.append("3rd party bidder ✓")
    if lead.address:
        reasons.append("address known")
    if lead.parcel_id:
        reasons.append("parcel ID known")

    return score, " | ".join(reasons)


# ═══════════════════════════════════════════════════════════════════════
# Verification status model  (HARDENING PASS — Parts 1-6)
#
# Strict separation of confidence layers:
#   • Auction data can create a POSSIBLE lead        → apparent_surplus
#   • PropertyRadar can ENRICH a lead                → estimated_surplus
#   • Only docket / official records CONFIRM surplus → confirmed_surplus
#
# A lead is confirmed_surplus ONLY if every required proof field is present.
# ═══════════════════════════════════════════════════════════════════════

# Classifications a docket scrape can assign.
_POSITIVE_CLASSIFICATIONS = {"green", "yellow"}   # reviewed AND still viable
_NEGATIVE_CLASSIFICATIONS = {"red", "killed"}     # reviewed, NOT viable

# Required proof fields for confirmed_surplus (spec Part 3).
def _has_required_proof(lead) -> bool:
    """
    True only if the lead carries every proof field required to call it
    confirmed_surplus. Any missing field => not confirmed.
    """
    if not lead.county_id:
        return False
    if not lead.case_number:
        return False
    if lead.true_surplus is None or lead.true_surplus <= 0:
        return False
    if not (lead.docket_url or lead.source_url):
        return False
    if not lead.proof_of_surplus:
        return False
    if not lead.sale_date:
        return False
    if not lead.final_sale_price or lead.final_sale_price <= 0:
        return False
    if (lead.classification or "").strip().lower() not in _POSITIVE_CLASSIFICATIONS:
        return False
    return True


def assign_status_fields(lead) -> None:
    """
    Assign research_status, lead_quality, money_status, evidence_level,
    pipeline_ready on a Lead in place.

    This is the single chokepoint that decides whether a lead may be called
    confirmed surplus. It is deliberately conservative: when in doubt, downgrade.
    """
    classification = (lead.classification or "").strip().lower()
    has_docket = bool(classification) or lead.prayer_amount > 0 or bool(lead.docket_url)
    # has_pr = ACTUAL PropertyRadar refinement, NOT owner_name presence. The
    # owner-name fix populates owner_name on every FL docket lead; treating that
    # as "PR-enriched" flipped 11 docket leads to estimated_surplus with no real
    # PR data and inflated the Estimated headline ~$900K. PR's real refinement
    # (a non-zero loan balance) isn't known until the dashboard merges PR data,
    # so the loader stays provisional-apparent here and dashboard_data's
    # _reassign_status_after_pr is the SOLE authority that promotes to
    # estimated_surplus — and only when pr_match AND pr_total_loan_balance > 0.
    has_pr     = bool(getattr(lead, "enriched", False))

    # ---- lead_quality: mirrors docket classification, else unknown ----
    if classification in _POSITIVE_CLASSIFICATIONS or classification in _NEGATIVE_CLASSIFICATIONS:
        lead.lead_quality = classification
    else:
        lead.lead_quality = "unknown"

    # ---- killed / red: reviewed but NOT viable (spec Part 6) ----
    if classification in _NEGATIVE_CLASSIFICATIONS:
        lead.research_status = "docket_reviewed"
        lead.evidence_level  = "docket_reviewed"
        lead.money_status    = "no_surplus" if classification == "killed" else "unknown"
        lead.pipeline_ready  = False
        return

    # ---- positive classification: candidate for confirmed_surplus ----
    if classification in _POSITIVE_CLASSIFICATIONS:
        if _has_required_proof(lead):
            lead.research_status = "docket_reviewed"
            lead.money_status    = "confirmed_surplus"
            lead.evidence_level  = "docket_confirmed"
            lead.pipeline_ready  = True
        else:
            # Reviewed green/yellow but missing proof => DOWNGRADE (spec Part 3)
            lead.research_status = "docket_reviewed"
            lead.money_status    = "estimated_surplus" if has_pr else "apparent_surplus"
            lead.evidence_level  = "docket_reviewed"
            lead.pipeline_ready  = False
        return

    # ---- docket-checked, metadata-only (no green/yellow/killed classification):
    #      e.g. Franklin — the portal exposes NO judgment amount, so there's no
    #      debt/prayer and no positive/negative grade, but the docket WAS reviewed
    #      and showed no post-sale kill signals. Stays apparent (never confirmed —
    #      no proof/debt); evidence_level reflects that a docket review happened,
    #      not "auction only". Keyed on the explicit docket_checked marker so a
    #      failed/no-match scrape (classification 'unknown') does NOT qualify. ----
    if getattr(lead, "docket_evidence_level", "") == "docket_checked":
        lead.research_status = "docket_reviewed"
        lead.money_status    = "estimated_surplus" if has_pr else "apparent_surplus"
        lead.evidence_level  = "docket_reviewed"
        lead.pipeline_ready  = False
        return

    # ---- no docket classification: PR-enriched or auction-only ----
    if has_pr:
        # PropertyRadar enriched, but PR does NOT verify surplus (spec Part 4)
        lead.research_status = "property_enriched"
        lead.money_status    = "estimated_surplus"
        lead.evidence_level  = "property_enriched"
        lead.pipeline_ready  = False
        return

    # ---- auction-only: apparent surplus, never confirmed (spec Part 5) ----
    lead.research_status = "auction_only"
    lead.money_status    = "apparent_surplus"
    lead.evidence_level  = "auction_only"
    lead.pipeline_ready  = False


# ═══════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════
def load_all_leads(
    min_surplus: float = 10_000,
    require_third_party: bool = True,
    counties: Optional[list[str]] = None,
    window_days: int = LEAD_WINDOW_DAYS,
    verbose: bool = True,
) -> list[Lead]:
    """
    Load all qualifying leads from raw JSONL files across all counties.

    Filters applied (in order):
      1. is_third_party (must be True if require_third_party)
      2. gross_surplus >= min_surplus
      3. sale_date must be parseable
      4. sale_date >= (today - window_days)  ← NEW in v2

    Args:
        min_surplus: Minimum gross surplus required to qualify (default $10K)
        require_third_party: Only include 3rd party bidder wins (default True)
        counties: Optional list of county_ids to include (default: all 10)
        window_days: Maximum age of sale_date in days (default: the shared
                     config.counties.LEAD_WINDOW_DAYS constant, which also
                     sets the auction scrape depth — the two must match so
                     every displayed lead is re-scraped for late-filed kill
                     signals throughout its window)
        verbose: Print summary of what was filtered out

    Returns:
        List of Lead objects, sorted by gross_surplus descending.
    """
    # Load docket scraper results once for the whole run
    _docket_lookup = _load_docket_data()

    target_counties = counties or list(COUNTY_INFO.keys())
    today = date.today()
    cutoff = today - timedelta(days=window_days)
    leads: list[Lead] = []

    # Track what got filtered out, per county
    stats = {
        cid: {"raw": 0, "kept": 0, "not_3rd_party": 0, "below_min": 0,
              "no_date": 0, "out_of_window": 0}
        for cid in target_counties
    }

    for county_id in target_counties:
        jsonl_path = _latest_jsonl_for_county(county_id)
        if not jsonl_path:
            if verbose:
                print(f"⚠ No data file found for {county_id}")
            continue

        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                stats[county_id]["raw"] += 1

                lead = _parse_lead(record, county_id, str(jsonl_path.name))
                if not lead:
                    continue

                # FP-11 docket-rescue: a lead with real docket-extracted
                # positive true_surplus must NOT be dropped by the
                # auction-side min-gross-surplus filter (Filter 2). That
                # filter uses sale − opening_bid, which for OH is arithmetic
                # on the fake 2/3-appraised value — meaningless without the
                # docket. Once the docket reveals a real prayer amount and
                # the math clears positively, the lead clears Filter 2.
                #
                # Docket-rescue does NOT bypass Filter 1 (third-party).
                # sold_to='Plaintiff' means the plaintiff took the property
                # back — no recoverable surplus exists for the homeowner
                # regardless of what the docket prayer math shows. Per SOP
                # step 11: plaintiff-won = KILL, no exception.
                #
                # We rescue green/yellow AND red leads (red = "competing
                # filers / additional creditors" per Eric — risky but
                # still real surplus money). We DO NOT rescue killed.
                _norm = _normalize_case_for_lookup(lead.case_number)
                _docket_preview = _docket_lookup.get((lead.county_id, _norm))
                _docket_rescue = False
                if _docket_preview:
                    prayer = float(_docket_preview.get("prayer_amount") or 0)
                    cls = (_docket_preview.get("classification") or "").lower()
                    # Prayer-plausibility floor ($10K): a sub-$10K prayer is
                    # court-cost/fee noise, NOT a real judgment — it must NOT
                    # docket-rescue a lead (Cuyahoga floors this at scrape time;
                    # this enforces the same rule for already-committed Summit/
                    # Montgomery PDF-extracted prayers). Such a lead falls through
                    # to the OH-no-debt path + the 1.5× overbid gate.
                    if (prayer >= 10000.0
                            and cls in (_POSITIVE_CLASSIFICATIONS | {"red"})
                            and cls != "killed"):
                        ts = float(lead.final_sale_price or 0) - prayer
                        if ts > 0:
                            _docket_rescue = True

                # OH-MORTGAGE-UNVERIFIED: an OH mortgage lead with no usable docket
                # prayer. Its gross_surplus is sale − 2/3-appraised opening bid =
                # a FAKE number, so we can't filter on its MAGNITUDE. OH tax
                # (oh_tax_minimum_bid) and docket-rescued leads are excluded.
                _oh_mortgage_unverified = (
                    lead.state == "OH"
                    and lead.debt_source != "oh_tax_minimum_bid"
                    and not _docket_rescue
                )
                # REAL-OVERBID gate (Option 2): surface an OH-mortgage-unverified
                # lead only when the sale meaningfully exceeds the 2/3-appraised
                # opening — sale ≥ the implied appraised value (1.5× the opening)
                # AND an absolute overbid ≥ min_surplus. Selling above appraised
                # value is the clearest no-docket signal of genuine surplus that
                # plausibly survives unknown real debt. Sold-at/near-2/3-minimum
                # leads (no real overbid) stay FILTERED — near-zero surplus odds.
                _oh_real_overbid = (
                    lead.opening_bid > 0
                    and lead.final_sale_price >= 1.5 * lead.opening_bid
                    and (lead.final_sale_price - lead.opening_bid) >= min_surplus
                )

                # Filter 1: 3rd party. Docket-rescue does NOT bypass this —
                # sold_to='Plaintiff' kills the lead at this gate regardless
                # of docket prayer math (SOP step 11: plaintiff-won = no
                # recoverable surplus). Rescue only applies to Filter 2.
                if require_third_party and not lead.is_third_party:
                    stats[county_id]["not_3rd_party"] += 1
                    continue

                # Filter 2: minimum surplus.
                if _oh_mortgage_unverified:
                    # Gate on a REAL overbid, not the fake 2/3-opening gross_surplus.
                    if not _oh_real_overbid:
                        stats[county_id]["below_min"] += 1
                        continue
                elif lead.gross_surplus < min_surplus and not _docket_rescue:
                    stats[county_id]["below_min"] += 1
                    continue

                # Filter 3: sale_date must be parseable
                parsed_date = _extract_sale_date(record)
                if not parsed_date:
                    stats[county_id]["no_date"] += 1
                    continue

                # Filter 4: sale_date within window_days of today
                if parsed_date < cutoff:
                    stats[county_id]["out_of_window"] += 1
                    continue

                # Score and keep
                lead.score, lead.score_reason = _score_lead(lead)
                stats[county_id]["kept"] += 1
                # Merge docket-scraper data if available.
                # FP-3 fix: if no docket data, true_surplus stays None
                # (NOT defaulted to gross_surplus). Apparent-only leads keep
                # true_surplus=None as the explicit "not verified" signal.
                _norm = _normalize_case_for_lookup(lead.case_number)
                _docket = _docket_lookup.get((lead.county_id, _norm))
                if _docket:
                    _apply_docket_to_lead(lead, _docket, lead.county_id)
                # Assign the verification status model (FP-6 gate)
                assign_status_fields(lead)
                leads.append(lead)

    leads.sort(key=lambda x: x.gross_surplus, reverse=True)

    # Print filter audit if verbose
    if verbose:
        total_raw = sum(s["raw"] for s in stats.values())
        total_kept = sum(s["kept"] for s in stats.values())
        total_dropped_window = sum(s["out_of_window"] for s in stats.values())
        total_dropped_date = sum(s["no_date"] for s in stats.values())

        print(f"\n  Date filter: keeping leads sold on or after {cutoff.isoformat()} (last {window_days} days)")
        print(f"  Loaded {total_kept} qualifying leads from {total_raw} raw records.")
        if total_dropped_window or total_dropped_date:
            print(f"  Dropped {total_dropped_window} as out-of-window, {total_dropped_date} with no parseable date.")

        # Show per-county breakdown if anything was dropped for date reasons
        if total_dropped_window or total_dropped_date:
            print("\n  Per-county date-filter impact:")
            for cid in target_counties:
                s = stats[cid]
                if s["out_of_window"] > 0 or s["no_date"] > 0:
                    print(f"    {cid:<18}: kept {s['kept']:>2}, "
                          f"dropped {s['out_of_window']:>2} out-of-window, "
                          f"{s['no_date']:>2} no-date")

    return leads


def get_summary(leads: list[Lead]) -> dict:
    by_county: dict[str, dict] = {}
    by_state: dict[str, dict] = {"FL": {"leads": 0, "surplus": 0.0},
                                  "OH": {"leads": 0, "surplus": 0.0}}
    by_score = {"A+": 0, "A": 0, "B": 0, "C": 0}

    for lead in leads:
        cid = lead.county_id
        if cid not in by_county:
            by_county[cid] = {
                "county_id":   cid,
                "county_name": lead.county_name,
                "state":       lead.state,
                "leads":       0,
                "surplus":     0.0,
                "top_lead":    0.0,
            }
        by_county[cid]["leads"] += 1
        by_county[cid]["surplus"] += lead.gross_surplus
        by_county[cid]["top_lead"] = max(by_county[cid]["top_lead"], lead.gross_surplus)

        if lead.state in by_state:
            by_state[lead.state]["leads"] += 1
            by_state[lead.state]["surplus"] += lead.gross_surplus

        if lead.score in by_score:
            by_score[lead.score] += 1

    return {
        "generated_at":    datetime.now().isoformat(timespec="seconds"),
        "total_leads":     len(leads),
        # FP-4 fix: this total is gross/apparent surplus ONLY. It is NOT
        # confirmed money. dashboard_data.py recomputes confirmed/estimated
        # totals from the verification status model.
        "total_apparent_surplus": sum(l.gross_surplus for l in leads),
        "by_county":       sorted(by_county.values(), key=lambda x: x["surplus"], reverse=True),
        "by_state":        by_state,
        "by_score":        by_score,
        "top_5_leads":     [
            {
                "county":      l.county_name,
                "state":       l.state,
                "case_number": l.case_number,
                "address":     l.address,
                "apparent_surplus": l.gross_surplus,
                "sale_price":  l.final_sale_price,
                "sale_date":   l.sale_date,
                "score":       l.score,
            }
            for l in leads[:5]
        ],
    }


# ═══════════════════════════════════════════════════════════════════════
# CLI for quick verification
# ═══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys

    print("=" * 70)
    print("  SurplusIQ — Data Loader Verification (v2 with 28-day cutoff)")
    print("=" * 70)
    print(f"\n📂 Reading from: {RAW_DIR}\n")

    leads = load_all_leads()
    summary = get_summary(leads)

    print(f"\n✓ Total APPARENT surplus (auction math, not confirmed): "
          f"${summary['total_apparent_surplus']:,.0f}\n")

    print("─" * 70)
    print("  BY STATE")
    print("─" * 70)
    for state, data in summary["by_state"].items():
        print(f"  {state}: {data['leads']:>3} leads | ${data['surplus']:>14,.0f}")

    print("\n" + "─" * 70)
    print("  BY COUNTY (sorted by surplus)")
    print("─" * 70)
    for c in summary["by_county"]:
        print(f"  {c['county_name']:<14} ({c['state']}): {c['leads']:>3} leads | "
              f"${c['surplus']:>14,.0f} | top: ${c['top_lead']:>11,.0f}")

    print("\n" + "─" * 70)
    print("  BY SCORE")
    print("─" * 70)
    for score, count in summary["by_score"].items():
        bar = "█" * count
        print(f"  {score:<3}: {count:>3}  {bar}")

    print("\n" + "─" * 70)
    print("  TOP 5 LEADS")
    print("─" * 70)
    for i, l in enumerate(summary["top_5_leads"], 1):
        print(f"  #{i}  ${l['surplus']:>11,.0f} | {l['score']:<3} | "
              f"{l['county']}, {l['state']} | sold {l['sale_date']} | {l['case_number']}")
        if l['address']:
            print(f"       {l['address'][:60]}")

    print("\n" + "=" * 70)
    print("  ✓ Data loader v2 operational. 28-day cutoff enforced.")
    print("=" * 70)
