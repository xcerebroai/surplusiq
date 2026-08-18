"""
Hamilton (OH) docket classifier — kill-signal detection, temporal, NO debt.

Hamilton's clerk portal (courtclerk.org, case_summary.php?sec=history) exposes a
full docket-event list but NO judgment amount and gated documents (see
knowledge/blocked_counties.md + the Phase-1B recon). So this NEVER produces a
debt figure — leads stay apparent_surplus, debt_source empty. Its job is to catch
DEAD leads using Hamilton's OWN docket vocabulary, ground-truthed on 6 real
dockets (2026-08-17, data/samples/hamilton/ci/). Nothing is ported from another
county — in particular the word "SURPLUS" never appears in Hamilton dockets;
Hamilton says "EXCESS FUNDS".

THE "COMPLETED SALE" ANCHOR (the whole point): a Hamilton sale is "completed"
when the docket carries a CONFIRMATION event ("CONFIRMATION ENTRY OF SALE AND
DISTRIBUTION OF PROCEEDS" / "FINAL COSTS FOR DECREE OF CONFIRMATION"). A vacate,
dismissal, or bankruptcy stay only kills when it is the LATEST CONTROLLING status
— i.e. NO confirmation post-dates it (and it was not itself withdrawn/terminated).
The auction feed's sale date is unreliable for this (Leath's feed said "sold" but
the docket shows an active stay and no confirmation — the sale never stuck), so we
anchor on the docket confirmation, exactly matching the client's "no subsequent
completed sale" language. Proven on the fixtures:
  • A2201245 McGraw : excess-funds claim → KILLED (the withdrawn vacate and the
      vacated/terminated bankruptcy stay do NOT contribute).
  • A2104251 Dalessandro: Rule 41(A)(2) dismissal AFTER confirmation → KILLED.
  • A2202173 Leath : bankruptcy stay, no termination, no confirmation → KILLED.
  • A2201677 Pitts : vacated sale + bankruptcy stays all superseded by the later
      confirmation → NOT killed (confirmed, no excess-funds line yet).
  • A2500210 Armstrong / A2500255 Hatcher: confirmed, no kill.

ABSENCE IS NOT A SIGNAL: excess-funds events appear MONTHS after confirmation, so
a confirmed case with no excess-funds line is NOT clean-verified — it stays
docket_checked / apparent_surplus, never elevated. The classifier returns
'reviewed_no_kill', not 'green'.
"""
from __future__ import annotations
import re
from datetime import date, datetime
from typing import Optional


# ── event matchers (operate on an UPPERCASE, whitespace-collapsed description) ──

def _is_excess_funds(d: str) -> bool:
    # Hamilton's surplus vocabulary. All observed excess-funds lines are claim/
    # notification/distribution activity — every one signals the surplus is being
    # processed/pursued. "SURPLUS" never appears in Hamilton dockets.
    return "EXCESS FUNDS" in d

def _is_confirmation(d: str) -> bool:
    # The "completed sale" anchor. NOT "MOTION TO CONFIRM …" (a motion, not the entry).
    if "MOTION TO CONFIRM" in d:
        return False
    return "CONFIRMATION ENTRY OF SALE" in d or "DECREE OF CONFIRMATION" in d

def _is_dismissal(d: str) -> bool:
    # A Rule 41(A)(2) / plaintiff dismissal or a vacated judgment = foreclosure
    # undone. Exclude an HOA/cross-claim "dismissal date" or a cross-claim dismissal.
    if "CROSS CLAIM" in d or "CROSS-CLAIM" in d:
        return False
    if "RULE 41" in d:
        return True
    if "DISMISSING PLAINTIFF" in d:            # "ORDER DISMISSING PLAINTIFF'S CLAIMS …"
        return True
    if "VACATE JUDGMENT" in d and "DISMISS" in d:
        return True
    return False

def _is_vacate_sale(d: str) -> bool:
    # A sale/confirmation vacate. NOT a WITHDRAWAL of such a motion, NOT a judgment
    # vacate (dismissal) or a bankruptcy-stay vacate.
    if "WITHDRAWAL" in d:
        return False
    if "VACATE JUDGMENT" in d or "VACAT" in d and "STAY" in d or "BANKRUPTCY STAY" in d:
        return False
    return bool(re.search(r"VACAT\w*\s+(SHERIFF'?S\s+SALE|SALE|CONFIRMATION)", d))

