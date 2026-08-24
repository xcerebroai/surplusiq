# Follow-ups (logged, not scheduled)

## Cuyahoga: lettered-suffix blanket cases (`CV24108733-A…-L`)
Surfaced by the data-health backtest (2026-08-24). A multi-parcel blanket judgment
whose parcels carry a lettered suffix (`CV24108733-A (64233)` … `-L`) returns
`scrape produced no data — check diagnostics` from the Cuyahoga docket scraper —
9 of 19 in-window parcels unverified on 2026-07-08…07-11, and `-J/-K/-L` again on
08-18. The health monitor WARNs on those days **on purpose** (a case shape the
scraper cannot resolve is a real verification gap). The fix is to teach
`core/dockets/cuyahoga.py` to strip the suffix for the clerk lookup and apply the
aggregate blanket-judgment math (`enrich._case_key_for_county`), not to suppress
the alarm. Investigation-first: capture the real docket for the base case
`CV24108733` and commit it as a fixture before coding.
