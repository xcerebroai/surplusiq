# SurplusIQ

Daily lead-intelligence that finds **real-estate foreclosure surplus** opportunities across 10 counties in Florida and Ohio. A foreclosure sale that fetches more than the debt owed leaves a *surplus* the former owner can claim. SurplusIQ finds those surpluses — but, crucially, it **validates them against the court docket**, not just auction math, because auction math alone produces false positives that waste a caller's time or, worse, send them after money that isn't there.

**Live dashboard:** https://xcerebroai.github.io/surplusiq/ (GitHub Pages, served from `docs/`).

This README is written for a developer who has never seen the repo — the architecture is meant to be **replicated into other states**, so the non-obvious design principles (the section at the bottom) matter as much as the code.

---

## What it does

Every foreclosure auction publishes a sale price and an opening bid. Naively, `surplus = sale − opening_bid`. That number is a trap:

- In **Florida** the opening bid *is* the final judgment amount, so the math is meaningful.
- In **Ohio** the opening bid is the statutory **2/3 of the appraised value** — a number with no relationship to the actual debt. The real debt lives in the judgment decree, and it must be extracted and computed. An OH lead without docket-extracted debt has **no credible surplus number at all**.

On top of that, a sale that looks like a surplus can already be dead: the sale was vacated, the owner filed bankruptcy, a competing surplus-recovery firm already filed a claim, or the funds were already disbursed. SurplusIQ reads the **docket** to catch these, and filters dead leads out of the deliverable entirely.

The output is a three-tier money model:

| Tier | Meaning |
|---|---|
| `confirmed_surplus` | Docket-proven surplus with all proof fields (e.g. a certificate of disbursement). The actual product. |
| `estimated_surplus` | PropertyRadar enrichment refined the number (real loan balance > $0). An estimate. |
| `apparent_surplus` | Auction math only, unverified. The honest default when no docket debt is available. |

`confirmed_surplus = $0` is a correct, honest result when nothing is docket-proven. The system never inflates it.

---

## Pipeline

```
  scrape ──────► docket-validate ──────► enrich ──────► classify ──────► publish
  (auction)      (court docket)          (PropertyRadar) (tiers/kills)    (leads.json → Pages)
```

1. **Scrape** — `core/auction/universal.py` pulls each county's foreclosure auction results (RealForeclose for FL, sheriffsaleauction.ohio.gov for OH) for the last `LEAD_WINDOW_DAYS` (28) days.
2. **Docket-validate** — `core/dockets/enrich.py` runs a per-county docket scraper against each auction case to extract the real debt (OH) and detect kill signals (all). Debt math for OH mortgages lives in `core/dockets/oh_debt.py`.
3. **Enrich** — `core/enrichment/propertyradar.py` adds owner, lien, and loan-balance data from the PropertyRadar API (used to *refine*, never to confirm or kill).
4. **Classify** — `core/loader.py` merges auction + docket + PR data, applies the state-aware surplus rules, the filters, and the verification-status model; `core/dockets/base.py:classify()` grades green/yellow/red/killed.
5. **Publish** — `core/dashboard_data.py` writes `docs/data/leads.json` + `summary.json`; the cron commits them and GitHub Pages serves the dashboard (`docs/index.html`).

### Directory layout

```
core/
  auction/universal.py      # the auction scraper (all 10 counties)
  dockets/
    base.py                 # DocketScraper base + shared kill/proof detectors + classify()
    <county>.py             # per-county docket scraper (cuyahoga, miami_dade, franklin, …)
    oh_debt.py              # OH-mortgage conservative debt extractor (principal+interest+junior+buffer)
    franklin_classify.py    # Franklin's temporal kill classifier (metadata-only county)
    manual_runner.py        # reusable human-in-the-loop / local-run scrape loop (Orange, Franklin)
    enrich.py               # orchestrates the docket step; SCRAPER_REGISTRY, county sets
  enrichment/
    propertyradar.py        # PropertyRadar API client (owner/lien/loan enrichment)
    lee_liens.py            # Lee County PR-first lien classifier
  loader.py                 # merge + filter + status model → Lead objects
  dashboard_data.py         # build docs/data/leads.json + summary.json
config/counties.py          # CountyConfig for all 10 counties; LEAD_WINDOW_DAYS / CONFIRMED_WINDOW_DAYS
data/
  raw/                      # auction scrape output (per county, per day)
  dockets/                  # docket scrape output (per county, per day)
  enriched/                 # PropertyRadar output
  samples/<county>/ci/      # REAL captured dockets/decrees — the acceptance-test fixtures
docs/
  index.html                # the dashboard (vanilla JS)
  data/leads.json           # the published deliverable
tests/                       # acceptance tests, one per county/capability (see below)
knowledge/blocked_counties.md   # investigation record for the portal-limited counties
```

