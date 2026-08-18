"""
SurplusIQ — Dashboard Data Exporter (HARDENING PASS — false-positive resistant)

Generates two JSON files the dashboard HTML reads:
  docs/data/leads.json    — all qualifying leads + verification status model
  docs/data/summary.json  — separated confirmed/estimated/apparent totals

Core rule (strict separation of confidence layers):
  • Auction math alone        → apparent_surplus  (never confirmed)
  • PropertyRadar enrichment  → estimated_surplus (PR cannot confirm surplus)
  • Docket + proof fields     → confirmed_surplus (only path to confirmed)

The headline never presents apparent surplus as confirmed money.
killed/red leads are kept in leads.json (QA trail) but excluded from the
confirmed total, pipeline-ready count, and top-confirmed list.

Usage:
    python -m core.dashboard_data
"""

from __future__ import annotations
import json
import re as _re
from datetime import datetime, date, timedelta
from pathlib import Path

from config.counties import LEAD_WINDOW_DAYS
from core.loader import (
    load_all_leads, get_summary, PROJECT_ROOT,
    _POSITIVE_CLASSIFICATIONS, _NEGATIVE_CLASSIFICATIONS,
    _load_docket_data, _normalize_case_for_lookup, derive_owner_from_docket,
)


_RAW_DIR = PROJECT_ROOT / "data" / "raw"


def _load_latest_raw_auction() -> dict:
    """Return {(county_id, normalized_case_number): record} from latest raw JSONL per county."""
    county_latest: dict[str, Path] = {}
    for f in sorted(_RAW_DIR.glob("*.jsonl")):
        prefix = _re.sub(r"_\d{4}-\d{2}-\d{2}\.jsonl$", "", f.name)
        county_latest[prefix] = f  # sorted order → later date wins
    lookup = {}
    for county_id, fpath in county_latest.items():
        try:
            with open(fpath) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    cid = d.get("county_id") or county_id
                    case = d.get("case_number", "")
                    if not case:
                        continue
                    norm = _normalize_case_for_lookup(case)
                    lookup[(cid, norm)] = d
        except Exception:
            continue
    return lookup


# Classifications that count as "docket-verified" — explicit allowlist.
# "unknown" is excluded because it means the scrape ran but produced no usable data.
DOCKET_VERIFIED_CLASSIFICATIONS = {"green", "yellow", "red", "killed"}


def _load_pr_enrichment() -> dict:
    """Load TODAY's PropertyRadar enrichment file only.

    Hard rule (FP-7 anti-stale): a lead may only be tagged estimated_surplus
    when this-run PR data backs it. We refuse to merge enrichment older than
    today — that prevents a stale tier badge from surviving a failed or
    skipped PR step. If no file dated today exists, return an empty lookup
    so every lead drops to apparent_surplus (or its docket-derived tier).
    """
    enriched_dir = PROJECT_ROOT / "data" / "enriched"
    if not enriched_dir.exists():
        return {}

    from datetime import date as _date
    today_str = _date.today().isoformat()
    todays_file = enriched_dir / f"all_enriched_{today_str}.json"
    if not todays_file.exists():
        print(f"   📡 PropertyRadar enrichment: no file for today ({today_str}) — "
              f"all leads will use docket-derived or apparent_surplus tier")
        return {}

    print(f"   📡 PropertyRadar enrichment: loading {todays_file.name} (today only)")
    try:
        with open(todays_file) as f:
            records = json.load(f)
    except Exception as e:
        print(f"   ⚠ Failed to load PR enrichment: {e}")
        return {}

    lookup = {}
    matched = 0
    for r in records:
        # Only keep records the PR client actually matched this run.
        # Records with pr_match=False are no-match leads that should drop
        # back to apparent_surplus.
        if not r.get("pr_match"):
            continue
        key = (r.get("county_id", ""), r.get("case_number", ""))
        lookup[key] = r
        matched += 1

    print(f"   ✓ {len(records)} PR enrichment records loaded, {matched} matched this run")
    return lookup


def _apply_pr_to_payload(payload_lead: dict, pr_record: dict) -> dict:
    """Merge PropertyRadar enrichment fields onto a payload lead dict."""
    if not pr_record or not pr_record.get("pr_match"):
        return payload_lead

    pr_fields = [
        "pr_match", "pr_radar_id", "pr_owner_name",
        "pr_mailing_address", "pr_mailing_city", "pr_mailing_state", "pr_mailing_zip",
        "pr_estimated_value", "pr_total_loan_balance", "pr_available_equity",
        "pr_first_loan_amount", "pr_first_loan_type", "pr_second_loan_amount",
        "pr_years_owned", "pr_owner_occupied", "pr_in_tax_delinquency",
        "pr_involuntary_lien", "pr_property_type", "pr_year_built",
        "pr_sqft", "pr_bedrooms", "pr_bathrooms",
        "real_surplus_estimate", "debt_coverage_ratio", "is_clean_surplus",
        "enrichment_status",
        # Lee PR-first lien-consumes-surplus verdict (Lee only)
        "lee_lien_classification", "lee_lien_is_hard_kill", "lee_lien_amount",
        "lee_lien_source", "lee_owner_timing_suspect", "lee_lien_reason",
        "lee_lien_flags",
    ]
    for field in pr_fields:
        if field in pr_record:
            payload_lead[field] = pr_record[field]

    return payload_lead


