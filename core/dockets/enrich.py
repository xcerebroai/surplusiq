"""
SurplusIQ — Docket Enrichment CLI

Reads current leads from data/raw/<county>_<date>.jsonl, runs the docket
scraper against each one, and saves enriched results to data/dockets/.

Usage:
  python -m core.dockets.enrich cuyahoga-oh                # all Cuyahoga leads
  python -m core.dockets.enrich cuyahoga-oh --case CV25110711
  python -m core.dockets.enrich cuyahoga-oh --headed       # see browser
"""

from __future__ import annotations
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, date
from typing import Optional

# Project paths
def _find_project_root() -> Path:
    p = Path(__file__).resolve()
    for parent in [p] + list(p.parents):
        if (parent / "config" / "counties.py").exists():
            return parent
    return Path(__file__).resolve().parent.parent.parent

PROJECT_ROOT = _find_project_root()
RAW_DIR = PROJECT_ROOT / "data" / "raw"
DOCKETS_DIR = PROJECT_ROOT / "data" / "dockets"
DOCKETS_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROJECT_ROOT))
from core.dockets import get_scraper, SCRAPER_REGISTRY


def latest_raw_file(county_id: str) -> Path | None:
    files = sorted(p for p in RAW_DIR.glob(f"{county_id}_*.jsonl") if p.stat().st_size > 0)
    return files[-1] if files else None


def load_cases_from_raw(county_id: str) -> list[dict]:
    f = latest_raw_file(county_id)
    if not f:
        return []
    records = []
    with open(f) as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _case_key_for_county(county_id: str):
    """Return a callable(raw_case_number)->normalized_case_key for grouping
    parcels under the same clerk case. Falls back to identity when no
    parser is registered. Used for multi-parcel blanket-judgment
    aggregation (see run_county docstring)."""
    try:
        if county_id == "summit-oh":
            from core.dockets.summit import parse_summit_case_number
            return lambda raw: (parse_summit_case_number(raw) or {}).get("joined") or raw
        if county_id == "montgomery-oh":
            from core.dockets.montgomery import parse_montgomery_case_number
            return lambda raw: (parse_montgomery_case_number(raw) or {}).get("search_text") or raw
        if county_id == "cuyahoga-oh":
            from core.dockets.cuyahoga import parse_cuyahoga_case_number
            def _cuy_key(raw):
                p = parse_cuyahoga_case_number(raw) or {}
                if p:
                    return f"{p.get('case_prefix','CV')}-{p.get('year','????')}-{p.get('number','')}"
                return raw
            return _cuy_key
    except Exception:
        pass
    return lambda raw: raw


def _parse_sale_date(rec: dict) -> Optional[date]:
    """Auction sale date from a parcel record (for OH-mortgage interest accrual)."""
    for k in ("sale_date", "auction_date", "soldDate", "AUCTIONDATE", "sale_datetime"):
        v = rec.get(k)
        if not v:
            continue
        s = str(v).strip()
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s) or re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
        if not m:
            continue
        for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(m.group(0), fmt).date()
            except ValueError:
                continue
    return None


def _apply_oh_mortgage_debt(result: dict, rep: dict, aggregate_sale: float) -> None:
    """OH-MORTGAGE conservative debt: the scraper stored the decree COMPONENTS;
    compute the sale-date math here (interest accrual needs the auction sale date,
    which lives on the parcels). Sets prayer_amount = conservative debt and the
    classification from the verdict: killed (debt ≥ sale) / yellow surplus / yellow
    'surplus uncertain' (old decree or no sale date). No-op for non-OH-mortgage."""
    comp = result.get("debt_components") or {}
    if not comp or comp.get("is_tax_decree") or float(comp.get("principal") or 0) <= 0:
        return
    from core.dockets.oh_debt import compute_from_components

    sale_date = _parse_sale_date(rep)
    if sale_date is None:
        # Can't compute interest without the sale date → conservative + flag.
        d = compute_from_components(comp, date.today(), aggregate_sale)
        d.verdict, d.flag = "flag_uncertain", "sale date unavailable — interest not applied, manual verify"
    else:
        d = compute_from_components(comp, sale_date, aggregate_sale)

    result["prayer_amount"] = d.total_debt
    result["debt_flag"] = d.flag
    _bk = (f"principal ${d.principal:,.0f} + interest ${d.accrued_interest:,.0f} + "
           f"junior ${d.junior_liens:,.0f} + buffer ${d.buffer:,.0f}")
    if d.verdict == "killed":
        result["debt_source"] = "oh_mortgage_computed"
        result["classification"] = "killed"
        result["classification_reason"] = (
            f"conservative debt ${d.total_debt:,.0f} ≥ sale ${aggregate_sale:,.0f} — "
            f"NO surplus ({_bk})")
    elif d.verdict == "flag_uncertain":
        result["debt_source"] = "oh_mortgage_uncertain"
        result["classification"] = "yellow"
        result["classification_reason"] = f"OH-mortgage surplus uncertain — {d.flag} ({_bk})"
    else:  # surplus
        result["debt_source"] = "oh_mortgage_computed"
        result["classification"] = "yellow"
        result["classification_reason"] = (
            f"conservative surplus ${d.surplus:,.0f} (debt ${d.total_debt:,.0f} = {_bk})")


