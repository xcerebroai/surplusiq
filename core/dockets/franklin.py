"""
Franklin County (OH) docket scraper — AUTONOMOUS LOCAL, metadata-only, no debt.

Franklin's Case Information Online portal is reachable and rich from a
RESIDENTIAL IP but Cloudflare-challenges the datacenter (GitHub Actions) — see
knowledge/blocked_counties.md. So Franklin runs LOCALLY and autonomously (no
human solve, unlike Orange), and is skipped in the cloud cron via
LOCAL_RUN_COUNTIES. It exposes NO judgment amount, so it NEVER emits a debt
figure; its job is kill-signal detection (catch dead leads the auction "Sold"
status hides) + owner extraction. Leads stay apparent_surplus, debt_source="".

Built on the reusable ManualCountyScraper loop (core/dockets/manual_runner.py)
with requires_human_solve=False. Classification is delegated to the
ground-truthed, temporally-anchored core/dockets/franklin_classify.

Verified DOM (data/samples/franklin/ci, 2026-08-11):
  • landing is a disclaimer gate: acceptDisclaimer form, ACCEPT button.
  • search form uses SEPARATE fields: #caseYear_nh (2-digit yr),
    #caseType_nh (dropdown, 'CV'), #caseSeq_nh (6-digit zero-padded), #btnSearch.
  • case detail: #defendant-container holds the DEFENDANT(S) table (homeowner);
    docket events are dated 'MM/DD/YY DESCRIPTION <microfilm ref>'.

Run it (residential machine):
    source .venv/bin/activate && python3 -m core.dockets.franklin
"""
from __future__ import annotations
import re
from datetime import datetime
from typing import Optional

from playwright.async_api import Page, TimeoutError as PWTimeout

from .base import DocketResult
from .manual_runner import ManualCountyScraper, run_manual_county
from .franklin_classify import parse_docket_events, classify_franklin

BASE_URL = "https://fcdcfcjs.co.franklin.oh.us/CaseInformationOnline"
LANDING_URL = f"{BASE_URL}/"

# The foreclosed homeowner is the FIRST defendant listed (individual, LLC, or
# estate/"UNKNOWN HEIRS OF <name>"). The government/tax parties (treasurer,
# state, HUD, etc.) are always secondary defendants — skip them if one somehow
# leads. Verified across 13 real dockets: the first non-government defendant is
# the homeowner every time. (Plaintiff banks live in #plaintiff-container, a
# separate node, so they never appear here.)
_GOVT = re.compile(
    r"\b(TREASURER|STATE OF\b|UNITED STATES|SECRETARY OF HOUSING|DEPARTMENT OF|"
    r"DEPT OF|CITY OF|COUNTY OF|INTERNAL REVENUE|OHIO STATE DEPARTMENT)\b",
    re.I,
)


def parse_franklin_case_number(raw: str) -> Optional[dict]:
    """Parse a Franklin case number into components.
    '24CV9172 (18608)' / '2024CV009172' -> {year, prefix, number, search_text}."""
    if not raw:
        return None
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", raw.strip())
    cleaned = re.sub(r"[-\s]", "", cleaned).upper()
    m = re.match(r"^(\d{2})(CV)(\d+)$", cleaned)
    if m:
        yr = int(m.group(1))
        year = 2000 + yr if yr <= 30 else 1900 + yr
        return {"year": year, "prefix": m.group(2), "number": int(m.group(3)), "search_text": cleaned}
    m = re.match(r"^(\d{4})(CV)(\d+)$", cleaned)
    if m:
        return {"year": int(m.group(1)), "prefix": m.group(2), "number": int(m.group(3)),
                "search_text": cleaned}
    return None