def _surplus_for_payload(payload_lead: dict) -> tuple:
    """
    Return (amount, bucket) where bucket is one of:
      confirmed_surplus | estimated_surplus | apparent_surplus | no_surplus

    HARDENING (FP-6): the bucket is driven by money_status assigned by the
    loader's verification status model — NOT by classification alone, and
    NOT by trusting true_surplus blindly. The status model has already run
    the proof-field gate, so this function only reads its verdict.
    """
    money_status = (payload_lead.get("money_status") or "unknown").strip().lower()

    # FP-18 Item 2 (display bug fix): when a lead has BOTH a real docket
    # prayer (≥ $10K, debt_source=docket_prayer/pdf_extract:*) AND a
    # positive true_surplus, use true_surplus as the displayed amount
    # REGARDLESS OF TIER. Previously docket-checked YELLOW/RED Summit
    # leads (which sit in apparent_surplus because no proof-of-disbursement
    # filing yet) returned gross_surplus = sale - opening_bid, which is
    # meaningless OH arithmetic. CV2025094689 showed $0 instead of its
    # real $11,461 true_surplus; CV2025115614 showed $2,800 instead of
    # its real $17,532. Both jump above $10K with this fix.
    prayer = float(payload_lead.get("prayer_amount") or 0)
    ts = payload_lead.get("true_surplus")
    debt_src = payload_lead.get("debt_source") or ""
    state = (payload_lead.get("state") or "").upper()
    # A docket-extracted prayer is "usable" only with a recognized real-debt
    # source AND a plausible (≥ $10K) amount AND a positive surplus. NOTE
    # 'prayer_field' (Cuyahoga's structured prayer) is now recognized — it was
    # the gap that hid Cuyahoga's real true_surplus behind opening-bid math.
    has_real_docket_debt = (
        prayer >= 10000.0
        and ts is not None
        and ts > 0
        and (debt_src.startswith("docket_prayer")
             or debt_src.startswith("pdf_extract:")
             or debt_src == "oh_mortgage_computed")   # Summit/Cuyahoga conservative debt
        # NB: 'prayer_field' (Cuyahoga complaint prayer) is NO LONGER "real debt" —
        # it's principal-only (no interest/costs), so it now flags uncertain (below).
        # When a decree IS parsed, the scraper sets debt_source='oh_mortgage_decree'
        # → enrich → 'oh_mortgage_computed', which IS real debt.
    )
    if has_real_docket_debt:
        return (float(ts), money_status if money_status in
                ("confirmed_surplus", "estimated_surplus", "apparent_surplus")
                else "apparent_surplus")

    if money_status == "confirmed_surplus":
        ts = payload_lead.get("true_surplus")
        return (float(ts) if ts is not None else 0.0, "confirmed_surplus")

    # FL COUNTY-COURT / HOA-lien foreclosure: the plaintiff is an HOA/condo
    # association, the opening bid is the small association lien, and the SENIOR
    # MORTGAGE SURVIVES the sale (buyer takes subject to it). So sale − opening_bid
    # is NOT a real surplus — the senior debt is unknown. Return None so the
    # phantom can't pollute the KPI totals, the sort, or the surplus floor; the
    # dashboard renders a caution with the reason. This is reached ONLY when the
    # lead is not docket-confirmed (the has_real_docket_debt + confirmed_surplus
    # returns above already handled real-debt/proven cases) — so a rare
    # free-and-clear HOA case that clears the docket proof gate still confirms.
    # Case-type-gated (not ratio-gated): the confirmed circuit lead at ratio 0.030
    # is never touched. Same shape as oh_debt's HOA/junior-lien flag.
    if payload_lead.get("fl_county_court"):
        return (None, "fl_hoa_unverified")

    # FL TAX DEED (FS 197.582) — a DIFFERENT mechanism from the HOA case. The
    # REDEEMED guard runs first: a redeemed tax deed is a NON-SALE (owner paid
    # the back taxes) → no surplus figure at all. A real tax-deed sale has a
    # surplus POOL (sale − tax opening bid) that IS real and clerk-held, but it
    # is distributed to lienholders (incl. the former mortgagee, whose mortgage
    # the tax deed extinguished into a claim) BEFORE the former owner, so
    # owner-net is unknown from auction data. Return None (excluded from the
    # owner-surplus totals + floor); the dashboard shows the pool from
    # gross_surplus with the lien caveat. Reached only when NOT docket-confirmed.
    if payload_lead.get("fl_tax_deed_redeemed"):
        return (None, "fl_taxdeed_redeemed")
    if payload_lead.get("fl_tax_deed"):
        # RealTDM claim-status confirmed a Surplus Letter is posted → surface the
        # CLERK-stated pool (better than auction math), still labeled pre-lien and
        # excluded from owner-surplus totals. Bucket distinguishes it for the UI.
        if payload_lead.get("taxdeed_verdict") == "surplus_confirmed":
            return (None, "fl_taxdeed_confirmed_pool")
        return (None, "fl_taxdeed_pool")

    # OH TAX (RC 5721): opening_bid IS the Minimum Bid = real tax debt, so
    # true_surplus = sale − opening is valid (set in _parse_lead with
    # debt_source='oh_tax_minimum_bid'). Display it as apparent surplus.
    if debt_src == "oh_tax_minimum_bid" and ts is not None and ts > 0:
        return (float(ts), "apparent_surplus")

    # OH MORTGAGE with computed debt but UNCERTAIN surplus (old decree with no
    # parseable interest rate, or no sale date) — a real surplus may exist but we
    # can't state it confidently, so show it like the OH-no-debt unverified
    # treatment ("— surplus uncertain, manual review"), NOT a confident green.
    # 'oh_mortgage_uncertain' = decree computed but no rate (old decree) or no sale
    # date. 'prayer_field' = Cuyahoga complaint prayer (principal only, decree not
    # parseable) — both are a real principal but an INCOMPLETE debt, so neither may
    # show a confident green surplus: render "— surplus uncertain, manual review".
    if debt_src in ("oh_mortgage_uncertain", "prayer_field"):
        return (None, "oh_uncertain")

    # OH MORTGAGE without usable docket debt → UNVERIFIED. The opening bid is the
    # statutory 2/3-appraised value, NOT real debt, so it must NEVER stand in as a
    # surplus figure (Eric's rule). Return None so the number can't pollute the
    # sort, the KPI totals, or the surplus floor; the dashboard renders "—" and a
    # manual-verify docket link. Covers no-docket counties (Franklin/Hamilton) and
    # sub-$10K / implausible-prayer cases. FL is never OH → unaffected.
    if state == "OH":
        return (None, "oh_unverified")

    if money_status == "estimated_surplus":
        # PR-refined surplus only when TLB > $0 — the same FP-8 rule that
        # _reassign_status_after_pr enforces. Defensive double-check here
        # so docket-reviewed-but-unproven leads (which the loader also tags
        # estimated_surplus when has_pr is True) can't slip through with
        # an unrefined number.
        tlb = float(payload_lead.get("pr_total_loan_balance") or 0)
        pr = payload_lead.get("real_surplus_estimate")
        if tlb > 0 and pr is not None:
            return (float(pr), "estimated_surplus")
        # No real refinement — surplus number is auction math.
        return (float(payload_lead.get("gross_surplus", 0.0)), "apparent_surplus")

    if money_status == "no_surplus":
        return (0.0, "no_surplus")

    # apparent_surplus or unknown => auction math only
    return (float(payload_lead.get("gross_surplus", 0.0)), "apparent_surplus")


