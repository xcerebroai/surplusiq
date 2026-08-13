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


# Generic / non-real-party defendant names (unknown heirs, tenants, John Doe,
# "any and all parties claiming", etc.). Mirrors core.dockets.miami_dade's
# _GENERIC_PARTY — kept local to avoid importing the Playwright-heavy scraper
# module at load time. Used to skip junk when falling back to the defendants
# list for owner_name (a corporate owner like an investor LLC IS a real party
# and is deliberately NOT excluded here — only true non-parties are).
_GENERIC_DEFENDANT = re.compile(
    r"unknown|tenant|any and all|all other|in possession|et al|john doe|jane doe|"
    r"parties claiming|lienors|creditors|whether dissolved|n/k/a|a/k/a unknown", re.I)

# Foreclosure-PLAINTIFF institution markers. Some counties' docket "defendants"
# lists are not cleanly role-separated (Broward mixes the plaintiff bank/HOA into
# the party list, sometimes token-reversed), so a bare defendants[0] could put a
# bank or HOA in the owner column. These markers identify institutional plaintiffs
# that are never a residential owner of record — while deliberately NOT matching a
# plain investor LLC/INC owner (e.g. "LOYAL CIMA LLC", "2912 CLINTON AVE LLC").
_PLAINTIFF_ORG = re.compile(
    r"\b(bank|mortgage|homeowner|home\s*owners?|association|assn|condominium|"
    r"savings|credit union|servicing|federal home loan|n\.?a\.?)\b", re.I)


def derive_owner_from_docket(docket: dict) -> str:
    """Best owner-of-record name from a docket record, or "" if none derivable.

    Each county's scraper stores party data differently:
      • Miami-Dade / Summit / Montgomery / Duval: a role-filtered "defendants"
        list (owner first, plaintiff excluded). The scraper blanks owner_name
        when the defendant of record is a company — but a foreclosed property
        owned by an investor LLC (e.g. "LOYAL CIMA LLC") makes that LLC the
        surplus-entitled party, so blank is strictly worse.
      • Cuyahoga: no defendants list, only a clean "PLAINTIFF vs. DEFENDANT,
        ET AL." case_title.

    Guards: skip true non-parties (_GENERIC_DEFENDANT: John Doe, tenants, unknown
    heirs) and institutional plaintiffs (_PLAINTIFF_ORG: bank/mortgage/HOA/…) so a
    plaintiff never lands in the owner column — needed because some counties'
    party lists are NOT role-separated (Broward mixes the plaintiff in, sometimes
    token-reversed) — WITHOUT dropping a legitimate investor LLC/INC owner. Never
    fabricates: no usable party data → "".
    """
    owner = (docket.get("owner_name", "") or "").strip()
    if owner:
        return owner
    for cand in docket.get("defendants", []) or []:
        cand = (cand or "").strip()
        if (len(cand) >= 3 and not _GENERIC_DEFENDANT.search(cand)
                and not _PLAINTIFF_ORG.search(cand)):
            return cand
    m = re.search(r"\bvs\.?\s+(.+?)(?:,?\s+et\s+al\b|$)",
                  docket.get("case_title", "") or "", re.I)
    if m:
        cand = re.sub(r"\s+", " ", m.group(1)).strip().strip(",").strip()
        if (len(cand) >= 3 and not _GENERIC_DEFENDANT.search(cand)
                and not _PLAINTIFF_ORG.search(cand)):
            return cand
    return ""


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


# FL COUNTY-COURT (HOA/condo lien) foreclosure detector — LOCAL PER COUNTY.
# A county-court foreclosure is filed by an HOA/condo association over a small
# maintenance-fee lien; the SENIOR MORTGAGE SURVIVES the sale (buyer takes
# subject to it). So the opening bid is the small association lien, NOT the real
# debt, and sale − opening_bid is a phantom surplus. Prefix taxonomy confirmed
# from real historical raw (2026-08):
#   • Broward — circuit = 'CACE'; county-court = 'CO**' (CONO/COCE/COWE/COSO).
#   • Orange / Duval / Miami-Dade / Lee — circuit = '-CA-'; county-court = '-CC-'.
# NOT shared across counties — each county's format is encoded explicitly.
def is_fl_county_court_case(county_id: str, case_number: str) -> bool:
    cn = (case_number or "").upper()
    if county_id == "broward-fl":
        # first token before the dash: CACE = circuit, CO** = county court
        m = re.match(r"^([A-Z]{2,5})-", cn)
        return bool(m and m.group(1).startswith("CO"))
    if county_id in ("orange-fl", "duval-fl", "miami-dade-fl", "lee-fl"):
        return "-CC-" in cn
    return False


