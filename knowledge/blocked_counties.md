# Blocked / Special-Access Counties — Findings & Decisions

Authoritative record so the three "blocked" counties are not re-investigated
from scratch. All findings are from real-browser testing on 2026-08-11
(Playwright headed local + GitHub Actions datacenter runs). urllib/requests
probing does NOT work against CAPTCHA/Cloudflare and was not used for those.

Counties on the normal autonomous cloud cron: miami-dade, broward, duval, lee
(FL) and cuyahoga, montgomery, summit (OH). The three below are the exceptions.

---

## Orange (orange-fl) — PARKED (manual-solve, not worth the time cost)

**Portal:** `myeclerk.myorangeclerk.com/Cases/Search` (ASP.NET MVC, IIS).

**Gate:** reCAPTCHA **v2 checkbox**, required **per search**. Confirmed
`data-size` absent (= normal/checkbox, not invisible); a separate v3 score token
also loads but the gate is the v2 checkbox.

**Session depth = 1.** The decisive test: after one human solve establishes a
valid `ASP.NET_SessionId`, replaying the search POST with the session cookie +
a fresh anti-forgery token but **no captcha token** returns HTTP 200 with
`case_data_present=False, captcha_demanded=True`. The server re-validates a
captcha on **every** search — one solve unlocks exactly one lookup. Session
cookies do not carry it.

**No ungated route found:** old `/CaseSearch` → 404; `/api/cases` → SPA shell
(not a JSON API); no mobile subdomain (`m.myorangeclerk.com` doesn't resolve);
name/case/citation/business search all live in **one form behind one captcha**.

**Build status:** a full manual-solve scraper IS built and working —
`core/dockets/orange.py` (search-form mechanics verified) on the reusable
`core/dockets/manual_runner.py` (one headed session, per-case
navigate→autofill→pause-for-human-solve→submit→capture, resumable via a progress
file). Registered in `SCRAPER_REGISTRY`; in `MANUAL_COUNTIES` so the cloud cron
skips it (`enrich.run_county` returns skipped — verified). Run locally with
`python -m core.dockets.orange`.

**Decision — PARKED.** Per-case solve time (60–120s, image challenges) makes the
automation roughly as slow as a fully manual lookup, so it isn't earning its
cost. Kept in the repo, not in active use. Orange currently renders honestly as
`apparent_surplus` (FL opening-bid auction math) + PropertyRadar enrichment,
`evidence_level: auction_only`, never docket-verified.

**Only remaining path if revisited:** a CAPTCHA-solving service (2Captcha/anti-
captcha style). No solver, no proxy is wired today and none is planned.

---

## Hamilton (hamilton-oh) — MISDIAGNOSED → autonomous LOCAL build (2026-08-17)

**Portal:** `courtclerk.org` (Cloudflare **Turnstile**, not a managed challenge).

**The prior "PR-FALLBACK, permanent" call was WRONG** — it was a Playwright
artifact, exactly like Broward. Every earlier probe drove the page under
Playwright, whose CDP `Runtime.enable` instrumentation makes Cloudflare withhold
the Turnstile widget and challenge. In **genuine Chrome** (no automation on the
page) the homepage and the case-search page both load clean and the Turnstile
token **auto-issues in ~8s with zero interaction, re-issuing on every page load**
(sitekey `0x…`). A CI probe (Actions run 32083163234) confirmed the token does
NOT issue from the datacenter IP — so Hamilton is **residential-only**, same shape
as Broward.

**Flow:** search form `#cc_frm` on `/records-search/common-pleas-civil-case-search/`
→ auto-issued token → inject `sec=history` (a GET form drops the action's
`?sec=history`) → `GET /data/case_summary.php?sec=history` → the docket-event
DataTables table (Date | Description | Notes | Amount).

**No debt figure** — the "JUDGMENT AND DECREE IN FORECLOSURE" event carries no
amount, the Amount column is only the clerk cost-ledger, and documents are gated
("REDACTION MAY BE NEEDED", zero viewable links). So Hamilton is **Franklin-class**:
metadata + docket-event kill detection, leads stay `apparent_surplus`,
`debt_source` empty. Kill vocabulary is Hamilton's OWN — **"EXCESS FUNDS"** (never
"surplus"): `MOTION/RENEWED MOTION FOR DISTRIBUTION OF EXCESS FUNDS`,
`NOTIFICATION TO JUDGMENT DEBTOR OF EXCESS FUNDS`; plus bankruptcy stay
(`SUGGESTION OF BANKRUPTCY` / `NOTIFICATION OF AUTOMATIC STAY`, resolved by
`… TERMINATION/VACATING … STAY`), Rule-41(A)(2) dismissal, and sale-vacate — all
anchored on the docket `CONFIRMATION ENTRY OF SALE AND DISTRIBUTION OF PROCEEDS`.