def _reassign_status_after_pr(payload: dict) -> None:
    """
    Re-run the verification status model on a payload dict AFTER PR enrichment
    has been merged. Three-tier model honesty (FP-8):

      estimated_surplus  REQUIRES pr_total_loan_balance > 0 — i.e. PR actually
                         refined the surplus arithmetic. A PR match with
                         TotalLoanBalance == $0 means PR couldn't refine
                         the math (data lag — freshly-foreclosed property
                         hasn't propagated through PR's source data yet),
                         so the lead drops to apparent_surplus. The PR
                         data (owner, lien flags, tax_delinquency, distress)
                         stays attached as INTEL FIELDS on the lead.

      This NEVER upgrades a lead to confirmed_surplus — PR cannot confirm
      surplus (spec Part 4).

      Docket-classified green/yellow leads are ALSO re-tiered here (regression
      fix): the loader can't see PR's loan balance, so it leaves them provisional-
      apparent; this function is the sole place estimated_surplus is granted, and
      only on a matched PR record with TLB > 0. owner_name presence never counts.
    """
    classification = (payload.get("classification") or "").strip().lower()

    # Killed/red are final — PR never changes a reviewed-negative verdict.
    if classification in _NEGATIVE_CLASSIFICATIONS:
        return
    # Confirmed (passed the proof-of-surplus gate) is never downgraded by PR.
    if (payload.get("money_status") or "").strip().lower() == "confirmed_surplus":
        return
    if not payload.get("pr_match"):
        return

    # estimated_surplus REQUIRES real PR refinement: a matched record with a
    # non-zero loan balance (FP-8). TLB == 0 (PR data lag on fresh foreclosures)
    # stays apparent; the PR intel fields remain attached either way.
    tlb = float(payload.get("pr_total_loan_balance") or 0)
    refined = tlb > 0

    if classification in _POSITIVE_CLASSIFICATIONS:
        # Docket-validated green/yellow without proof: keep the docket research/
        # evidence fields; set ONLY the money tier.
        payload["money_status"] = "estimated_surplus" if refined else "apparent_surplus"
        return

    # No docket classification: PR-enriched lead.
    payload["research_status"] = "property_enriched"
    payload["evidence_level"]  = "property_enriched"
    payload["lead_quality"]    = "unknown"
    payload["pipeline_ready"]  = False
    payload["money_status"]    = "estimated_surplus" if refined else "apparent_surplus"


def _apply_lee_lien_verdict(payload: dict) -> None:
    """Apply the Lee PR-first lien-consumes-surplus verdict to a payload.

      killed     → set classification='killed' so the FP-14 filter drops the lead
                   out of the deliverable (same kill rule as the docket counties),
                   citing the itemized lien amount in classification_reason for the
                   audit log.
      lien_risk  → stays VISIBLE; attach a caution flag + reason for the dashboard.
      clean/other→ no change (the lien gate found nothing that consumes surplus).

    Lee-only and PR-gated: leads without a lee_lien_classification are untouched.
    Anti-fabrication is enforced upstream — only a real itemized SecondAmount >
    surplus with trustworthy owner data produces is_hard_kill (=> killed here)."""
    cls = (payload.get("lee_lien_classification") or "").strip().lower()
    if not cls:
        return
    amt = float(payload.get("lee_lien_amount") or 0)
    reason = payload.get("lee_lien_reason") or ""

    if cls == "killed" and payload.get("lee_lien_is_hard_kill"):
        payload["classification"] = "killed"
        payload["lead_quality"]   = "killed"
        payload["money_status"]   = "no_surplus"
        payload["evidence_level"] = "pr_lien_consumes_surplus"
        payload["classification_reason"] = (
            f"PR lien check: itemized second-position lien ${amt:,.0f} exceeds "
            f"apparent surplus — surplus consumed (Lee PR-first kill)")
    elif cls == "lien_risk":
        payload["lee_lien_caution"] = True
        if reason:
            payload["lee_lien_caution_reason"] = reason


_CONFIRMED_STORE = PROJECT_ROOT / "data" / "dockets" / "_confirmed_retained.json"


def _apply_confirmed_retention(leads_payload: list, present_keys: set) -> int:
    """Persist confirmed-tier leads and carry them past the standard window.

    Store keyed by (county|normalized-case). Each build:
      1. upsert every currently-confirmed lead (stamp last_verified = today);
      2. carry forward stored leads that are NO LONGER in the feed but still
         within CONFIRMED_WINDOW_DAYS (flagged retained, last-verified shown);
      3. drop stored leads that reappeared-but-are-no-longer-confirmed
         (re-verified as claimed/killed) or that aged past the confirmed window.
    """
    from config.counties import CONFIRMED_WINDOW_DAYS, LEAD_WINDOW_DAYS
    today = date.today()

    def key(cid, cn):
        return f"{cid}|{_normalize_case_for_lookup(cn)}"

    try:
        store = json.loads(_CONFIRMED_STORE.read_text()) if _CONFIRMED_STORE.exists() else {}
    except Exception:
        store = {}

    # Live docket lookup — used to self-heal a blank owner on carried-forward
    # confirmed snapshots taken before the owner-derivation fix (see step 2).
    _retention_docket_lookup = _load_docket_data()

    # 1. upsert current confirmed leads
    live_confirmed = set()
    for p in leads_payload:
        if p.get("money_status") == "confirmed_surplus":
            k = key(p["county_id"], p["case_number"])
            live_confirmed.add(k)
            first = store.get(k, {}).get("confirmed_first_seen", today.isoformat())
            store[k] = {"payload": p, "sale_date": p.get("sale_date", ""),
                        "confirmed_first_seen": first, "last_verified": today.isoformat()}
            p["confirmed_retained"] = False
            p["confirmed_last_verified"] = today.isoformat()

    # 2/3. carry-forward, prune, and drop re-verified-not-confirmed
    carried = 0
    for k, entry in list(store.items()):
        if k in live_confirmed:
            continue
        cid, norm = k.split("|", 1)
        try:
            age = (today - date.fromisoformat((entry.get("sale_date") or "")[:10])).days
        except Exception:
            age = 10 ** 6
        if age > CONFIRMED_WINDOW_DAYS:
            del store[k]                      # aged out of the confirmed window
            continue
        if (cid, norm) in present_keys:
            # Reappeared this build but NOT currently confirmed → re-verified as
            # claimed/killed/downgraded. Drop it — do not carry a stale confirm.
            del store[k]
            continue
        # Out of the auction feed but still within the confirmed window → carry
        # the last-verified snapshot, clearly flagged retained.
        cp = dict(entry["payload"])
        cp["confirmed_retained"] = True
        cp["confirmed_last_verified"] = entry.get("last_verified", "")
        # Self-heal a blank owner on snapshots taken before the owner-derivation
        # fix: the payload froze owner_name="" but the live docket still carries
        # the party data. Re-derive from the current docket record (never the
        # auction, never fabricated) so retained confirmed leads show their owner.
        if not (cp.get("owner_name") or "").strip():
            _dk = _retention_docket_lookup.get((cid, norm))
            if _dk:
                _own = derive_owner_from_docket(_dk)
                if _own:
                    cp["owner_name"] = _own
        leads_payload.append(cp)
        carried += 1

    try:
        _CONFIRMED_STORE.write_text(json.dumps(store, indent=1))
    except Exception as e:
        print(f"   ⚠ confirmed-retention store write failed: {e}")
    if carried:
        print(f"   ✓ Retained {carried} confirmed lead(s) past the {LEAD_WINDOW_DAYS}-day "
              f"window (confirmed window {CONFIRMED_WINDOW_DAYS}d)")
    return carried


