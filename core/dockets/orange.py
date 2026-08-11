"""
Orange County (FL) docket scraper — MANUAL, human-solve local county.

The Orange eClerk portal (myeclerk.myorangeclerk.com/Cases/Search) gates EVERY
search behind a reCAPTCHA v2 checkbox. Testing (2026-08-11) proved it is
per-search: one solve unlocks exactly one lookup — the server rejects
session-only requests (HTTP 200, case_data_present=False, captcha_demanded=True)
even with a valid ASP.NET_SessionId + fresh anti-forgery token. So Orange
cannot run in GitHub Actions; it runs LOCALLY, headed, with a human solving one
checkbox per case. Every other county and the cloud cron are untouched.

Runs via the reusable ManualCountyScraper loop (core/dockets/manual_runner.py).
This module supplies Orange's verified DOM facts + case parsing + detail capture.

Verified DOM (data/diagnostics/orange-dom, 2026-08-11):
  • case-number input : #caseNumber        (lowercase c)
  • submit button     : #caseSearch
  • v2 token textarea : #g-recaptcha-response  (non-empty value == solved;
                        the separate #g-recaptcha-response-100000 is a v3 score
                        token — ignored)
  • no disclaimer interstitial — the form renders directly on /Cases/Search.

PHASE 1 (this file): the solve loop + search-form mechanics + RAW detail capture
for ground-truthing. scrape_detail submits, waits for the result page, and saves
the full HTML/text/screenshot to data/diagnostics/orange-fl/ so Phase 2 can
build the classifier from real docket vocabulary. Classification stays
"unknown" until the Phase-3 classifier lands — nothing is assumed clean.

Run it:
    source .venv/bin/activate && python3 -m core.dockets.orange
"""
from __future__ import annotations
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page, TimeoutError as PWTimeout

from .base import DocketResult
from .manual_runner import ManualCountyScraper, DIAG_ROOT, run_manual_county


def parse_orange_case_number(raw: str) -> str | None:
    """Normalize an auction case_number to the form the search box expects.

    Auction records carry values like '2024-CA-009840-O (COUNT I)' or
    '2025-CA-000239-O (Ct II)'. The clerk searches on the bare case number
    'YYYY-(CA|CC|...)-NNNNNN-O'; the count/parcel suffix must be stripped.
    """
    if not raw:
        return None
    # Strip a trailing parenthetical (count/parcel/auction id).
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", raw.strip())
    # Orange civil format: 4-digit year, 2-letter type, 6-digit seq, '-O'.
    m = re.match(r"^(\d{4}-[A-Za-z]{2}-\d{4,6}-[A-Za-z])$", cleaned)
    if m:
        return m.group(1).upper()
    # Fall back to the cleaned string if it still looks case-like.
    if re.search(r"\d{4}-[A-Za-z]{2}-\d", cleaned):
        return cleaned.upper()
    return None


class OrangeDocketScraper(ManualCountyScraper):
    county_id = "orange-fl"
    county_name = "Orange"
    search_url = "https://myeclerk.myorangeclerk.com/Cases/Search"
    case_input_sel = "#caseNumber"
    submit_sel = "#caseSearch"
    token_sel = "#g-recaptcha-response"
    solve_ready_sel = "iframe[src*='recaptcha/api2/anchor']"  # the v2 checkbox

    def parse_case_number(self, raw: str):
        return parse_orange_case_number(raw)

    async def scrape_detail(self, page: Page, case_number: str,
                            auction: dict = None) -> DocketResult:
        """Submit the (already-solved) search and capture the result page.

        PHASE 1: the post-search detail DOM has not been ground-truthed yet, so
        this saves the full HTML/text/screenshot for Phase 2 and returns a
        minimal, honestly-UNKNOWN result. It never guesses a classification.
        """
        result = DocketResult(
            county_id=self.county_id,
            case_number=case_number,
            case_url=self.search_url,
            scraped_at=datetime.now().isoformat(),
            classification="unknown",
            classification_reason="Phase 1 capture — Orange classifier not built yet",
            evidence_level="docket_captured",
        )

        await self.submit(page)
        # Wait for the result to render (post-back or client nav).
        await page.wait_for_timeout(2500)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass

        body = await page.inner_text("body")
        norm = re.sub(r"\s+", " ", body)
        digits = re.sub(r"[^0-9]", "", case_number)
        case_found = bool(digits and digits in re.sub(r"[^0-9]", "", norm))

        diag = DIAG_ROOT / "orange-fl"
        diag.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9]+", "_", case_number).strip("_")
        try:
            (diag / f"{slug}.html").write_text(await page.content(), encoding="utf-8")
            (diag / f"{slug}.txt").write_text(body, encoding="utf-8")
            await page.screenshot(path=str(diag / f"{slug}.png"), full_page=True, timeout=8000)
        except Exception as e:
            print(f"     ⚠ capture save issue for {case_number}: {type(e).__name__}")

        result.evidence_level = "docket_captured" if case_found else "docket_capture_empty"
        result.classification_reason = (
            f"Phase 1 capture — case_data_present={case_found}; "
            f"raw detail saved to data/diagnostics/orange-fl/{slug}.html "
            f"(classifier pending)")
        print(f"     · captured {case_number}: case_data_present={case_found}, "
              f"{len(body)} chars → diagnostics/orange-fl/{slug}.*")
        return result


def _load_orange_cases() -> list[dict]:
    # Lazy import to avoid a circular import at module load.
    from .enrich import load_cases_from_raw
    return load_cases_from_raw("orange-fl")


def main():
    import asyncio
    cases = _load_orange_cases()
    if not cases:
        print("No Orange auction data found in data/raw/orange-fl_*.jsonl. "
              "Run the auction scraper first (or wait for the daily cron).")
        return
    scraper = OrangeDocketScraper(headless=False)
    asyncio.run(run_manual_county(scraper, cases))


if __name__ == "__main__":
    main()