# FL TAX-DEED detector — a DIFFERENT mechanism from the county-court HOA case.
# A tax-deed sale extinguishes junior liens (incl. the mortgage) and FL statute
# (FS 197.582) provides a claimable surplus. So the surplus POOL (sale − opening
# bid) is REAL and clerk-held — NOT a phantom. But the opening bid is the
# delinquent taxes + costs, NOT the judgment debt, and the pool is distributed
# to lienholders (governmental, then the former mortgagee, then others) BEFORE
# the former owner, so owner-net is unknown from auction data. Detect on the
# reliable auction_type field, with a per-county case-format backup:
#   Miami-Dade 'YYYY' + 'A' + digits (e.g. 2026A00192); Duval 'YYYY-NNNN' + 'TD'.
def is_fl_tax_deed(county_id: str, case_number: str, auction_type: str) -> bool:
    if (auction_type or "").strip().upper() == "TAXDEED":
        return True
    cn = (case_number or "").upper().strip()
    if county_id == "miami-dade-fl":
        return bool(re.match(r"^\d{4}A\d{3,}$", cn))
    if county_id == "duval-fl":
        return bool(re.match(r"^\d{4}-\d+TD$", cn))
    return False


def is_tax_deed_redeemed(record: dict) -> bool:
    """A tax deed the owner REDEEMED (paid the back taxes) is a NON-SALE — no
    surplus exists. Primary signal: auction_status contains 'redeem' (covers
    'Redeemed' and 'Redeemed After Sale'; verified to catch all 34 real cases).
    Defensive backup: final_sale_price == assessed_value exactly, the artifact a
    redeemed auction leaves when the scraper reads assessed value as the sale
    (a subset of the status signal — 0 false positives observed)."""
    st = (record.get("auction_status") or "").lower()
    if "redeem" in st:
        return True
    sale = record.get("final_sale_price") or 0
    assessed = record.get("assessed_value") or 0
    return bool(sale > 0 and assessed > 0 and sale == assessed)


def _apply_taxdeed_claim_status(lead, rec: dict) -> None:
    """Apply a RealTDM tax-deed CLAIM-STATUS record to a tax-deed lead.

      • killed            → classification='killed' + reason (FP-14 filters it,
                            killed_leads shows the cited reason).
      • surplus_confirmed → keep it a labeled pool but surface the CLERK-stated
                            pool amount (better than auction math; still pre-lien,
                            NOT owner-net) + the 90-day claim window.
      • pool_pending      → no change (fresh sale; absence of a surplus doc is
                            the normal in-window state — never a penalty).
    Never computes owner-net; never presents the pool as owner-recoverable."""
    verdict = (rec.get("taxdeed_verdict") or "").strip()
    lead.taxdeed_verdict = verdict
    if verdict == "killed":
        lead.classification = "killed"
        lead.classification_reason = rec.get("classification_reason") or rec.get("taxdeed_reason") or "tax-deed dead"
        sig = rec.get("kill_signal") or "taxdeed_killed"
        lead.kill_signals = list(set((lead.kill_signals or []) + [sig]))
    elif verdict == "surplus_confirmed":
        pool = rec.get("surplus_pool_amount")
        lead.taxdeed_surplus_pool = float(pool) if pool is not None else None
        dd = rec.get("claim_deadline_days")
        lead.taxdeed_claim_deadline_days = int(dd) if dd is not None else None
    # pool_pending: intentionally no change.


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

    # Owner = defendant homeowner from the docket. Fill ONLY when the auction
    # scrape left owner_name blank, and never overwrite a name the auction
    # already provided.
    docket_owner = derive_owner_from_docket(docket)
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

    # Appraised-value sanity signal (OH mortgage leads WITHOUT real docket debt).
    # OH opening_bid should be the statutory 2/3-appraised value; an opener far
    # below that (reduced-minimum second auction, or a data anomaly) makes
    # gross_surplus = sale − opener NOT a credible surplus. Flag + show context;
    # never suppress. Set by _flag_mispriced_opener; inert until appraised_value
    # is populated (OH auction scrape).
    mispriced_opener:    bool  = False
    sale_vs_appraised:   float = 0.0   # final_sale_price / appraised_value
    # FL county-court (HOA/condo lien) foreclosure — senior mortgage survives the
    # sale, so sale − opening_bid is NOT a real surplus (set in _parse_lead).
    fl_county_court:     bool  = False
    # FL tax-deed sale (FS 197.582). fl_tax_deed: the surplus POOL is real but
    # pre-lien (owner-net unknown). fl_tax_deed_redeemed: a NON-SALE (owner paid
    # the back taxes) — no surplus at all. Both set in _parse_lead.
    fl_tax_deed:          bool  = False
    fl_tax_deed_redeemed: bool  = False
    # Tax-deed CLAIM-STATUS (RealTDM claim-status layer, Miami-Dade). Verdict:
    # pool_pending (fresh) / surplus_confirmed / killed. On surplus_confirmed the
    # clerk-stated POOL amount (pre-lien, NOT owner-net) + 90-day claim window.
    taxdeed_verdict:        str   = ""
    taxdeed_surplus_pool:   Optional[float] = None
    taxdeed_claim_deadline_days: Optional[int] = None

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
    _auction_type = (record.get("auction_type") or "").strip()
    _is_tax_deed = is_fl_tax_deed(county_id, _case_no, _auction_type)
    _tax_deed_redeemed = _is_tax_deed and is_tax_deed_redeemed(record)
    if _is_tax_deed:
        # TAX DEED — the tax opening bid is NOT the judgment debt, so it must NOT
        # be tagged fl_opening_bid. A redeemed (non-sale) tax deed has NO surplus;
        # a real tax-deed sale has a pre-lien POOL (owner-net unknown). Neither is
        # a clean owner true_surplus → leave None; the dashboard surfaces the pool
        # figure from gross_surplus with the FS 197.582 lien caveat.
        initial_true_surplus = None
        initial_debt_source = ""
    elif state == "FL" and final > 0 and opening > 0:
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
        fl_county_court = is_fl_county_court_case(county_id, _case_no),
        fl_tax_deed          = _is_tax_deed and not _tax_deed_redeemed,
        fl_tax_deed_redeemed = _tax_deed_redeemed,
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
# Statutory OH first-auction opener = 2/3 (0.667) of appraised value. We flag an
# opener below 90% of that floor — i.e. opening_bid < 0.60 × appraised. Reasoning:
# real OH openers cluster EXACTLY at 2/3 appraised (observed ratio 1.00 across
# every normal OH lead); an opener >10% under the statutory floor means a
# reduced-minimum second auction (Ohio allows no/low minimum after a failed
# first sale) or a data anomaly. In either case the opener no longer approximates
# the debt, so gross_surplus = sale − opener is NOT a credible surplus. The 0.60
# line sits well below the 0.667 normal cluster → zero false positives on normal
# leads while catching genuine distortions (Franklin 25CV9562 at 0.067, Hamilton
# A2505625 at 0.305). Err toward flagging (caution), never suppress.
MISPRICED_OPENER_FLOOR = 0.60   # fraction of appraised below which the opener is anomalous


