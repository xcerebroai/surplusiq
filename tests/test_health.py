"""
SurplusIQ — Data-health monitor acceptance test (core.health).

    python -m tests.test_health

Network-free. Two layers:
  1. Unit rules on synthetic runs — expected-vs-unexpected emptiness, the
     0.6×/0.3× baseline bands, the 2-run CRITICAL streak, the Orange-registry
     any-drop WARN, local-run staleness wording, history upsert, chip labels,
     and the row classifier on REAL committed row shapes.
  2. The historical BACKTEST over the committed data/dockets history
     (frozen at 2026-08-24): MUST fire on Broward 2026-08-08 and on the other
     outages the replay surfaced (Duval 08-06/08-21, Cuyahoga 07-13, Miami
     08-01, Montgomery 07-01, RealTDM 08-20), and MUST stay quiet on every
     healthy / expected-empty day checked here (incl. Miami tax-deed share
     days and Hamilton's partial local run).
"""
from __future__ import annotations
import json
import glob
import os
import sys

from core import health as H

_PASS, _FAIL = 0, 0


def check(cond, msg):
    global _PASS, _FAIL
    if cond:
        _PASS += 1; print(f"  [PASS] {msg}")
    else:
        _FAIL += 1; print(f"  [FAIL] {msg}")


def _src(source="docket", kind="cron", attempted=10, rows=None, content=None,
         failed=None, present=True):
    rows = attempted if rows is None else rows
    content = rows if content is None else content
    failed = (rows - content) if failed is None else failed
    m = H.SourceMetrics(source=source, kind=kind, file_present=present, attempted=attempted,
                        rows=rows, content_rows=content, failed_rows=failed, skipped_rows=0,
                        content_rate=content / max(rows, 1),
                        coverage=min(1.0, content / max(attempted, 1)))
    m.effective_rate = min(m.content_rate, m.coverage) if (kind == "cron" and attempted) else m.content_rate
    return m


def _prior(rates, attempted=10):
    """Prior history dicts (oldest→newest) with the given effective rates."""
    return [{"attempted": attempted, "effective_rate": r, "coverage": r, "low_streak": 0,
             "status": "OK"} for r in rates]


# ─────────────────────────────────────────────────────────────────────
def test_topology_in_sync():
    print("\n[1] source topology mirrors the scraper registries")
    from core.dockets.enrich import WORKING_DOCKET_COUNTIES
    from core.dockets import LOCAL_RUN_COUNTIES
    cron_docket = {c for c, srcs in H.CRON_SOURCES.items() if "docket" in srcs}
    check(cron_docket == set(WORKING_DOCKET_COUNTIES),
          f"cron docket counties == enrich.WORKING_DOCKET_COUNTIES ({sorted(cron_docket)})")
    check(H.LOCAL_RUN_COUNTIES == set(LOCAL_RUN_COUNTIES),
          f"health.LOCAL_RUN_COUNTIES == dockets.LOCAL_RUN_COUNTIES ({sorted(H.LOCAL_RUN_COUNTIES)})")
    check(set(H.LOCAL_SOURCES) <= set(LOCAL_RUN_COUNTIES), "scored local sources ⊆ local-run counties")
    check(set(H.LOCAL_RUN_COMMAND) == H.LOCAL_RUN_COUNTIES, "every local-run county names its manual command")


