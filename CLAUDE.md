# CLAUDE.md — SurplusIQ Project Memory & Build Rules

Auto-loaded every session. Hard constraints. Overrides convenience.
See `README.md` for the full architecture and `ARCHITECTURE.md` for the module map.

## PROJECT

SurplusIQ: daily lead-intelligence finding real-estate foreclosure SURPLUS opportunities across 10 counties.
Repo: github.com/xcerebroai/surplusiq. Live dashboard: https://xcerebroai.github.io/surplusiq/ (GitHub Pages, source = `docs/`).
10 counties — FL: miami-dade, broward, duval, lee, orange. OH: cuyahoga, franklin, montgomery, summit, hamilton.

## WORKING STYLE

Direct execution. Give exact, copy-paste-able commands. Hard technical pushback when warranted is welcome.
When delivering instructions/content to paste elsewhere, deliver as ONE single block, never split.

## TESTING — GITHUB-FIRST, ALWAYS

NEVER run cloud scrapers locally to "prove" them — GitHub Actions is the only environment that proves a cloud scraper.
(Exception: the two LOCAL-RUN counties, orange-fl and franklin-oh, run ONLY locally — see below.)
Loop: edit → commit → push → trigger Daily Refresh via `gh workflow run` → diagnose from `gh run view <id> --log`.
Single-county test: `-f county=<id> -f commit_results=false`. Set `commit_results=true` only once proven.
"Run completed / green" does NOT mean "works" — verify the PUBLISHED `docs/data/leads.json`, per-lead, not the run status.
Pure transforms (`core.dashboard_data`, tests, the loader on committed data) MAY run locally — they're deterministic.

## NEVER FABRICATE DATA — CORE ANTI-FALSE-POSITIVE RULE

If a debt amount cannot be extracted (PDF not found, parse failed, ambiguous): field stays 0, lead classified unknown/apparent — NEVER green, NEVER confirmed.
A scraper that runs clean but returns a guessed number is worse than one that fails loudly. Fail loudly.
Debt comes from the document or it does not exist. No inference, no estimation.
An OH prayer/debt equal to the opening bid is the 2/3-appraised trap — reject it. The returned case number must match the searched case number.

## THREE-TIER MONEY MODEL — never conflate tiers

- `confirmed_surplus` — docket-verified WITH all proof fields. The only tier that means real money.
- `estimated_surplus` — PropertyRadar refined the surplus NUMBER (real TLB > $0). An estimate.
- `apparent_surplus` — auction math only. Unverified. The honest default.

`confirmed_surplus = $0` is a correct, honest result when nothing is docket-proven. Never inflate it.
`tests.test_verification` (the status model) must stay green.

## CLIENT-DEFINED CORRECTIONS (govern all build decisions)