class FranklinDocketScraper(ManualCountyScraper):
    county_id = "franklin-oh"
    county_name = "Franklin"
    search_url = LANDING_URL
    requires_human_solve = False          # autonomous; IP-gated, not CAPTCHA-gated

    def parse_case_number(self, raw: str):
        """→ (year2, type, seq6) tuple the search form needs, or None. CV only."""
        p = parse_franklin_case_number(raw)
        if not p or p["prefix"] != "CV":
            return None
        return (f"{p['year'] % 100:02d}", "CV", f"{p['number']:06d}")

    async def open_search_form(self, page: Page) -> bool:
        """Load the landing page, accept the disclaimer, land on the search form."""
        await page.goto(LANDING_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(800)
        # If the search form is already present, the disclaimer is done.
        if await page.locator("#caseSeq_nh").count() == 0:
            try:
                async with page.expect_navigation(wait_until="networkidle", timeout=25000):
                    await page.click("input[value='ACCEPT'], input[name='Accept']", timeout=10000)
            except PWTimeout:
                pass
        try:
            await page.wait_for_selector("#caseSeq_nh", state="visible", timeout=15000)
            return True
        except PWTimeout:
            return False

    async def fill_case(self, page: Page, form_value) -> None:
        year2, ctype, seq6 = form_value
        await page.fill("#caseYear_nh", year2)
        try:
            await page.select_option("#caseType_nh", ctype)
        except Exception:
            pass
        await page.fill("#caseSeq_nh", seq6)

    async def _extract_owner(self, page: Page) -> tuple[str, list]:
        """Parse #defendant-container's DEFENDANT(S) table directly. Each
        defendant has a visible NAME row plus a HIDDEN 'defdetail*' address row
        (display:none) — we take only the name rows. Returns
        (homeowner_name, all_defendant_names)."""
        names = await page.evaluate("""() => {
            const c = document.querySelector('#defendant-container');
            if (!c) return [];
            return [...c.querySelectorAll('tbody tr')]
              .filter(tr => !(tr.id||'').startsWith('defdetail')
                         && getComputedStyle(tr).display !== 'none')
              .map(tr => {
                 const tds = [...tr.querySelectorAll('td')]
                    .map(td => (td.innerText||'').replace(/\\s+/g,' ').trim());
                 return tds.find(t => t && t.toLowerCase() !== 'name'
                                   && !/no attorney on record/i.test(t)) || '';
              })
              .filter(Boolean);
        }""")
        names = [re.sub(r"\s+", " ", n).strip() for n in names]
        owner = next((n for n in names if not _GOVT.search(n)), names[0] if names else "")
        return owner, names

    async def _extract_plaintiff(self, page: Page) -> str:
        try:
            txt = await page.evaluate("""() => {
                const c = document.querySelector('#plaintiff-container');
                if (!c) return '';
                const tds = [...c.querySelectorAll('table tr td')].map(td=>(td.innerText||'').trim()).filter(Boolean);
                return tds[0] || '';
            }""")
            return re.sub(r"\s+", " ", txt or "").strip()
        except Exception:
            return ""

    async def scrape_detail(self, page: Page, case_number: str,
                            auction: dict = None) -> DocketResult:
        result = DocketResult(
            county_id=self.county_id, case_number=case_number,
            case_url=page.url, scraped_at=datetime.now().isoformat(),
            foreclosure_type="mortgage_foreclosure",
            debt_source="", prayer_amount=0.0,      # Franklin has NO debt figure
        )
        try:
            async with page.expect_navigation(wait_until="networkidle", timeout=25000):
                await page.click("#btnSearch", timeout=12000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(600)

        body = await page.inner_text("body")
        if "NO CASE MATCHED" in body.upper():
            result.classification = "unknown"
            result.classification_reason = "no case matched the search criteria"
            print(f"     ⚠ {case_number}: NO CASE MATCHED")
            return result

        result.owner_name, result.defendants = await self._extract_owner(page)
        result.plaintiff = await self._extract_plaintiff(page)

        events = parse_docket_events(body)
        sale_date = (auction or {}).get("auction_date") or (auction or {}).get("sale_date") or ""
        verdict = classify_franklin(events, sale_date)

        result.kill_signals = verdict["kill_signals"]
        result.competing_filers = verdict["competing_filers"]
        result.evidence_level = verdict["evidence_level"]
        result.classification_reason = verdict["classification_reason"]
        if verdict["classification"] == "killed":
            result.classification = "killed"
        else:
            # docket-checked, no kill, no debt → stays apparent_surplus. Not
            # "green" (no proof/debt); the empty classification + docket_checked
            # evidence level signals "reviewed, nothing killed it".
            result.classification = ""
            result.lead_status = "pursuable_with_caution"

        tag = "KILLED" if result.classification == "killed" else "docket-checked"
        print(f"     ✓ {case_number}: {tag} | owner={result.owner_name or '(none)'} "
              f"| {len(events)} events" +
              (f" | {result.classification_reason}" if result.classification == "killed" else ""))
        return result


def _load_franklin_cases() -> list[dict]:
    from .enrich import load_cases_from_raw
    return load_cases_from_raw("franklin-oh")


def main():
    import asyncio
    cases = _load_franklin_cases()
    if not cases:
        print("No Franklin auction data in data/raw/franklin-oh_*.jsonl. "
              "Run the auction scraper first (or wait for the daily cron).")
        return
    scraper = FranklinDocketScraper(headless=False)
    asyncio.run(run_manual_county(scraper, cases))


if __name__ == "__main__":
    main()
