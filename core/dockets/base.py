"""
SurplusIQ — Docket Scraper Base Class

Every county has a different clerk-of-court website. This base class
defines the common interface and shared utilities so each county scraper
follows the same pattern.

Per Eric's operational walkthrough, the docket scraper needs to produce:

  - true_debt           (the real amount owed, not the 2/3-appraised bid)
  - kill_signals        (motion to vacate, bankruptcy, etc.)
  - proof_of_surplus    (certificate of disbursement / notice of surplus filed)
  - additional_parties  (defendants beyond the homeowner = creditors)
  - claim_filed         (someone already filed for the surplus)
  - last_activity_date  (most recent docket event)
  - classification      (green / yellow / red / killed)

Each county subclass implements:

  - format_case_number(raw) -> dict  (parses case # into search components)
  - scrape_case(case_number) -> DocketResult
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional


# ─── Kill signals: phrases that disqualify a lead entirely ───
# (Per knowledge/system_rules.md Section 2)
KILL_SIGNAL_PATTERNS = {
    "motion_to_vacate":  ["motion to vacate", "set aside sale", "cancel sale", "vacate the sale"],
    "sale_vacated":      ["order vacating sale", "sale vacated", "order to vacate"],
    "bankruptcy":        ["bankruptcy", "chapter 7", "chapter 13", "automatic stay", "notice of bankruptcy"],
    "already_disbursed": ["order to disburse surplus", "surplus disbursed", "funds disbursed"],
    "owner_filed_claim": ["owner's claim", "owner claim for surplus", "homeowner claim"],
    "escheated":         ["sent to state", "funds escheated", "unclaimed funds remitted"],
}


# ─── Bankruptcy resolution guard (OH) ───
# A bankruptcy signal alone must NOT kill — a stay filed years ago that was
# lifted/dismissed and the sale then completed leaves a live lead (Miami-Dade
# already flags active-vs-resolved; OH inherited the blunt rule). Same temporal
# approach as franklin_classify: bankruptcy KILLS only when it is the latest
# controlling status — no explicit resolution AND no sale progress dated after
# the bankruptcy event. Vocabulary ground-truthed on real OH dockets:
#   • Cuyahoga CV25122629: "MOTION TO VACATE JUDGMENT DUE TO BANKRUPTCY",
#     "SINCE THIS COURT NEVER FORMALLY STAYED THE CASE", then ORDER OF SALE
#     ISSUED / SOLD DATE after it.
#   • Franklin dockets: "BANKRUPTCY STAY - CASE INACTIVATED", "REINSTATED".
BANKRUPTCY_EVENT_MARKERS = (
    "bankrupt", "chapter 7", "chapter 13", "automatic stay", "suggestion of bankruptcy",
)
BANKRUPTCY_RESOLUTION_MARKERS = (
    "relief from stay", "stay lifted", "stay is lifted", "stay terminated",
    "stay dissolved", "stay vacated", "bankruptcy dismissed", "bankruptcy closed",
    "dismissing bankruptcy", "reinstated", "reinstate", "reactivated",
    "never formally stayed",
)
# A COMPLETED sale dated AFTER a bankruptcy event proves the case moved past it.
# Deliberately strict: only an actual sale ("SOLD DATE ...") or a confirmation
# counts — a mere new "order of sale issued" is just another attempt that could
# itself be stopped by the same bankruptcy, so it does NOT prove resolution.
BANKRUPTCY_SALE_PROGRESS_MARKERS = (
    "sold date", "confirmation of sale", "order confirming sale", "confirming sale",
)


# ─── Surplus proof signals: positive indicators that confirm real surplus ───
PROOF_OF_SURPLUS_PATTERNS = {
    "certificate_of_disbursement": ["certificate of disbursement"],
    "notice_of_surplus":           ["notice of surplus", "notice of excess proceeds"],
    "excess_proceeds":             ["excess proceeds", "excess funds", "surplus funds"],
}


# ─── Competing-firm signals: someone else might beat us to the claim ───
COMPETING_FILER_PATTERNS = {
    "motion_to_intervene":   ["motion to intervene"],
    "motion_for_surplus":    ["motion for surplus funds", "motion for disbursement", "claim for surplus"],
    "assignment_of_rights":  ["assignment of interest", "assignment of rights", "transfer of claim"],
}


@dataclass
class DocketEvent:
    """One row from the docket events table."""
    filing_date:   str = ""    # ISO format YYYY-MM-DD
    document_type: str = ""    # e.g. "Motion for Default Judgment"
    description:   str = ""    # Full description text
    pdf_url:       str = ""    # Link to PDF if present


@dataclass
class DocketResult:
    """
    Full output from scraping one case's docket page.
    This gets attached to a Lead record and used for tier scoring.
    """
    # Identification
    county_id:        str = ""
    case_number:      str = ""
    case_url:         str = ""
    scraped_at:       str = ""

    # Case basics
    case_title:       str = ""
    case_designation: str = ""    # e.g. "FORECLOSURE MARSH. OF LIEN"
    filing_date:      str = ""    # When the case was filed
    last_status:      str = ""    # ACTIVE, INACTIVE, etc.
    last_disposition: str = ""    # DEFAULT, DISMISSED, etc.
    last_activity_date: str = ""  # Most recent docket entry date

    # Money
    prayer_amount:    float = 0.0  # The TRUE debt amount
    debt_source:      str = ""     # "prayer_field", "pdf_extract", "propertyradar_estimate"

    # OH-mortgage conservative debt (core/dockets/oh_debt). The scraper parses the
    # decree and stores the COMPONENTS here (principal/rate/from-date/base/junior);
    # enrich.run_county does the sale-date math (needs the auction sale date) and
    # sets prayer_amount + classification + debt_flag from the verdict. Inert
    # default → counties that don't use it are unaffected.
    debt_components:  dict = field(default_factory=dict)
    debt_flag:        str = ""     # "" or "surplus uncertain — manual review" reason

    # Parties
    plaintiff:           str = ""
    defendants:          list = field(default_factory=list)
    additional_parties:  list = field(default_factory=list)  # creditors beyond homeowner
    owner_name:          str = ""   # the DEFENDANT HOMEOWNER (foreclosed party entitled
                                    # to surplus). Set by county scrapers that extract it
                                    # (e.g. Miami-Dade). Inert default → other counties
                                    # unaffected; loader copies it to lead.owner_name only
                                    # when present and the auction scrape left it blank.

    # Signals
    kill_signals:        list = field(default_factory=list)
    proof_of_surplus:    str = ""    # Empty = pending, or e.g. "certificate_of_disbursement"
    proof_amount:        float = 0.0
    competing_filers:    list = field(default_factory=list)
    owner_filed_claim:   bool = False

    # Docket events (for audit / debug)
    events:              list = field(default_factory=list)

    # Result classification
    classification:      str = "unknown"   # green | yellow | red | killed | unknown
    classification_reason: str = ""

    # ── Eric's review taxonomy (added 2026-06-09) ──
    # Populated by county parsers that implement the docket-review model.
    # Defaults are inert so counties that don't set them are unaffected.
    foreclosure_type: str = ""   # "mortgage_foreclosure" | "tax_deed" | ""
    evidence_level:   str = ""   # auction_only | docket_checked | no_claim_found |
                                 # claim_filed | title_report_reviewed | lien_identified |
                                 # bankruptcy_found | sale_issue_found | not_pursuable |
                                 # pursuable_with_caution | pursuable
    lead_status:      str = ""   # "pursuable" | "pursuable_with_caution" | "not_pursuable"
    claim_filed:      bool = False   # any surplus claim already filed (owner OR third party)
    claim_type:       str = ""       # the matched claim phrase, e.g. "owner's claim for surplus"

    def to_dict(self) -> dict:
        return asdict(self)


class DocketScraper:
    """Base class — each county subclasses this."""

    county_id: str = ""
    county_name: str = ""

    def __init__(self, headless: bool = True):
        self.headless = headless

    async def scrape_case(self, case_number: str) -> DocketResult:
        """Override this in each subclass."""
        raise NotImplementedError(f"{self.__class__.__name__} must implement scrape_case()")

    # ─── Shared utilities ───

    def detect_kill_signals(self, full_text: str) -> list:
        """Scan text for kill-signal phrases. Returns list of detected signal types."""
        text_lower = full_text.lower()
        found = []
        for signal_type, patterns in KILL_SIGNAL_PATTERNS.items():
            if any(p in text_lower for p in patterns):
                found.append(signal_type)
        return found

    def detect_proof_of_surplus(self, full_text: str) -> str:
        """Returns the proof type found, or empty string if none."""
        text_lower = full_text.lower()
        for proof_type, patterns in PROOF_OF_SURPLUS_PATTERNS.items():
            if any(p in text_lower for p in patterns):
                return proof_type
        return ""

    def detect_competing_filers(self, full_text: str) -> list:
        """Returns list of competing-filer types detected."""
        text_lower = full_text.lower()
        found = []
        for filer_type, patterns in COMPETING_FILER_PATTERNS.items():
            if any(p in text_lower for p in patterns):
                found.append(filer_type)
        return found

    def _bankruptcy_is_controlling(self, result: DocketResult) -> bool:
        """Return True if bankruptcy should KILL — it is the LATEST controlling
        status (no explicit resolution AND no sale progress dated after the
        bankruptcy event). Return False when resolved/superseded (→ do not kill;
        the lead is flagged via the retained signal but not disqualified).

        Conservative on missing evidence: if the 'bankruptcy' signal matched the
        docket text but NO dated bankruptcy event exists to anchor a timeline,
        we cannot prove resolution → treat as controlling (kill) so an active
        unresolved bankruptcy never leaks through as live."""
        events = result.events or []
        bk_dates = [
            (e.get("filing_date") or "")
            for e in events
            if any(m in (e.get("description") or "").lower() for m in BANKRUPTCY_EVENT_MARKERS)
        ]
        if not bk_dates:
            return True  # no anchorable bankruptcy event → conservative kill
        latest_bk = max(bk_dates)
        for e in events:
            desc = (e.get("description") or "").lower()
            if any(m in desc for m in BANKRUPTCY_RESOLUTION_MARKERS):
                return False  # explicit resolution anywhere → resolved
            d = e.get("filing_date") or ""
            if d > latest_bk and any(m in desc for m in BANKRUPTCY_SALE_PROGRESS_MARKERS):
                return False  # the sale moved forward after the bankruptcy → resolved
        return True  # bankruptcy is the latest status, silence after → controlling

    def classify(self, result: DocketResult, final_sale_price: float) -> tuple:
        """
        Determine classification (green/yellow/red/killed) and reason.

        Kill conditions:
          - any kill signal present
          - owner already filed
          - real surplus is negative or near zero

        Red conditions (low opportunity):
          - competing filer detected (someone else already filed motion)
          - many additional defendants (3+)

        Yellow conditions (monitor):
          - no proof of surplus filed yet (too early)
          - sale confirmed but docs still pending

        Green conditions (high opportunity):
          - real surplus exists AND
          - no kill signals AND
          - no competing filers AND
          - no owner claim filed AND
          - proof of surplus is filed (certificate of disbursement or notice of surplus)
        """
        # KILLED checks — but a RESOLVED bankruptcy does not disqualify. If the
        # only kill signal is a bankruptcy that was lifted/dismissed or that the
        # sale completed past, drop it from the kill decision (Miami-Dade parity).
        effective_kills = list(result.kill_signals)
        if "bankruptcy" in effective_kills and not self._bankruptcy_is_controlling(result):
            effective_kills = [k for k in effective_kills if k != "bankruptcy"]
        if effective_kills:
            return ("killed", f"kill signal: {effective_kills[0]}")
        if result.owner_filed_claim:
            return ("killed", "owner already filed surplus claim")

        # Real surplus check (Ohio: needs prayer_amount; Florida: prayer_amount unused since opening_bid=debt)
        true_surplus = final_sale_price - result.prayer_amount if result.prayer_amount > 0 else None
        if true_surplus is not None and true_surplus < 10_000:
            return ("killed", f"true surplus only ${true_surplus:,.0f} after debt verification")

        # RED conditions
        if result.competing_filers:
            return ("red", f"competing filer: {result.competing_filers[0]}")
        if len(result.additional_parties) >= 3:
            return ("red", f"{len(result.additional_parties)} additional defendants (creditors)")

        # UNKNOWN — scrape didn't actually populate anything
        if not result.case_title and not result.events and result.prayer_amount == 0:
            return ("unknown", "scrape produced no data — check diagnostics")

        # UNKNOWN — scrape didn't actually populate anything
        if not result.case_title and not result.events and result.prayer_amount == 0:
            return ("unknown", "scrape produced no data — check diagnostics")

        # YELLOW — no proof yet (sale too recent)
        if not result.proof_of_surplus:
            return ("yellow", "sale confirmed but proof of surplus not yet filed")

        # GREEN — everything clean
        return ("green", "proof of surplus filed, no competing claims, clean parties")
