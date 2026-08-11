"""
SurplusIQ — Universal Auction Scraper
Works for both RealForeclose (FL) and SheriffSaleAuction (OH) platforms
since they're both built by Grant Street Group with identical page structure.

Targets the "Preview Items For Sale" page:
- FL: https://<county>.realforeclose.com/index.cfm?zaction=AUCTION&Zmethod=PREVIEW
- OH: https://<county>.sheriffsaleauction.ohio.gov/index.cfm?zaction=AUCTION&zmethod=PREVIEW

Extracts from each auction item:
- Auction Status (Sold / Redeemed / Canceled)
- Auction Type (TAXDEED / MORTGAGE / HOA)
- Case Number
- Certificate # (tax deeds)
- Opening Bid
- Final Sale Amount
- Sold To (3rd Party Bidder vs plaintiff)
- Parcel ID
- Property Address
- Assessed Value
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, Browser, BrowserContext, TimeoutError as PWTimeout

# Project paths — find surplusiq root regardless of where we're run from
def _find_project_root() -> Path:
    """Walk up from this file until we find the surplusiq root (contains config/counties.py)."""
    p = Path(__file__).resolve()
    for parent in [p] + list(p.parents):
        if (parent / "config" / "counties.py").exists():
            return parent
    # Fallback to 4 levels up
    return Path(__file__).resolve().parent.parent.parent.parent

PROJECT_ROOT = _find_project_root()
DATA_DIR     = PROJECT_ROOT / "data"
RAW_DIR      = DATA_DIR / "raw"
DIAG_DIR     = DATA_DIR / "diagnostics"
for d in (RAW_DIR, DIAG_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Import county config
sys.path.insert(0, str(PROJECT_ROOT))
from config.counties import ALL_COUNTIES, get_county, CountyConfig, LEAD_WINDOW_DAYS

MIN_SURPLUS = 10000


def clean_dollar(s) -> float:
    """Convert '$123,456.78' to float."""
    if not s:
        return 0.0
    try:
        return float(re.sub(r"[^\d.]", "", str(s)))
    except ValueError:
        return 0.0


def is_third_party(sold_to: str) -> bool:
    """
    True if the 'Sold To' field literally matches '3rd Party Bidder' 
    (or close variants). This is Grant Street Group's explicit label.
    """
    if not sold_to:
        return False
    s = sold_to.lower().strip()
    if "3rd party" in s or "third party" in s or "third-party" in s:
        return True
    # If it says "plaintiff" or "no bid", definitely not
    if any(kw in s for kw in ["plaintiff", "no bid", "cancel", "no sale"]):
        return False
    return False  # Default to false if label is anything else


class UniversalAuctionScraper:
    """Scraper that handles both RealForeclose and SheriffSaleAuction."""

    def __init__(self, county: CountyConfig):
        self.county = county
        # Strip trailing /index.cfm or / to get clean base
        base = county.auction_url.rstrip("/")
        if base.endswith("/index.cfm"):
            base = base[:-len("/index.cfm")]
        self.base_url = base
        self.diag_dir = DIAG_DIR / county.id
        self.diag_dir.mkdir(parents=True, exist_ok=True)

    def build_preview_url(self, auction_date: Optional[date] = None) -> str:
        """Build the preview-by-date URL."""
        if auction_date:
            date_str = auction_date.strftime("%m/%d/%Y")
            return f"{self.base_url}/index.cfm?zaction=AUCTION&Zmethod=PREVIEW&AUCTIONDATE={date_str}"
        return f"{self.base_url}/index.cfm?zaction=AUCTION&Zmethod=PREVIEW"

    async def handle_captcha(self, page: Page) -> None:
        """Pause for manual CAPTCHA solve if detected."""
        if not self.county.has_captcha:
            return
        try:
            content = await page.content()
            captcha_markers = ["captcha", "recaptcha", "hcaptcha", "i'm not a robot", "verify you"]
            if any(m in content.lower() for m in captcha_markers):
                print(f"\n    ⚠️  CAPTCHA detected on {self.county.name}")
                print(f"    Solve it in the browser window, then press ENTER...")
                input()
                print(f"    ✓ Continuing...")
        except Exception:
            pass

    async def handle_terms_agreement(self, page: Page, target_url: Optional[str] = None) -> None:
        """Click through T&C for Montgomery, then navigate back to target URL."""
        if not self.county.requires_terms_agreement:
            return
        try:
            # Look for agree/accept buttons
            for selector in [
                "input[type='button'][value*='Agree' i]",
                "input[type='submit'][value*='Agree' i]",
                "button:has-text('Agree')",
                "button:has-text('Accept')",
                "a:has-text('Agree')",
            ]:
                btn = await page.query_selector(selector)
                if btn:
                    await btn.click()
                    print(f"    ✓ Agreed to T&C")
                    # Montgomery's EULA redirects to home page after agreement.
                    # Re-navigate to the target preview URL.
                    # The 2000ms post-click sleep is dropped — the subsequent
                    # page.goto already establishes a fresh domcontentloaded
                    # wait, which subsumes any post-T&C settling time.
                    if target_url:
                        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                    return
        except Exception:
            pass

    async def save_diagnostic(self, page: Page, label: str) -> None:
        """Save screenshot + HTML for debugging."""
        try:
            ss = self.diag_dir / f"{label}.png"
            html = self.diag_dir / f"{label}.html"
            await page.screenshot(path=str(ss), full_page=True)
            html.write_text(await page.content())
        except Exception as e:
            print(f"    ⚠ Diagnostic save failed: {e}")

    async def scrape_preview_page(self, page: Page, auction_date: Optional[date] = None) -> list:
        """
        Scrape one preview page. Returns list of raw sale dicts.

        The Grant Street page structure has:
        - "Auctions Closed or Canceled" section with completed sales
        - Each auction item has a clear card/table with status, type, case#, bids, sold-to, etc.
        """
        url = self.build_preview_url(auction_date)
        print(f"    → {url}")

        # Guard: if the page closed during a prior iteration's teardown,
        # don't even try goto — the caller's is_closed() check will catch it
        # before the next iteration.
        if page.is_closed():
            print(f"    ⚠ page already closed; skipping {auction_date or 'default'}")
            return []

        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            # Grant Street auction pages are JS-heavy and render the
            # auction-item DOM asynchronously after domcontentloaded fires.
            # FP-12 first tried wait_for_selector against a union of known
            # item selectors, but that lost ALL FL counties (run 26520090637:
            # broward 16→0, miami-dade 12→0, duval 2→0, lee 1→0, orange 1→0)
            # — either state="attached" fired on a placeholder before items
            # populated, OR the selector list was incomplete for FL's DOM.
            # Network-idle is the safer wait here: it lets the XHR/JS that
            # populates the auction list finish before we extract. Still
            # faster than the old 3500ms blind sleep on most pages.
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except PWTimeout:
                pass
            # Earlier attempt (FP-12) added a 500ms post-networkidle floor
            # to fix a Broward 16→15 reproducible delta. Run 26532517350
            # showed the floor did NOT fix Broward (still 15) AND
            # introduced a Cuyahoga regression (33→19). Reverted. The
            # Broward delta needs a different diagnostic — likely
            # Broward-specific, not a global timing issue.
            await self.handle_terms_agreement(page, target_url=url)
            await self.handle_captcha(page)
        except PWTimeout:
            print(f"    ⚠ Timeout loading page")
            return []
        except Exception as e:
            print(f"    ⚠ Load error: {e}")
            return []

        # Verify we're on the auction page, not login/marketing
        content = await page.content()

        # ALWAYS save a diagnostic for Ohio counties so we can debug structure issues
        if self.county.auction_platform == "sheriffsaleauction":
            label = f"sample_{auction_date or 'default'}"
            await self.save_diagnostic(page, label)

        if not self._is_valid_auction_page(content):
            await self.save_diagnostic(page, f"invalid_{auction_date or 'default'}")
            print(f"    ⚠ Not a valid auction page")
            return []

        # Extract auction items
        sales = await self._extract_auction_items(page, auction_date)

        # Handle pagination if present
        page_num = 1
        while page_num < 10:  # Safety limit
            has_next = await self._go_to_next_page(page)
            if not has_next:
                break
            page_num += 1
            # Pagination wait — same FL-loss concern as the preview-page
            # wait above. networkidle is the safe condition.
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except PWTimeout:
                pass
            more_sales = await self._extract_auction_items(page, auction_date)
            sales.extend(more_sales)

        return sales

    def _is_valid_auction_page(self, html: str) -> bool:
        """Check if page shows auction data, not a login wall."""
        if len(html) < 3000:
            return False
        # Grant Street marketing page shows this exact phrase
        if "KNOWING THERE IS NO GUARANTEE" in html.upper() and "AUCTION" not in html[:2000].upper():
            return False
        # Look for positive signals of the auction page
        positive_markers = [
            "Preview Items For Sale",
            "Auction Sold",
            "Auction Status",
            "Case #",
            "Opening Bid",
            "Sold To",
            "3rd Party Bidder",
        ]
        return any(m in html for m in positive_markers)

    async def _extract_auction_items(self, page: Page, auction_date: Optional[date]) -> list:
        """Extract all auction items from the current page."""
        sales = []

        # Grant Street uses div-based auction cards. Try multiple selectors.
        # Florida (RealForeclose) uses one set; Ohio (SheriffSaleAuction) sometimes uses another.
        item_selectors = [
            "div.AUCTION_ITEM",
            "div.AITEM",
            "[id^='Area_W']",
            "[class*='auction-item']",
            "div.auctionItem",
            # Ohio-specific selectors observed in diagnostic HTML:
            "div.product",
            "div.news-box",
            "div.row.border-bottom",
        ]

        items = []
        matched_selector = None
        for sel in item_selectors:
            items = await page.query_selector_all(sel)
            if items:
                matched_selector = sel
                break

        if matched_selector and self.county.auction_platform == "sheriffsaleauction" and len(items) >= 5:
            # Only print on first match per page when we get meaningful counts
            pass  # Keep silent unless debugging

        # Fallback: parse the entire page HTML with regex
        if not items:
            content = await page.content()
            return self._regex_extract(content, auction_date)

        for item in items:
            try:
                sale = await self._parse_item_element(item, auction_date)
                if sale:
                    sales.append(sale)
            except Exception as e:
                continue

        return sales

    async def _parse_item_element(self, item, auction_date: Optional[date]) -> Optional[dict]:
        """Parse a single auction item element into a structured sale record."""
        text = (await item.inner_text()).strip()
        if len(text) < 30:
            return None

        # STRICT DATE GUARD: require "Auction Sold MM/DD/YYYY" matching URL date
        sold_ts_match = re.search(
            r"Auction\s+(?:Sold|Redeemed)\s*\n?\s*(\d{1,2})/(\d{1,2})/(\d{4})",
            text, re.IGNORECASE
        )
        if not sold_ts_match:
            return None
        try:
            actual_sale_date = date(
                int(sold_ts_match.group(3)),
                int(sold_ts_match.group(1)),
                int(sold_ts_match.group(2)),
            )
        except ValueError:
            return None
        if auction_date and actual_sale_date != auction_date:
            return None

        # Status detection — check for explicit "Auction Sold" heading FIRST
        # (Cuyahoga/Ohio uses "Auction Sold" as section heading, with "Case Status: ACTIVE" elsewhere)
        if "Auction Sold" in text:
            status = "Sold"
        elif "Auction Redeemed" in text:
            status = "Redeemed"
        else:
            # Fall back to label-based extraction (Florida uses "Auction Status: Sold")
            status = self._extract_field(text, ["Auction Status"]) or ""
            if not status:
                # Last resort: keyword inference
                if re.search(r"\bSold\b", text):      status = "Sold"
                elif "Redeemed" in text:              status = "Redeemed"
                elif "Cancel" in text:                status = "Canceled"
                elif "Postponed" in text:             status = "Postponed"
                elif "Bankruptcy" in text:            status = "Bankruptcy"
                elif "Withdrawn" in text:             status = "Withdrawn"

        # Only process truly sold auctions — skip canceled, pending, waiting, postponed, etc.
        status_lower = status.lower()
        if any(bad in status_lower for bad in ["cancel", "pending", "waiting", "postpon", "bankrupt", "withdraw"]):
            return None
        if "sold" not in status_lower and "redeemed" not in status_lower:
            return None

        # Also skip if text explicitly contains "Canceled per" anywhere
        # (Orange marks this on items that LOOK sold but were canceled)
        if "canceled per" in text.lower() or "cancelled per" in text.lower():
            return None

        # Case number
        case_num = self._extract_field(text, ["Case #", "Case Number", "Case"]) or ""
        if not case_num:
            # Try regex as fallback
            m = re.search(r"\b(\d{4}[-\s]?[A-Z]{2,4}[-\s]?\d{4,}|CV[-\s]?\d{2}[-\s]?\d{4,}|\d{2}CV\d{4,})\b", text)
            if m:
                case_num = m.group(1)
        if not case_num:
            return None

        # Financial fields — Miami-Dade uses "Opening Bid", Broward uses "Final Judgment Amount"
        opening_bid = clean_dollar(self._extract_field(text, [
            "Opening Bid",
            "Final Judgment Amount",
            "Judgment Amount",
            "Plaintiff Max Bid",
        ]))
        final_sale  = clean_dollar(self._extract_field(text, ["Amount", "Sold Amount", "Winning Bid"]))
        assessed    = clean_dollar(self._extract_field(text, ["Assessed Value"]))
        # OH RealAuction (sheriffsaleauction.ohio.gov) exposes "Appraised Value"
        # alongside the 2/3 opening bid; FL realforeclose does not. Capture it —
        # sale-vs-appraised is a more meaningful signal than sale-vs-2/3-floor.
        # STORED ONLY (no surplus-math change): opening_bid still drives math.
        appraised   = clean_dollar(self._extract_field(text, ["Appraised Value", "Appraisal Value"]))

        # If "Amount" not found directly, look for the largest dollar value below status
        if not final_sale:
            dollars = re.findall(r"\$([\d,]+(?:\.\d{2})?)", text)
            amounts = [clean_dollar(d) for d in dollars]
            amounts = sorted([a for a in amounts if a > 500])
            if len(amounts) >= 2:
                # Usually: assessed, opening, final — final is the highest
                final_sale = amounts[-1]
                if not opening_bid:
                    opening_bid = amounts[-2]

        if not final_sale or not opening_bid:
            return None

        surplus = final_sale - opening_bid

        # Skip if surplus is negative (sale price less than debt/opening bid)
        # These are NOT real surplus opportunities — property sold for LESS than owed.
        if surplus < 0:
            return None

        # Sold To
        sold_to = self._extract_field(text, ["Sold To", "Winner", "Bidder"]) or ""

        # Auction Type
        auction_type = self._extract_field(text, ["Auction Type", "Type"]) or ""

        # Certificate # (tax deeds)
        cert_num = self._extract_field(text, ["Certificate #", "Certificate"]) or ""

        # Parcel ID
        parcel = self._extract_field(text, ["Parcel ID", "Parcel"]) or ""

        # Address
        address = self._extract_address(text)

        surplus = final_sale - opening_bid

        return {
            "county_id":         self.county.id,
            "county_name":       self.county.name,
            "state":             self.county.state,
            "case_number":       case_num.strip(),
            "certificate_num":   cert_num.strip(),
            "auction_type":      auction_type.strip(),
            "auction_status":    status,
            "opening_bid":       opening_bid,
            "final_sale_price":  final_sale,
            "gross_surplus":     surplus,
            "assessed_value":    assessed,
            "appraised_value":   appraised,
            "sold_to":           sold_to.strip(),
            "is_third_party":    is_third_party(sold_to),
            "parcel_id":         parcel.strip(),
            "address":           address,
            "auction_date":      actual_sale_date.isoformat(),
            "scraped_at":        datetime.now().isoformat(),
            "source_url":        self.build_preview_url(auction_date),
            "raw_text":          text[:800],
        }

    def _extract_field(self, text: str, field_names: list) -> str:
        """
        Extract a field value from text. Handles patterns like:
        'Case #:\\n2025A00443'
        'Opening Bid: $11,552.36'
        """
        for field in field_names:
            # Multi-line pattern (label then value on next line)
            pattern = rf"{re.escape(field)}\s*:?\s*\n\s*([^\n]+)"
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(1).strip()
            # Same-line pattern
            pattern = rf"{re.escape(field)}\s*:?\s*([^\n]+)"
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                # Avoid matching the next field's label
                val = re.split(r"(?=[A-Z][a-z]+\s*#|:)", val)[0].strip()
                if val:
                    return val
        return ""

    def _extract_address(self, text: str) -> str:
        """Find a property address line in the auction item text."""
        # Match lines like "13791 SW 66 ST\\nMIAMI, FL- 33183-2297"
        for line in text.split("\n"):
            line = line.strip()
            if re.search(r"\d+.*\b(ST|AVE|BLVD|DR|RD|LN|CT|WAY|PL|CIR|HWY|STREET|AVENUE|DRIVE|ROAD|LANE|COURT)\b", line, re.IGNORECASE):
                return line
        return ""

    def _regex_extract(self, html: str, auction_date: Optional[date]) -> list:
        """Fallback: regex-based extraction of auction data from raw HTML."""
        sales = []
        seen = set()

        # Strip HTML tags for cleaner text parsing
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)

        # Look for patterns like "Case #: 2024-CA-001234" + dollar amounts
        pattern = re.compile(
            r"Case\s*#?\s*:?\s*([\w\-]+)"
            r".{5,500}?"
            r"Opening\s*Bid\s*:?\s*\$([\d,]+(?:\.\d{2})?)"
            r".{5,500}?"
            r"\$([\d,]+(?:\.\d{2})?)",
            re.IGNORECASE | re.DOTALL
        )

        for m in pattern.finditer(text):
            case_num = m.group(1).strip()
            if case_num in seen or len(case_num) < 5:
                continue
            seen.add(case_num)

            opening = clean_dollar(m.group(2))
            final   = clean_dollar(m.group(3))

            if final <= opening or opening < 500:
                continue

            sales.append({
                "county_id":         self.county.id,
                "county_name":       self.county.name,
                "state":             self.county.state,
                "case_number":       case_num,
                "opening_bid":       opening,
                "final_sale_price":  final,
                "gross_surplus":     final - opening,
                "sold_to":           "",
                "is_third_party":    False,  # Unknown from regex
                "auction_date":      actual_sale_date.isoformat(),
                "scraped_at":        datetime.now().isoformat(),
                "extraction_method": "regex",
            })

        return sales

    async def _go_to_next_page(self, page: Page) -> bool:
        """Click 'Next' button if present. Returns True if navigated."""
        try:
            for sel in [
                "a:has-text('NEXT')",
                "input[value='NEXT']",
                ".NEXT",
                "a[title*='Next']",
            ]:
                btn = await page.query_selector(sel)
                if btn:
                    is_disabled = await btn.get_attribute("disabled")
                    if is_disabled:
                        return False
                    await btn.click()
                    return True
        except Exception:
            pass
        return False

    async def scrape(self, days_back: int = LEAD_WINDOW_DAYS, headless: bool = True) -> list:
        """
        Main entry point. Scrapes the last N days of auctions.
        Returns list of raw sale dicts.
        """
        print(f"\n🏛  {self.county.name}, {self.county.state}")
        print(f"    Platform: {self.county.auction_platform}")
        print(f"    URL: {self.base_url}")

        if self.county.vpn_blocked:
            print(f"    ⚠️  {self.county.name} blocks VPNs. Ensure VPN is OFF.")

        # CAPTCHA/T&C counties run headed so user can solve manually
        run_headless = headless and not self.county.has_captcha and not self.county.requires_terms_agreement
        if not run_headless:
            print(f"    ⚠️  Running headed for manual assistance")

        all_sales = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=run_headless,
                # NO slow_mo in production — that injects a 250ms pause before
                # every Playwright action and silently 5-10x slows the entire
                # scrape. Scraper waits should be condition-based
                # (wait_for_selector / wait_for_url / wait_for_function),
                # never global throttles. See CLAUDE.md → "Scraper waits".
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1400, "height": 900},
            )
            page = await context.new_page()

            try:
                # Load homepage first for session establishment.
                # goto() already waits for the DOM; the extra 2000ms sleep
                # was a hold-over from the slow_mo era and adds nothing.
                await page.goto(self.base_url, timeout=30000)
                await self.handle_terms_agreement(page)

                # Scrape current page (today/most recent)
                today_sales = await self.scrape_preview_page(page)
                all_sales.extend(today_sales)
                print(f"    → Current page: {len(today_sales)} sales")

                # Scrape previous days. Break the loop if the page or its
                # context has died — otherwise page.goto() in
                # scrape_preview_page logs "Load error: ... has been closed"
                # for every remaining day. (Symptom seen on Duval and others
                # when a CAPTCHA/EULA flow tore down the page mid-run.)
                for n in range(1, days_back + 1):
                    if page.is_closed():
                        print(f"    ⚠ page is closed; aborting day-loop at n={n}")
                        break
                    check_date = date.today() - timedelta(days=n)
                    day_sales = await self.scrape_preview_page(page, check_date)
                    all_sales.extend(day_sales)
                    if day_sales:
                        print(f"    → {check_date}: {len(day_sales)} sales")
                    # Polite-pacing between day-iterations; condition-based
                    # waits replaced the slow_mo and the 3.5s preview-page
                    # sleep, so a smaller polite pause is enough to avoid
                    # hammering Grant Street's CDN.
                    await asyncio.sleep(0.5)

            except Exception as e:
                print(f"    ❌ Error: {e}")
                await self.save_diagnostic(page, "error")
            finally:
                await browser.close()

        # Deduplicate by case number (same case can appear across multiple scraped dates)
        dedup = {}
        for sale in all_sales:
            case_num = sale.get("case_number", "")
            if not case_num:
                continue
            # Keep the most complete record per case (highest field count)
            if case_num not in dedup:
                dedup[case_num] = sale
            else:
                existing = dedup[case_num]
                existing_filled = sum(1 for v in existing.values() if v)
                new_filled = sum(1 for v in sale.values() if v)
                if new_filled > existing_filled:
                    dedup[case_num] = sale

        all_sales = list(dedup.values())

        # Save output
        today = date.today().isoformat()
        out_file = RAW_DIR / f"{self.county.id}_{today}.jsonl"
        with open(out_file, "w") as f:
            for sale in all_sales:
                f.write(json.dumps(sale) + "\n")

        # Summary
        third_party = sum(1 for s in all_sales if s.get("is_third_party"))
        surplus_leads = sum(1 for s in all_sales
                           if s.get("is_third_party") and s.get("gross_surplus", 0) >= MIN_SURPLUS)
        print(f"\n    ✅ {len(all_sales)} total | {third_party} 3rd-party | {surplus_leads} ≥$10K surplus")
        print(f"    💾 {out_file.relative_to(PROJECT_ROOT)}")

        return all_sales


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

async def run_one(county_id: str, headless: bool = True, days_back: int = LEAD_WINDOW_DAYS):
    county = get_county(county_id)
    scraper = UniversalAuctionScraper(county)
    return await scraper.scrape(days_back=days_back, headless=headless)


DEFAULT_PARALLEL_SCRAPERS = 3


def _resolve_parallel_cap(headless: bool) -> int:
    """Compute the concurrency cap for run_all.

    - Headed runs ALWAYS serial (1). Some scrapers pause for input() on a
      CAPTCHA/EULA flow, and overlapping prompts from concurrent scrapers
      would be unusable.
    - Headless runs default to DEFAULT_PARALLEL_SCRAPERS (3). Cap chosen
      because 5+ OH counties share the Grant Street auction backend at
      sheriffsaleauction.ohio.gov — too much concurrency risks CDN
      rate-limiting from a single runner IP. The GitHub Actions standard
      runner (7 GB RAM, 2 vCPU) also comfortably handles 3 concurrent
      Chromium contexts (~1.2-1.5 GB combined) but starts squeezing at
      5+ with xvfb in the mix.
    - PARALLEL_SCRAPERS env var overrides (clamped 1-10) for debugging.
    """
    if not headless:
        return 1
    raw = os.environ.get("PARALLEL_SCRAPERS", "").strip()
    if raw.isdigit():
        return max(1, min(10, int(raw)))
    return DEFAULT_PARALLEL_SCRAPERS


async def _run_one_isolated(county, sem: asyncio.Semaphore, headless: bool, days_back: int):
    """Wrap a single scrape in the concurrency semaphore so the runner
    only ever sees `cap` concurrent Chromium contexts. Any exception is
    re-raised so asyncio.gather(return_exceptions=True) can capture it
    at the batch level — one county's crash never aborts the others."""
    async with sem:
        scraper = UniversalAuctionScraper(county)
        return await scraper.scrape(days_back=days_back, headless=headless)