def export_dashboard_data():
    docs_data = PROJECT_ROOT / "docs" / "data"
    docs_data.mkdir(parents=True, exist_ok=True)

    print("📊 Loading leads...")
    leads = load_all_leads()
    summary = get_summary(leads)
    print(f"   ✓ {len(leads)} leads loaded")
    print(f"   ✓ ${summary['total_apparent_surplus']:,.0f} apparent surplus (pre-enrichment)")

    pr_lookup = _load_pr_enrichment()

    leads_payload = []
    pr_matches = 0
    docket_matches = 0
    total_real_surplus = 0.0

    # CountyConfig lookup for manual-verify clerk links per lead.
    try:
        from config.counties import COUNTY_BY_ID
    except Exception:
        COUNTY_BY_ID = {}
    # Counties where the docket is not automatable (Cloudflare). Dashboard
    # surfaces these prominently with a manual-verify link.
    CF_BLOCKED = {"franklin-oh"}

    # Local-run counties (Franklin, Orange) refresh only on a manual local run,
    # while the auction feed refreshes daily via cron. Compute per-county docket
    # freshness so stale/uncovered cases are labeled honestly and never shown as
    # verified. A county is STALE if the auction feed contains cases its docket
    # JSONL doesn't cover, or the last scrape is older than the refresh window.
    try:
        from core.dockets import LOCAL_RUN_COUNTIES
    except Exception:
        LOCAL_RUN_COUNTIES = set()
    _dockets_dir = PROJECT_ROOT / "data" / "dockets"
    _stale_cutoff = (date.today() - timedelta(days=LEAD_WINDOW_DAYS)).isoformat()
    _local_status = {}
    for _cid in LOCAL_RUN_COUNTIES:
        _covered, _last = set(), ""
        for _f in sorted(_dockets_dir.glob(f"{_cid}_*.jsonl")):
            try:
                for _line in _f.open():
                    try:
                        _r = json.loads(_line)
                    except Exception:
                        continue
                    _cn = _r.get("case_number", "")
                    if _cn:
                        _covered.add(_normalize_case_for_lookup(_cn))
                    _sa = _r.get("scraped_at", "") or ""
                    if _sa > _last:
                        _last = _sa
            except Exception:
                continue
        _auction = {_normalize_case_for_lookup(l.case_number) for l in leads if l.county_id == _cid}
        _uncovered = _auction - _covered
        _last_day = _last[:10]
        _local_status[_cid] = {
            "covered": _covered,
            "last_scraped": _last_day,
            "uncovered_count": len(_uncovered),
            "stale": bool(_uncovered) or (not _last_day) or (_last_day < _stale_cutoff),
        }

    for l in leads:
        cc = COUNTY_BY_ID.get(l.county_id)
        clerk_search_url = getattr(cc, "clerk_search_url", "") if cc else ""
        # Tax-deed leads: the foreclosure clerk docket scraper falls through on
        # tax-deed case numbers, so the verify link must point at the county's
        # SEPARATE tax-deed system (RealTDM / realtaxdeed / deedauction), where
        # the real excess-proceeds / claim status lives.
        if getattr(l, "fl_tax_deed", False) or getattr(l, "fl_tax_deed_redeemed", False):
            _td = getattr(cc, "tax_deed_url", "") if cc else ""
            if _td:
                clerk_search_url = _td
        payload = {
            "county_id":        l.county_id,
            "county_name":      l.county_name,
            "state":            l.state,
            "case_number":      l.case_number,
            "address":          l.address,
            # Docket-extracted defendant homeowner (Miami-Dade / Broward / Duval
            # owner fixes). The loader copies docket owner_name onto the lead when
            # the auction scrape left it blank; surface it so the deliverable shows
            # the homeowner, not just PR's post-auction owner (pr_owner_name).
            "owner_name":       getattr(l, "owner_name", ""),
            "parcel_id":        l.parcel_id,
            "auction_type":     l.auction_type,
            "opening_bid":      l.opening_bid,
            "final_sale_price": l.final_sale_price,
            "gross_surplus":    l.gross_surplus,
            "assessed_value":   l.assessed_value,
            "appraised_value":  getattr(l, "appraised_value", 0.0),
            "sale_vs_appraised": getattr(l, "sale_vs_appraised", 0.0),
            "mispriced_opener":  getattr(l, "mispriced_opener", False),
            "fl_county_court":   getattr(l, "fl_county_court", False),
            "fl_tax_deed":          getattr(l, "fl_tax_deed", False),
            "fl_tax_deed_redeemed": getattr(l, "fl_tax_deed_redeemed", False),
            "taxdeed_verdict":      getattr(l, "taxdeed_verdict", ""),
            "taxdeed_surplus_pool": getattr(l, "taxdeed_surplus_pool", None),
            "taxdeed_claim_deadline_days": getattr(l, "taxdeed_claim_deadline_days", None),
            # Orange Court Registry Balance lifecycle marker (core.dockets.orange_registry).
            # Present ⇒ the lead was registry-checked; the dashboard renders the stage
            # + clerk balance + as-of date (and the HOA caution on distributed CC funds).
            "registry_status":      getattr(l, "registry_status", ""),
            "registry_balance":     getattr(l, "registry_balance", None),
            "registry_as_of":       getattr(l, "registry_as_of", ""),
            "registry_hoa_caution": getattr(l, "registry_hoa_caution", False),
            "sale_date":        l.sale_date,
            "sale_datetime":    getattr(l, "sale_datetime", ""),
            "sold_to":          l.sold_to,
            "auction_status":   l.auction_status,
            "score":            l.score,
            "source_url":       getattr(l, "source_url", ""),
            # Manual-verify clerk portal link — always present so Eric can
            # eyeball any case directly. For Franklin/Hamilton (Cloudflare-
            # blocked, PR-fallback only) this is the ONLY path to the docket.
            "clerk_manual_search_url": clerk_search_url,
            "requires_manual_docket_verify": l.county_id in CF_BLOCKED,

            # Docket-enrichment fields
            "classification":         getattr(l, "classification", ""),
            "classification_reason":  getattr(l, "classification_reason", ""),
            "prayer_amount":          getattr(l, "prayer_amount", 0.0),
            "true_surplus":           getattr(l, "true_surplus", None),
            "debt_source":            getattr(l, "debt_source", ""),
            "kill_signals":           getattr(l, "kill_signals", []),
            "proof_of_surplus":       getattr(l, "proof_of_surplus", ""),
            "competing_filers":       getattr(l, "competing_filers", []),
            "additional_parties":     getattr(l, "additional_parties", []),
            "docket_url":             getattr(l, "docket_url", ""),

            # Eric's review taxonomy (Miami-Dade docket validation). Empty for
            # counties without the review model — frontend hides empty badges.
            "foreclosure_type":       getattr(l, "foreclosure_type", ""),
            "docket_evidence_level":  getattr(l, "docket_evidence_level", ""),
            "lead_status":            getattr(l, "lead_status", ""),
            "claim_filed":            getattr(l, "claim_filed", False),
            "claim_type":             getattr(l, "claim_type", ""),

            # Verification status model (HARDENING — Parts 1-6)
            "research_status":  getattr(l, "research_status", "unknown"),
            "lead_quality":     getattr(l, "lead_quality", "unknown"),
            "money_status":     getattr(l, "money_status", "unknown"),
            "evidence_level":   getattr(l, "evidence_level", "unknown"),
            "pipeline_ready":   getattr(l, "pipeline_ready", False),

            "pr_match": False,
        }

        # Local-run county freshness (Franklin/Orange). A case ABSENT from the
        # county's docket JSONL is NOT docket-verified — it's uncovered by the
        # last local run and must render honestly as stale, regardless of any
        # other flag. Cron counties are implicitly covered by the daily scrape.
        _lr = _local_status.get(l.county_id)
        if _lr is not None:
            _covered_here = _normalize_case_for_lookup(l.case_number) in _lr["covered"]
            payload["local_run_county"]   = True
            payload["docket_last_scraped"] = _lr["last_scraped"]
            payload["docket_stale"]       = _lr["stale"]
            payload["docket_covered"]     = _covered_here
            if not _covered_here:
                # Uncovered by the local docket run → never show as verified.
                payload["requires_manual_docket_verify"] = True
                if payload.get("docket_evidence_level") in ("", "docket_checked"):
                    payload["docket_evidence_level"] = "stale_uncovered"
        else:
            payload["local_run_county"] = False
            payload["docket_covered"]   = True

        pr_record = pr_lookup.get((l.county_id, l.case_number))
        if pr_record:
            _apply_pr_to_payload(payload, pr_record)
            if payload.get("pr_match"):
                pr_matches += 1
                # PR merged AFTER loader status assignment. Re-run the status
                # model on the merged payload so a PR-matched auction-only lead
                # is correctly re-tagged property_enriched / estimated_surplus.
                _reassign_status_after_pr(payload)
                # Lee PR-first lien gate: a confirmed lien-consumes-surplus is a
                # KILL (filtered out by FP-14 below); lien_risk stays visible as a
                # caution. Runs after the status model so the kill overrides tier.
                _apply_lee_lien_verdict(payload)

        # docket match count: any real docket classification
        if (payload.get("classification") or "").strip().lower() in DOCKET_VERIFIED_CLASSIFICATIONS:
            docket_matches += 1

        amount, bucket = _surplus_for_payload(payload)
        payload["best_real_surplus"]   = amount
        payload["real_surplus_source"] = bucket
        # Only confirmed surplus contributes to the confirmed total.
        if bucket == "confirmed_surplus":
            total_real_surplus += amount

        # ── FP-9: priority_rank — surface "docket-checked" leads at the
        # top of the dashboard so Summit YELLOW/RED leads don't get
        # buried under PR-only and auction-math rows.
        #
        # NAMING: the docket_verified_positive field is internal-only.
        # The visible label is "📋 Docket-checked" — explicitly NOT
        # "Verified" to avoid implying confirmed_surplus. These leads
        # are still apparent_surplus until a proof-of-disbursement
        # filing flips them to confirmed.
        #
        # Rank semantics (lower = higher priority on the dashboard):
        #   0  confirmed_surplus (docket + proof field)
        #   1  docket-checked: real prayer >= $10K, positive (sale−prayer),
        #      classification in {green,yellow,red}, NOT killed
        #   2  estimated_surplus (PR-refined math, TLB > $0)
        #   3  apparent_surplus with PR intel (owner/liens attached)
        #   4  apparent_surplus, no PR data
        #   5  killed / no_surplus (still shown but de-emphasized)
        cls_lc = (payload.get("classification") or "").lower()
        ts = payload.get("true_surplus")
        prayer = float(payload.get("prayer_amount") or 0)
        DOCKET_VERIFIED_MIN_PRAYER = 10000.0
        if bucket == "confirmed_surplus":
            payload["priority_rank"] = 0
        elif (cls_lc in ("green", "yellow", "red")
              and prayer >= DOCKET_VERIFIED_MIN_PRAYER
              and ts is not None and ts > 0):
            payload["priority_rank"] = 1
            payload["docket_verified_positive"] = True
        elif bucket == "estimated_surplus":
            payload["priority_rank"] = 2
        elif bucket == "apparent_surplus" and payload.get("pr_match"):
            payload["priority_rank"] = 3
        elif bucket == "apparent_surplus":
            payload["priority_rank"] = 4
        else:
            payload["priority_rank"] = 5

        leads_payload.append(payload)

    # ── FP-14: filter killed leads OUT of the dashboard entirely ───────
    # Per Eric's May 12 spec: "Killed leads are filtered OUT of the
    # deliverable, not badged." Killed = motion to vacate, bankruptcy
    # filed, dismissal, sale vacated, owner already filed claim, funds
    # already disbursed, escheated to state. These leads have ZERO
    # actionable surplus opportunity and rendering them — even greyed —
    # wastes Eric's screen real estate and erodes trust in the
    # deliverable. Killed-lead data stays in data/dockets/ for audit.
    # Every case seen this build (live + killed), so confirmed-retention can tell
    # "dropped from the auction feed" (carry forward) from "reappeared but now
    # killed/downgraded" (re-verified as no longer confirmed → do NOT carry).
    _present_keys = {(p["county_id"], _normalize_case_for_lookup(p["case_number"]))
                     for p in leads_payload}
    pre_kill = len(leads_payload)
    _killed_audit = [p for p in leads_payload
                     if (p.get("lead_quality") or "").lower() == "killed"
                     or (p.get("classification") or "").lower() == "killed"]
    leads_payload = [p for p in leads_payload
                     if (p.get("lead_quality") or "").lower() != "killed"
                     and (p.get("classification") or "").lower() != "killed"]
    killed_removed = pre_kill - len(leads_payload)
    print(f"   ✓ Filtered {killed_removed} KILLED leads out of dashboard (FP-14 spec)")
    # Audit trail: log WHY each killed lead was dropped (the docket-cited
    # reason), so a reviewer can confirm a kill without opening the docket.
    for p in _killed_audit:
        print(f"      🗑  KILLED {p.get('county_id'):<14} {p.get('case_number','?'):<22} "
              f"[{p.get('docket_evidence_level') or p.get('evidence_level') or '?'}] "
              f"— {p.get('classification_reason','(no reason)')}")

    # ── Build killed_leads.json — shown on dashboard as proof-of-work ──
    # Combine two sources of kills:
    #   (a) Docket-killed: passed through the pipeline, classified 'killed'
    #       by a docket signal (vacate, bankruptcy, claim, conservative debt).
    #   (b) Plaintiff-killed: dropped at Filter 1 (sold_to='Plaintiff') before
    #       docket processing. Load separately with require_third_party=False
    #       and tag them.
    # Only include leads with a meaningful former apparent surplus (≥$10K) —
    # the demonstration value is "real money that was there, then killed."
    _KILL_LABEL = {
        "motion_to_vacate":   "Sale vacated",
        "sale_vacated":       "Sale vacated",
        "bankruptcy":         "Bankruptcy filed",
        "already_disbursed":  "Funds already disbursed",
        "owner_filed_claim":  "Owner filed claim",
        "escheated":          "Funds escheated",
        "surplus_claim_filed":"Surplus claim filed",
        "surplus_firm_appearance": "Surplus claim filed",
        "sale_issue":         "Sale cancelled/vacated",
        "sale_issue_found":   "Sale cancelled/vacated",
        "sold_to_plaintiff":  "Sold to plaintiff",
    }

    def _killed_entry(p, kill_source):
        signals = p.get("kill_signals") or []
        primary_signal = signals[0] if signals else kill_source
        label = _KILL_LABEL.get(primary_signal) or _KILL_LABEL.get(kill_source) or "Killed"
        former = float(p.get("gross_surplus") or 0)
        reason = p.get("classification_reason") or ""
        # For OH debt-kills the classification_reason IS the detail.
        if "conservative debt" in reason or "true surplus only" in reason:
            label = "Debt exceeds sale"
        return {
            "county_id":            p.get("county_id", ""),
            "county_name":          p.get("county_name", ""),
            "state":                p.get("state", ""),
            "case_number":          p.get("case_number", ""),
            "address":              p.get("address", ""),
            "sale_date":            p.get("sale_date", ""),
            "sold_to":              p.get("sold_to", ""),
            "former_surplus":       former,
            "kill_label":           label,
            "kill_signals":         signals,
            "kill_detail":          reason[:180],
            "docket_url":           p.get("docket_url", ""),
            "source_url":           p.get("source_url", ""),
        }

    killed_entries = []
    # (a) docket-killed
    for p in _killed_audit:
        former = float(p.get("gross_surplus") or 0)
        if former >= 10_000:
            killed_entries.append(_killed_entry(p, "docket_signal"))

    # (b) plaintiff-killed: reload without third-party filter, grab Plaintiff wins
    try:
        all_leads_incl_plaintiff = load_all_leads(require_third_party=False)
        for l in all_leads_incl_plaintiff:
            if (l.sold_to or "").lower().startswith("plaintiff") and l.gross_surplus >= 10_000:
                cc = COUNTY_BY_ID.get(l.county_id)
                p_stub = {
                    "county_id":   l.county_id,
                    "county_name": l.county_name,
                    "state":       l.state,
                    "case_number": l.case_number,
                    "address":     l.address,
                    "sale_date":   l.sale_date,
                    "sold_to":     l.sold_to,
                    "gross_surplus": l.gross_surplus,
                    "kill_signals":  ["sold_to_plaintiff"],
                    "classification_reason": (
                        f"Sold to plaintiff — no recoverable surplus for homeowner "
                        f"(SOP step 11). Apparent surplus was ${l.gross_surplus:,.0f}."
                    ),
                    "docket_url":  getattr(l, "docket_url", ""),
                    "source_url":  getattr(l, "source_url", ""),
                }
                killed_entries.append(_killed_entry(p_stub, "sold_to_plaintiff"))
    except Exception as _e:
        print(f"   ⚠ killed_leads: plaintiff reload failed: {_e}")

    # (c) docket-killed leads filtered BEFORE reaching leads_payload
    # (OH real-overbid gate drops leads whose docket says killed but whose
    # sale didn't clear 1.5× opening bid — these never enter leads_payload
    # so _killed_audit misses them entirely). CV19923457 is the prototype:
    # conservative debt kills it but it also fails the overbid gate, so
    # path (a) never sees it.
    try:
        _raw_lkp = _load_latest_raw_auction()
        _all_dockets = _load_docket_data()
        _seen = {(e["county_id"], _normalize_case_for_lookup(e["case_number"]))
                 for e in killed_entries if e.get("case_number")}
        _cutoff = date.today() - timedelta(days=LEAD_WINDOW_DAYS)
        for (cid, norm), docket in _all_dockets.items():
            if (docket.get("classification") or "").lower() != "killed":
                continue
            if (cid, norm) in _seen:
                continue
            raw = _raw_lkp.get((cid, norm))
            if not raw:
                continue
            gross = float(raw.get("gross_surplus") or 0)
            if gross < 10_000:
                continue
            sd = raw.get("auction_date") or raw.get("sale_date") or ""
            if sd:
                try:
                    if date.fromisoformat(sd[:10]) < _cutoff:
                        continue
                except Exception:
                    pass
            cc = COUNTY_BY_ID.get(cid)
            p_stub = {
                "county_id":            cid,
                "county_name":          (cc.name if cc else cid),
                "state":                (cc.state if cc else ""),
                "case_number":          docket.get("case_number", ""),
                "address":              raw.get("address", ""),
                "sale_date":            sd,
                "sold_to":              raw.get("sold_to", ""),
                "gross_surplus":        gross,
                "kill_signals":         list(docket.get("kill_signals") or []),
                "classification_reason": docket.get("classification_reason") or "",
                "docket_url":           docket.get("case_url") or "",
                "source_url":           raw.get("source_url", ""),
            }
            killed_entries.append(_killed_entry(p_stub, "docket_signal"))
            _seen.add((cid, norm))
            print(f"      📋 docket-only kill: {cid} {docket.get('case_number','')} "
                  f"(former ${gross:,.0f})")
    except Exception as _e:
        print(f"   ⚠ killed_leads: docket-only path failed: {_e}")

    # Sort: largest former surplus first (most dramatic kills at top)
    killed_entries.sort(key=lambda k: k["former_surplus"], reverse=True)
    killed_file = docs_data / "killed_leads.json"
    with open(killed_file, "w") as f:
        json.dump(killed_entries, f, indent=2)
    print(f"   ✓ Wrote {killed_file.relative_to(PROJECT_ROOT)} ({len(killed_entries)} killed leads with ≥$10K former surplus)")

    # ── FP-18 Item 2: $5K min-surplus floor safety net ────────────────
    # After the display-bug fix lifts docket-checked leads to their real
    # true_surplus, a near-zero number should be exceptionally rare. But
    # if the math genuinely produces sub-$5K (e.g. tax-deed sale where
    # debt nearly equals sale), those leads aren't actionable and should
    # not occupy dashboard real estate. Eric's standard: filter, don't
    # silently drop — count is logged AND audit fields in summary.json
    # preserve the pre-filter total.
    # OH-mortgage-unverified leads (real_surplus_source='oh_unverified',
    # best_real_surplus=None) have NO known surplus figure — they must be EXEMPT
    # from the floor (you can't floor-filter an unknown number; dropping them
    # would re-hide exactly the leads Eric wants surfaced for manual verify).
    MIN_DISPLAY_SURPLUS = 5000.0
    pre_floor = len(leads_payload)

    def _below_floor(p):
        if p.get("real_surplus_source") in ("oh_unverified", "oh_uncertain", "fl_hoa_unverified", "fl_taxdeed_pool", "fl_taxdeed_redeemed", "fl_taxdeed_confirmed_pool"):
            return False  # no known surplus figure → can't floor-filter; keep visible
        return (p.get("best_real_surplus") or 0) < MIN_DISPLAY_SURPLUS

    below_floor = [p for p in leads_payload if _below_floor(p)]
    leads_payload = [p for p in leads_payload if not _below_floor(p)]
    floor_removed = pre_floor - len(leads_payload)
    print(f"   ✓ Filtered {floor_removed} sub-${MIN_DISPLAY_SURPLUS:,.0f} leads out of dashboard (FP-18 floor)")
    if below_floor:
        for p in below_floor[:5]:
            print(f"      below-floor: {p.get('county_id'):<14} {p.get('case_number','?'):<24}  best=${p.get('best_real_surplus') or 0:,.0f}")

    # Sort by priority_rank (asc), then by best_real_surplus (desc) within rank.
    # The dashboard frontend can re-sort but the wire order is the audit order.
    # ── Confirmed-lead retention (Phase 3) ─────────────────────────────
    # Confirmed-tier leads are the deliverable — retain them past the standard
    # window (config.CONFIRMED_WINDOW_DAYS) even after the auction feed drops
    # them. RE-VERIFIED, not frozen: while a confirmed lead is still in the feed
    # it is re-scraped + re-classified daily and a competing claim/disbursement
    # flips it to killed (dropped here); once it leaves the feed it is carried
    # at its last-verified snapshot, flagged retained with the date so it never
    # masquerades as freshly checked.
    confirmed_retained_count = _apply_confirmed_retention(leads_payload, _present_keys)

    leads_payload.sort(key=lambda p: (p.get("priority_rank", 5), -float(p.get("best_real_surplus", 0) or 0)))

    leads_file = docs_data / "leads.json"
    with open(leads_file, "w") as f:
        json.dump(leads_payload, f, indent=2)
    print(f"   ✓ Wrote {leads_file.relative_to(PROJECT_ROOT)}")
    print(f"   ✓ Enrichment coverage: {pr_matches} PR / {docket_matches} docket / {len(leads_payload)} total")
    print(f"   ✓ Total real surplus (best available): ${total_real_surplus:,.0f}")

    # ── HARDENING (FP-4): rebuild ALL summary numbers from money_status ──
    # The headline must NOT present apparent surplus as confirmed money.
    def _bucket(p):
        return (p.get("money_status") or "unknown").strip().lower()

    # OH-mortgage-UNVERIFIED leads (no known surplus, best_real_surplus=None) stay
    # VISIBLE in the list but must NOT inflate the real-surplus KPI count/total —
    # they're tracked in their own 'unverified' bucket for the headline.
    def _unverified(p):
        return p.get("real_surplus_source") in ("oh_unverified", "oh_uncertain", "fl_hoa_unverified", "fl_taxdeed_pool", "fl_taxdeed_redeemed", "fl_taxdeed_confirmed_pool")

    confirmed = [p for p in leads_payload if _bucket(p) == "confirmed_surplus" and not _unverified(p)]
    estimated = [p for p in leads_payload if _bucket(p) == "estimated_surplus" and not _unverified(p)]
    apparent  = [p for p in leads_payload if _bucket(p) == "apparent_surplus" and not _unverified(p)]
    unverified = [p for p in leads_payload if _unverified(p)]
    killed    = [p for p in leads_payload if (p.get("lead_quality") or "") == "killed"]
    red       = [p for p in leads_payload if (p.get("lead_quality") or "") == "red"]
    docket_verified_positive = [p for p in leads_payload if p.get("docket_verified_positive")]

    # `or 0` guards: OH-mortgage-unverified leads carry best_real_surplus=None
    # (unknown surplus) and must contribute 0 to any total, never crash the sum.
    confirmed_total = sum((p.get("best_real_surplus") or 0) for p in confirmed)
    estimated_total = sum((p.get("best_real_surplus") or 0) for p in estimated)
    apparent_total  = sum((p.get("best_real_surplus") or 0) for p in apparent)
    docket_verified_positive_total = sum((p.get("best_real_surplus") or 0) for p in docket_verified_positive)

    def _top5(bucket_list):
        s = sorted(bucket_list, key=lambda p: (p.get("best_real_surplus") or 0), reverse=True)
        return [{
            "county":      p.get("county_name", ""),
            "state":       p.get("state", ""),
            "case_number": p.get("case_number", ""),
            "address":     p.get("address", ""),
            "surplus":     p.get("best_real_surplus") or 0,
            "money_status": p.get("money_status", "unknown"),
            "evidence_level": p.get("evidence_level", "unknown"),
        } for p in s[:5]]

    print(f"   ✓ Confirmed: {len(confirmed)} leads / ${confirmed_total:,.0f}")
    print(f"   ✓ Estimated: {len(estimated)} leads / ${estimated_total:,.0f}")
    print(f"   ✓ Apparent:  {len(apparent)} leads / ${apparent_total:,.0f}")
    print(f"   ✓ 📋 Docket-checked (real prayer, positive sale−prayer, non-killed): {len(docket_verified_positive)} leads / ${docket_verified_positive_total:,.0f}")
    print(f"   ✓ Killed: {len(killed)} | Red: {len(red)} (excluded from confirmed)")

    summary_file = docs_data / "summary.json"
    # FP-19 Item 3: total_leads is the POST-FILTER (visible) count.
    # The raw pre-filter count + per-filter breakdown stays in the
    # payload as separate audit fields, so anyone inspecting summary.json
    # can reconcile every dropped lead.
    summary_payload = {
        "generated_at":           summary["generated_at"],
        "total_leads":            len(leads_payload),               # visible
        "total_leads_pre_filter": summary["total_leads"],            # raw scraped
        "killed_filtered_count":  killed_removed,                    # FP-14
        "killed_shown_count":     len(killed_entries),               # in killed_leads.json
        "below_floor_filtered_count": floor_removed,                  # FP-18
        "confirmed_retained_count": confirmed_retained_count,         # carried past window (post-filter)
        "min_display_surplus":    MIN_DISPLAY_SURPLUS,                # FP-18 threshold

        # FP-4: separated, honestly-labeled totals. No single "total_surplus"
        # that conflates confirmed money with auction guesses.
        "confirmed_surplus_total":  confirmed_total,
        "estimated_surplus_total":  estimated_total,
        "apparent_surplus_total":   apparent_total,
        # FP-9 headline: how many leads have a docket-extracted prayer AND
        # positive true_surplus AND a non-killed classification. These are
        # the highest-quality leads on the dashboard.
        "docket_verified_positive_count": len(docket_verified_positive),
        "docket_verified_positive_total": docket_verified_positive_total,

        "confirmed_surplus_count":  len(confirmed),
        "estimated_surplus_count":  len(estimated),
        "apparent_surplus_count":   len(apparent),
        # OH-mortgage leads with a real overbid but no docket debt: shown for
        # manual verify, NOT counted as real-surplus leads (best_real_surplus null).
        "unverified_count":         len(unverified),
        "killed_count":             len(killed),
        "red_count":                len(red),
        "pipeline_ready_count":     sum(1 for p in leads_payload if p.get("pipeline_ready")),

        "pr_matched_count":       pr_matches,
        "docket_matched_count":   docket_matches,

        "by_state":  summary["by_state"],
        "by_county": summary["by_county"],
        "by_score":  summary["by_score"],

        # Local-run county freshness (Franklin/Orange): last-scraped date + stale
        # flag so the dashboard can warn when manual docket data lags the feed.
        "local_run_status": {
            cid: {"last_scraped": s["last_scraped"], "stale": s["stale"],
                  "uncovered_count": s["uncovered_count"]}
            for cid, s in _local_status.items()
        },

        "top_5_confirmed_leads": _top5(confirmed),
        "top_5_estimated_leads": _top5(estimated),
        "top_5_apparent_leads":  _top5(apparent),

        "coverage": {
            "states":   ["FL", "OH"],
            "counties": [
                {"id": "miami-dade-fl", "name": "Miami-Dade", "state": "FL"},
                {"id": "broward-fl",    "name": "Broward",    "state": "FL"},
                {"id": "duval-fl",      "name": "Duval",      "state": "FL"},
                {"id": "lee-fl",        "name": "Lee",        "state": "FL"},
                {"id": "orange-fl",     "name": "Orange",     "state": "FL"},
                {"id": "cuyahoga-oh",   "name": "Cuyahoga",   "state": "OH"},
                {"id": "franklin-oh",   "name": "Franklin",   "state": "OH"},
                {"id": "montgomery-oh", "name": "Montgomery", "state": "OH"},
                {"id": "summit-oh",     "name": "Summit",     "state": "OH"},
                {"id": "hamilton-oh",   "name": "Hamilton",   "state": "OH"},
            ],
        },
    }

    with open(summary_file, "w") as f:
        json.dump(summary_payload, f, indent=2)
    print(f"   ✓ Wrote {summary_file.relative_to(PROJECT_ROOT)}")
    print(f"   ✓ CONFIRMED surplus = ${confirmed_total:,.0f} "
          f"({len(confirmed)} leads) — apparent ${apparent_total:,.0f} NOT counted as confirmed")

    return leads_file, summary_file


if __name__ == "__main__":
    leads_file, summary_file = export_dashboard_data()
    print()
    print("=" * 70)
    print("  ✓ Dashboard data export complete")
    print(f"  📁 {leads_file}")
    print(f"  📁 {summary_file}")
    print("=" * 70)