def test_row_classifier_on_real_shapes():
    print("\n[2] row classifier on real committed row shapes")
    def first(path):
        with open(path) as f:
            return json.loads(next(l for l in f if l.strip()))
    broward_shell = first("data/dockets/broward-fl_2026-08-08.jsonl")
    check(H.classify_row(broward_shell) == "failed",
          "Broward 08-08 shell (unknown / 'no rendered ViewDetails trigger') → failed")
    broward_ok = first("data/dockets/broward-fl_2026-08-07.jsonl")
    check(H.classify_row(broward_ok) == "content", "Broward 08-07 green row with events → content")
    duval_closed = first("data/dockets/duval-fl_2026-08-06.jsonl")
    check(H.classify_row(duval_closed) == "failed", "Duval 08-06 ERR_CONNECTION_CLOSED row → failed")
    franklin = first("data/dockets/franklin-oh_2026-08-11.jsonl")
    check(H.classify_row(franklin) == "content",
          "Franklin metadata-only (classification '' + docket_checked + owner) → content")
    hamilton = first("data/dockets/hamilton-oh_2026-08-17.jsonl")
    check(H.classify_row(hamilton) == "content", "Hamilton no-debt docket_checked row → content")
    orange = first("data/dockets/orange-fl_2026-08-18.jsonl")
    check(H.row_source(orange) == "registry" and H.classify_row(orange) == "content",
          "Orange registry row (registry_status set, classification '') → registry/content")
    taxdeed = first("data/dockets/miami-dade-fl-taxdeed_2026-08-18.jsonl")
    check(H.row_source(taxdeed) == "taxdeed" and H.classify_row(taxdeed) == "content",
          "RealTDM row with taxdeed_status → taxdeed/content")
    taxdeed_fail = first("data/dockets/miami-dade-fl-taxdeed_2026-08-20.jsonl")
    check(H.classify_row(taxdeed_fail) == "failed", "RealTDM 08-20 'scrape error: TimeoutError' → failed")
    skip = {"classification": "unknown",
            "classification_reason": "docket retrieval failed: tax_deed (RealTDM routing not implemented — separate task)"}
    check(H.classify_row(skip) == "skipped", "Miami tax_deed routing refusal → skipped (structural, not a failure)")
    check(H.classify_row({"classification": "", "classification_reason": "", "events": []}) == "empty",
          "row with nothing either way → empty")
    check(H.classify_row({"classification": "killed", "evidence_level": ""}) == "content",
          "Cuyahoga-style killed row with blank evidence_level → content")

    # The two vocabularies never overlap on the committed history.
    both = 0
    for f in glob.glob("data/dockets/*_2026-*.jsonl"):
        with open(f) as fh:
            for line in fh:
                if not line.strip():
                    continue
                r = json.loads(line)
                if H.classify_row(r) == "content" and H.FAILURE_PATTERN.search(r.get("classification_reason") or ""):
                    both += 1
    check(both == 0, f"no committed content row carries a failure-pattern reason (overlap={both})")


def test_emptiness_rules():
    print("\n[3] expected vs unexpected emptiness")
    m = H.evaluate_source(_src(attempted=0, rows=0, content=0), _prior([1.0] * 10))
    check(m.status == H.OK and "expected empty" in m.reasons[0], "attempted==0 → OK 'no in-window sales' (cannot alarm)")
    m = H.evaluate_source(_src(attempted=0, rows=5, content=0, failed=5), _prior([1.0] * 10))
    check(m.status == H.OK, "attempted==0 even with 5 failed rows → still OK (structurally incapable of firing)")
    m = H.evaluate_source(_src(attempted=6, rows=6, content=0, failed=6), [])
    check(m.status == H.CRITICAL, "attempted 6, 6 rows, 0 content, NO baseline → CRITICAL immediately (08-08 shape)")
    m = H.evaluate_source(_src(attempted=1, rows=1, content=0, failed=1), _prior([1.0] * 10))
    check(m.status == H.CRITICAL and m.low_streak == 1, "attempted 1, 0 content → CRITICAL on the first run, no streak needed")
    m = H.evaluate_source(_src(attempted=9, rows=0, content=0, failed=0, present=False), _prior([1.0] * 10))
    check(m.status == H.CRITICAL and "no file written" in m.reasons[0], "cron day, file missing, 9 attempted → CRITICAL 'no file written'")


def test_baseline_bands():
    print("\n[4] baseline bands (county's own trailing-10 median)")
    m = H.evaluate_source(_src(attempted=19, rows=19, content=10), _prior([1.0] * 10))
    check(m.status == H.WARN and m.baseline == 1.0, "10/19 (53%) vs baseline 100% → WARN (< 60%)")
    m = H.evaluate_source(_src(attempted=28, rows=28, content=19), _prior([1.0] * 10))
    check(m.status == H.OK, "19/28 (68%) vs baseline 100% → OK (≥ 60%)")
    m = H.evaluate_source(_src(attempted=6, rows=6, content=5), _prior([0.83] * 10))
    check(m.status == H.OK, "Summit shape 5/6 vs its own 83% baseline → OK (scale-free)")
    m = H.evaluate_source(_src(attempted=10, rows=10, content=8), _prior([1.0, 0.5]))
    check(m.status == H.OK and m.baseline is None, "fewer than 3 baseline runs → only the immediate rule applies")
    m = H.evaluate_source(_src(attempted=10, rows=10, content=2), _prior([1.0] * 10))
    check(m.status == H.WARN and m.low_streak == 1, "20% vs 100% first run → WARN (streak 1), not yet CRITICAL")
    prior = _prior([1.0] * 10); prior[-1]["low_streak"] = 1
    m = H.evaluate_source(_src(attempted=10, rows=10, content=2), prior)
    check(m.status == H.CRITICAL and m.low_streak == 2, "20% vs 100% for 2 consecutive runs → CRITICAL")
    m = H.evaluate_source(_src(attempted=10, rows=10, content=8), prior)
    check(m.low_streak == 0, "recovery resets the low streak")
    # coverage catches a crash that skipped most cases even though the written rows are perfect
    m = H.evaluate_source(_src(attempted=16, rows=1, content=1), _prior([1.0] * 10))
    check(m.status == H.WARN and m.coverage < 0.1, "1 perfect row of 16 attempted (crash-skip) → coverage collapse → WARN")
    m = H.evaluate_source(_src(source="docket", kind="local", attempted=30, rows=6, content=6), _prior([1.0] * 10))
    check(m.status == H.OK, "local-run partial pass (6/6 rows, 30 raw parcels) → OK (coverage is the staleness check's job)")