**Decision — LOCAL-RUN, genuine-Chrome bridge extension** (Broward pattern).
In `LOCAL_RUN_COUNTIES`; removed from `enrich.PR_FALLBACK_COUNTIES`. Runner:
`python -m core.dockets.hamilton` (one-time Load-unpacked of
`core/dockets/hamilton_extension`). Classifier + 6-fixture acceptance test:
`core/dockets/hamilton_classify.py`, `tests/test_hamilton_docket.py`.

The prior no-official-feed notes still hold (no Clerk API/bulk data; records
capped at 10/month; Recorder subdomain is deeds not dockets) — they just no longer
matter, because the public case-search portal itself is reachable in genuine Chrome.

---

## Franklin (franklin-oh) — MISDIAGNOSED / IP-gated → autonomous LOCAL build (in progress)

**Portal:** `fcdcfcjs.co.franklin.oh.us/CaseInformationOnline/` (Cloudflare).

**Not a hard block — IP-reputation-gated:**
- **Residential IP:** landing 200, no challenge. Accept the disclaimer
  (`acceptDisclaimer` form, ACCEPT button) → full case-search form → search
  works. Verified case **24 CV 009172** returned a complete foreclosure record
  (KeyBank NA plaintiff, parties, attorneys, service dates, filings back to
  11/27/2024) — the entire docket renders inline. **No human interaction, no
  CAPTCHA.**
- **Datacenter IP (GitHub Actions):** landing 200, but the disclaimer-accept
  POST triggers the Cloudflare "Just a moment" managed challenge; the search
  form never loads.

**Consequence:** Franklin can run **autonomously from a local (residential-IP)
machine** — no human, unlike Orange — but **cannot run in GitHub Actions**.

**DEBT-SOURCE INVESTIGATION (2026-08-11) — no online judgment figure exists.**
Franklin's portal gives docket metadata but no debt amount, so all four
alternate online sources were checked (residential IP). None yield a scrapeable
per-case judgment/debt dollar figure:
1. **Sheriff sale notice (RealAuction `franklin.sheriffsaleauction.ohio.gov`):**
   per-property detail shows **Appraised Value** (e.g. 24CV9172 = $399,000) +
   2/3 **Opening Bid** ($266,000) + Deposit. "Judgment" appears only as the
   label "Plaintiff/Judgment Creditor" — a NAME field, no amount.
2. **Existing auction raw (`data/raw/franklin-oh_*.jsonl`):** same RealAuction
   source. We capture opening_bid but discard the appraised value; no judgment
   field exists in the feed. (Appraised value is worth capturing but is NOT the
   debt.)
3. **Separate document system:** none. CIO is the only clerk system and exposes
   no document images (Sup.R. 45(C) discretionary); the JUDGMENT ENTRY on
   24CV9172 (filed 02/13/25) carries only a microfilm ref (`0H182 C74 9`), not a
   fetchable doc. The Sheriff's official Real Estate Sales page links ONLY to
   the RealAuction site — no sheriff judgment/writ portal.
4. **Legal-notice publication:** foreclosure notices run in the Daily Reporter /
   Columbus Dispatch classifieds. Ohio sale notices (ORC 2329.26) state the
   sale terms + appraised value, not reliably the judgment principal, and the
   classifieds are a newspaper archive — not a per-case queryable source keyed
   to our cases. Not a viable automated debt source.

**CONCLUSION: Franklin is metadata-only online.** The real judgment/debt figure
is not reachable in any scrapeable per-case online form — it lives only in the
microfilm court record (in-person / paid copy). So oh_debt has no input and
Franklin leads cannot be moved out of `apparent_surplus` by a docket scrape.

**Build notes (if a metadata-only scraper is chosen):**
- Landing is a disclaimer gate; accept before the search form appears.
- Case-number format: 2-digit year / type dropdown / 6-digit zero-padded seq
  (e.g. `24` / `CV` / `009172`).
- OH county → needs the `oh_debt` conservative extractor. `franklin.py` still
  carries a **dead copy of the old max()-near-keyword extractor** that must be
  replaced with `oh_debt` (same as the Summit/Cuyahoga/Montgomery port).
- Datacenter block **confirmed 3/3** (Actions runs 31502945449 / 31503146928 /
  31503336730, 2026-08-11, spaced): every run landing HTTP 200 → disclaimer-POST
  → Cloudflare "Just a moment..." → no search form. Consistent across separate
  runners/IPs. Residential access verified reliable. Franklin autonomous-local
  build proceeds on this basis.
