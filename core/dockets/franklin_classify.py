"""
Franklin (OH) docket classifier — kill-signal detection, temporal, no debt.

Franklin exposes docket metadata but NO judgment amount (see
knowledge/blocked_counties.md), so this NEVER produces a debt figure. Its only
job is to catch DEAD leads the auction "Sold" status hides, using Franklin's OWN
docket vocabulary (ground-truthed on 13 real dockets, 2026-08-11 —
data/samples/franklin/ci/). Nothing is ported from another county.

TEMPORAL RULE (the whole point): the AUCTION SALE DATE is "the completed sale."
Only docket events dated AFTER the sale date can kill it — an earlier
vacate/withdraw/dismiss was superseded by the completed sale (a prior aborted
sale in a re-sale sequence) and is noise. Proven on real fixtures:
  • 25CV7609: sold 07-17, WITHDRAWING PROPERTY 07-22 (post-sale) → KILLED.
  • 25CV6087: ORDER TO VACATE 06-22, sold 08-07 (vacate pre-sale) → NOT killed.

Kill vocabulary (Franklin's exact terms — the words SURPLUS/REDEEM/SATISFACTION/
SOLD TO never appear in Franklin dockets, so they are NOT coded):
  • BANKRUPTCY STAY - CASE INACTIVATED
  • MOTION/ORDER TO VACATE, MOTION VACATE ORDER OF SALE
  • ENTRY WITHDRAWING PROPERTY FROM SHERIFF SALE
  • DISMISSAL BY PLAINTIFF, MOTION TO DISMISS (NOT "DISMISS CROSS CLAIM")
  • ORDER ... EXCESS FUNDS  → surplus adjudicated/distributed (Franklin's word
        for surplus is "excess funds"). MOTION/APPLICATION ... EXCESS FUNDS →
        also a kill, cited as a competing claimant.
Positive/neutral (never a kill): DECREE OF FORECLOSURE, JUDGMENT ENTRY,
NOTICE OF SHERIFF'S SALE, ORDER OF SALE, CONFIRMATION OF SALE.
"""
from __future__ import annotations
import re
from datetime import date
from typing import Optional


def parse_docket_events(text: str) -> list[tuple[date, str]]:
    """Extract (date, description) pairs from Franklin CIO docket text.

    Docket entries are flattened as 'MM/DD/YY DESCRIPTION <microfilm ref>'.
    We split on the date tokens and strip the trailing microfilm reference
    (e.g. '0H182 C74 9') and any fee/columnar tail.
    """
    norm = re.sub(r"\s+", " ", text or "")
    dsec = norm[norm.find("Docket Information"):] if "Docket Information" in norm else norm
    parts = re.split(r"(\b\d{2}/\d{2}/\d{2}\b)", dsec)
    events: list[tuple[date, str]] = []
    for i in range(1, len(parts) - 1, 2):
        token, tail = parts[i], parts[i + 1]
        # description = up to the microfilm ref (0X999 X99 9) or a big gap
        desc = re.sub(r"\s+0[A-Z]\d{3}\s+[A-Z]\d{2}\s+\d.*$", "", tail.strip())
        desc = re.sub(r"\s{2,}.*$", "", desc).strip().upper()[:80]
        if not desc:
            continue
        try:
            mm, dd, yy = token.split("/")
            dt = date(2000 + int(yy), int(mm), int(dd))
        except ValueError:
            continue
        events.append((dt, desc))
    return events


# ── kill-event matchers on an UPPERCASE description ──
def _is_vacate(d: str) -> bool:
    return "TO VACATE" in d or "VACATE ORDER OF SALE" in d

def _is_withdraw(d: str) -> bool:
    return "WITHDRAWING PROPERTY FROM SHERIFF SALE" in d

def _is_dismiss_case(d: str) -> bool:
    if "DISMISS CROSS CLAIM" in d:          # a sub-party dismissal, not the case
        return False
    return "DISMISSAL BY PLAINTIFF" in d or "MOTION TO DISMISS" in d

def _is_excess(d: str) -> bool:
    return "EXCESS FUNDS" in d

def _is_bankruptcy(d: str) -> bool:
    return "BANKRUPTCY STAY" in d and "INACTIVATED" in d


def _to_date(s) -> Optional[date]:
    if isinstance(s, date):
        return s
    if not s:
        return None
    s = str(s)[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            from datetime import datetime
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def classify_franklin(events: list[tuple[date, str]], sale_date) -> dict:
    """Classify a Franklin lead from its docket events + the auction sale date.

    Returns a dict:
      classification    : 'killed' | 'reviewed_no_kill'
      classification_reason
      kill_signals      : list[str]
      competing_filers  : list[str]
      evidence_level    : 'docket_checked' | 'not_pursuable'
    NEVER returns a debt/prayer figure — Franklin has none.
    """
    sd = _to_date(sale_date)
    # Only events AFTER the completed sale can kill it (prior events superseded).
    post = [(dt, d) for dt, d in events if sd is None or dt > sd]
    post.sort()

    kill_signals: list[str] = []
    competing: list[str] = []
    reason = ""
    # Evaluate in reason-priority order but honor "post-sale only".
    for dt, d in post:
        if _is_excess(d):
            if "ORDER" in d and "MOTION" not in d and "APPLICATION" not in d:
                kill_signals.append("excess_funds_distributed")
                reason = f"excess funds distributed by court order ({dt}) — surplus adjudicated/paid out"
            else:
                kill_signals.append("excess_funds_claim")
                competing.append("excess_funds_claimant")
                reason = f"competing claimant filed for excess funds ({dt}) — surplus being claimed"
        elif _is_withdraw(d):
            kill_signals.append("withdrawn_from_sale")
            reason = f"property withdrawn from sheriff sale after the sale ({dt}) — sale undone"
        elif _is_vacate(d):
            kill_signals.append("sale_vacated")
            reason = f"sale vacated/set aside after the sale ({dt})"
        elif _is_dismiss_case(d):
            kill_signals.append("case_dismissed")
            reason = f"case dismissed after the sale ({dt})"
        elif _is_bankruptcy(d):
            kill_signals.append("bankruptcy_stay")
            reason = f"bankruptcy stay inactivated the case after the sale ({dt}) — sale won't stick"

    if kill_signals:
        # De-dup, keep the last (latest) event's reason.
        return {
            "classification": "killed",
            "classification_reason": reason,
            "kill_signals": sorted(set(kill_signals)),
            "competing_filers": sorted(set(competing)),
            "evidence_level": "not_pursuable",
        }
    return {
        "classification": "reviewed_no_kill",
        "classification_reason": "docket checked — no post-sale kill signals "
                                 "(no debt figure available for Franklin)",
        "kill_signals": [],
        "competing_filers": [],
        "evidence_level": "docket_checked",
    }