def _flag_mispriced_opener(lead) -> None:
    """OH mortgage leads WITHOUT real docket debt only. Leads that carry a
    trustworthy docket debt figure (oh_mortgage_computed / _uncertain / tax /
    FL opening) are left untouched — they already have a credible number.
    Inert when appraised_value is 0 (not yet scraped)."""
    if lead.state != "OH":
        return
    if (lead.debt_source or "") != "":        # any real debt provenance → skip
        return
    ap = getattr(lead, "appraised_value", 0.0) or 0.0
    if ap <= 0 or lead.opening_bid <= 0:
        return
    lead.sale_vs_appraised = round((lead.final_sale_price or 0) / ap, 3)
    if lead.opening_bid < MISPRICED_OPENER_FLOOR * ap:
        lead.mispriced_opener = True


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
                    # A tax-deed claim-status record (RealTDM) has its own shape —
                    # route it to the dedicated handler, NOT the foreclosure merge.
                    if "taxdeed_verdict" in _docket:
                        _apply_taxdeed_claim_status(lead, _docket)
                    else:
                        _apply_docket_to_lead(lead, _docket, lead.county_id)
                # Assign the verification status model (FP-6 gate)
                assign_status_fields(lead)
                _flag_mispriced_opener(lead)
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
        # FL county-court/HOA leads have NO credible surplus (senior mortgage
        # survives); FL tax-deed pools are real but pre-lien (owner-net unknown)
        # and redeemed tax deeds are non-sales — none is a clean owner-recoverable
        # figure, so all contribute $0 to the county surplus breakdown (consistent
        # with the KPI apparent-total treatment). They stay counted as leads.
        _not_owner_surplus = (getattr(lead, "fl_county_court", False)
                              or getattr(lead, "fl_tax_deed", False)
                              or getattr(lead, "fl_tax_deed_redeemed", False))
        credible_surplus = 0.0 if _not_owner_surplus else lead.gross_surplus
        by_county[cid]["leads"] += 1
        by_county[cid]["surplus"] += credible_surplus
        by_county[cid]["top_lead"] = max(by_county[cid]["top_lead"], credible_surplus)

        if lead.state in by_state:
            by_state[lead.state]["leads"] += 1
            by_state[lead.state]["surplus"] += credible_surplus

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
