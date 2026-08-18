"""
THROWAWAY — Hamilton Turnstile token-issuance probe (Phase 1, mirrors the Broward
probe). Question: does the Cloudflare Turnstile token on the Hamilton case-search
page auto-issue in HEADLESS Chrome from the GitHub Actions datacenter IP?

It performs NO search and reads NO case data. It only:
  1. lands on the homepage (warm the session), pauses
  2. navigates to the Common Pleas Civil case-search form (has the Turnstile widget)
  3. polls up to 60s for the hidden cf-chl-widget-*_response field to populate

Reports BOOLEANS only — never token values. Delete after the question is answered.
"""
from __future__ import annotations
import asyncio
import os
import tempfile
import time

from playwright.async_api import async_playwright

HOMEPAGE = "https://www.courtclerk.org/"
SEARCH = "https://www.courtclerk.org/records-search/common-pleas-civil-case-search/"
REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADLESS = os.environ.get("PROBE_HEADLESS", "1") != "0"

POPULATED_JS = r"""() => {
  let n = 0;
  for (const i of document.querySelectorAll('input')) {
    if ((i.name || '').includes('cf-chl-widget') && (i.value || '').length > 20) n++;
    if ((i.name || '') === 'cf-turnstile-response' && (i.value || '').length > 20) n++;
  }
  return n;
}"""
STATE_JS = r"""() => ({
  widgets: document.querySelectorAll('.cf-turnstile,[data-sitekey]').length,
  webdriver: navigator.webdriver,
  title: document.title,
  interstitial: /just a moment|verify you are human|attention required|checking your browser/i
      .test(document.documentElement.innerHTML.slice(0, 4000)),
})"""

_MASK = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
window.chrome=window.chrome||{runtime:{}};
Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
"""


async def attempt(pw, udd, label):
    ctx = await pw.chromium.launch_persistent_context(
        udd, headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1400, "height": 900}, user_agent=REAL_UA,
        locale="en-US", timezone_id="America/New_York", ignore_https_errors=True)
    await ctx.add_init_script(_MASK)
    out = {"label": label, "homepage_ok": False, "search_ok": False,
           "webdriver_undefined": None, "widget_rendered": False,
           "cf_interstitial": False, "token_populated": False, "seconds": None}
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=45000)
            out["homepage_ok"] = True
        except Exception as e:
            out["error"] = f"homepage: {type(e).__name__}"; return out
        await page.wait_for_timeout(5000)
        try:
            await page.goto(SEARCH, wait_until="domcontentloaded", timeout=45000)
            out["search_ok"] = True
        except Exception as e:
            out["error"] = f"search: {type(e).__name__}"; return out
        start = time.monotonic()
        while time.monotonic() - start < 60:
            try:
                if await page.evaluate(POPULATED_JS) > 0:
                    out["token_populated"] = True
                    out["seconds"] = round(time.monotonic() - start, 1)
                    break
            except Exception:
                pass
            await page.wait_for_timeout(2000)
        try:
            st = await page.evaluate(STATE_JS)
            out["webdriver_undefined"] = st["webdriver"] is None
            out["widget_rendered"] = st["widgets"] > 0
            out["cf_interstitial"] = bool(st["interstitial"])
        except Exception:
            pass
        return out
    finally:
        await ctx.close()


async def main():
    results = []
    with tempfile.TemporaryDirectory(prefix="ham_probe_") as udd:
        async with async_playwright() as pw:
            results.append(await attempt(pw, udd, "attempt-1"))
            await asyncio.sleep(25)
            results.append(await attempt(pw, udd, "attempt-2 (reused profile)"))
    print("\n===== HAMILTON TURNSTILE CI PROBE =====")
    print(f"headless={HEADLESS}\n")
    for r in results:
        print(f"--- {r['label']} ---")
        for k in ("homepage_ok", "search_ok", "webdriver_undefined", "widget_rendered",
                  "cf_interstitial", "token_populated", "seconds"):
            print(f"  {k:20}: {r.get(k)}")
        if r.get("error"):
            print(f"  error               : {r['error']}")
        print()
    print(f"VERDICT: token auto-issued headless from this IP = "
          f"{any(r.get('token_populated') for r in results)}")


if __name__ == "__main__":
    asyncio.run(main())
