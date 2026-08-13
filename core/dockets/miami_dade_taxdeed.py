"""
Miami-Dade tax-deed CLAIM-STATUS layer (RealTDM) — the tax-deed analogue of the
foreclosure docket layer. It does NOT compute or improve the surplus dollar
figure (owner-net is unreachable — see knowledge/blocked_counties.md and the
Phase-1 investigation). It reads the RealTDM case status + document list to:

  • KILL dead tax-deed leads (redeemed / vacated / bankruptcy / escheated /
    surplus already disbursed), and
  • CONFIRM a surplus is real & claimable when a Surplus Letter is posted —
    surfacing the clerk-stated POOL amount from the letter PDF, labeled pre-lien
    (still NOT owner-net).

Vocabulary ground-truthed on real RealTDM cases (data/samples/miami_dade_taxdeed/
ci/REALTDM_VOCAB.json, 2026-08-13). SCOPE: Miami-Dade only — Duval/Broward run
different tax-deed systems with their own status models.

Portal: miamidade.realtdm.com — open (no CAPTCHA, no Cloudflare), Angular app,
confirmed to work headless from a datacenter IP (cloud-runnable).
"""
from __future__ import annotations
import re
from datetime import datetime
from typing import Optional

# ── KILL statuses (uppercase, American one-L 'CANCELED'). Keyed on the status
#    string; each is a real value seen on a real case. ──
TAXDEED_KILL_STATUSES = {
    "COMPLETED - REDEEMED":      "owner redeemed the tax certificate — no sale occurred, no surplus",
    "CANCELED - VACATE SALE":    "tax-deed sale vacated",
    "CANCELED - PER BANKRUPTCY": "tax-deed case canceled due to bankruptcy",
    "COMPLETED - ESCHEATMENT":   "surplus unclaimed and escheated to the state — the money is gone",
}
# A Surplus Court Order document = distribution adjudicated → surplus disbursed.
_SURPLUS_ORDER_MARKERS = ("surplus court order",)
# A Surplus Letter document (BOTH the underscore and space variants) = surplus
# confirmed & claimable. 'RETURNED CERT SURPLUS MAIL' is supporting only.
_SURPLUS_LETTER_MARKERS = ("surplus_letter", "surplus letter")
_SURPLUS_MAIL_ONLY = "returned cert surplus mail"


def classify_tax_deed(status: str, doc_types: list) -> dict:
    """Pure claim-status classifier. Returns:
      {verdict, reason, kill_signal}
    verdict ∈ 'killed' | 'surplus_confirmed' | 'pool_pending'.

    Rules (order matters):
      1. Surplus Court Order present → KILLED (disbursed/adjudicated).
      2. A KILL status → KILLED (redeemed / vacated / bankruptcy / escheated).
      3. Surplus Letter present → SURPLUS_CONFIRMED (real, claimable; pool from PDF).
      4. Otherwise → POOL_PENDING (fresh sale, no surplus doc yet — the NORMAL,
         expected in-window state; NEVER penalized for the missing document).
    """
    st = (status or "").strip().upper()
    docs_l = [ (d or "").lower() for d in (doc_types or []) ]

    # 1. Surplus Court Order → disbursed/adjudicated → dead for our purposes.
    if any(any(m in d for m in _SURPLUS_ORDER_MARKERS) for d in docs_l):
        return {"verdict": "killed",
                "reason": "surplus distribution adjudicated (Surplus Court Order filed) — surplus disbursed",
                "kill_signal": "taxdeed_surplus_disbursed"}

    # 2. KILL status.
    if st in TAXDEED_KILL_STATUSES:
        sig = {
            "COMPLETED - REDEEMED": "taxdeed_redeemed",
            "CANCELED - VACATE SALE": "taxdeed_vacated",
            "CANCELED - PER BANKRUPTCY": "taxdeed_bankruptcy",
            "COMPLETED - ESCHEATMENT": "taxdeed_escheated",
        }[st]
        return {"verdict": "killed", "reason": TAXDEED_KILL_STATUSES[st], "kill_signal": sig}

    # 3. Surplus Letter present (underscore OR space) → confirmed & claimable.
    #    'RETURNED CERT SURPLUS MAIL' alone does NOT qualify.
    def _is_surplus_letter(d: str) -> bool:
        if _SURPLUS_MAIL_ONLY in d:
            return False
        return any(m in d for m in _SURPLUS_LETTER_MARKERS)
    if any(_is_surplus_letter(d) for d in docs_l):
        return {"verdict": "surplus_confirmed",
                "reason": "Surplus Letter posted — surplus confirmed and claimable (clerk-held pool; pre-lien)",
                "kill_signal": ""}

    # 4. Fresh / neutral — the surplus process hasn't produced a document yet.
    #    Absence of a surplus doc is EXPECTED (the letter appears ~1yr post-sale),
    #    never a negative signal.
    return {"verdict": "pool_pending",
            "reason": "tax-deed sale — no surplus document posted yet (normal; surplus process lags the sale)",
            "kill_signal": ""}