def test_registry_any_drop():
    print("\n[5] Orange registry: any drop from 100% → WARN immediately")
    m = H.evaluate_source(_src(source="registry", attempted=16, rows=15, content=15), _prior([1.0] * 5))
    check(m.status == H.WARN and "dropped from 100%" in " ".join(m.reasons), "15/16 registry lookups after a 100% streak → WARN, no streak")
    m = H.evaluate_source(_src(source="registry", attempted=16, rows=16, content=16), _prior([1.0] * 5))
    check(m.status == H.OK, "16/16 → OK")
    m = H.evaluate_source(_src(source="registry", attempted=16, rows=15, content=15), [])
    check(m.status == H.WARN, "first-ever registry run at 15/16 → WARN (no prior 100% assumed clean)")


def test_staleness():
    print("\n[6] local-run staleness reads as an action item, never a system fault")
    base = {"last_scraped": "2026-08-11", "age_days": 13, "uncovered_count": 1, "stale": True}
    st, reasons = H.evaluate_staleness(base, 5, "franklin-oh")
    txt = " ".join(reasons)
    check(st == H.WARN, "13d-old run + 1 uncovered → WARN")
    check("needs a manual run" in txt and "python -m core.dockets.franklin" in txt and "not a system fault" in txt,
          "reason names the manual command and says it is not a system fault")
    st, _ = H.evaluate_staleness(dict(base, age_days=7, uncovered_count=0), 5, "hamilton-oh")
    check(st == H.OK, "7d old, fully covered → OK (soft bound is > 7d)")
    st, _ = H.evaluate_staleness(dict(base, age_days=8, uncovered_count=0), 5, "hamilton-oh")
    check(st == H.WARN, "8d old, fully covered → WARN")
    st, _ = H.evaluate_staleness(dict(base, age_days=29, uncovered_count=0), 5, "broward-fl")
    check(st == H.CRITICAL, "29d old (past the 28d lead window) → CRITICAL")
    st, _ = H.evaluate_staleness(dict(base, age_days=40), 0, "broward-fl")
    check(st == H.OK, "no published leads → OK regardless of age (expected empty)")


def test_history_and_chips():
    print("\n[7] history upsert + chip labels")
    hist = [{"date": "2026-08-18", "county": "cuyahoga-oh", "status": "OK"}]
    out = H.upsert_history([{"date": "2026-08-18", "county": "cuyahoga-oh", "status": "WARN"},
                            {"date": "2026-08-19", "county": "cuyahoga-oh", "status": "OK"}], hist)
    check(len(out) == 2 and out[0]["status"] == "WARN", "same-day re-run REPLACES the line (never double-counts)")
    chip = H.chip_for({"status": H.WARN, "sources": {"docket": {"status": H.OK}},
                       "local_run": {"status": H.WARN}})
    check(chip["label"] == "Needs local run" and chip["level"] == "warn", "local-only WARN → 'Needs local run'")
    chip = H.chip_for({"status": H.WARN, "sources": {"docket": {"status": H.WARN}}, "local_run": None})
    check(chip["label"] == "Degraded", "content WARN → 'Degraded'")
    chip = H.chip_for({"status": H.CRITICAL, "sources": {"docket": {"status": H.CRITICAL}}, "local_run": None})
    check(chip["label"] == "Collapsed", "content CRITICAL → 'Collapsed'")
    chip = H.chip_for({"status": H.OK, "sources": {}, "local_run": None})
    check(chip["label"] == "Healthy", "OK → 'Healthy'")
    chip = H.chip_for({"status": H.NOT_MONITORED, "sources": {}, "local_run": None})
    check(chip["level"] == "none", "no docket layer → no chip")