async def run_one(county_id: str, case_number: str, headless: bool, final_sale_price: float = 0.0) -> dict:
    scraper = get_scraper(county_id, headless=headless)
    result = await scraper.scrape_case(case_number)

    # Re-classify with the actual sale price if provided
    if final_sale_price > 0:
        result.classification, result.classification_reason = scraper.classify(result, final_sale_price)

    return result.to_dict()


async def run_county(county_id: str, headless: bool, only_case: str | None = None) -> dict:
    # Cloudflare-blocked counties: no docket automation. These are Eric-approved
    # PR-fallback counties — the docket step skips them and PropertyRadar
    # enrichment + the dashboard's manual-verify clerk link carry the load.
    PR_FALLBACK_COUNTIES = {"hamilton-oh"}
    if county_id in PR_FALLBACK_COUNTIES:
        print(f"⏭  {county_id} is Cloudflare-blocked with no authorized data feed. "
              f"Skipping docket scrape — routed through PropertyRadar enrichment + "
              f"the dashboard's manual-verify clerk link.")
        return {"county_id": county_id, "results": [], "skipped": True,
                "reason": "PR-fallback county (Cloudflare block)"}

    # Local-run counties cannot run in GitHub Actions (orange-fl needs a human
    # CAPTCHA solve; franklin-oh is Cloudflare-blocked from the datacenter but
    # works autonomously from a residential IP). They are scraped locally via
    # their own command and their docket JSONL merges into the dashboard normally.
    from core.dockets import LOCAL_RUN_COUNTIES, MANUAL_COUNTIES
    if county_id in LOCAL_RUN_COUNTIES:
        how = ("a MANUAL-solve county (per-search CAPTCHA)" if county_id in MANUAL_COUNTIES
               else "an autonomous LOCAL county (residential-IP only; Cloudflare blocks CI)")
        print(f"⏭  {county_id} is {how}. Skipping in the automated run — "
              f"scrape it locally with `python -m core.dockets.{county_id.split('-')[0]}`.")
        return {"county_id": county_id, "results": [], "skipped": True,
                "reason": "local-run county (run locally only)"}

    if county_id not in SCRAPER_REGISTRY:
        raise SystemExit(f"No docket scraper for {county_id}. Available: {list(SCRAPER_REGISTRY.keys())}")

    records = load_cases_from_raw(county_id)
    if not records:
        raise SystemExit(f"No raw scraper data found for {county_id}. Run the auction scraper first.")

    if only_case:
        records = [r for r in records if r.get("case_number", "").startswith(only_case[:8])]

    # ─── Group parcels by clerk-case-key for blanket-judgment aggregation ─
    # Multiple auction parcels can share ONE foreclosure judgment (e.g.
    # Summit CV-2025-02-0548 secured 12 parcels jointly and severally for
    # one $852K judgment). Per-parcel `true_surplus = sale - debt` wrongly
    # KILLS each parcel even when sum-of-sales clears the debt. Group by
    # case_key first, scrape the docket once per group, then compare the
    # AGGREGATE sale across the group to the single prayer amount.
    case_key_fn = _case_key_for_county(county_id)
    groups: dict[str, list[dict]] = {}
    for rec in records:
        key = case_key_fn(rec.get("case_number", "")) or rec.get("case_number", "")
        groups.setdefault(key, []).append(rec)

    multi_groups = [k for k, v in groups.items() if len(v) > 1]
    print(f"\n🏛  {county_id} docket scrape — {len(records)} parcels in {len(groups)} cases "
          f"({len(multi_groups)} multi-parcel groups)")
    print(f"    Headless: {headless}")
    print()

    results = []
    group_idx = 0
    for case_key, parcels in groups.items():
        group_idx += 1
        # Scrape using the first parcel's case number; the clerk lookup is
        # identical for every parcel in the group.
        rep = parcels[0]
        rep_case = rep.get("case_number", "")
        aggregate_sale = sum(float(p.get("final_sale_price") or 0.0) for p in parcels)
        aggregate_apparent = sum(float(p.get("gross_surplus") or 0.0) for p in parcels)

        if len(parcels) > 1:
            print(f"  [{group_idx}/{len(groups)}] {case_key}  ⛓ BLANKET-JUDGMENT GROUP "
                  f"({len(parcels)} parcels, aggregate sale ${aggregate_sale:,.0f})")
        else:
            print(f"  [{group_idx}/{len(groups)}] {rep_case}  "
                  f"(apparent surplus ${aggregate_apparent:,.0f})")

        # Scrape with aggregate_sale so classify() compares against pooled
        # proceeds, not just one parcel's sale.
        try:
            result = await run_one(
                county_id, rep_case,
                headless=headless,
                final_sale_price=aggregate_sale,
            )
        except Exception as e:
            print(f"      ⚠ scrape failed: {e}")
            continue

        # OH-mortgage: compute conservative debt from stored components + sale date.
        _apply_oh_mortgage_debt(result, rep, aggregate_sale)

        prayer = result.get("prayer_amount", 0.0)
        # Aggregate true_surplus: pooled sale across the case minus the
        # single judgment amount. For non-multi-parcel cases this collapses
        # back to the old (final - prayer) math.
        true_surplus = aggregate_sale - prayer if prayer > 0 else None
        cls = result.get("classification", "?")
        reason = result.get("classification_reason", "")

        if prayer > 0:
            if len(parcels) > 1:
                print(f"      prayer amount: ${prayer:,.0f}   |   aggregate true surplus: ${true_surplus:,.0f}  "
                      f"(over {len(parcels)} parcels)")
            else:
                print(f"      prayer amount: ${prayer:,.0f}   |   true surplus: ${true_surplus:,.0f}")
        else:
            print(f"      prayer amount: not found")
        print(f"      classification: {cls.upper()}  ({reason})")
        if result.get("kill_signals"):
            print(f"      🚨 kill signals: {', '.join(result['kill_signals'])}")
        if result.get("proof_of_surplus"):
            print(f"      ✅ proof: {result['proof_of_surplus']}")
        if result.get("competing_filers"):
            print(f"      ⚠️  competing: {', '.join(result['competing_filers'])}")
        print()

        # Emit ONE enriched record per ORIGINAL parcel, all sharing the same
        # docket result but each carrying its own auction-side data. This
        # keeps the dashboard's per-parcel rendering intact while the
        # classification (KILLED/RED/YELLOW/GREEN) honors the aggregated math.
        for parcel in parcels:
            parcel_record = dict(result)
            parcel_record["case_number"] = parcel.get("case_number", "")
            parcel_record["_auction_data"] = {
                "final_sale_price": float(parcel.get("final_sale_price") or 0.0),
                "opening_bid":      float(parcel.get("opening_bid") or 0.0),
                "apparent_surplus": float(parcel.get("gross_surplus") or 0.0),
                "true_surplus":     true_surplus,
                "address":          parcel.get("address", ""),
            }
            parcel_record["_blanket_group"] = {
                "case_key":             case_key,
                "parcel_count":         len(parcels),
                "aggregate_sale":       aggregate_sale,
                "aggregate_apparent":   aggregate_apparent,
            }
            results.append(parcel_record)

    # Save enriched results
    out_file = DOCKETS_DIR / f"{county_id}_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    with open(out_file, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    print(f"\n💾 Saved {len(results)} docket results to {out_file.relative_to(PROJECT_ROOT)}")

    # Summary
    print("\n" + "=" * 60)
    print("  Summary")
    print("=" * 60)
    by_class = {}
    for r in results:
        c = r.get("classification", "unknown")
        by_class[c] = by_class.get(c, 0) + 1
    for c in ["green", "yellow", "red", "killed", "unknown"]:
        if by_class.get(c):
            print(f"  {c.upper():<8}  {by_class[c]:>3} leads")
    print()

    return {"county_id": county_id, "results": results}