def extract_surplus_pool(pdf_text: str) -> dict:
    """Pure extractor for the Surplus Letter PDF text. Returns:
      {pool_amount: float|None, claim_deadline_days: int|None}
    Anchored on the real letter phrasing:
      'A surplus of $58,504.73 will remain and be held by this office for a
       period of (not less than) ninety (90) days ...'
    Anti-fabrication: no anchor match → None (never guess)."""
    text = re.sub(r"\s+", " ", pdf_text or "")
    pool = None
    m = re.search(r"surplus of\s+\$?\s*([\d,]+\.\d{2})", text, re.I)
    if m:
        try:
            pool = float(m.group(1).replace(",", ""))
        except ValueError:
            pool = None
    days = None
    md = re.search(r"period of\s*\(?\s*not less than\s*\)?\s*(?:\w+\s*)?\(?(\d{1,3})\)?\s*days", text, re.I)
    if not md:
        md = re.search(r"\((\d{1,3})\)\s*days", text)
    if md:
        try:
            days = int(md.group(1))
        except ValueError:
            days = None
    return {"pool_amount": pool, "claim_deadline_days": days}


# ── Live scraper (Playwright). Pure logic above is unit-tested; this drives the
#    open RealTDM portal to fetch (status, doc_types, surplus-letter PDF text). ──
LIST_URL = "https://miamidade.realtdm.com/public/cases/list"


