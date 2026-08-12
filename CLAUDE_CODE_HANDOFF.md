# SurplusIQ — Handoff & State Snapshot

Authoritative state as of 2026-08-11. Read `README.md` (architecture + replication
principles), `CLAUDE.md` (build rules), and `ARCHITECTURE.md` (module map) first.
This doc is the current-state summary and the open-work queue.

## Where it stands

- **10 counties, 9 docket scrapers built.** Cloud cron validates 6 (cuyahoga, montgomery, summit + miami-dade, broward, duval). Franklin + Orange are local-run. Hamilton is PR-fallback.
- **Three OH conservative-debt scrapers live** (Cuyahoga, Summit, Montgomery) on `core/dockets/oh_debt.py`. The old `max()`-near-keyword extractor is gone from all of them (a dead copy previously lingered in Franklin — now replaced by the metadata-only classifier).
- **Test suite: 11 files, 255 checks, all green**, wired into the CI test gate (runs before every scrape; a red suite blocks the publish).
- **First-ever `confirmed_surplus` is live** — Miami-Dade `2026-004417-CA-01`, $97,051.34, certificate of disbursement. Retained past the 28-day window via `CONFIRMED_WINDOW_DAYS=90`.

## Recently completed (the 1.0 push)

- **OH debt correctness** — Montgomery ported to `oh_debt` (Summit-family parser + HOA/junior-lien flag + $10K floor + distribution-order exclusion). All three OH docket counties now on the conservative model. This was the stated 1.0 blocker (OH debt shipping false positives) — closed.
- **28-day window reconciliation** — `LEAD_WINDOW_DAYS` in `config/counties.py` is the single source of truth for both scrape depth and display window (previously drifted: 14d scrape vs 28d display).
- **Franklin** — investigated (IP-gated, not blocked; no online judgment amount), built as an autonomous local metadata-only scraper (kill signals + owner), wired with the local-run cron skip.
- **Orange** — investigated (reCAPTCHA v2 per-search, session depth 1), built a reusable manual-solve runner, then parked on time-cost grounds (kept in repo).
- **Hamilton** — confirmed portal-inaccessible with no authorized data feed; PR-fallback, documented.
- **Appraised-value sanity flag** — OH-no-debt leads with a mispriced opener (`opening_bid < 0.60 × appraised`) flagged amber, not shown as a confident surplus.
- **Bankruptcy resolution guard** — `base.py` no longer auto-kills OH leads on a resolved bankruptcy (Miami-Dade parity; temporal, completed-sale-anchored).
- **Confirmed-lead retention** — confirmed leads carried past the window (re-verified while in the feed, snapshot once out).
- **Local-run staleness** — coverage-based; a case uncovered by a local docket run renders docket-not-verified.
- **Cleanup** — dead code removed, docs rewritten to match code, root `README.md` added.

## Open work queue

1. **Orange** — parked. Only a CAPTCHA-solving service would make it economical (none wired, none planned). Revisit only if the manual solve time becomes worthwhile.
2. **Franklin cadence** — it's local-run, so its docket data only refreshes when a human runs `python -m core.dockets.franklin`. The staleness UI flags when it lags, but there's no automated reminder. Consider a scheduled local runner if Franklin volume grows.
3. **PropertyRadar Documents endpoint** — `GET /v1/documents/{DocumentID}` returns document-level lien detail (deeper than the property-level flags). Evaluate ONLY if the property-level fields prove too coarse on real run data. Do not build pre-emptively.
4. **New-state replication** — the architecture is built to extend to other states. Follow the README's per-county investigation-first principle; do not assume any new portal or vocabulary matches an existing county.

## Known constraints

- **`data/dockets/_confirmed_retained.json`** is gitignored internal state (the confirmed-retention store) — it lives only where the dashboard is built. In cloud-only operation it persists via the committed rebuild; a confirmed lead is re-stamped each build while it's in the feed.
- **`core/auction/base.py`** has stale `days_back=7` defaults but is not imported by the live path (`universal.py` is the entry point). Low priority.
- Miami-Dade docket blocked only by reCAPTCHA v3 was a **misdiagnosis** — v3 passes headless; it joined the cron June 2026. Don't re-flag it as blocked.

## First moves for a continuing agent

1. `git fetch && git log origin/main -1` (expect a daily-refresh commit); `git status` clean.
2. Read `README.md` + `CLAUDE.md` + `ARCHITECTURE.md` + `knowledge/blocked_counties.md`.
3. Run the suite: `source .venv/bin/activate && for t in tests/test_*.py; do python3 -m tests.$(basename $t .py); done` — expect 255/255.
4. Verify the published `docs/data/leads.json` on origin, per-lead — never trust run status alone.
