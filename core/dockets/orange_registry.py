"""
SurplusIQ — Orange (FL) Court Registry Balance lookup — CLOUD-CRON step.

Reads the clerk's Court Registry Balance (myeclerk.myorangeclerk.com/
RegistryBalance/Index) for each in-window Orange lead and writes a registry
docket record the loader merges. Unlike Broward/Hamilton (Cloudflare Turnstile,
residential-only bridge extension), the Orange registry page uses INVISIBLE
reCAPTCHA v2 that PASSES SILENTLY from the GitHub Actions datacenter IP (proven
2026-08-18, Actions run 32152421272) — so this is a normal Playwright step that
rides the daily cron. No bridge extension, no local-run.

TRANSPORT / CLASSIFY SPLIT (deliberate — for the fragility fallback):
  • lookup_registry()   = pure I/O. Drives the page, returns a parsed result or a
    LOUD failure. NEVER fabricates, never infers a balance.
  • classify_registry() = pure logic (orange_registry_classify). Only ever called
    on a validated ok=True lookup.
If the invisible reCAPTCHA ever starts challenging, swapping the transport for the
Broward/Hamilton residential bridge-extension is a wiring change here, not a rewrite.

FAIL LOUD (the flagged fragility, built for — not just noted):
  • A challenge, timeout, or unparseable page → the lookup FAILS visibly. The lead
    is left EXACTLY as it was, marked registry-not-checked; nothing is written as
    clean and no balance is invented.
  • Degradation detector: if lookups start failing/challenging across the run, the
    runner surfaces a prominent COUNTY-HEALTH warning instead of burying it — so a
    silent multi-day degradation (the Broward-for-nine-days failure mode) can't recur.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

from .orange_registry_classify import classify_registry, STAGE_DISTRIBUTED

HOMEPAGE = "https://myeclerk.myorangeclerk.com/"
REGISTRY_URL = "https://myeclerk.myorangeclerk.com/RegistryBalance/Index"
REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCKETS_DIR = PROJECT_ROOT / "data" / "dockets"

# Degradation thresholds — trip the county-health warning.
_DEGRADE_FAIL_FRACTION = 0.30      # >30% of lookups failing
_DEGRADE_MIN_ATTEMPTS = 4          # …once we've tried at least this many

_PARSE_JS = r"""() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const bt = norm(document.body.innerText);
  const noReg = /does not have an associated registry/i.test(bt);
  const bframe = document.querySelector("iframe[src*='api2/bframe']");
  const challenged = !!(bframe && bframe.offsetParent !== null
      && bframe.getBoundingClientRect().height > 20);
  let registry_type = null, balanceStr = null;
  const tbl = Array.from(document.querySelectorAll('table'))
      .find(t => /registry type/i.test(t.innerText) && /\$[\d,]+\.\d{2}/.test(t.innerText));
  if (tbl) {
    for (const r of tbl.querySelectorAll('tr')) {
      const cells = Array.from(r.cells).map(c => norm(c.innerText));
      if (cells.length >= 2 && /\$[\d,]+\.\d{2}/.test(cells[1])) {
        registry_type = cells[0]; balanceStr = cells[1];
      }
    }
  }
  const am = bt.match(/as of\s+(\d{1,2}\/\d{1,2}\/\d{4})/i);
  return { noReg, challenged, registry_type, balanceStr, as_of: am ? am[1] : '' };
}"""


def _money(s):
    if not s:
        return None
    m = re.search(r"[\d,]+\.\d{2}", s)
    return float(m.group(0).replace(",", "")) if m else None


async def lookup_registry(page: Page, case_number: str) -> dict:
    """One registry lookup on an existing page. Returns a PARSED result or a loud
    failure — never fabricates. Keys:
      ok        : bool  (a valid server response was parsed)
      found     : bool  (a registry account exists; False = no account)
      balance   : float|None
      registry_type, as_of : str
      challenged: bool  (reCAPTCHA showed a challenge — degradation signal)
      error     : str
    """
    out = {"ok": False, "found": False, "balance": None, "registry_type": "",
           "as_of": "", "challenged": False, "error": ""}
    try:
        await page.goto(REGISTRY_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(1500)
        await page.fill("input[name=caseNumber]", case_number, timeout=10000)
        await page.click("input[value='Request Balance']", timeout=10000)
    except Exception as e:
        out["error"] = f"submit: {type(e).__name__}"
        return out

    # Poll up to 30s for a REAL response (balance or no-registry) or a challenge.
    for _ in range(20):
        await page.wait_for_timeout(1500)
        try:
            st = await page.evaluate(_PARSE_JS)
        except Exception:
            continue
        if st.get("challenged"):
            out["challenged"] = True
            out["error"] = "recaptcha_challenge"
            return out
        if st.get("balanceStr"):
            bal = _money(st["balanceStr"])
            if bal is None:
                out["error"] = "balance_unparseable"; return out
            out.update({"ok": True, "found": True, "balance": bal,
                        "registry_type": st.get("registry_type") or "",
                        "as_of": st.get("as_of") or ""})
            return out
        if st.get("noReg"):
            out.update({"ok": True, "found": False, "as_of": st.get("as_of") or ""})
            return out
    out["error"] = "timeout_no_response"
    return out


def _registry_record(case_number: str, verdict: dict, lookup: dict, auction: dict) -> dict:
    """Build the docket record the loader merges (dispatched on 'registry_status')."""
    killed = verdict.get("kill")
    rec = {
        "county_id": "orange-fl",
        "case_number": case_number,
        "scraped_at": datetime.now().isoformat(),
        "case_url": REGISTRY_URL,
        # registry-specific fields (loader routes on registry_status):
        "registry_status": verdict["registry_status"],
        "registry_balance": verdict.get("registry_balance"),
        "registry_as_of": verdict.get("registry_as_of", ""),
        "registry_type": lookup.get("registry_type", ""),
        "hoa_caution": bool(verdict.get("hoa_caution")),
        "confirmed_eligible": bool(verdict.get("confirmed_eligible")),
        "classification": "killed" if killed else "",
        "kill_signals": [verdict["kill_signal"]] if killed else [],
        "classification_reason": verdict["reason"],
        "_auction_data": {
            "final_sale_price": float(auction.get("final_sale_price") or 0.0),
            "opening_bid":      float(auction.get("opening_bid") or 0.0),
            "apparent_surplus": float(auction.get("gross_surplus") or 0.0),
            "address":          auction.get("address", ""),
            "sale_date":        auction.get("auction_date") or auction.get("sale_date") or "",
        },
    }
    return rec


def _load_orange_cases() -> list:
    from .enrich import load_cases_from_raw
    return load_cases_from_raw("orange-fl")


def _parse_orange_case(raw: str):
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", (raw or "").strip()).upper()
    return cleaned if re.match(r"^\d{4}-[A-Z]{2}-\d{6}-[A-Z]$", cleaned) else None


async def run_registry(cases, headless: bool = True, only_case: str | None = None) -> dict:
    from .manual_runner import _progress_paths, _load_progress, _write_outputs
    day = datetime.now().strftime("%Y-%m-%d")
    out_file, progress_file = _progress_paths("orange-fl", day)
    progress = _load_progress(progress_file)          # raw_case -> record (resumable)

    work, skipped = [], []
    for rec in cases:
        raw = rec.get("case_number", "")
        if only_case and not raw.upper().startswith(only_case.upper()[:11]):
            continue
        cn = _parse_orange_case(raw)
        if not cn:
            skipped.append(raw); continue
        if raw in progress:
            continue
        work.append((cn, raw, rec))

    total = len(cases)
    print("\n" + "=" * 64)
    print("  ORANGE REGISTRY BALANCE — cloud lookup (invisible reCAPTCHA)")
    print("=" * 64)
    print(f"  in-window cases : {total}")
    print(f"  already checked : {len(progress)}")
    print(f"  to do this run  : {len(work)}")
    if skipped:
        print(f"  unparseable     : {len(skipped)} (stay registry-not-checked)")
    if not work:
        print("  ✓ nothing to do — all in-window cases already checked this cycle.")
        return {"county_id": "orange-fl", "checked": len(progress), "remaining": 0}

    attempts = ok_count = fail_count = challenge_count = 0
    distributed = killed = pending = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900},
                                        user_agent=REAL_UA, locale="en-US",
                                        ignore_https_errors=True)
        page = await ctx.new_page()
        try:
            await page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)          # warm session, human-paced
        except Exception:
            pass
        try:
            for i, (cn, raw, auction) in enumerate(work, 1):
                attempts += 1
                lookup = await lookup_registry(page, cn)
                if not lookup["ok"]:
                    fail_count += 1
                    if lookup.get("challenged"):
                        challenge_count += 1
                    # FAIL LOUD, leave the lead untouched (registry-not-checked).
                    print(f"  ✖ {raw}: registry-not-checked — {lookup['error']}")
                    await page.wait_for_timeout(2500)
                    continue
                ok_count += 1
                verdict = classify_registry(
                    lookup, auction.get("final_sale_price"),
                    auction.get("opening_bid"), cn)
                rec = _registry_record(raw, verdict, lookup, auction)
                progress[raw] = rec
                _write_outputs(out_file, progress_file, progress)
                st = verdict["registry_status"]
                if verdict.get("kill"):
                    killed += 1
                elif st == STAGE_DISTRIBUTED:
                    distributed += 1
                else:
                    pending += 1
                bal = verdict.get("registry_balance")
                tag = ("KILLED (funds gone)" if verdict.get("kill")
                       else st.upper() + (f" ${bal:,.0f}" if bal else ""))
                print(f"  ✓ {raw}: {tag}")
                await page.wait_for_timeout(2500)      # gentle pacing between lookups
        finally:
            await browser.close()

    # ── degradation / county-health signal (never bury it) ──
    degraded = (challenge_count > 0 or
                (attempts >= _DEGRADE_MIN_ATTEMPTS and
                 fail_count / max(attempts, 1) > _DEGRADE_FAIL_FRACTION))
    print("\n" + "=" * 64)
    print(f"  DONE: {ok_count}/{attempts} lookups OK "
          f"(distributed {distributed}, pending {pending}, killed {killed}) — "
          f"{fail_count} failed.")
    if degraded:
        print("\n  ⚠⚠ COUNTY HEALTH — ORANGE REGISTRY DEGRADED ⚠⚠")
        if challenge_count:
            print(f"     {challenge_count} lookup(s) hit a reCAPTCHA CHALLENGE — the "
                  "silent-pass may be gone. If this persists, flip the transport to "
                  "the residential bridge-extension (see module header).")
        print(f"     {fail_count}/{attempts} lookups failed this run. Investigate "
              "BEFORE the next cron so it does not degrade silently.")
        print("     (::warning:: emitted for CI visibility.)")
        print("::warning::Orange registry lookups degraded — "
              f"{fail_count}/{attempts} failed, {challenge_count} challenged.")
    print(f"  Output: {out_file}")
    print("=" * 64)
    return {"county_id": "orange-fl", "checked": len(progress),
            "ok": ok_count, "failed": fail_count, "challenged": challenge_count,
            "degraded": degraded,
            "distributed": distributed, "pending": pending, "killed": killed}


def main():
    ap = argparse.ArgumentParser(
        prog="python -m core.dockets.orange_registry",
        description="Orange Court Registry Balance cloud lookup (rides the daily "
                    "cron — invisible reCAPTCHA passes from CI). Fails loud, never "
                    "fabricates a balance.")
    ap.add_argument("--headed", action="store_true", help="show the browser (default headless)")
    ap.add_argument("--case", help="run one specific case-number prefix only")
    args = ap.parse_args()

    cases = _load_orange_cases()
    if not cases:
        print("No Orange auction data in data/raw/orange-fl_*.jsonl. "
              "Run the auction scraper first (or wait for the daily cron).")
        return
    asyncio.run(run_registry(cases, headless=not args.headed, only_case=args.case))


if __name__ == "__main__":
    main()