# Working docket scrapers — verified on real Actions runs to reach the docket
# and classify from real docket content. Used as the expansion target for the
# `all_working` shorthand below. The three OH counties extract a real prayer
# amount; Miami-Dade (FL) is flag-based (pursuable/caution/killed from docket
# events — FL debt is the opening bid, not a prayer field). Miami-Dade joined
# 2026-06-10 after run 27283288299 proved reCAPTCHA v3 passes clean from the
# GitHub Actions datacenter IP (9/9 mortgage-FC cases reached searchResults,
# zero v3 blocks).
#
# Broward (FL) was REMOVED 2026-08-13: ~Aug 8 the clerk deleted the public GET
# endpoint and put search behind Cloudflare Turnstile. The token auto-issues
# headed on a residential IP but NOT from the CI datacenter (Phase-1 probe:
# 4/4 attempts, webdriver masked, no token). Broward is now a LOCAL-RUN county
# (LOCAL_RUN_COUNTIES) on the autonomous Franklin pattern — the cron skips it and
# it runs locally via `python -m core.dockets.broward`. See knowledge notes /
# the broward.py header for the rebuilt flow.
#
# Hamilton + Franklin remain Cloudflare-blocked and excluded (PR-fallback /
# local-run). Add to this set ONLY after a scraper proves itself on real run data.
WORKING_DOCKET_COUNTIES = ["cuyahoga-oh", "montgomery-oh", "summit-oh", "miami-dade-fl", "duval-fl"]


