"""
SurplusIQ — Broward County Docket Scraper

Built 2026-06-10 from the investigation ground-truth in
`data/samples/broward/SURPLUS_VOCAB_FINDINGS.md` (Actions run 27312374787,
22 real cases, 9 with live surplus-claim activity). Investigation tooling
(scripts/broward_investigate.py + the Broward Investigate workflow) is REMOVED
as part of this commit — this file is the production replacement.

ACCESS PATH — REBUILT 2026-08-13 (LOCAL-RUN, residential IP only).

Around 2026-08-08 the clerk deleted the old public GET endpoint
(CaseNumberSearchResultsPUBLIC now hard-redirects to an ASP.NET 404) and put the
search step behind Cloudflare Turnstile. The token auto-issues with zero
interaction in a real headed browser on a RESIDENTIAL IP ("Success!"), but a
Phase-1 CI probe proved it does NOT issue from the GitHub Actions datacenter IP
(headless OR headed, warm profile, webdriver masked). So Broward is now a
LOCAL-RUN county (LOCAL_RUN_COUNTIES) on the autonomous Franklin pattern — the
cloud cron skips it; `python -m core.dockets.broward` runs it locally.

Only the SEARCH step changed. Detail extraction + the whole classifier below are
verbatim from the pre-Aug-8 build (live-reconfirmed 2026-08-13).

  1. GET  https://www.browardclerk.org/                             (warm session)
  2. GET  /Web2/CaseSearchECA/Index/?AccessLevel=ANONYMOUS          (form + auto-token)
  3. activate the Case Number tab, fill #CaseNumber, wait for the caseSearchForm's
     cf-turnstile-response to auto-populate (no human action)
  4. POST /Web2/CaseSearchECA/CaseNumberSearchResults  (submit caseSearchForm:
     __RequestVerificationToken, CaseNumber, cf-turnstile-response, AccessLevel)
  5. → GET /Web2/CaseSearchECA/Results?TYPE=GetCaseSearchByCase_ECA&INPUT=<blob>
  6. CLICK the case row → /Web2/CaseSearchECA/GetCaseDetail?Viewer=<blob>  (UNCHANGED)
  7. Docket events table headers: Date | Description | Additional Text | View/Pages

DETECTION — THE KEY FINDING: the kill terms are NOT in the Description column.
`Description` holds GENERIC clerk labels (Motion for Disbursement, Motion to
Intervene, Order Disbursing Funds, Order Granting, Notice of Appearance). ALL
the surplus-specific text lives in `Additional Text`. We therefore scan the
COMBINED Description + Additional Text of every row, not Description alone.
A Description-only matcher misses every one of the 9 real claims.

See SURPLUS_VOCAB_FINDINGS.md for the per-string evidence cited inline below.
"""

from __future__ import annotations
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import threading
import time
import html as _html
from datetime import datetime
from pathlib import Path
from typing import Optional

from .base import DocketScraper, DocketResult, DocketEvent


BASE_URL = "https://www.browardclerk.org/Web2/CaseSearchECA"
LANDING_URL = f"{BASE_URL}/Index/?AccessLevel=ANONYMOUS"
HOMEPAGE_URL = "https://www.browardclerk.org/"
REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Persistent browser profile — Cloudflare clearance cookies survive between runs,
# and a warm profile with history reads as far less suspicious than a cold one.
PROFILE_DIR = PROJECT_ROOT / "data" / "browser_profiles" / "broward-fl"

# Extension-bridge architecture. Turnstile issues its token ONLY to a genuine
# browser and suppresses its widget under ANY Playwright/CDP automation (proven
# 2026-08-13/14: window.turnstile loads but the challenge never renders — the CDP
# Runtime.enable tell, present whether Playwright launches or attaches). So the
# runner does NOT drive Chrome over CDP. It launches genuine Chrome against a
# dedicated profile that has the unpacked bridge extension loaded (a one-time
# manual "Load unpacked" — Chrome 151 removed CLI --load-extension), serves a tiny
# localhost queue, and the extension's content script does the whole search flow
# and posts the docket back. The browser is unmodified genuine Chrome — no tell.
BRIDGE_PORT = int(os.environ.get("BROWARD_BRIDGE_PORT", "8799"))
EXTENSION_DIR = Path(__file__).resolve().parent / "broward_extension"
_MAX_ATTEMPTS = 2                       # per-case retries before leaving it unverified

_CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


def _find_chrome() -> str:
    """Locate a genuine Chrome/Chromium executable. CHROME_PATH overrides."""
    for c in _CHROME_CANDIDATES:
        if c and os.path.exists(c):
            return c
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "Google Chrome not found. Install it or set CHROME_PATH to the binary "
        "(Broward is local-run and requires a genuine Chrome for Turnstile).")