def _is_vacate_withdrawal(d: str) -> bool:
    return "WITHDRAWAL" in d and "MOTION TO VACATE" in d

def _is_bankruptcy_stay(d: str) -> bool:
    return "SUGGESTION OF BANKRUPTCY" in d or "NOTIFICATION OF AUTOMATIC STAY" in d

def _is_bankruptcy_resolution(d: str) -> bool:
    return ("TERMINATION OF AUTOMATIC STAY" in d
            or "ORDER VACATING STAY" in d
            or "VACATE BANKRUPTCY STAY" in d
            or "VACATING STAY" in d)


def _to_date(s) -> Optional[date]:
    if isinstance(s, date):
        return s
    if not s:
        return None
    s = str(s).strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s) or re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if not m:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(m.group(0), fmt).date()
        except ValueError:
            continue
    return None


def normalize_events(rows: list) -> list:
    """Rows (dicts with date + desc/description) → [(date, UPPER description)],
    dropping rows without a parseable date. Network-free."""
    out = []
    for r in rows or []:
        dt = _to_date(r.get("date", ""))
        desc = re.sub(r"\s+", " ", (r.get("desc") or r.get("description") or "")).strip().upper()
        if dt and desc:
            out.append((dt, desc))
    return out


# ── owner from the "PLAINTIFF vs. DEFENDANT" caption ──
# The caption names the lender (plaintiff) vs. the homeowner (defendant). Junior
# creditors (Portfolio Recovery, TD Bank, LVNV, Midland) appear only as served
# co-defendants in the docket body, never as the caption defendant — so taking the
# caption defendant inherently excludes them. The guard below is belt-and-suspenders
# for the rare institutional caption-defendant.
_OWNER_EXCLUDE = re.compile(
    r"\b(bank|mortgage|servicing|financial|funding|savings|credit union|"
    r"national association|n\.?a\.?|homeowners?|home\s*owners?|association|assn|"
    r"condominium|condo|\bhoa\b|portfolio recovery|lvnv|midland credit|td bank|"
    r"llc|l\.?l\.?c|\blp\b|l\.?p\.?|trust|holdings?|capital|\bfund\b|"
    r"clunk|reisenfeld|robertson anschutz|\bwwr\b|selene|newrez|pennymac|"
    r"fifth third|us bank|u\.?s\.? bank)\b", re.I)
_OWNER_GENERIC = re.compile(
    r"unknown|any and all|et al|john doe|jane doe|\bdoe\b|tenant|occupant|"
    r"parties|heirs?|devisees|in possession", re.I)

# IN REM captions name the property/estate, so the caption "defendant" is an
# unknown-spouse/heirs wrapper around the real owner, e.g. "UNKNOWN SPOUSE IF ANY
# OF DAVID PETTIGREW" / "UNKNOWN HEIRS … OF ROBERT JOHNSON". Pull the individual
# named after the final "OF". Narrow by construction: only fires on the
# spouse/heirs/devisees/next-of-kin/estate form, and the extracted name still runs
# the lender/firm/HOA/junior-creditor exclusions — so an embedded company is dropped.
_INREM_OWNER = re.compile(
    r"\b(?:SPOUSE|HEIRS?|DEVISEES?|LEGATEES?|NEXT\s+OF\s+KIN|ESTATE)\b.*?\bOF\s+"
    r"([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]*){1,4})\s*$", re.I)  # continuation allows single-letter middle initials