def test_historical_backtest():
    print("\n[8] historical backtest over the committed docket history (frozen ≤ 2026-08-24)")
    hist = H.replay(until="2026-08-24")
    by = {(h["county"], h["date"]): h for h in hist}
    check(len(hist) >= 480, f"replayed {len(hist)} county-runs")

    def status(c, d):
        return by.get((c, d), {}).get("status", "MISSING")

    # MUST fire
    check(status("broward-fl", "2026-08-08") == H.CRITICAL,
          "Broward 2026-08-08 → CRITICAL, first run (rows 4→6 but content 0)")
    check(all(status("broward-fl", f"2026-08-{d:02d}") == H.CRITICAL for d in range(9, 17)),
          "Broward stays CRITICAL 08-09…08-16 (the nine silent days)")
    for c, d, why in [("duval-fl", "2026-08-06", "ERR_CONNECTION_CLOSED on all 7"),
                      ("duval-fl", "2026-08-01", "timeout on all 8"),
                      ("duval-fl", "2026-08-21", "UCN search box did not render on all 13"),
                      ("cuyahoga-oh", "2026-07-13", "0/19"),
                      ("cuyahoga-oh", "2026-08-09", "0/13"),
                      ("miami-dade-fl", "2026-08-01", "every mortgage case: no navigation to searchResults"),
                      ("montgomery-oh", "2026-07-01", "0/6 (06-30 was 6/6)")]:
        check(status(c, d) == H.CRITICAL, f"{c} {d} → CRITICAL ({why})")
    e = by.get(("miami-dade-fl", "2026-08-20"), {})
    check(e.get("sources", {}).get("taxdeed", {}).get("status") == H.CRITICAL
          and e.get("sources", {}).get("docket", {}).get("status") == H.OK,
          "Miami 08-20: taxdeed source CRITICAL (RealTDM timeouts) while docket source OK — per-source isolation")
    check(status("cuyahoga-oh", "2026-07-08") == H.WARN,
          "Cuyahoga 07-08 → WARN (CV24108733-A…-I lettered-suffix blanket case, 9/19 unverified — kept on purpose)")
    check(status("cuyahoga-oh", "2026-08-08") == H.WARN,
          "Cuyahoga 08-08 → WARN (9 cases with content the day before came back empty — precursor to 08-09)")

    # MUST stay quiet
    for c, d, why in [("duval-fl", "2026-08-05", "day before the outage"),
                      ("duval-fl", "2026-08-07", "recovered"),
                      ("duval-fl", "2026-08-22", "recovered"),
                      ("broward-fl", "2026-08-07", "last good Broward day"),
                      ("miami-dade-fl", "2026-06-26", "8 tax-deed rows entered the window (structural skip, not failure)"),
                      ("miami-dade-fl", "2026-07-24", "tax-deed share day"),
                      ("miami-dade-fl", "2026-08-07", "5 rows, 4 content"),
                      ("summit-oh", "2026-07-08", "Summit's steady 1-unknown/day shape"),
                      ("cuyahoga-oh", "2026-07-07", "68% vs 100% — normal band"),
                      ("cuyahoga-oh", "2026-08-17", "17/17"),
                      ("montgomery-oh", "2026-06-30", "6/6"),
                      ("montgomery-oh", "2026-07-03", "recovered"),
                      ("hamilton-oh", "2026-08-17", "6 rows covering all qualifying leads out of 30 raw parcels"),
                      ("franklin-oh", "2026-08-11", "metadata-only rows"),
                      ("orange-fl", "2026-08-18", "16/16 registry lookups"),
                      ("orange-fl", "2026-08-24", "registry still 100%")]:
        check(status(c, d) == H.OK, f"{c} {d} → OK ({why})")

    non_ok = [h for h in hist if h["status"] in (H.WARN, H.CRITICAL)]
    check(len(non_ok) == 32, f"exactly 32 non-OK county-runs in the frozen window (got {len(non_ok)})")
    check(all("retrieval failure" in " ".join(h["reasons"]) or "no file" in " ".join(h["reasons"])
              for h in non_ok if h["status"] == H.CRITICAL),
          "every historical CRITICAL is a full retrieval-failure day")
    check(not any(h["local_run"] for h in hist), "replay never judges local staleness (no historical lead feed)")


def main() -> int:
    os.chdir(os.path.join(os.path.dirname(__file__), ".."))
    print("=" * 70)
    print("core.health — silent-degradation monitor acceptance")
    print("=" * 70)
    test_topology_in_sync()
    test_row_classifier_on_real_shapes()
    test_emptiness_rules()
    test_baseline_bands()
    test_registry_any_drop()
    test_staleness()
    test_history_and_chips()
    test_historical_backtest()
    print("\n" + "=" * 70)
    print(f"  {_PASS} passed, {_FAIL} failed")
    print("=" * 70)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
