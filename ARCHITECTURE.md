# SurplusIQ — Architecture

The actual module map, read from the code. For the "why" and the replication
principles, see `README.md`. For build rules, see `CLAUDE.md`.

## Pipeline

```
  scrape ──────► docket-validate ──────► enrich ──────► classify ──────► publish
  universal.py   dockets/enrich.py       propertyradar   loader.py        dashboard_data.py
  → data/raw/    → data/dockets/         → data/enriched  (Lead objects)   → docs/data/leads.json
```

Runs daily via `.github/workflows/daily-refresh.yml` (11:00 UTC), which gates on
the full test suite, scrapes → dockets → PR → dashboard, commits `docs/data/`,
and GitHub Pages serves it. Two counties (franklin-oh, orange-fl) run only
locally and merge their docket JSONL on the next cloud build.

## Module map (real files only)

```
core/
  auction/
    universal.py          # ALL 10 counties' auction scraper (RealForeclose FL, sheriffsaleauction OH)
    base.py               # auction scraper base (dead-ish; universal.py is the entry point)
  dockets/
    __init__.py           # SCRAPER_REGISTRY, MANUAL_COUNTIES, LOCAL_RUN_COUNTIES, get_scraper()
    base.py               # DocketScraper base, DocketResult/DocketEvent dataclasses,
                          #   shared detectors (kill/proof/competing-filer), classify(),
                          #   bankruptcy resolution guard
    enrich.py             # orchestrates the docket step; WORKING_DOCKET_COUNTIES; parallel run
    oh_debt.py            # OH-mortgage conservative debt (principal+interest+junior+buffer)
    cuyahoga.py           # OH: structured prayer field + decree (oh_debt)
    montgomery.py         # OH: decree PDF (oh_debt, Summit-family parser)
    summit.py             # OH: decree PDF (oh_debt)
    franklin.py           # OH: metadata-only (kill signals + owner, NO debt); LOCAL-RUN
    franklin_classify.py  # Franklin's temporal kill classifier (pure, tested)
    hamilton.py           # OH: registered but PR-fallback (portal-inaccessible)
    miami_dade.py         # FL: docket review (flag-based)
    broward.py            # FL: docket + recovery-firm kill gate (local firm list)
    duval.py              # FL: docket, 3-signal cross-confirm (own firm list)
    orange.py             # FL: manual-solve (reCAPTCHA v2); LOCAL-RUN
    manual_runner.py      # reusable human-in-the-loop / autonomous local scrape loop
  enrichment/
    propertyradar.py      # PropertyRadar API client (owner/lien/loan enrichment)
    lee_liens.py          # Lee County PR-first lien classifier
  loader.py               # merge auction+docket+PR → Lead; filters; status/tier model;
                          #   appraised-value sanity flag; docket-rescue; overbid gate
  dashboard_data.py       # build docs/data/leads.json + summary.json; kill filter;
                          #   confirmed-lead retention; local-run staleness
config/
  counties.py             # CountyConfig × 10; LEAD_WINDOW_DAYS=28; CONFIRMED_WINDOW_DAYS=90
data/
  raw/                    # auction output (per county, per day)
  dockets/                # docket output (per county, per day) + _confirmed_retained.json (gitignored)
  enriched/               # PropertyRadar output
  samples/<county>/ci/    # REAL captured dockets/decrees — acceptance-test fixtures
docs/
  index.html              # dashboard (vanilla JS)
  data/leads.json         # the published deliverable
  data/summary.json       # headline totals + per-county + local_run_status
tests/                     # 17 acceptance suites (one per county/capability + test_health backtest)
knowledge/blocked_counties.md   # investigation record for portal-limited counties
.github/workflows/
  daily-refresh.yml       # the cron (test gate → scrape → docket → PR → dashboard → commit)
  tests.yml               # standalone test suite on push
```

## Key data structures

- **`config/counties.py:CountyConfig`** — per-county URLs, clerk system, case format, flags.
- **`core/dockets/base.py:DocketResult`** — docket scrape output attached to a lead
  (prayer_amount, debt_components, kill_signals, proof_of_surplus, competing_filers,
  classification, owner_name, evidence_level, events).
- **`core/loader.py:Lead`** — the merged auction+docket+PR record with the verification
  status model (money_status, evidence_level, true_surplus, debt_source, appraised_value,
  mispriced_opener, …). `to_dict()` → the dashboard payload.

## Entry points

| Purpose | Command |
|---|---|
| Auction scrape | `python -m core.auction.universal --all` |
| Cloud docket step | `python -m core.dockets.enrich auto` (or a county-id / comma list) |
| Franklin docket (local) | `python -m core.dockets.franklin` |
| Orange docket (local) | `python -m core.dockets.orange` |
| Build dashboard | `python -m core.dashboard_data` |

`run.py` at the repo root is **dead** pre-refactor code — never run it.

## Empty scaffold packages (not used)

`core/clerks/`, `core/documents/`, `core/scoring/`, `core/surplus/`, `core/taxdeed/`,
`core/output/` are 0-line `__init__.py` placeholders from an early SOW-era design that
was never built. The real logic lives in `core/dockets/` + `core/loader.py` +
`core/dashboard_data.py`. (These are on the cleanup delete list.)
