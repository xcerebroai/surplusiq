"""
Manual-county docket runner — REUSABLE human-in-the-loop scrape loop.

Some county portals gate every search behind a CAPTCHA that cannot be solved
headless in CI (Orange's reCAPTCHA v2 checkbox is per-search; a human solves one
checkbox per case). This runner owns ONE headed browser session for a whole run
and drives the tight solve loop; a county subclass supplies only its DOM facts
and detail-page parsing. It is NOT Orange-specific — any per-search-CAPTCHA
county plugs in by subclassing ManualCountyScraper.

Design goals (the UX is the whole point):
  • ONE browser for the entire run — never relaunch per case.
  • Per case: navigate → auto-fill the case number → PAUSE with the checkbox
    visible and ready → the instant the token appears, submit + scrape → next.
  • Minimal dead time: the form is loaded and filled BEFORE the human is
    prompted, so they click in rhythm.
  • Resumable: completed cases persist to a progress file; an interrupted run
    resumes with only the unscraped cases. Unscraped cases are never written as
    clean — they simply stay absent from the docket JSONL, which the loader
    treats as docket-not-verified (apparent_surplus), the correct default.

Output matches every other county: data/dockets/<county_id>_<YYYY-MM-DD>.jsonl,
one record per parcel with _auction_data, rewritten after each case so an
interrupt always leaves a valid file.
"""
from __future__ import annotations
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

from .base import DocketScraper, DocketResult

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCKETS_DIR = PROJECT_ROOT / "data" / "dockets"
DIAG_ROOT = PROJECT_ROOT / "data" / "diagnostics"