async def run_all(headless: bool = True, days_back: int = LEAD_WINDOW_DAYS):
    """Run all counties — parallel by default, bounded by a semaphore.

    See _resolve_parallel_cap for the concurrency rationale. Per-county
    failures are isolated: a county that raises is logged and skipped;
    successful counties still write their data.
    """
    cap = _resolve_parallel_cap(headless)
    print(f"\n🏛  Parallel auction scrape — {len(ALL_COUNTIES)} counties, cap={cap}, headless={headless}")
    sem = asyncio.Semaphore(cap)
    results = await asyncio.gather(
        *(_run_one_isolated(c, sem, headless, days_back) for c in ALL_COUNTIES),
        return_exceptions=True,
    )

    all_results: dict[str, list] = {}
    failures: list[tuple[str, BaseException]] = []
    for county, r in zip(ALL_COUNTIES, results):
        if isinstance(r, BaseException):
            failures.append((county.id, r))
            all_results[county.id] = []
        else:
            all_results[county.id] = r

    if failures:
        print(f"\n⚠  {len(failures)}/{len(ALL_COUNTIES)} counties failed (other counties' data still saved):")
        for cid, exc in failures:
            print(f"     ❌ {cid}: {type(exc).__name__}: {exc}")
    return all_results


if __name__ == "__main__":
    args = sys.argv[1:]
    headless = "--headed" not in args
    days = LEAD_WINDOW_DAYS
    for a in args:
        if a.startswith("--days="):
            days = int(a.split("=")[1])

    if "--all" in args:
        print("\n🏛  Running all 10 counties...")
        results = asyncio.run(run_all(headless=headless, days_back=days))
        total = sum(len(v) for v in results.values())
        print(f"\n{'='*60}")
        print(f"  ALL COUNTIES — TOTAL: {total} sales")
        print(f"{'='*60}")
        for cid, sales in results.items():
            print(f"  {cid:20}  {len(sales):>5} sales")
    else:
        county_id = next((a for a in args if not a.startswith("--")), "miami-dade-fl")
        asyncio.run(run_one(county_id, headless=headless, days_back=days))
