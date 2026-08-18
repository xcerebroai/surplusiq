"""
THROWAWAY — Orange Registry Balance invisible-reCAPTCHA CI probe.

Question: on myeclerk.myorangeclerk.com/RegistryBalance/Index, does the INVISIBLE
reCAPTCHA v2 pass SILENTLY on the "Request Balance" submit from a GitHub Actions
datacenter IP, or does it throw a challenge? This is the exact thing the cloud
cron would do (Playwright from the datacenter), so it decides cloud-cron vs
local-run for Orange.

Reports BOOLEANS only — never the balance figure or token value. Delete after the
question is answered.
"""
from __future__ import annotations
import asyncio
import os
import tempfile
import time

from playwright.async_api import async_playwright

HOMEPAGE = "https://myeclerk.myorangeclerk.com/"
REGISTRY = "https://myeclerk.myorangeclerk.com/RegistryBalance/Index"
CASE = "2025-CA-003157-O"          # a current Orange lead known to HAVE a registry account
REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADLESS = os.environ.get("PROBE_HEADLESS", "1") != "0"

_MASK = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
window.chrome=window.chrome||{runtime:{}};
Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
"""

# A REAL server response = a dollar balance figure OR the no-registry message
# (NOT just the "Registry Type / Balance" header template, which is present in the
# DOM before any lookup and caused a t=0 false positive in v1). A visible bframe =
# a challenge (failed silent pass).
POLL_JS = r"""() => {
  const bt = (document.body.innerText || '');
  const hasBalance = /registry type/i.test(bt) && /\$[\d,]+\.\d{2}/.test(bt);
  const noRegistry = /does not have an associated registry/i.test(bt);
  const bframe = document.querySelector("iframe[src*='api2/bframe']");
  const challengeVisible = !!(bframe && bframe.offsetParent !== null
      && bframe.getBoundingClientRect().height > 20);
  return { hasBalance, noRegistry, challengeVisible };
}"""


async def attempt(pw, udd, label):
    ctx = await pw.chromium.launch_persistent_context(
        udd, headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1400, "height": 900}, user_agent=REAL_UA,
        locale="en-US", timezone_id="America/New_York", ignore_https_errors=True)
    await ctx.add_init_script(_MASK)
    out = {"label": label, "homepage_ok": False, "registry_ok": False,
           "webdriver_undefined": None, "recaptcha_invisible": None,
           "submitted": False, "baseline_had_result": None,
           "passed_silently": False, "challenge_shown": False, "seconds": None}
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=45000)
            out["homepage_ok"] = True
        except Exception as e:
            out["error"] = f"homepage: {type(e).__name__}"; return out
        await page.wait_for_timeout(4000)
        try:
            await page.goto(REGISTRY, wait_until="domcontentloaded", timeout=45000)
            out["registry_ok"] = True
        except Exception as e:
            out["error"] = f"registry: {type(e).__name__}"; return out
        await page.wait_for_timeout(3000)

        try:
            anchor = await page.query_selector("iframe[src*='api2/anchor']")
            if anchor:
                src = await anchor.get_attribute("src")
                out["recaptcha_invisible"] = ("size=invisible" in (src or ""))
            out["webdriver_undefined"] = (await page.evaluate("() => navigator.webdriver")) is None
        except Exception:
            pass

        # baseline BEFORE submit — should have neither a $ balance nor the no-reg msg.
        try:
            base = await page.evaluate(POLL_JS)
            out["baseline_had_result"] = bool(base.get("hasBalance") or base.get("noRegistry"))
        except Exception:
            pass

        try:
            await page.fill("input[name=caseNumber]", CASE, timeout=8000)
            await page.click("input[value='Request Balance']", timeout=8000)
            out["submitted"] = True
        except Exception as e:
            out["error"] = f"submit: {type(e).__name__}"; return out

        start = time.monotonic()
        while time.monotonic() - start < 30:
            await page.wait_for_timeout(1500)          # min wait first — no t=0 match
            try:
                st = await page.evaluate(POLL_JS)
            except Exception:
                st = {}
            if st.get("challengeVisible"):
                out["challenge_shown"] = True
                out["seconds"] = round(time.monotonic() - start, 1); break
            if st.get("hasBalance") or st.get("noRegistry"):
                out["passed_silently"] = True
                out["seconds"] = round(time.monotonic() - start, 1); break
        return out
    finally:
        await ctx.close()


async def main():
    results = []
    with tempfile.TemporaryDirectory(prefix="orange_probe_") as udd:
        async with async_playwright() as pw:
            results.append(await attempt(pw, udd, "attempt-1"))
            await asyncio.sleep(25)
            results.append(await attempt(pw, udd, "attempt-2 (reused profile)"))

    print("\n===== ORANGE REGISTRY invisible-reCAPTCHA CI PROBE =====")
    print(f"headless={HEADLESS}\n")
    for r in results:
        print(f"--- {r['label']} ---")
        for k in ("homepage_ok", "registry_ok", "recaptcha_invisible",
                  "webdriver_undefined", "submitted", "baseline_had_result",
                  "passed_silently", "challenge_shown", "seconds"):
            print(f"  {k:20}: {r.get(k)}")
        if r.get("error"):
            print(f"  error               : {r['error']}")
        print()
    print("VERDICT: invisible reCAPTCHA passed silently from this IP = "
          f"{any(r.get('passed_silently') and not r.get('challenge_shown') for r in results)}")


if __name__ == "__main__":
    asyncio.run(main())