def _launch_chrome(url: str) -> None:
    """Open `url` in genuine Chrome on the dedicated broward-fl profile (which has
    the bridge extension loaded). If a Chrome is already running on that profile,
    this opens a tab in it; either way the content script starts. Shutdown is by
    profile path (_kill_chrome), so we don't rely on this process handle."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [_find_chrome(),
         f"--user-data-dir={PROFILE_DIR}",
         "--no-first-run", "--no-default-browser-check",
         "--window-size=1400,900", "--lang=en-US",
         url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _kill_chrome() -> None:
    """Terminate the Chrome instance bound to the dedicated broward-fl profile.
    The unique profile path never matches the user's normal Chrome."""
    subprocess.run(["pkill", "-f", f"user-data-dir={PROFILE_DIR}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _chrome_running_on_profile() -> bool:
    return subprocess.run(
        ["pgrep", "-f", f"user-data-dir={PROFILE_DIR}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


FORECLOSURE_MORTGAGE = "mortgage_foreclosure"


# ─────────────────────────────────────────────────────────────────────────────
# SURPLUS-CLAIM detection — match against COMBINED Description + Additional Text,
# case-insensitive, OCR-typo-tolerant. Every hit REQUIRES the "surplus" anchor so
# routine rows ("Certificate of Disbursements", "Value Claim Form", default
# motions naming "claimants") never false-fire. Real OCR corruption seen in the
# dockets — "ASS␣GNEE", "INTERVEN", "D␣SBURSE" — is matched on the distinctive
# stem, not the exact spelling.
#
# A row is a surplus claim when it has "surplus" AND a disbursement/claim verb.
# Verbs (stems, OCR tolerant):
SURPLUS_VERB_PATTERNS = [
    r"d\s?i?\s?sburs",      # disburse / disbursement / "D SBURSE" (CACE-25-002358 "MOTION TO DISBURSE SURPLUS FUNDS")
    r"claim",               # "CLAIM TO SURPLUS FUNDS" (CACE-24-008631); "Claim For The Surplus Retained By Clerk" (CACE-25-004451)
    r"proceed",             # "SURPLUS PROCEEDS" (CACE-24-012541; CONO-25-048381)
    r"interven",            # "MOTION TO INTERVENE AND TO DISBURSE SURPLUS FUNDS" (CACE-25-005168 / CACE-25-001564)
]
# Strong standalone phrases (surplus + a noun that only appears in claim filings):
SURPLUS_PHRASE_PATTERNS = [
    r"surplus\s+funds?",        # CACE-25-001564 "DISBURSEMENT OF SURPLUS FUNDS"
    r"surplus\s+proceeds",      # CACE-24-012541 "SURPLUS PROCEEDS"
    r"surplus\s+retained",      # CACE-25-004451 "Surplus Retained By Clerk"
    r"remaining\s+surplus",     # CACE-23-015282 "ORDER DISBURSING THE REMAINING SURPLUS FUNDS"
]

# ⚠️ SALE-ISSUE / BANKRUPTCY DETECTION DELIBERATELY NOT IMPLEMENTED HERE.
# Eric Rule 2 (sale vacated = hard kill) and Rule 1 (bankruptcy = flag) sound
# simple but are LOSSY on Broward's real dockets: a foreclosure sale routinely
# gets cancelled / postponed / "entered in error" / stayed for an earlier
# bankruptcy, then re-noticed and SOLD. Naive "Foreclosure Sale CANCELED" /
# "Motion to Cancel Sale" / "Motion to Vacate" matching false-killed 4 of 22 real
# sold-with-surplus cases (CACE-19-010632, CACE-21-019437, CACE-23-019364,
# CACE-24-007807) — every one has a Certificate of Sale + Certificate of Title
# dated AFTER the cancellations, and CACE-24-007807's last cancel motion was
# DENIED. We only ever scrape cases the auction step already marked SOLD, so the
# sale completed. A genuine post-title vacate kill needs date-ordered logic
# (vacate AFTER the Certificate of Title) — a separate scoped task, NOT this one.

# ⚠️ DELIBERATELY NOT A KILL TERM: "Certificate of Disbursement(s)".
# Eric's spec lists it as a surplus signal, but the ground-truth (22 cases) shows
# it on ~EVERY sold case — it's the clerk's routine record of paying sale proceeds
# to lienholders per the final judgment, NOT proof the SURPLUS was claimed. Killing
# on it would kill every lead. Flagged here as a documented contradiction with
# Eric's written spec; confirm with him before ever promoting it to a signal.
_ADMIN_NOISE_NOTE = "certificate of disbursements = routine admin, not a surplus claim"

# ─────────────────────────────────────────────────────────────────────────────
# NOTICE-OF-APPEARANCE classifier. Description is always the generic "Notice of
# Appearance"; the distinguishing data is entirely in Additional Text. Three-stage
# filter proven on the real data (SURPLUS_VOCAB_FINDINGS.md "hard classifier"):
#   Stage 1  benign-party  — carries a "Party: Plaintiff|Defendant" token
#   Stage 2  benign-counsel — ESQ / "as counsel for" / co-counsel / email designation
#   Stage 3  residual       — surplus-keyword / known-firm → KILL; else → CAUTION,
#                             with PARTY-NAME, purchaser/non-party, and HOA exclusions
# Stages 1+2 are gated by the KNOWN-FIRM guard ONLY (a ground-truthed recovery-firm
# NAME), NOT by generic keywords: a row that ALSO names a known recovery firm is NOT
# benigned (e.g. CONO-25-048381 "HOME DEFENSE ... EVO RECOVERY ... Party: Defendant
# Zoubaa" carries a Party token but names a known surplus firm). A generic keyword
# alone ("funding", "consulting", "equity group") must NOT override a structured
# party/counsel token — that was the GHIDOTTI-class false kill (a "Party: Plaintiff
# XYZ Funding LLC" is the case's own lender). Generic keywords act ONLY in Stage 3.

# Generic keywords that self-identify a surplus-recovery / funding operation:
RECOVERY_KEYWORDS = [
    "surplus",            # PRIORITY SURPLUS LLC (COCE-25-085528)
    "funding",            # GET LIQUID FUNDING, LLC (CACE-24-012541, CACE-25-005168, COWE-25-085495, COWE-26-005819)
    "recovery",           # The Recovery Agents, LLC (CACE-24-007420); EVO RECOVERY CONSULTATION (CONO-25-048381)
    # NOTE: bare "assignee" is intentionally NOT a keyword. It is ambiguous — a
    # PLAINTIFF can be a loan assignee, so "Attorneys for the Plaintiffs Assignee"
    # (GHIDOTTI/BERGER LLP, CACE-13-021361) is benign plaintiff counsel, not a
    # surplus firm. The live CI run false-killed that $327K lead on it. The real
    # owner-assignment recovery cases are caught by KNOWN_RECOVERY_FIRMS (New
    # Beginnings Trustee, Capital Crafter) and the surplus-claim motion path
    # ("Assignee's Motion to Disburse Surplus" → surplus+disburse). An unknown
    # "X as Assignee for <owner>" NOA falls to residual CAUTION, which is correct.
    "processing services",  # PRESTIGE PROCESSING SERVICES, LLC (CACE-23-015282)
    "consultation",       # EVO RECOVERY CONSULTATION CORP
    "consulting",
    "equity group",       # AMERIFUND EQUITY GROUP (CACE-24-008631)
]
# Specific firm names ground-truthed in the 9 claim cases — KILL even when the
# name carries no generic keyword (Eric's "known company list", seeded). Extend as
# Eric supplies more names.
KNOWN_RECOVERY_FIRMS = [
    "get liquid funding", "priority surplus", "the recovery agents", "amerifund",
    "new beginnings trustee", "new beginnings", "prestige processing",
    "home defense", "evo recovery", "capital crafter",
]
# Org keywords that mark a benign HOA / condo / church party (not a surplus firm):
HOA_ORG_KEYWORDS = [
    "homeowners association", "homeowner's association", "condominium", "condo association",
    "village", "ministries", "church", "property owners association", " hoa",
]
# Purchaser / auction-winner markers — the highest bidder is not a surplus claimant:
PURCHASER_MARKERS = ["non-party", "non party", "purchaser", "highest bidder", "successful bidder"]

# NOA verdicts
NOA_BENIGN = "benign"
NOA_RECOVERY_KILL = "recovery_firm"
NOA_RESIDUAL_CAUTION = "residual_caution"


def parse_broward_case_number(raw: str) -> Optional[dict]:
    """Parse a Broward case number → search components.

    Broward civil case format: 4-letter division prefix + 2-digit year + 6-digit
    sequence, e.g. CACE-25-003189, COCE-25-085528, COWE-26-005819, CONO-25-048381.
    The public search takes the de-dashed token (CaseNum=CACE25003189).

    Returns {prefix, year, number, nodash, dashed} or None.
    """
    if not raw:
        return None
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", raw.strip())          # strip " (12345)" auction suffix
    compact = re.sub(r"[^A-Za-z0-9]", "", cleaned).upper()
    m = re.match(r"^([A-Z]{4})(\d{2})(\d{6})$", compact)
    if not m:
        return None
    return {
        "prefix": m.group(1),
        "year":   m.group(2),
        "number": m.group(3),
        "nodash": compact,
        "dashed": f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
    }


# ── pure helpers (network-free → unit-testable against committed docket JSON) ──

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()


def _name_tokens(name: str) -> frozenset:
    """Alpha tokens (len≥2) of a name, minus org-suffix noise. Used for the
    party-name exclusion so 'RANDOLPH MERRITT' matches 'Merritt, Randolph'."""
    toks = re.findall(r"[A-Za-z]{2,}", (name or "").lower())
    stop = {"llc", "inc", "corp", "co", "the", "and", "of", "as", "for", "party",
            "plaintiff", "defendant", "esq", "trust", "na", "association"}
    return frozenset(t for t in toks if t not in stop)


def collect_party_and_purchaser_names(rows: list) -> tuple:
    """Walk every row's Additional Text and collect:
      - party_names:     names following a 'Party: Plaintiff|Defendant <name>' or a
                         leading 'Defendant|Plaintiff <name>' token (the case's own
                         parties — used to exclude a pro-se homeowner appearance).
      - purchaser_names: names following a 'Non-Party <name>' token.
    Returns (set[frozenset_tokens], set[frozenset_tokens])."""
    party, purchaser = set(), set()
    for r in rows:
        ad = _norm(r.get("additional", ""))
        for m in re.finditer(r"party:\s*(?:plaintiff|defendant)\s+([^,]+(?:,\s*[A-Za-z.]+)?)", ad, re.I):
            toks = _name_tokens(m.group(1))
            if toks:
                party.add(toks)
        # leading "Defendant <name>" / "Plaintiff <name>" without the "Party:" prefix
        m2 = re.match(r"(?:defendant|plaintiff)\s+([A-Z][^,]{2,40})", ad, re.I)
        if m2:
            toks = _name_tokens(m2.group(1))
            if toks:
                party.add(toks)
        for m in re.finditer(r"non-?party\s+([A-Za-z0-9 .,&'-]{3,40})", ad, re.I):
            toks = _name_tokens(m.group(1))
            if toks:
                purchaser.add(toks)
    return party, purchaser


# ── Owner (defendant-homeowner) extraction ──────────────────────────────────
# Broward stores parties as lowercase token sets (for matching); for the OWNER we
# need the PROPER-CASED defendant name string. The docket text lists individuals
# as "Defendant Last, First M" — the comma-after-surname structure cleanly admits
# people and rejects multi-word corporates ("...Ministries Inc", "...Mortgage Llc")
# which lack it. Joint owners concatenate ("Fedele, Michael Defendant Fedele,
# Rae A.") → the pattern stops at the next role token, taking the first.
# The SURNAME may be multiple words ("LEON ALVARADO, PAULINA" — a Hispanic
# two-word surname): `(?:\s+word)*` before the comma admits it. Without this the
# name failed to parse, owner_name went blank, and the loader's fallback put the
# co-defendant HUD ("Secretary of Housing and Urban Development") in the owner
# column (COCE-25-060300). A no-comma corporate ("...Association No.3, Inc") still
# won't match — the digit in "No.3" breaks the pre-comma word run, and the only
# comma sits before a corporate suffix. Trailing middle-initial(s) must be a
# STANDALONE letter (negative lookahead for a following letter) — otherwise
# "Michael Defendant ..." captures a spurious "D" from the next role token.
_BROWARD_DEFENDANT_RE = re.compile(
    r"\bdefendant\s+([A-Za-z][A-Za-z'’.\-]+(?:\s+[A-Za-z][A-Za-z'’.\-]+)*,\s+[A-Za-z][A-Za-z'’.\-]+"
    r"(?:\s+[A-Za-z](?![A-Za-z])\.?){0,2})",
    re.I)
# Government / public-agency parties that co-appear as defendants (HUD, the U.S.,
# a county treasurer) but are NEVER the residential owner of record. Kept separate
# from the corporate marker because these carry no LLC/Inc suffix.
_OWNER_GOVT_MARKER = re.compile(
    r"\b(secretary|housing and urban|urban development|united states|"
    r"treasurer|internal revenue|\bhud\b|\birs\b|state of|county of|city of|"
    r"veterans affairs|comptroller|commissioner)\b", re.I)
_OWNER_CORP_MARKER = re.compile(
    r"\b(bank|mortgage|trust|funding|servicing|n\.?\s?a\.?|association|assn|"
    r"condominium|condo|homeowners?|l\.?l\.?c|llc|inc\b|incorporated|corp|company|"
    r"\bco\.|ltd|l\.?p\.?|holdings?|capital|\bfund\b|funds\b|department|united states|"
    r"secretary|housing and urban|urban development|treasurer|veterans affairs|"
    r"ministries|church|realty|properties|\bgroup\b|investments?|enterprises?|"
    r"management|financial|lending|loans?)\b", re.I)
_OWNER_GENERIC = re.compile(
    r"unknow|tenant|any and all|all other|in possession|et al|john doe|jane doe|"
    r"\bdoe\b|parties claiming|lienors|creditors|\bheirs?\b|devisees", re.I)


def collect_defendant_names(rows: list) -> list:
    """Proper-cased 'Last, First' defendant names from the docket, in first-seen
    order, de-duplicated."""
    names, seen = [], set()
    for r in rows:
        ad = _norm(r.get("additional", ""))
        for m in _BROWARD_DEFENDANT_RE.finditer(ad):
            nm = m.group(1).strip().rstrip(",").strip()
            key = nm.lower()
            if key and key not in seen:
                seen.add(key)
                names.append(nm)
    return names


def owner_from_defendants(names: list) -> str:
    """First INDIVIDUAL defendant homeowner — excludes corporate/HOA/generic and
    any surplus-recovery firm. Bias to BLANK over a wrong (e.g. bank) name."""
    for nm in names:
        low = nm.lower()
        if _OWNER_CORP_MARKER.search(nm) or _OWNER_GENERIC.search(nm):
            continue
        if "surplus" in low or any(f in low for f in KNOWN_RECOVERY_FIRMS):
            continue
        return nm
    return ""


def _is_known_firm(text: str) -> bool:
    """Specific, ground-truthed recovery-firm NAMES only (Eric's known-company
    list). Distinct from _is_recovery_firm — it does NOT match generic keywords.
    This is the guard that disables the benign party/counsel stages: only a known
    firm name overrides a structured 'Party:'/counsel token, never a bare keyword."""
    low = text.lower()
    return any(k in low for k in KNOWN_RECOVERY_FIRMS)


def _is_recovery_firm(text: str) -> bool:
    low = text.lower()
    return (any(k in low for k in KNOWN_RECOVERY_FIRMS)
            or any(k in low for k in RECOVERY_KEYWORDS))


def classify_appearance(additional: str, party_names: set, purchaser_names: set) -> tuple:
    """Classify ONE Notice-of-Appearance row's Additional Text.

    Returns (verdict, firm_name) where verdict ∈ {benign, recovery_firm,
    residual_caution}. firm_name is the appearing-entity text for the reason line.
    """
    ad = _norm(additional)
    low = ad.lower()
    is_known = _is_known_firm(ad)
    is_recovery = _is_recovery_firm(ad)

    # Stage 1 — benign party token. Gated on a KNOWN-FIRM match ONLY, NOT generic
    # keywords: a "Party: Plaintiff XYZ Funding LLC" is the case's own lender,
    # benign — the generic "funding"/"consulting"/"equity group" keyword must not
    # override a structured party token (the GHIDOTTI-class false kill). A row
    # naming a ground-truthed recovery firm still falls through to the Stage-3 kill.
    if re.search(r"party:\s*(?:plaintiff|defendant)", low) and not is_known:
        return (NOA_BENIGN, ad)
    # leading "Defendant <name>" / "Plaintiff <name>" (no "Party:" prefix), e.g.
    # "Defendant Asmart Group, LLC" — a named case party, benign.
    if re.match(r"(?:defendant|plaintiff)\s+[A-Z]", ad, re.I) and not is_known:
        return (NOA_BENIGN, ad)

    # Stage 2 — benign counsel. Same known-firm gate. "Attorneys for the
    # Plaintiff(s)/Defendant(s)" (incl. "...for Plaintiff XYZ Equity Group") is case
    # counsel, benign — what the GHIDOTTI/BERGER live false kill needed.
    if not is_known and re.search(
            r"\besq\b|as counsel for|co-?counsel|attorneys? for|"
            r"designation of (?:electronic )?(?:e-?)?mail", low):
        return (NOA_BENIGN, ad)

    # Stage 3 — residual bucket. Apply exclusions BEFORE flagging.
    appear_toks = _name_tokens(ad)
    # (a) the appearance is the case's own party (e.g. pro-se homeowner Randolph
    #     Merritt, who is also "Party: Defendant Merritt, Randolph") → benign.
    if appear_toks and any(len(appear_toks & p) >= 2 for p in party_names):
        return (NOA_BENIGN, ad)
    # (b) purchaser / auction winner / non-party → benign.
    if any(mk in low for mk in PURCHASER_MARKERS):
        return (NOA_BENIGN, ad)
    if appear_toks and any(len(appear_toks & p) >= 2 for p in purchaser_names):
        return (NOA_BENIGN, ad)
    # (c) HOA / condo / church / association → benign.
    if any(k in low for k in HOA_ORG_KEYWORDS):
        return (NOA_BENIGN, ad)

    # Residual firm. Known/keyword recovery firm → KILL. Unknown → CAUTION.
    if is_recovery:
        return (NOA_RECOVERY_KILL, ad)
    return (NOA_RESIDUAL_CAUTION, ad)


def _row_text(r: dict) -> str:
    return _norm(f"{r.get('description','')} {r.get('additional','')}")


def detect_surplus_claim(rows: list) -> str:
    """Return the verbatim Additional/Description text of the first surplus-claim
    row, or '' if none. A row qualifies when it carries the 'surplus' anchor AND a
    disbursement/claim verb (or a strong surplus phrase)."""
    for r in rows:
        blob = _row_text(r)
        low = blob.lower()
        if "surplus" not in low:
            continue
        if any(re.search(p, low) for p in SURPLUS_VERB_PATTERNS) or \
           any(re.search(p, low) for p in SURPLUS_PHRASE_PATTERNS):
            return blob
    return ""


class BrowardDocketScraper(DocketScraper):

    county_id = "broward-fl"
    county_name = "Broward"

    def classify(self, result: DocketResult, final_sale_price: float) -> tuple:
        """No-op override (same rationale as Miami-Dade): scrape_case already ran
        the full Broward evidence model. The base prayer-vs-sale math is wrong for
        FL (opening_bid IS the debt; no prayer field), so we return the
        docket-computed classification unchanged rather than let enrich.run_one
        re-derive it."""
        return (result.classification, result.classification_reason)

    # ── Phase 2: parse extracted docket rows → Eric's review fields ──────────
    # Takes ROWS (not raw HTML) so it is fully network-free and unit-testable
    # against data/samples/broward/ci/<CASE>_docket.json.

    def parse_docket(self, rows: list, result: DocketResult,
                     case_caption: str = "") -> None:
        rows = rows or []
        result.events = [
            DocketEvent(filing_date=r.get("date", ""),
                        description=(_norm(r.get("description", ""))[:200]),
                        document_type=(_norm(r.get("additional", ""))[:200])).__dict__
            for r in rows[:120]
        ]

        party_names, purchaser_names = collect_party_and_purchaser_names(rows)
        # Owner-completeness signal: did we recover at least one case party name?
        result.defendants = [" ".join(sorted(p)) for p in party_names][:20]
        # Owner = proper-cased individual defendant homeowner (NOT the token sets
        # above, NOT plaintiff/corporate/HOA/purchaser/surplus-firm).
        result.owner_name = owner_from_defendants(collect_defendant_names(rows))

        # Sale confirmation (for the "Certificate of Sale found" valid-reason line).
        sale_confirmed = any(
            re.search(r"certificate of (?:sale|title)", _row_text(r), re.I) for r in rows
        )

        # ── Surplus-claim activity (PRIMARY kill path) ──
        claim_evidence = detect_surplus_claim(rows)

        # ── Notice-of-Appearance classifier (SECONDARY kill path) ──
        noa_kill_firm = ""
        noa_caution_firm = ""
        for r in rows:
            if "notice of appearance" not in (r.get("description", "") or "").lower():
                continue
            verdict, firm = classify_appearance(r.get("additional", ""),
                                                party_names, purchaser_names)
            if verdict == NOA_RECOVERY_KILL and not noa_kill_firm:
                noa_kill_firm = firm
            elif verdict == NOA_RESIDUAL_CAUTION and not noa_caution_firm:
                noa_caution_firm = firm

        # Stash for _apply_evidence_level (auditable evidence strings).
        result._evidence = {                       # type: ignore[attr-defined]
            "claim": claim_evidence,
            "noa_kill": noa_kill_firm,
            "noa_caution": noa_caution_firm,
            "sale_confirmed": sale_confirmed,
            "owner_present": bool(party_names),
            "docket_rows": len(rows),
        }

    def _apply_evidence_level(self, result: DocketResult) -> None:
        """Map docket evidence → evidence_level / lead_status / classification +
        plain-language reason (Eric's templates). Precedence, most-disqualifying
        first: surplus claim → recovery NOA → residual unknown NOA(caution) →
        clean (Tier A pursuable / Tier B-C caution).

        Sale-issue / bankruptcy are intentionally NOT decided here — see the note
        at the top of the module (lossy on Broward's cancel-and-resell dockets).
        """
        result.foreclosure_type = FORECLOSURE_MORTGAGE
        ev = getattr(result, "_evidence", {})

        # 1 — surplus-claim activity already filed → KILLED.
        if ev.get("claim"):
            result.claim_filed = True
            result.claim_type = ev["claim"][:200]
            result.kill_signals = ["surplus_claim_filed"]
            result.evidence_level = "claim_filed"
            result.lead_status = "not_pursuable"
            result.classification = "killed"
            result.classification_reason = (
                f"Docket checked. Surplus claim activity found: '{ev['claim'][:160]}'. "
                f"Lead already being pursued."
            )
            return

        # 2 — surplus-recovery firm filed a Notice of Appearance → KILLED.
        if ev.get("noa_kill"):
            result.claim_filed = True
            result.claim_type = f"Notice of Appearance: {ev['noa_kill']}"
            result.kill_signals = ["surplus_firm_appearance"]
            result.evidence_level = "claim_filed"
            result.lead_status = "not_pursuable"
            result.classification = "killed"
            result.classification_reason = (
                f"Docket checked. Notice of Appearance found from {ev['noa_kill']}. "
                f"Lead already being pursued."
            )
            return

        # 3 — residual NOA from an UNKNOWN firm → CAUTION (per approved decision:
        #     unknown residual firm = possible surplus claimant, manual review).
        if ev.get("noa_caution"):
            result.evidence_level = "pursuable_with_caution"
            result.lead_status = "pursuable_with_caution"
            result.classification = "yellow"
            result.classification_reason = (
                f"Docket checked. Possible surplus claimant appeared: {ev['noa_caution']}. "
                f"Not on known-recovery-firm list — manual review required."
            )
            return

        # 4 — clean docket. Tier A (pursuable) vs Tier B/C (caution) by completeness.
        sale_line = ("Certificate of Sale found. " if ev.get("sale_confirmed")
                     else "Sale confirmed on auction side. ")
        if ev.get("owner_present") and ev.get("docket_rows", 0) > 0:
            result.evidence_level = "no_claim_found"
            result.lead_status = "pursuable"
            result.classification = "green"
            result.classification_reason = (
                f"Docket checked. {sale_line}No notice of appearance, surplus claim, "
                f"or motion for surplus funds found."
            )
        else:
            # Tier B/C — owner/party not recoverable from the docket, or docket
            # sparse/weak. Address completeness is enforced downstream by the
            # loader (it holds the auction-side address); the scraper flags the
            # owner/docket gap here.
            result.evidence_level = "pursuable_with_caution"
            result.lead_status = "pursuable_with_caution"
            result.classification = "yellow"
            result.classification_reason = (
                "Docket checked. No surplus claim found, but owner/address/unit "
                "incomplete or docket partial. Manual review required."
            )

    # ── Live docket via the genuine-Chrome bridge extension ──────────────────
    # Browser-driving lives OUTSIDE the class now (see _run_local + the bridge
    # extension). scrape_case is kept for the single-case CLI but Broward can no
    # longer be driven by Playwright/CDP — Turnstile suppresses its widget under
    # any automation. It points the caller at the local runner.

    async def scrape_case(self, case_number: str) -> DocketResult:
        result = DocketResult(
            county_id=self.county_id, case_number=case_number,
            scraped_at=datetime.now().isoformat(),
            foreclosure_type=FORECLOSURE_MORTGAGE,
        )
        result.case_url = LANDING_URL
        result.classification = "unknown"
        result.evidence_level = "auction_only"
        result.classification_reason = (
            "Broward is extension-driven local-run (Turnstile blocks headless/CDP). "
            "Run `python -m core.dockets.broward` (needs local Chrome + the bridge "
            "extension), or one case with `--case <CASE>`.")
        return result


# ─────────────────────────────────────────────────────────────────────────────
# AUTONOMOUS LOCAL RUN — genuine-Chrome bridge (Franklin-pattern, LOCAL_RUN).
# Turnstile issues its token only to a real browser, so the runner does NOT drive
# Chrome. It serves a tiny localhost queue; the unpacked bridge extension (loaded
# ONCE into the broward-fl profile) runs the whole search flow in genuine Chrome
# and posts each docket back. Output matches every other county:
# data/dockets/broward-fl_<YYYY-MM-DD>.jsonl, one record per case with
# _auction_data, rewritten after each case (resumable). A case that fails or isn't
# found is left ABSENT from the JSONL — the loader renders it docket-not-verified.

def _load_broward_cases() -> list[dict]:
    from .enrich import load_cases_from_raw
    return load_cases_from_raw("broward-fl")


class _Bridge:
    """Thread-safe shared state for the localhost queue the extension talks to.
    The classifier (parse_docket/_apply_evidence_level) runs unchanged, in the
    HTTP handler thread, on the rows the extension posts back."""

    def __init__(self, cases, only_case):
        from .manual_runner import _progress_paths, _load_progress, _write_outputs
        self.lock = threading.Lock()
        self.scraper = BrowardDocketScraper()
        self._write_outputs = _write_outputs
        day = datetime.now().strftime("%Y-%m-%d")
        self.out_file, self.progress_file = _progress_paths("broward-fl", day)
        self.progress = _load_progress(self.progress_file)   # raw_case -> out_rec
        self.total = len(cases)

        self.raw_of, self.auction_of, self.order, self.skipped = {}, {}, [], []
        for rec in cases:
            raw = rec.get("case_number", "")
            if only_case and not raw.upper().startswith(only_case.upper()[:8]):
                continue
            parsed = parse_broward_case_number(raw)
            if not parsed:
                self.skipped.append(raw); continue
            nd = parsed["nodash"]
            self.raw_of[nd] = raw
            self.auction_of[nd] = rec
            if raw not in self.progress:
                self.order.append(nd)
        self.attempts = {}
        self.done = set()          # nodash resulted (ok) or attempt-capped
        self.results_ok = 0

    def next_case(self):
        with self.lock:
            for nd in self.order:
                if nd in self.done:
                    continue
                if self.attempts.get(nd, 0) >= _MAX_ATTEMPTS:
                    self.done.add(nd); continue
                self.attempts[nd] = self.attempts.get(nd, 0) + 1
                return {"case": nd}
            return {"done": True}

    def record(self, payload):
        nd = (payload or {}).get("case", "")
        raw = self.raw_of.get(nd, nd)
        ok = bool(payload.get("ok") and payload.get("rows") and payload.get("case_present"))
        if not ok:
            with self.lock:
                capped = self.attempts.get(nd, 0) >= _MAX_ATTEMPTS
                if capped:
                    self.done.add(nd)
            print(f"  · {raw}: not verified — {payload.get('error') or 'no rows'}"
                  + ("" if capped else " (will retry)"))
            return {"ok": False}

        result = DocketResult(
            county_id="broward-fl", case_number=raw,
            scraped_at=datetime.now().isoformat(),
            foreclosure_type=FORECLOSURE_MORTGAGE,
        )
        result.case_url = LANDING_URL
        if payload.get("caption"):
            result.case_title = payload["caption"][:200]
        self.scraper.parse_docket(payload["rows"], result,
                                  case_caption=payload.get("caption", ""))
        self.scraper._apply_evidence_level(result)

        out_rec = result.to_dict()
        rec = self.auction_of.get(nd, {})
        out_rec["_auction_data"] = {
            "final_sale_price": float(rec.get("final_sale_price") or 0.0),
            "opening_bid":      float(rec.get("opening_bid") or 0.0),
            "apparent_surplus": float(rec.get("gross_surplus") or 0.0),
            "address":          rec.get("address", ""),
            "sale_date":        rec.get("sale_date", ""),
        }
        with self.lock:
            self.progress[raw] = out_rec
            self.done.add(nd)
            self.results_ok += 1
            self._write_outputs(self.out_file, self.progress_file, self.progress)
        tag = result.classification.upper() or "DOCKET-CHECKED"
        extra = f"  \U0001F6A8 {', '.join(result.kill_signals)}" if result.kill_signals else ""
        print(f"  ✓ {raw}: {tag} | owner={result.owner_name or '(none)'} "
              f"| {len(payload['rows'])} rows{extra}")
        return {"ok": True, "classification": result.classification}

    def all_done(self):
        with self.lock:
            return all(nd in self.done for nd in self.order)


def _make_handler(bridge, hits):
    class _H(http.server.BaseHTTPRequestHandler):
        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            hits.append(1)
            if self.path.startswith("/next"):
                self._send(bridge.next_case())
            elif self.path.startswith("/ping"):
                self._send({"ok": True})
            else:
                self._send({"error": "unknown"}, 404)

        def do_POST(self):
            hits.append(1)
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                payload = json.loads(raw.decode() or "{}")
            except Exception:
                payload = {}
            if self.path.startswith("/result"):
                self._send(bridge.record(payload))
            elif self.path.startswith("/log"):
                self._send({"ok": True})
            else:
                self._send({"error": "unknown"}, 404)

        def log_message(self, *a):
            pass
    return _H


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _serve(bridge, hits):
    httpd = _Server(("127.0.0.1", BRIDGE_PORT), _make_handler(bridge, hits))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _wait(fn, timeout, step=0.5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(step)
    return False


def _run_local(cases, only_case=None):
    bridge = _Bridge(cases, only_case)
    print("\n" + "=" * 64)
    print("  LOCAL DOCKET RUN — Broward (broward-fl)  [genuine-Chrome bridge]")
    print("=" * 64)
    print(f"  in-window cases : {bridge.total}")
    print(f"  already scraped : {len(bridge.progress)}")
    print(f"  to do this run  : {len(bridge.order)}")
    if bridge.skipped:
        print(f"  unparseable     : {len(bridge.skipped)} (stay docket-not-verified)")
    if not bridge.order:
        print("  ✓ nothing to do — all in-window cases already scraped this cycle.")
        return {"county_id": "broward-fl", "scraped": len(bridge.progress), "remaining": 0}

    hits = []
    httpd = _serve(bridge, hits)
    if _chrome_running_on_profile():
        print("  ⚠ a Chrome is already running on the broward-fl profile — opening "
              "a tab in it. Close it first if the run stalls.")
    print(f"\n  Launching genuine Chrome (profile: {PROFILE_DIR.name}) → the bridge "
          f"extension drives {len(bridge.order)} case(s)...\n")
    _launch_chrome(LANDING_URL)

    if not _wait(lambda: bool(hits), 25):
        _kill_chrome(); httpd.shutdown()
        print("\n  ✖ The bridge extension never contacted the runner. Confirm it is "
              "loaded:\n    chrome://extensions → Developer mode → Load unpacked → "
              f"{EXTENSION_DIR}\n    (loaded INTO the broward-fl profile), then re-run.")
        return {"county_id": "broward-fl", "scraped": len(bridge.progress),
                "remaining": len(bridge.order), "error": "extension not detected"}

    deadline = time.time() + max(180, bridge.total * 45 + 60)
    while not bridge.all_done() and time.time() < deadline:
        time.sleep(2)

    _kill_chrome()
    httpd.shutdown()
    remaining = len(bridge.order) - bridge.results_ok
    print("\n" + "=" * 64)
    print(f"  DONE: {bridge.results_ok} verified this run, "
          f"{len(bridge.progress)}/{bridge.total} total.")
    if remaining > 0:
        print(f"  {remaining} case(s) unverified — DOCKET-NOT-VERIFIED. Re-run to finish.")
    print(f"  Output: {bridge.out_file}")
    print("=" * 64)
    return {"county_id": "broward-fl", "scraped": len(bridge.progress), "remaining": remaining}


def _selftest():
    """Validate the bridge channel: launch Chrome and confirm the extension can
    reach the localhost endpoint (Private Network Access etc.). Runs no cases."""
    print(f"Bridge self-test on port {BRIDGE_PORT}. Extension dir: {EXTENSION_DIR}")

    class _Empty:
        total = 0; order = []; progress = {}; skipped = []
        def next_case(self): return {"done": True}
        def record(self, p): return {"ok": True}
        def all_done(self): return True

    hits = []
    httpd = _serve(_Empty(), hits)
    _launch_chrome(LANDING_URL)
    ok = _wait(lambda: bool(hits), 30)
    _kill_chrome(); httpd.shutdown()
    if ok:
        print("✓ Channel OK — the extension reached the runner over localhost. "
              "Private Network Access is not blocking it.")
    else:
        print("✖ No contact. Either the extension isn't loaded in the broward-fl "
              "profile (chrome://extensions → Load unpacked → "
              f"{EXTENSION_DIR}), or localhost is gated. If it's loaded and still "
              "failing, the fallback is chrome.downloads (see README).")
    return ok


def main():
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m core.dockets.broward",
        description="Autonomous LOCAL Broward docket run via the genuine-Chrome "
                    "bridge extension. One-time setup: chrome://extensions → "
                    "Developer mode → Load unpacked → point at "
                    "core/dockets/broward_extension (in the broward-fl profile).")
    ap.add_argument("--selftest", action="store_true",
                    help="validate the extension⇄runner localhost channel; run no cases")
    ap.add_argument("--case", help="run one specific case-number prefix only")
    args = ap.parse_args()

    if args.selftest:
        _selftest(); return

    cases = _load_broward_cases()
    if not cases:
        print("No Broward auction data in data/raw/broward-fl_*.jsonl. "
              "Run the auction scraper first (or wait for the daily cron).")
        return
    _run_local(cases, only_case=args.case)


if __name__ == "__main__":
    main()