1. PropertyRadar is a lien REPORT, not a kill switch. It refines/enriches; never confirms or kills.
2. OH opening bid = statutory 2/3-appraised value, NOT real debt. Real OH debt = docket decree only.
3. Florida is "one tier" — FL opening bid IS the real debt.
4. Killed leads are filtered OUT of the deliverable entirely — not badged, not kept.
5. Docket is primary for Ohio.
6. Only Cuyahoga exposes a structured prayer field. Franklin/Montgomery/Summit/Hamilton need the decree PDF (Franklin's exposes none — metadata-only).

## CURRENT STATE (2026-08, read from code)

All 9 cloud/local docket scrapers are BUILT (`SCRAPER_REGISTRY` in `core/dockets/__init__.py`: cuyahoga, miami-dade, franklin, montgomery, summit, hamilton, broward, duval, orange).

**Cloud cron** (`WORKING_DOCKET_COUNTIES` in `core/dockets/enrich.py`, run by `docket_county=auto`):
cuyahoga-oh, montgomery-oh, summit-oh (OH conservative debt) + miami-dade-fl, broward-fl, duval-fl (FL docket review).

**Local-run only** (`LOCAL_RUN_COUNTIES`, skipped by the cron — cannot run in CI):
- `franklin-oh` — autonomous, residential-IP only (Cloudflare blocks the datacenter). Metadata-only: kill signals + owner, NO debt. `python -m core.dockets.franklin`.
- `orange-fl` — per-search reCAPTCHA v2, human solves one checkbox per case. Parked on time-cost grounds. `python -m core.dockets.orange`.

**PR-fallback** (`PR_FALLBACK_COUNTIES`): `hamilton-oh` — Cloudflare managed challenge on every IP AND no authorized data feed. Permanent. See `knowledge/blocked_counties.md`.

OH conservative debt = `core/dockets/oh_debt.py` (principal + accrued interest on the correct base + junior liens + buffer), used by Cuyahoga/Summit/Montgomery. Miami-Dade joined the cron June 2026 after reCAPTCHA v3 was proven to pass headless from the Actions IP — it is NOT blocked.

Test suite: 11 files, 255 checks, all green, wired into the CI test gate (runs before every scrape).

## ENTRY POINTS

Real: `core.auction.universal` (scrape), `core.dockets.enrich` (cloud docket step), `core.dockets.franklin` / `core.dockets.orange` (local docket), `core.dashboard_data` (build dashboard). Config: `config/counties.py`.

## KEY IMPLEMENTATION RULES (hard-won — carry forward)

### State-aware surplus (FL vs OH)
- OH: `opening_bid` is 2/3-appraised, NOT debt. No decree ⇒ `true_surplus=None`, `debt_source=""`. `debt_source` values: `""`, `oh_mortgage_computed`/`oh_mortgage_uncertain` (decree math), `oh_tax_minimum_bid`, `pdf_extract:…`.
- FL: `opening_bid` IS the judgment. `_parse_lead` sets `true_surplus = sale − opening_bid`, `debt_source="fl_opening_bid"`.
- `true_surplus` alone never promotes to `confirmed_surplus` — the lead must clear `_has_required_proof` (classification green/yellow + proof_of_surplus + docket_url + sale_date + sale price > 0).

### Temporal kill logic
A kill signal only counts if it POST-DATES the completed sale (anchor on the auction sale date). Superseded pre-sale vacates/bankruptcies are noise. Implemented in `franklin_classify.py` and the bankruptcy guard in `base.py`.

### Bankruptcy resolution guard (`base.py`)
Bankruptcy alone does NOT kill. It kills only when it is the LATEST controlling status — no explicit resolution (relief from stay / dismissed / reinstated) AND no COMPLETED sale (SOLD DATE / confirmation) dated after the bankruptcy event. Otherwise the signal is dropped from the kill decision. Vocabulary ground-truthed on real OH dockets.

### OH prayer plausibility floor
A sub-$10K OH prayer/principal is fee-noise, not a judgment — reject it (Cuyahoga at scrape time; Summit/Montgomery via the loader docket-rescue floor). `oh_debt` conservative model: principal + `rate × interest_base × years(from_date→sale)` + stated junior liens + 10% buffer; watch split-balance decrees (interest accrues on a subset of principal).

### Appraised-value sanity (OH-no-debt only)
`opening_bid < 0.60 × appraised_value` flags a mispriced opener (reduced-minimum second auction / anomaly) → amber caution, gross_surplus not credible. `MISPRICED_OPENER_FLOOR` in loader. Gated on `debt_source==""` — never touches leads with real docket debt. Inert until `appraised_value` (OH RealAuction) is scraped.

### Windows — one source of truth per window (`config/counties.py`)
`LEAD_WINDOW_DAYS=28` governs BOTH the auction scrape depth and the display window (they MUST match, or leads display without re-verification). `CONFIRMED_WINDOW_DAYS=90` retains confirmed-tier leads past the standard window (past FL's 60-day junior-lien claim window, within escheat). Confirmed leads are RE-VERIFIED while in the feed and carried at their last-verified snapshot once they leave it (store: `data/dockets/_confirmed_retained.json`); a reappeared-but-no-longer-confirmed lead is dropped, not carried.

### Local-run staleness = coverage, not just age
`dashboard_data` marks a local-run county (Franklin/Orange) stale when the auction feed has cases its docket JSONL doesn't cover. A case ABSENT from the local docket renders docket-not-verified (`stale_uncovered`), NEVER checked-and-clean, regardless of other flags. `summary.local_run_status` surfaces last-scraped + stale per county.

### Killed-leads handling & display
Killed leads are filtered OUT of `leads.json` (FP-14) and written to `killed_leads.json` for the QA trail. Dashboard status badges distinguish: `📋 Verified` (docket-PDF-backed positive), `📋 Docket-checked · no kill` (Franklin metadata-only), `⚠ docket stale` (local-run uncovered), `docket-not-verified · portal-gated` (Orange), `🔒 docket-not-verified · portal-inaccessible` (Hamilton). The FP-18 $5K display floor filters near-zero surpluses (exempts OH-unverified).

### PropertyRadar (`core/enrichment/propertyradar.py`)
Token = `PROPERTYRADAR_TOKEN` env/Actions secret, NO hardcoded fallback (fails loud on missing). `estimated_surplus` requires a real this-run PR match with TLB > $0 — a $0-TLB match attaches intel fields but keeps the lead `apparent_surplus` (freshly-foreclosed properties haven't propagated through PR). Only today's `all_enriched_<today>.json` is loaded — a failed/skipped PR step drops leads back to their docket-derived tier, never leaving yesterday's badge stuck.

### Per-county recovery-firm lists are LOCAL
Broward ≠ Duval ≠ (any OH county). Never reuse one county's competing-filer/recovery-firm list for another without local ground-truth.

## SCOPE DISCIPLINE

Narrow, scoped changes. State exactly what changed and what did not.
One scraper change per verification run (bundling two makes a regression un-rootcauseable). Dashboard/loader/test/doc/YAML changes may bundle with one scraper change.
Investigation-first per county: capture real dockets, read the actual text, commit them as fixtures — never code a spec on faith.
`dashboard/` (not `docs/`) is dead. `run.py` is dead. Do not run or resurrect them.
