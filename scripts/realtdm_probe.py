"""THROWAWAY RealTDM datacenter-IP probe — investigation only, no pipeline use."""
import asyncio, re
from playwright.async_api import async_playwright
UA=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
async def main():
    async with async_playwright() as pw:
        b=await pw.chromium.launch(headless=True)
        ctx=await b.new_context(user_agent=UA, viewport={"width":1400,"height":1000}); page=await ctx.new_page()
        r=await page.goto("https://miamidade.realtdm.com/public/cases/list", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3000)
        h=await page.content()
        print("LIST HTTP:", r.status if r else None, "| title:", await page.title())
        print("  recaptcha:", bool(re.search(r'recaptcha',h,re.I)), "| cloudflare:", bool(re.search(r'just a moment|cf-mitigated|challenge-platform',h,re.I)))
        # search 2026A00192
        try:
            await page.fill("#filterCaseNumber","2026A00192")
            await page.click("text=Process Search", timeout=8000); await page.wait_for_timeout(3500)
            found=await page.evaluate("()=>document.body.innerText.includes('2026A00192')")
            cid=await page.evaluate("()=>document.querySelector('tr.load-case[data-caseid]')?.getAttribute('data-caseid')")
            print("  search reached results:", found, "| case row id:", cid)
            if cid:
                await page.click(f"tr.load-case[data-caseid='{cid}']", timeout=8000); await page.wait_for_timeout(3500)
                body=await page.inner_text("body")
                print("  detail reached:", 'Case Details' in body or 'Opening Bid' in body, "| url:", page.url)
        except Exception as e:
            print("  search/detail ERROR:", type(e).__name__, str(e)[:60])
        await b.close()
asyncio.run(main())