---

## How to run it

### Cloud (the daily cron)

`.github/workflows/daily-refresh.yml` runs at 11:00 UTC (6 AM CDT) and does the full pipeline for the **cloud-runnable** counties. It also runs on `workflow_dispatch` with inputs (`county`, `run_dockets`, `run_pr`, `docket_county`, `commit_results`) for targeted testing. A **test gate** runs the entire suite before any scrape — a red suite blocks the publish.

```
gh workflow run daily-refresh.yml                                  # full pipeline
gh workflow run daily-refresh.yml -f county=summit-oh -f commit_results=false   # one county, no commit
```

The docket step's `docket_county=auto` expands to `WORKING_DOCKET_COUNTIES` (the verified scrapers) and runs them in parallel.

### Local-run counties (cannot run in CI)

Two counties are **local-only** and skipped by the cron (their docket JSONL still merges into the dashboard on the next cloud build):

- **`orange-fl`** — the clerk portal gates every search behind a reCAPTCHA v2 checkbox (per-search; one solve unlocks one lookup). A human solves one checkbox per case. *Currently parked* on time-cost grounds — built and runnable, not in active use.
  ```
  source .venv/bin/activate && python3 -m core.dockets.orange
  ```
- **`franklin-oh`** — Cloudflare challenges the datacenter IP but the portal is fully open from a **residential** IP with no CAPTCHA and no human. Runs autonomously — just not from GitHub Actions.
  ```
  source .venv/bin/activate && python3 -m core.dockets.franklin
  ```

Both use the reusable `core/dockets/manual_runner.py` loop (one browser session, resumable via a progress file, interrupt-safe). Local-run counties surface a **last-scraped date** on the dashboard and are marked stale when their docket data doesn't cover the current auction feed (coverage-based, not just age).

### Tests

Standalone scripts (not pytest); each prints `RESULT: N/N` and exits non-zero on failure. The CI test gate runs all of them.

```
source .venv/bin/activate
python3 -m tests.test_verification        # 32 — the status/tier model
python3 -m tests.test_summit_debt         # 13 — OH conservative debt (Summit decrees)
python3 -m tests.test_cuyahoga_debt       # 27 — OH conservative debt (Cuyahoga decrees)
python3 -m tests.test_montgomery_debt     # 31 — OH conservative debt (Montgomery decrees)
python3 -m tests.test_franklin_docket     # 24 — Franklin temporal kill classifier
python3 -m tests.test_bankruptcy_guard    # 10 — OH bankruptcy resolution guard
python3 -m tests.test_appraised_sanity    #  9 — OH mispriced-opener flag
python3 -m tests.test_miami_dade_docket   # 10 — Miami-Dade docket review
python3 -m tests.test_broward_docket      # 35 — Broward docket + recovery-firm gate
python3 -m tests.test_duval_docket        # 29 — Duval docket
python3 -m tests.test_lee_liens           # 35 — Lee PR-first lien classifier
```

Every OH-debt / docket test runs the **exact production logic** against **real captured dockets** in `data/samples/`.

---

## Per-county status (read from the code)

Registered docket scrapers (`core/dockets/__init__.py:SCRAPER_REGISTRY`): cuyahoga, miami-dade, franklin, montgomery, summit, hamilton, broward, duval, orange.
Cloud docket cron (`enrich.py:WORKING_DOCKET_COUNTIES`): cuyahoga, montgomery, summit (OH) + miami-dade, broward, duval (FL).

| County | ST | Docket validation | Runs where | Notes |
|---|---|---|---|---|
| Miami-Dade | FL | Docket review (flag-based) | cloud cron | reCAPTCHA v3 passes headless — joined June 2026 |
| Broward | FL | Docket (recovery-firm kill gate) | cloud cron | local firm list |
| Duval | FL | Docket (3-signal cross-confirm) | cloud cron | own firm list, distinct from Broward |
| Lee | FL | PR-first lien classifier | cloud cron | no docket portal; lien-based |
| Orange | FL | Manual-solve docket | **local only** | reCAPTCHA v2 per-search; parked (time cost) |
| Cuyahoga | OH | Conservative debt (structured prayer + decree) | cloud cron | $10K prayer floor |
| Montgomery | OH | Conservative debt (decree PDF) | cloud cron | Summit-family parser |
| Summit | OH | Conservative debt (decree PDF) | cloud cron | — |
| Franklin | OH | **Metadata-only** (kill signals + owner; NO debt) | **local only** | portal exposes no judgment amount; residential-IP autonomous |
| Hamilton | OH | **None** — portal-inaccessible | PR-fallback | see below |