def owner_from_caption(caption: str) -> str:
    """The defendant homeowner from a 'PLAINTIFF vs. DEFENDANT' caption, or ''
    when it is institutional/generic. Never returns the plaintiff lender. For an
    IN REM 'unknown spouse/heirs of <NAME>' defendant, returns the embedded
    individual <NAME> — never guesses when no individual is named."""
    if not caption:
        return ""
    parts = re.split(r"\s+vs\.?\s+", caption, flags=re.I)
    if len(parts) < 2:
        return ""
    d = parts[-1].strip().lstrip(".").strip()
    d = re.sub(r",?\s+et\s+al.*$", "", d, flags=re.I).strip().strip(",").strip()
    if not d or len(d) < 3:
        return ""
    # Direct individual defendant.
    if not _OWNER_EXCLUDE.search(d) and not _OWNER_GENERIC.search(d):
        return d
    # IN REM wrapper: extract the named individual, then re-apply the guards so an
    # embedded company (e.g. "spouse of Portfolio Recovery Associates LLC") is dropped.
    m = _INREM_OWNER.search(d)
    if m:
        name = m.group(1).strip().strip(",").strip()
        if name and len(name) >= 3 and not _OWNER_EXCLUDE.search(name) and not _OWNER_GENERIC.search(name):
            return name
    return ""


def classify_hamilton(rows: list, caption: str = "", sale_date=None) -> dict:
    """Classify a Hamilton lead from its docket rows + auction sale date.

    Returns:
      classification    : 'killed' | 'reviewed_no_kill'
      classification_reason
      kill_signals      : list[str]
      competing_filers  : list[str]
      evidence_level    : 'not_pursuable' | 'docket_checked'
      owner_name        : str
      confirmed         : bool   (a confirmation event is present)
    NEVER returns a debt/prayer figure — Hamilton exposes none.
    """
    events = normalize_events(rows)
    owner = owner_from_caption(caption)

    def latest(pred):
        ds = [dt for dt, d in events if pred(d)]
        return max(ds) if ds else None

    conf = latest(_is_confirmation)                 # the "completed sale" anchor

    def superseded(kill_dt) -> bool:
        return conf is not None and kill_dt is not None and conf > kill_dt

    def killed(sig, reason, competing=None):
        return {"classification": "killed", "classification_reason": reason,
                "kill_signals": [sig], "competing_filers": competing or [],
                "evidence_level": "not_pursuable", "owner_name": owner,
                "confirmed": conf is not None}

    # 1 — EXCESS-FUNDS CLAIM → kill (surplus being processed/pursued). Top priority.
    ex = latest(_is_excess_funds)
    if ex is not None:
        return killed("excess_funds_claim",
                      f"excess funds being distributed/claimed ({ex}) — surplus in process",
                      competing=["excess_funds_claimant"])

    # 2 — DISMISSAL (Rule 41(A)(2) / vacated judgment), unless a later confirmation.
    dis = latest(_is_dismissal)
    if dis is not None and not superseded(dis):
        return killed("case_dismissed",
                      f"foreclosure dismissed / judgment vacated ({dis}) — no completed sale stands")

    # 3 — VACATE sale/confirmation, unless withdrawn or superseded by later confirmation.
    vac = latest(_is_vacate_sale)
    if vac is not None:
        wdl = latest(_is_vacate_withdrawal)
        withdrawn = wdl is not None and wdl >= vac
        if not withdrawn and not superseded(vac):
            return killed("sale_vacated",
                          f"sale/confirmation vacated ({vac}) and not withdrawn or re-confirmed")

    # 4 — BANKRUPTCY stay, unless terminated or superseded by a later confirmation.
    bk = latest(_is_bankruptcy_stay)
    if bk is not None:
        term = latest(_is_bankruptcy_resolution)
        terminated = term is not None and term >= bk
        if not terminated and not superseded(bk):
            return killed("bankruptcy_stay",
                          f"bankruptcy stay is the latest controlling status ({bk}) — "
                          f"no termination and no confirmation after it; sale won't stick")

    # 5 — No kill. NOT elevated: a missing excess-funds line = not-yet-processed,
    #     never clean-verified. Stays docket_checked / apparent_surplus.
    reason = ("docket checked — no kill signals" +
              (" (sale confirmed; excess-funds not yet processed — not clean-verified)"
               if conf is not None else " (sale not yet confirmed in docket)") +
              ". No debt figure available for Hamilton.")
    return {"classification": "reviewed_no_kill", "classification_reason": reason,
            "kill_signals": [], "competing_filers": [],
            "evidence_level": "docket_checked", "owner_name": owner,
            "confirmed": conf is not None}