class ManualCountyScraper(DocketScraper):
    """Base for counties needing a per-search human CAPTCHA solve.

    A subclass sets the DOM selectors + search URL and implements
    `parse_case_number` and `scrape_detail`. The default fill/submit/token
    helpers use the selectors, so a simple county only overrides scrape_detail.
    """
    search_url: str = ""
    case_input_sel: str = ""      # case-number text input
    submit_sel: str = ""          # search/submit button
    token_sel: str = ""           # element whose non-empty value == solved
    solve_ready_sel: str = ""     # the CAPTCHA widget — must render BEFORE we
                                  # prompt the human (else they see no checkbox)
    # True  → per-search human CAPTCHA solve (Orange).
    # False → autonomous, but still local-only (IP-gated to residential, e.g.
    #         Franklin) — the loop runs each case without pausing for a human.
    requires_human_solve: bool = True

    # ── county-specific hooks ──
    def parse_case_number(self, raw: str) -> Optional[str]:
        """Normalize an auction case_number into the exact string the search
        form expects. Return None to skip an unparseable case."""
        raise NotImplementedError

    async def scrape_detail(self, page: Page, case_number: str,
                            auction: Optional[dict] = None) -> DocketResult:
        """Parse the post-search detail page into a DocketResult. `auction` is
        the case's auction-side record (sale date/price) for counties whose
        classification needs it (e.g. Franklin's temporal anchor)."""
        raise NotImplementedError

    # ── default DOM helpers (override only if a county differs) ──
    async def open_search_form(self, page: Page) -> bool:
        await page.goto(self.search_url, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_selector(self.case_input_sel, state="visible", timeout=20000)
        except PWTimeout:
            return False
        # Let async widgets (the CAPTCHA) finish loading, then confirm the solve
        # widget is actually on screen BEFORE the caller prompts the human.
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except PWTimeout:
            pass
        if self.solve_ready_sel:
            try:
                await page.wait_for_selector(self.solve_ready_sel, timeout=12000)
            except PWTimeout:
                print("     ⚠ CAPTCHA widget slow to render — it should appear shortly")
        return True

    async def fill_case(self, page: Page, form_value: str) -> None:
        await page.fill(self.case_input_sel, form_value)

    async def token_present(self, page: Page) -> bool:
        try:
            return bool(await page.eval_on_selector(self.token_sel, "el => el.value || ''"))
        except Exception:
            return False

    async def submit(self, page: Page) -> None:
        await page.click(self.submit_sel, timeout=15000)


def _progress_paths(county_id: str, day: str) -> tuple[Path, Path]:
    out_file = DOCKETS_DIR / f"{county_id}_{day}.jsonl"
    progress_file = DOCKETS_DIR / f".{county_id}_{day}.progress.json"
    return out_file, progress_file


def _load_progress(progress_file: Path) -> dict:
    if progress_file.exists():
        try:
            return json.loads(progress_file.read_text())
        except Exception:
            return {}
    return {}


def _write_outputs(out_file: Path, progress_file: Path, progress: dict) -> None:
    """Rewrite both the progress file and the docket JSONL from the full
    progress map. Called after every case so an interrupt never corrupts state."""
    DOCKETS_DIR.mkdir(parents=True, exist_ok=True)
    progress_file.write_text(json.dumps(progress, indent=1))
    with open(out_file, "w") as f:
        for rec in progress.values():
            f.write(json.dumps(rec) + "\n")


async def _wait_for_solve(scraper: ManualCountyScraper, page: Page,
                          poll_ms: int = 400) -> None:
    """Tight, indefinite poll for the CAPTCHA token. Reminds every 30s. The
    fast poll means submit fires the instant the human finishes clicking."""
    t0 = time.time()
    last = 0.0
    while True:
        if await scraper.token_present(page):
            return
        waited = time.time() - t0
        if waited - last >= 30:
            last = waited
            print(f"     … still waiting for the checkbox ({waited:.0f}s) — Ctrl-C to stop & resume later")
        await page.wait_for_timeout(poll_ms)


async def run_manual_county(scraper: ManualCountyScraper, cases: list[dict], *,
                            day: Optional[str] = None,
                            headless: Optional[bool] = None) -> dict:
    """Drive the human-in-the-loop scrape for one county.

    `cases` is the auction-side records list (from load_cases_from_raw): each
    dict carries case_number, final_sale_price, opening_bid, gross_surplus,
    address, sale_date. Returns a summary dict.
    """
    day = day or datetime.now().strftime("%Y-%m-%d")
    out_file, progress_file = _progress_paths(scraper.county_id, day)
    progress = _load_progress(progress_file)

    # Build the work list: (form_value, case_number, auction) for unscraped cases.
    work, skipped_unparseable = [], []
    for rec in cases:
        raw = rec.get("case_number", "")
        form_value = scraper.parse_case_number(raw)
        if not form_value:
            skipped_unparseable.append(raw)
            continue
        if raw in progress:
            continue
        work.append((form_value, raw, rec))

    total = len(cases)
    already = len(progress)
    print("\n" + "=" * 64)
    print(f"  MANUAL DOCKET RUN — {scraper.county_name} ({scraper.county_id})")
    print("=" * 64)
    print(f"  in-window cases : {total}")
    print(f"  already scraped : {already} (resuming)" if already else f"  already scraped : 0")
    print(f"  to do this run  : {len(work)}")
    if skipped_unparseable:
        print(f"  unparseable     : {len(skipped_unparseable)} (stay docket-not-verified): {skipped_unparseable}")
    if not work:
        print("  ✓ nothing to do — all in-window cases already scraped this cycle.")
        return {"county_id": scraper.county_id, "scraped": already, "remaining": 0}
    if scraper.requires_human_solve:
        print("\n  You'll solve ONE checkbox per case. The form is pre-filled and the")
        print("  checkbox is ready when prompted — click it and the scrape fires.\n")
    else:
        print("\n  Autonomous local run (no human input) — IP-gated to residential.\n")

    # A human-solve county MUST be headed (the human needs the window). An
    # autonomous local county may run headless. Explicit `headless` overrides.
    if headless is None:
        headless = not scraper.requires_human_solve
    scraped_this_run = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 950}, locale="en-US")
        page = await ctx.new_page()
        try:
            for i, (form_value, case_number, auction) in enumerate(work, 1):
                pos = already + i
                # Load + auto-fill BEFORE prompting, so the human clicks in rhythm.
                if not await scraper.open_search_form(page):
                    print(f"  [{pos}/{total}] {case_number}: search form failed to load — skipping (stays docket-not-verified)")
                    continue
                await scraper.fill_case(page, form_value)
                if scraper.requires_human_solve:
                    print(f"  ▶ case {pos} of {total} — SOLVE THE CHECKBOX  ({case_number})")
                    await _wait_for_solve(scraper, page)
                else:
                    print(f"  ▶ case {pos} of {total} — scraping  ({case_number})")
                try:
                    result = await scraper.scrape_detail(page, case_number, auction)
                except Exception as e:
                    print(f"     ⚠ scrape failed for {case_number}: {type(e).__name__}: {e} — leaving docket-not-verified")
                    continue

                rec = result.to_dict()
                rec["_auction_data"] = {
                    "final_sale_price": float(auction.get("final_sale_price") or 0.0),
                    "opening_bid":      float(auction.get("opening_bid") or 0.0),
                    "apparent_surplus": float(auction.get("gross_surplus") or 0.0),
                    "address":          auction.get("address", ""),
                    "sale_date":        auction.get("sale_date", ""),
                }
                progress[case_number] = rec
                _write_outputs(out_file, progress_file, progress)
                scraped_this_run += 1
                print(f"     ✓ scraped {case_number} → {result.classification.upper()} "
                      f"(saved {len(progress)}/{total})")
        except KeyboardInterrupt:
            print("\n  ⏸ interrupted — progress saved. Re-run to resume with the remaining cases.")
        finally:
            await browser.close()

    remaining = total - len(progress) - len(skipped_unparseable)
    try:
        disp = out_file.relative_to(PROJECT_ROOT)
    except ValueError:
        disp = out_file
    print("\n" + "=" * 64)
    print(f"  DONE: {scraped_this_run} scraped this run, {len(progress)}/{total} total.")
    if remaining > 0:
        print(f"  {remaining} case(s) still unscraped — they remain DOCKET-NOT-VERIFIED "
              f"(apparent_surplus). Re-run to finish.")
    print(f"  Output: {disp}")
    print("=" * 64)
    return {"county_id": scraper.county_id, "scraped": len(progress),
            "remaining": remaining, "out_file": str(out_file)}