### The two portal-limited counties (evidence in `knowledge/blocked_counties.md`)

- **Hamilton (`hamilton-oh`)** — Cloudflare **managed challenge** on every IP including residential (a human hit it 6× manually; never auto-resolves). There is also **no authorized data feed**: no Clerk API/bulk/subscription, commercial records requests capped at 10/month, Ohio Sup.R. 45(C) makes remote access discretionary, no statewide portal, and the one open subdomain is the Recorder's property records (not court dockets). **PR-fallback, permanent** — PropertyRadar enrichment + a manual-verify clerk link. Not "we couldn't beat the bot," but "no authorized data route exists."
- **Franklin (`franklin-oh`)** — IP-gated, not a hard block. From a residential IP the portal is fully scrapeable (verified end-to-end). But the portal exposes **no judgment amount** — docket entries carry only microfilm reference codes, and none of the four alternate online sources (sheriff sale notice, auction feed, a separate document system, legal-notice publication) exposes a per-case debt figure. So Franklin is **metadata-only**: it catches kill signals and extracts the owner, but produces no debt → its leads stay `apparent_surplus`.

---

## Design principles for a new-state build (the non-obvious parts)

If you replicate this into another state, these are the rules that were learned the hard way. Skipping them reintroduces the bugs they prevent.

1. **Investigation-first, per county.** Every clerk portal and every docket vocabulary is different. Before writing a scraper or a classifier, **capture several real dockets** and read the actual text. Never code a spec on faith — ground-truth the field names, the kill phrasings, and the party structure against real data, and commit those captures as test fixtures. (See any `data/samples/<county>/ci/` set and its matching `tests/test_<county>_*.py`.)

2. **FL vs OH debt are fundamentally different.**
   - **FL:** the auction opening bid **is** the final judgment amount. `surplus = sale − opening_bid` is real.
   - **OH:** the opening bid is 2/3 of the appraised value — **meaningless as debt**. Real OH debt comes from the judgment decree and must be computed conservatively: **principal + accrued interest on the correct interest-bearing base + stated junior liens + a conservative buffer** for the unquantified late-charge/advance/cost language every decree carries. Watch split-balance decrees where interest accrues on only a subset of principal. If the debt can't be extracted, the lead has **no surplus number** — it does not fall back to the fake 2/3 math.

3. **Anti-fabrication.** If a figure can't be extracted (PDF missing, parse failed, ambiguous), the field stays 0/None and the lead is `unknown`/`apparent` — **never** green/confirmed, never a guessed number. A scraper that fails loudly beats one that returns a plausible-but-wrong number.

4. **Temporal kill logic.** A kill signal only counts if it **post-dates the completed sale**. Foreclosure dockets are full of superseded events — a sale vacated and then re-sold, a bankruptcy stay lifted and the sale completed. Anchor kill detection on the auction sale date: an earlier vacate/bankruptcy that a later completed sale moved past is **not** a kill. (See `franklin_classify.py` and the bankruptcy guard in `base.py`.)

5. **Per-county recovery-firm lists are local, not shared.** The surplus-recovery firms that file competing claims differ by county (Broward's list ≠ Duval's). Never reuse one county's firm list for another without local ground-truth.

6. **Staleness is coverage, not just age, for local-run counties.** A county scraped by a manual local run (not the daily cron) can have docket data that lags the auction feed. Mark it stale when the feed contains cases the docket data doesn't cover — a case *absent* from the local docket must render docket-not-verified, never as checked-and-clean, regardless of any other flag.

7. **One source of truth per window.** `config/counties.py` owns `LEAD_WINDOW_DAYS` (28, the standard display + scrape window) and `CONFIRMED_WINDOW_DAYS` (90, confirmed-lead retention). Don't scatter window magic numbers — the scrape depth and the display window must be the same value, or leads get displayed without being re-verified.

---

## Stack

Python 3.12, Playwright (Chromium), pdfplumber (decree PDFs), BeautifulSoup, requests (PropertyRadar). GitHub Actions for the cron; GitHub Pages for the dashboard. No server — the deliverable is a static `leads.json` + a vanilla-JS dashboard.
