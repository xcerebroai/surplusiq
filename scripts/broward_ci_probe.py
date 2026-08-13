"""
THROWAWAY — Broward Turnstile token-issuance probe (Phase 1).

Question this answers: does the Cloudflare Turnstile token on the Broward
case-search form auto-issue in a HEADLESS browser from a datacenter IP (GitHub
Actions), the way it does headed on a residential IP?

It performs NO search and reads NO case data. It only:
  1. lands on the homepage (warm the session), pauses
  2. navigates to the ANONYMOUS case-search Index
  3. polls up to 60s for the hidden `cf-turnstile-response` field to populate

Reports BOOLEANS / timings ONLY — never the token value.

Config follows the mandated recipe: persistent user-data-dir, realistic
UA/viewport/locale/timezone, --disable-blink-features=AutomationControlled,
no --disable-web-security / --no-sandbox. Headless toggled by PROBE_HEADLESS
(default "1"). Two spaced attempts reusing the same warm profile.

Delete after the question is answered.
"""

from __future__ import annotations
import asyncio
import os
import tempfile
import time

from playwright.async_api import async_playwright

HOMEPAGE = "https://www.browardclerk.org/"
INDEX = "https://www.browardclerk.org/Web2/CaseSearchECA/Index/?AccessLevel=ANONYMOUS"
REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

HEADLESS = os.environ.get("PROBE_HEADLESS", "1") != "0"

# JS: is any turnstile-response hidden input populated (value len > 20)?
# Returns a plain int count — never the token text.
POPULATED_COUNT_JS = r"""() => {
  let n = 0;
  for (const i of document.querySelectorAll('input[type=hidden]')) {
    if ((i.name || '').includes('turnstile-response') && (i.value || '').length > 20) n++;
  }
  return n;
}"""

WIDGET_STATE_JS = r"""() => ({
  widgets: document.querySelectorAll('.cf-turnstile').length,
  cfIframes: Array.from(document.querySelectorAll('iframe'))
      .filter(f => /challenges\.cloudflare/i.test(f.src || '')).length,
  webdriver: navigator.webdriver,
  title: document.title,
  // Cloudflare full-page interstitial markers ("Just a moment...", managed challenge)
  interstitial: /just a moment|attention required|checking your browser|cf-challenge|_cf_chl/i
      .test(document.documentElement.innerHTML.slice(0, 4000)),
})"""


async def one_attempt(pw, user_data_dir: str, label: str) -> dict:
    context = await pw.chromium.launch_persistent_context(
        user_data_dir,
        headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1400, "height": 900},
        user_agent=REAL_UA,
        locale="en-US",
        timezone_id="America/New_York",
        ignore_https_errors=True,
    )
    out = {
        "label": label, "homepage_ok": False, "index_ok": False,
        "webdriver_undefined": None, "widget_rendered": False,
        "cf_interstitial": False, "token_populated": False,
        "seconds_to_token": None, "final_title": "",
    }
    try:
        page = context.pages[0] if context.pages else await context.new_page()

        # 1 — homepage, human-paced pause
        try:
            await page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=45000)
            out["homepage_ok"] = True
        except Exception as e:
            out["error"] = f"homepage: {type(e).__name__}"
            return out
        await page.wait_for_timeout(5000)

        # 2 — case-search Index
        try:
            await page.goto(INDEX, wait_until="domcontentloaded", timeout=45000)
            out["index_ok"] = True
        except Exception as e:
            out["error"] = f"index: {type(e).__name__}"
            return out

        # 3 — poll up to 60s for the token to auto-issue
        start = time.monotonic()
        deadline = start + 60
        while time.monotonic() < deadline:
            try:
                if await page.evaluate(POPULATED_COUNT_JS) > 0:
                    out["token_populated"] = True
                    out["seconds_to_token"] = round(time.monotonic() - start, 1)
                    break
            except Exception:
                pass
            await page.wait_for_timeout(2000)

        try:
            st = await page.evaluate(WIDGET_STATE_JS)
            out["webdriver_undefined"] = (st["webdriver"] is None)
            out["widget_rendered"] = st["widgets"] > 0 or st["cfIframes"] > 0
            out["cf_interstitial"] = bool(st["interstitial"])
            out["final_title"] = st["title"]
        except Exception:
            pass
        return out
    finally:
        await context.close()


async def main():
    results = []
    with tempfile.TemporaryDirectory(prefix="broward_probe_") as udd:
        async with async_playwright() as pw:
            # Attempt 1 — cold-ish profile (fresh dir, but real warm-up nav)
            results.append(await one_attempt(pw, udd, "attempt-1 (warm session, same profile)"))
            # spaced pause, then attempt 2 reusing the SAME profile (clearance cookies persist)
            await asyncio.sleep(25)
            results.append(await one_attempt(pw, udd, "attempt-2 (reused profile)"))

    print("\n===== BROWARD TURNSTILE CI PROBE =====")
    print(f"headless={HEADLESS}\n")
    for r in results:
        print(f"--- {r['label']} ---")
        for k in ("homepage_ok", "index_ok", "webdriver_undefined", "widget_rendered",
                  "cf_interstitial", "token_populated", "seconds_to_token", "final_title"):
            print(f"  {k:22}: {r.get(k)}")
        if r.get("error"):
            print(f"  error                 : {r['error']}")
        print()

    any_token = any(r.get("token_populated") for r in results)
    print(f"VERDICT: token auto-issued headless from this IP = {any_token}")


if __name__ == "__main__":
    asyncio.run(main())