DEFAULT_PARALLEL_DOCKETS = 3


def _resolve_parallel_cap(headless: bool) -> int:
    """Concurrency cap for parallel docket runs. Mirrors the auction
    scraper's _resolve_parallel_cap — see core/auction/universal.py.
    Headed runs ALWAYS serial (some docket scrapers may pause for input).
    Headless default = 3. PARALLEL_DOCKETS env override clamped 1–10."""
    if not headless:
        return 1
    raw = os.environ.get("PARALLEL_DOCKETS", "").strip()
    if raw.isdigit():
        return max(1, min(10, int(raw)))
    return DEFAULT_PARALLEL_DOCKETS


async def _run_county_isolated(county_id: str, sem: asyncio.Semaphore,
                                headless: bool, only_case: str | None) -> dict:
    """Wrap a single county docket run in the concurrency semaphore so the
    runner only ever sees `cap` concurrent Chromium contexts. Exceptions
    propagate up to asyncio.gather(return_exceptions=True) at the batch
    level — one county's crash never aborts the others."""
    async with sem:
        return await run_county(county_id, headless=headless, only_case=only_case)


async def run_counties_parallel(county_ids: list[str], headless: bool,
                                  only_case: str | None = None) -> dict:
    """Run docket scrapers for every county in `county_ids` concurrently,
    bounded by the same cap/env-override pattern as the auction scrapers.
    Per-county failures are isolated (return_exceptions=True): one crash
    never aborts the others, and successful counties still save their data."""
    cap = _resolve_parallel_cap(headless)
    print(f"\n🏛  Parallel docket scrape — {len(county_ids)} counties, cap={cap}, headless={headless}")
    sem = asyncio.Semaphore(cap)
    results = await asyncio.gather(
        *(_run_county_isolated(cid, sem, headless, only_case) for cid in county_ids),
        return_exceptions=True,
    )
    all_results: dict[str, dict] = {}
    failures: list[tuple[str, BaseException]] = []
    for cid, r in zip(county_ids, results):
        if isinstance(r, BaseException):
            failures.append((cid, r))
            all_results[cid] = {"county_id": cid, "results": [], "error": str(r)}
        else:
            all_results[cid] = r
    if failures:
        print(f"\n⚠  {len(failures)}/{len(county_ids)} docket counties failed (others still saved):")
        for cid, exc in failures:
            print(f"     ❌ {cid}: {type(exc).__name__}: {exc}")
    else:
        print(f"\n✅ All {len(county_ids)} docket counties completed successfully")
    return all_results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("county_id",
                    help="County identifier (e.g. cuyahoga-oh), a comma-separated "
                         "list (e.g. cuyahoga-oh,montgomery-oh,summit-oh), or the "
                         "special value 'all_working' to run every verified docket "
                         "scraper in parallel.")
    ap.add_argument("--case", help="Run against one specific case number prefix only")
    ap.add_argument("--headed", action="store_true", help="Show browser window (default: headless)")
    args = ap.parse_args()

    headless = not args.headed

    if args.county_id == "all_working":
        targets = list(WORKING_DOCKET_COUNTIES)
    elif "," in args.county_id:
        targets = [s.strip() for s in args.county_id.split(",") if s.strip()]
    else:
        targets = [args.county_id]

    if len(targets) == 1:
        # Single-county path — preserve existing serial behavior + logging
        asyncio.run(run_county(targets[0], headless=headless, only_case=args.case))
    else:
        asyncio.run(run_counties_parallel(targets, headless=headless, only_case=args.case))


if __name__ == "__main__":
    main()