class MiamiDadeTaxDeedScraper:
    county_id = "miami-dade-fl"

    def __init__(self, headless: bool = True):
        self.headless = headless

    async def _submit(self, page):
        try:
            async with page.expect_navigation(wait_until="networkidle", timeout=20000):
                await page.evaluate("()=>{const b=document.querySelector('button.filters-submit');if(b)b.click();}")
        except Exception:
            await page.wait_for_timeout(3000)
        await page.wait_for_timeout(1400)

    async def scrape_case(self, case_number: str) -> dict:
        """Fetch (status, doc_types, surplus pool) for one tax-deed case. Uses a
        fresh browser context so the RealTDM filter panel is in its initial
        (visible) state. Returns a claim-status record ready to write as JSONL."""
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
        result = {
            "county_id": self.county_id, "case_number": case_number,
            "scraped_at": datetime.now().isoformat(), "source": "realtdm",
            "taxdeed_status": "", "taxdeed_verdict": "pool_pending",
            "taxdeed_reason": "", "kill_signal": "",
            "surplus_pool_amount": None, "claim_deadline_days": None,
            "classification": "", "classification_reason": "",
        }
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            ctx = await browser.new_context(
                viewport={"width": 1400, "height": 1400},
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"))
            page = await ctx.new_page()
            try:
                await page.goto(LIST_URL, wait_until="networkidle", timeout=45000)
                await page.wait_for_timeout(900)
                await page.fill("#filterCaseNumber", case_number)
                await self._submit(page)
                cid = await page.evaluate(
                    "()=>document.querySelector('tr.load-case[data-caseid]')?.getAttribute('data-caseid')")
                if not cid:
                    result["taxdeed_reason"] = "case not found in RealTDM"
                    return result
                await page.click(f"tr.load-case[data-caseid='{cid}']", timeout=10000)
                await page.wait_for_timeout(2800)
                body = await page.inner_text("body")
                m = re.search(r"Status:\s*\n?\s*([A-Z][A-Za-z0-9 \-()/]+)", body)
                status = m.group(1).strip() if m else ""
                doc_types = []
                try:
                    await page.click("text=Documents", timeout=6000)
                    await page.wait_for_timeout(2200)
                    # Collect each document row's FULL text (any row that names a
                    # .pdf). The classifier checks its markers as substrings, so
                    # full-row text reliably catches multi-word doc types like
                    # 'Surplus Court Order' whose complex filename defeated a
                    # leading-label regex (would misread a disbursed case as
                    # merely confirmed).
                    doc_types = await page.evaluate(
                        r"""()=>{const out=[];document.querySelectorAll('tr').forEach(r=>{
                          const t=(r.innerText||'').replace(/\s+/g,' ').trim();
                          if(/\.pdf/i.test(t)) out.push(t);});return [...new Set(out)];}""")
                except PWTimeout:
                    pass
                result["taxdeed_status"] = status
                verdict = classify_tax_deed(status, doc_types)
                result.update({"taxdeed_verdict": verdict["verdict"],
                               "taxdeed_reason": verdict["reason"],
                               "kill_signal": verdict["kill_signal"]})
                # For a confirmed surplus, fetch the Surplus Letter PDF → pool.
                if verdict["verdict"] == "surplus_confirmed":
                    pdf_text = await self._fetch_surplus_letter_text(page)
                    if pdf_text:
                        ext = extract_surplus_pool(pdf_text)
                        result["surplus_pool_amount"] = ext["pool_amount"]
                        result["claim_deadline_days"] = ext["claim_deadline_days"]
                # Map to the docket classification the loader consumes.
                if verdict["verdict"] == "killed":
                    result["classification"] = "killed"
                    result["classification_reason"] = "tax-deed: " + verdict["reason"]
                return result
            except Exception as e:
                result["taxdeed_reason"] = f"scrape error: {type(e).__name__}: {e}"
                return result
            finally:
                await browser.close()

    async def run(self, headless: bool = True) -> dict:
        """Scrape RealTDM claim-status for every in-window Miami-Dade tax-deed
        SOLD lead in the latest raw, and write data/dockets/
        miami-dade-fl-taxdeed_<date>.jsonl (merged by the loader like any docket
        file). Redeemed non-sales (sold_to blank) are skipped — the loader's
        redeemed guard already handles them and they carry no surplus."""
        import json
        from datetime import date
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        raw_dir = root / "data" / "raw"
        files = sorted(raw_dir.glob("miami-dade-fl_*.jsonl"))
        if not files:
            print("No Miami-Dade raw data found."); return {"scraped": 0}
        seen = {}
        for line in files[-1].open():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if (r.get("auction_type") or "").strip().upper() != "TAXDEED":
                continue
            if not r.get("is_third_party"):        # skip redeemed / non-sales
                continue
            seen[r.get("case_number", "")] = r
        cases = [c for c in seen if c]
        print(f"🏷  Miami-Dade tax-deed claim-status — {len(cases)} sold tax-deed case(s)")
        results = []
        for i, cn in enumerate(cases, 1):
            print(f"  [{i}/{len(cases)}] {cn}")
            rec = await self.scrape_case(cn)
            print(f"     → {rec['taxdeed_status']!r}: {rec['taxdeed_verdict']}"
                  + (f" pool ${rec['surplus_pool_amount']:,.2f}" if rec.get("surplus_pool_amount") else ""))
            results.append(rec)
        out_dir = root / "data" / "dockets"; out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"miami-dade-fl-taxdeed_{date.today().isoformat()}.jsonl"
        with out.open("w") as f:
            for r in results:
                f.write(json.dumps(r) + "\n")
        print(f"💾 wrote {len(results)} tax-deed claim-status records → {out.name}")
        return {"scraped": len(results), "out_file": str(out)}

    async def _fetch_surplus_letter_text(self, page) -> str:
        """Download the Surplus Letter PDF from the (already-open) Documents tab
        and return its text. Best-effort — returns '' on any failure."""
        import io
        try:
            async with page.expect_download(timeout=15000) as dl:
                await page.click("tr:has-text('SURPLUS_LETTER') >> text=View", timeout=6000)
            d = await dl.value
            path = await d.path()
            import pdfplumber
            txt = ""
            with pdfplumber.open(path) as pdf:
                for pg in pdf.pages[:3]:
                    txt += (pg.extract_text() or "") + "\n"
            return txt
        except Exception:
            return ""


def main():
    import asyncio, argparse
    ap = argparse.ArgumentParser(description="Miami-Dade tax-deed claim-status (RealTDM)")
    ap.add_argument("--headed", action="store_true", help="show browser (default headless)")
    ap.add_argument("--case", help="scrape a single case and print (no file write)")
    args = ap.parse_args()
    s = MiamiDadeTaxDeedScraper(headless=not args.headed)
    if args.case:
        import json
        print(json.dumps(asyncio.run(s.scrape_case(args.case)), indent=1))
    else:
        asyncio.run(s.run(headless=not args.headed))


if __name__ == "__main__":
    main()
