"""
SurplusIQ — Data-health monitor for SILENT docket-layer degradation.

WHY: on 2026-08-08 the Broward docket layer started returning shells (rows
present, every one classification='unknown', no owner, no events, reason
"docket retrieval failed: no rendered ViewDetails trigger") and the daily cron
published them for nine days. Row count ROSE 4→6 that day; only the CONTENT
collapsed. File-existence / row-count monitoring cannot see this. This module
measures content, per county, per run, against the county's OWN recent history.

PURE + NETWORK-FREE: reads committed outputs only (data/dockets/*.jsonl,
data/raw/*.jsonl, data/health/history.jsonl). Deterministic → unit-testable and
replayable over git history (`--backtest`).

CORE METRIC (per county, per source, per run)
  attempted     in-window sold auction parcels the step was asked to check —
                the latest non-empty raw file dated ≤ run date (exactly what
                core.dockets.enrich.load_cases_from_raw feeds the scraper),
                filtered per source (registry: parseable Orange case numbers;
                taxdeed: Miami-Dade TAXDEED third-party sales).
  rows          rows written to that day's docket file for the source
  content_rows  rows with REAL parsed content (see classify_row)
  failed_rows   classification=='unknown' OR a retrieval-failure reason
  content_rate  content_rows / max(rows, 1)
  coverage      content_rows / max(attempted, 1)   (clamped to 1.0)
  Baseline = the county/source's own trailing-10-run median (runs with
  attempted ≥ 1). Scale-free, county-shape-free. No fixed thresholds.

EXPECTED vs UNEXPECTED EMPTINESS (anti-false-alarm core)
  attempted == 0                     → OK, "no in-window sales". Cannot alarm.
  attempted ≥ 1 and content_rows == 0 → CRITICAL immediately (the 08-08 shape).
  attempted ≥ 1 and rate collapsed   → WARN (< 0.6×baseline) /
                                        CRITICAL (< 0.3×baseline, 2 runs running)
  Orange registry: any drop from 100% coverage → WARN immediately (the
  invisible-reCAPTCHA silent pass may be leniency, not earned trust).
  Local-run counties (Broward/Franklin/Hamilton/Orange docket): uncovered feed
  cases or a run older than STALE_SOFT_DAYS → WARN; older than the lead window
  → CRITICAL.

SURFACES: docs/data/health.json (dashboard chip + banner), a `health` block in
summary.json, the cron "Data health gate" step (annotations, step summary,
non-zero exit on CRITICAL), an auto-issue and the healthchecks.io ping — the
last three live in .github/workflows/daily-refresh.yml and only read this
module's exit code / report.

PERSISTENCE: data/health/history.jsonl — one line per county per run date,
upserted (a same-day re-run replaces, never double-counts). Source of both the
baseline and the streak.

CLI:
  python -m core.health                       # gate for today (persists)
  python -m core.health --date 2026-08-08     # gate for a given run date
  python -m core.health --backtest            # replay all committed history
  python -m core.health --backfill --since 2026-08-04   # seed history.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional


def _find_project_root() -> Path:
    p = Path(__file__).resolve()
    for parent in [p] + list(p.parents):
        if (parent / "config" / "counties.py").exists():
            return parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _find_project_root()
RAW_DIR = PROJECT_ROOT / "data" / "raw"
DOCKETS_DIR = PROJECT_ROOT / "data" / "dockets"
HEALTH_DIR = PROJECT_ROOT / "data" / "health"
HISTORY_FILE = HEALTH_DIR / "history.jsonl"
DOCS_HEALTH_FILE = PROJECT_ROOT / "docs" / "data" / "health.json"

sys.path.insert(0, str(PROJECT_ROOT))
try:
    from config.counties import LEAD_WINDOW_DAYS, ALL_COUNTIES  # pure config
except Exception:                                            # pragma: no cover
    LEAD_WINDOW_DAYS, ALL_COUNTIES = 28, []

# ── Thresholds (the approved design) ─────────────────────────────────────
BASELINE_RUNS = 10          # trailing runs (with attempted ≥ 1) in the median
MIN_BASELINE_RUNS = 3       # fewer → only the immediate (content==0) rule applies
WARN_FRACTION = 0.60        # rate < 0.6 × baseline → WARN
CRITICAL_FRACTION = 0.30    # rate < 0.3 × baseline …
CRITICAL_STREAK = 2         # … for ≥ 2 consecutive runs → CRITICAL
STALE_SOFT_DAYS = 7         # local-run county: last run older than this → WARN
STALE_HARD_DAYS = LEAD_WINDOW_DAYS   # … older than the lead window → CRITICAL
SUMMARY_STALE_HOURS = 36    # dashboard banner when summary.generated_at is older

OK, WARN, CRITICAL, NOT_MONITORED = "OK", "WARN", "CRITICAL", "NOT_MONITORED"
_RANK = {NOT_MONITORED: 0, OK: 1, WARN: 2, CRITICAL: 3}

# ── Source topology ──────────────────────────────────────────────────────
# Mirrors core.dockets.enrich.WORKING_DOCKET_COUNTIES / core.dockets.LOCAL_RUN_
# COUNTIES (tests.test_health asserts they stay in sync). Kept local so this
# module never imports the Playwright-backed scraper package.
#   cron  — produced by the daily GitHub Actions run; a MISSING file on a
#           full-pipeline day is a run that wrote nothing (alarm if attempted).
#   local — produced by a manual/residential run; absence is staleness, not a
#           failed run.
CRON_SOURCES: dict[str, list[str]] = {
    "cuyahoga-oh":   ["docket"],
    "montgomery-oh": ["docket"],
    "summit-oh":     ["docket"],
    "miami-dade-fl": ["docket", "taxdeed"],
    "duval-fl":      ["docket"],
    "orange-fl":     ["registry"],
}
LOCAL_SOURCES: dict[str, list[str]] = {
    "broward-fl":  ["docket"],
    "franklin-oh": ["docket"],
    "hamilton-oh": ["docket"],
    # orange-fl's manual docket portal run is PARKED (per-search CAPTCHA); its
    # verification layer is the cloud registry above. Not scored as a source.
}
# Counties whose docket layer refreshes only on a manual/residential run —
# judged for STALENESS against the published lead feed (= core.dockets.
# LOCAL_RUN_COUNTIES; the dashboard's original _local_status set).
LOCAL_RUN_COUNTIES = {"orange-fl", "franklin-oh", "broward-fl", "hamilton-oh"}
LOCAL_RUN_COMMAND = {
    "franklin-oh": "python -m core.dockets.franklin (residential IP)",
    "broward-fl":  "python -m core.dockets.broward (residential IP, genuine-Chrome bridge)",
    "hamilton-oh": "python -m core.dockets.hamilton (residential IP, genuine-Chrome bridge)",
    "orange-fl":   "python -m core.dockets.orange_registry (cloud registry lookup)",
}
MONITORED_COUNTIES = sorted(set(CRON_SOURCES) | set(LOCAL_SOURCES) | LOCAL_RUN_COUNTIES)
ALL_COUNTY_IDS = [c.id for c in ALL_COUNTIES] or MONITORED_COUNTIES + ["lee-fl"]

# File-name prefix that holds a source's rows (all live in data/dockets/).
_SOURCE_FILE_PREFIX = {"taxdeed": "{county}-taxdeed", "docket": "{county}", "registry": "{county}"}

# Retrieval-failure vocabulary (classification_reason). Ground-truthed on the
# committed history: Broward 08-08 "no rendered ViewDetails trigger", Duval
# 08-01 "timeout", Duval 08-06 "net::ERR_CONNECTION_CLOSED", Miami-Dade
# "no navigation to searchResults", OH "scrape produced no data".
FAILURE_PATTERN = re.compile(
    r"retrieval failed|no rendered|challenge|captcha|timeout|blocked|"
    r"inaccessible|gated|no results|produced no data|connection_closed|"
    r"no navigation",
    re.I,
)
# STRUCTURAL SKIP — the step explicitly declined the case (it belongs to another
# source): Miami-Dade's docket scraper refuses tax-deed cases, which the RealTDM
# taxdeed source owns. Not a failure, not content: excluded from the rate.
SKIP_PATTERN = re.compile(r"routing not implemented", re.I)
CONTENT_CLASSES = {"green", "yellow", "red", "killed"}
_ORANGE_CASE_RE = re.compile(r"^\d{4}-[A-Z]{2}-\d{6}-[A-Z]$")


# ═════════════════════════════════════════════════════════════════════════
# Row-level classification
# ═════════════════════════════════════════════════════════════════════════
def row_source(row: dict) -> str:
    """Which layer produced this docket-dir row."""
    if "registry_status" in row:
        return "registry"
    if "taxdeed_verdict" in row:
        return "taxdeed"
    return "docket"


def classify_row(row: dict) -> str:
    """'content' | 'failed' | 'skipped' | 'empty'.

    skipped — a structural skip (SKIP_PATTERN): the case belongs to another
              source; excluded from the rate entirely.
    content — real parsed content: a green/yellow/red/killed classification, OR
              a non-empty owner, OR kill signals / events, OR prayer > 0, OR a
              registry stage, OR a tax-deed status, OR evidence_level
              'docket_checked' with a non-unknown class (Franklin metadata-only,
              Hamilton no-debt).
    failed  — classification 'unknown' OR a retrieval-failure reason.
    empty   — neither (a row that carries nothing either way).
    A row is never both: content wins only when failed is false, and the two
    vocabularies are disjoint on the committed history (tests assert it).
    """
    src = row_source(row)
    cls = (row.get("classification") or "").strip().lower()
    reason = row.get("classification_reason") or ""
    if src == "taxdeed":
        # RealTDM: a parsed status is content; an empty status is the scraper's
        # own failure shape ("case not found" / "scrape error").
        if (row.get("taxdeed_status") or "").strip():
            return "content"
        return "failed"
    if SKIP_PATTERN.search(reason):
        return "skipped"
    failed = cls == "unknown" or bool(FAILURE_PATTERN.search(reason))
    if failed:
        return "failed"
    try:
        prayer = float(row.get("prayer_amount") or 0)
    except (TypeError, ValueError):
        prayer = 0.0
    content = (
        cls in CONTENT_CLASSES
        or bool((row.get("owner_name") or "").strip())
        or bool(row.get("kill_signals"))
        or bool(row.get("events"))
        or prayer > 0
        or bool(row.get("registry_status"))
        or (row.get("evidence_level") == "docket_checked" and cls != "unknown")
    )
    return "content" if content else "empty"


# ═════════════════════════════════════════════════════════════════════════
# Inputs: raw feed + docket files (committed outputs only)
# ═════════════════════════════════════════════════════════════════════════
def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _date_of(path: Path, prefix: str) -> Optional[str]:
    m = re.fullmatch(re.escape(prefix) + r"_(\d{4}-\d{2}-\d{2})\.jsonl", path.name)
    return m.group(1) if m else None


def latest_raw_file(county_id: str, run_date: str, raw_dir: Path = RAW_DIR) -> Optional[Path]:
    """Latest NON-EMPTY raw file dated ≤ run_date — what enrich.py fed the
    scraper that day (it takes the latest non-empty raw; on the run day that is
    the file the auction step just wrote, or the previous one if it was empty)."""
    best = None
    for f in raw_dir.glob(f"{county_id}_*.jsonl"):
        d = _date_of(f, county_id)
        if d and d <= run_date and f.stat().st_size > 0:
            if best is None or d > best[0]:
                best = (d, f)
    return best[1] if best else None


def load_raw_cases(county_id: str, run_date: str, raw_dir: Path = RAW_DIR) -> list[dict]:
    f = latest_raw_file(county_id, run_date, raw_dir)
    return _read_jsonl(f) if f else []


def docket_file(county_id: str, source: str, run_date: str, dockets_dir: Path = DOCKETS_DIR) -> Path:
    prefix = _SOURCE_FILE_PREFIX[source].format(county=county_id)
    return dockets_dir / f"{prefix}_{run_date}.jsonl"


def load_source_rows(county_id: str, source: str, run_date: str,
                     dockets_dir: Path = DOCKETS_DIR) -> tuple[bool, list[dict]]:
    """(file_present, rows-of-this-source). orange-fl_<date>.jsonl can hold
    registry rows (cloud) or manual docket rows (local) — split by row shape."""
    f = docket_file(county_id, source, run_date, dockets_dir)
    if not f.exists():
        return False, []
    rows = _read_jsonl(f)
    mine = [r for r in rows if row_source(r) == source]
    # A file holding only ANOTHER source's rows is not this source's run
    # (orange-fl_<date>.jsonl with registry rows ≠ a manual docket run). An
    # empty file IS a run that wrote nothing.
    return (bool(mine) or not rows), mine


def normalize_case(case_number: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", case_number or "").strip().upper()


def attempted_cases(county_id: str, source: str, raw_records: Iterable[dict]) -> set[str]:
    """Normalized case numbers the source was asked to check (parcels for
    docket; each parcel of a blanket group emits its own row, so parcels ==
    rows on a clean run)."""
    out = set()
    for r in raw_records:
        cn = r.get("case_number") or ""
        if not cn:
            continue
        if source == "registry":
            if not _ORANGE_CASE_RE.match(normalize_case(cn)):
                continue          # unparseable → skipped by the scraper, never attempted
        elif source == "taxdeed":
            if (r.get("auction_type") or "").strip().upper() != "TAXDEED" or not r.get("is_third_party"):
                continue
        elif source == "docket" and "taxdeed" in CRON_SOURCES.get(county_id, []):
            if (r.get("auction_type") or "").strip().upper() == "TAXDEED":
                continue          # owned by the taxdeed source; the docket step declines it
        out.add(cn.strip().upper())   # parcel identity (keep the auction suffix)
    return out


# ═════════════════════════════════════════════════════════════════════════
# Metrics
# ═════════════════════════════════════════════════════════════════════════
@dataclass
class SourceMetrics:
    source: str
    kind: str                      # 'cron' | 'local'
    file_present: bool
    attempted: int
    rows: int
    content_rows: int
    failed_rows: int
    skipped_rows: int
    content_rate: float
    coverage: float
    baseline: Optional[float] = None       # trailing median of min(rate, coverage)
    baseline_runs: int = 0
    effective_rate: float = 0.0            # min(content_rate, coverage)
    low_streak: int = 0                    # consecutive runs < CRITICAL_FRACTION×baseline
    status: str = OK
    reasons: list = field(default_factory=list)


def compute_source_metrics(county_id: str, source: str, kind: str,
                           file_present: bool, rows: list[dict],
                           raw_records: list[dict]) -> SourceMetrics:
    attempted_set = attempted_cases(county_id, source, raw_records)
    attempted = len(attempted_set)
    if not raw_records:
        # No raw feed at all (pre-history fixture days): fall back to the rows'
        # own auction cross-check so a run is never scored against nothing.
        attempted = sum(1 for r in rows if r.get("_auction_data"))
    kinds = [classify_row(r) for r in rows]
    content = kinds.count("content")
    failed = kinds.count("failed")
    skipped = kinds.count("skipped")
    n = len(rows) - skipped
    rate = content / max(n, 1)
    coverage = min(1.0, content / max(attempted, 1))
    m = SourceMetrics(source=source, kind=kind, file_present=file_present,
                      attempted=attempted, rows=n, content_rows=content,
                      failed_rows=failed, skipped_rows=skipped,
                      content_rate=round(rate, 4), coverage=round(coverage, 4))
    # Cron sources process every attempted parcel in one pass, so coverage is a
    # real signal (a crash that skips cases hides from content_rate alone).
    # Local runs are resumable/partial by design — their "are the published
    # leads covered?" question is the staleness check, not coverage.
    if kind == "cron" and attempted:
        m.effective_rate = round(min(rate, coverage), 4)
    else:
        m.effective_rate = round(rate, 4)
    return m


def _baseline(prior: list[dict]) -> tuple[Optional[float], int]:
    """Median effective rate of the trailing BASELINE_RUNS prior runs that had
    auction cases. None until MIN_BASELINE_RUNS such runs exist."""
    rates = [p["effective_rate"] for p in prior if p.get("attempted", 0) >= 1][-BASELINE_RUNS:]
    if len(rates) < MIN_BASELINE_RUNS:
        return None, len(rates)
    return round(statistics.median(rates), 4), len(rates)


def evaluate_source(m: SourceMetrics, prior: list[dict]) -> SourceMetrics:
    """Severity for one source given its prior history (oldest→newest dicts of
    SourceMetrics). Mutates and returns m."""
    m.baseline, m.baseline_runs = _baseline(prior)
    prev_streak = prior[-1].get("low_streak", 0) if prior else 0
    reasons: list[str] = []
    status = OK

    if m.attempted == 0:
        m.status, m.reasons, m.low_streak = OK, ["no in-window sales — expected empty"], 0
        return m

    if m.content_rows == 0:
        # The 08-08 shape: cases to check, nothing real came back. No streak.
        what = ("no file written" if not m.file_present
                else f"{m.rows} rows, 0 with content ({m.failed_rows} retrieval failures)")
        m.status = CRITICAL
        m.low_streak = prev_streak + 1
        m.reasons = [f"{m.attempted} case(s) attempted, {what}"]
        return m

    low = m.baseline is not None and m.effective_rate < CRITICAL_FRACTION * m.baseline
    m.low_streak = prev_streak + 1 if low else 0

    if m.baseline is not None:
        if low and m.low_streak >= CRITICAL_STREAK:
            status = CRITICAL
            reasons.append(f"content rate {m.effective_rate:.0%} < 30% of baseline "
                           f"{m.baseline:.0%} for {m.low_streak} consecutive runs")
        elif m.effective_rate < WARN_FRACTION * m.baseline:
            status = WARN
            reasons.append(f"content rate {m.effective_rate:.0%} < 60% of baseline "
                           f"{m.baseline:.0%} ({m.content_rows}/{m.rows} rows, "
                           f"{m.attempted} attempted)")

    if m.source == "registry" and m.coverage < 1.0:
        # Any drop from 100% pass is a WARN on its own — leniency may be ending.
        prior_cov = [p["coverage"] for p in prior if p.get("attempted", 0) >= 1][-BASELINE_RUNS:]
        if not prior_cov or max(prior_cov) >= 1.0:
            status = max(status, WARN, key=_RANK.get)
            reasons.append(f"registry coverage {m.coverage:.0%} — dropped from 100% "
                           f"({m.attempted - m.content_rows} lookup(s) not checked: "
                           f"challenge/timeout)")

    m.status, m.reasons = status, reasons or ["within normal band"]
    return m


# ═════════════════════════════════════════════════════════════════════════
# Local-run coverage / staleness (generalised from dashboard_data._local_status)
# ═════════════════════════════════════════════════════════════════════════
def local_run_coverage(county_id: str, auction_cases: Iterable[str],
                       today: Optional[date] = None,
                       dockets_dir: Path = DOCKETS_DIR) -> dict:
    """Docket coverage of the current auction feed for a LOCAL-RUN county.
    Returns {covered:set, last_scraped:'YYYY-MM-DD', uncovered_count, stale,
    age_days}. `stale` keeps the dashboard's original meaning: any uncovered
    feed case, or no run, or a run older than the lead window."""
    today = today or date.today()
    covered, last = set(), ""
    for f in sorted(dockets_dir.glob(f"{county_id}_*.jsonl")):
        fd = _date_of(f, county_id)
        if fd and fd > today.isoformat():
            continue                          # replay: ignore runs after `today`
        for r in _read_jsonl(f):
            cn = r.get("case_number", "")
            if cn:
                covered.add(normalize_case(cn))
            sa = r.get("scraped_at", "") or ""
            if sa > last:
                last = sa
    auction = {normalize_case(c) for c in auction_cases if c}
    uncovered = auction - covered
    last_day = last[:10]
    try:
        age = (today - date.fromisoformat(last_day)).days if last_day else None
    except ValueError:
        age = None
    cutoff = (today - timedelta(days=LEAD_WINDOW_DAYS)).isoformat()
    return {
        "covered": covered,
        "last_scraped": last_day,
        "uncovered_count": len(uncovered),
        "uncovered": sorted(uncovered),
        "age_days": age,
        "stale": bool(uncovered) or (not last_day) or (last_day < cutoff),
    }


def evaluate_staleness(cov: dict, feed_size: int, county_id: str = "") -> tuple[str, list[str]]:
    """Severity of a local-run county's docket freshness. The wording is
    deliberate: this is NOT a system fault — it means a human needs to run the
    county's local scraper (the cloud cron cannot)."""
    if feed_size == 0:
        return OK, ["no in-window sales — expected empty"]
    cmd = LOCAL_RUN_COMMAND.get(county_id, "the county's local docket scraper")
    how = f"not a system fault — needs a manual run: {cmd}"
    age = cov.get("age_days")
    if not cov.get("last_scraped"):
        return CRITICAL, [f"never scraped locally — {feed_size} published lead(s) unverified", how]
    reasons = []
    status = OK
    if age is not None and age > STALE_HARD_DAYS:
        status = CRITICAL
        reasons.append(f"last local run {cov['last_scraped']} is {age}d old — past the "
                       f"{STALE_HARD_DAYS}d lead window")
    elif age is not None and age > STALE_SOFT_DAYS:
        status = WARN
        reasons.append(f"last local run {cov['last_scraped']} is {age}d old (> {STALE_SOFT_DAYS}d)")
    if cov.get("uncovered_count"):
        status = max(status, WARN, key=_RANK.get)
        reasons.append(f"{cov['uncovered_count']} published lead(s) not covered by the last local run "
                       f"(shown docket-not-verified)")
    if status == OK:
        return OK, [f"covered — last local run {cov['last_scraped']}"]
    return status, reasons + [how]


# ═════════════════════════════════════════════════════════════════════════
# History (data/health/history.jsonl)
# ═════════════════════════════════════════════════════════════════════════
def load_history(path: Path = HISTORY_FILE) -> list[dict]:
    rows = _read_jsonl(path) if path.exists() else []
    rows.sort(key=lambda r: (r.get("date", ""), r.get("county", "")))
    return rows


def upsert_history(entries: list[dict], history: list[dict]) -> list[dict]:
    """Replace any existing (date, county) line; keep sorted. Pure."""
    keyed = {(h["date"], h["county"]): h for h in history}
    for e in entries:
        keyed[(e["date"], e["county"])] = e
    return [keyed[k] for k in sorted(keyed)]


def write_history(history: list[dict], path: Path = HISTORY_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for h in history:
            fh.write(json.dumps(h, sort_keys=True) + "\n")


def _prior_source_history(history: list[dict], county_id: str, source: str,
                          before_date: str) -> list[dict]:
    """Oldest→newest source dicts for (county, source) strictly before
    before_date. Carried-forward copies are excluded so a quiet day never
    re-counts the same run in the baseline or the streak."""
    out = []
    for h in history:
        if h.get("county") != county_id or h.get("date", "") >= before_date:
            continue
        s = (h.get("sources") or {}).get(source)
        if s and not s.get("carried_from"):
            out.append(dict(s, _date=h.get("date", "")))
    return out


# ═════════════════════════════════════════════════════════════════════════
# Per-county evaluation for one run date
# ═════════════════════════════════════════════════════════════════════════
def evaluate_county_run(county_id: str, run_date: str, history: list[dict],
                        *, today: Optional[date] = None,
                        raw_dir: Path = RAW_DIR, dockets_dir: Path = DOCKETS_DIR,
                        auction_cases: Optional[Iterable[str]] = None,
                        cron_day: bool = True) -> dict:
    """Build the history entry for (county, run_date).

    cron_day     — the full daily pipeline ran on run_date, so a cron source
                   with no file is a run that wrote nothing (scored). In a
                   replay/backtest this is inferred from file presence.
    auction_cases — the PUBLISHED in-window lead cases (loader-qualified) for
                   local-run coverage/staleness. None ⇒ staleness not judged
                   (replay); the gate/dashboard pass the loader's leads.
    """
    today = today or date.today()
    raw = load_raw_cases(county_id, run_date, raw_dir)
    feed_cases = list(auction_cases) if auction_cases is not None else []

    sources: dict[str, dict] = {}
    worst, reasons = OK, []

    for kind, table in (("cron", CRON_SOURCES), ("local", LOCAL_SOURCES)):
        for source in table.get(county_id, []):
            present, rows = load_source_rows(county_id, source, run_date, dockets_dir)
            if kind == "local" and not present:
                continue                      # local absence = staleness, below
            if kind == "cron" and not present and not cron_day:
                # No pipeline today (local regen / partial run): carry the LAST
                # KNOWN run so the dashboard shows real state, not a phantom
                # collapse. Excluded from baselines/streaks (see _prior_…).
                prior = _prior_source_history(history, county_id, source, run_date)
                if not prior:
                    continue
                last = dict(prior[-1])
                last["carried_from"] = last.pop("_date", "")
                sources[source] = last
                if _RANK[last["status"]] > _RANK[worst]:
                    worst = last["status"]
                if last["status"] != OK:
                    reasons += [f"{source} (last run {last['carried_from']}): {r}" for r in last["reasons"]]
                continue
            m = compute_source_metrics(county_id, source, kind, present, rows, raw)
            evaluate_source(m, _prior_source_history(history, county_id, source, run_date))
            sources[source] = asdict(m)
            if _RANK[m.status] > _RANK[worst]:
                worst = m.status
            if m.status != OK:
                reasons += [f"{source}: {r}" for r in m.reasons]

    stale = None
    if county_id in LOCAL_RUN_COUNTIES and auction_cases is not None:
        cov = local_run_coverage(county_id, feed_cases, today=today, dockets_dir=dockets_dir)
        s_status, s_reasons = evaluate_staleness(
            cov, len(set(normalize_case(c) for c in feed_cases if c)), county_id)
        stale = {"last_scraped": cov["last_scraped"], "age_days": cov["age_days"],
                 "uncovered_count": cov["uncovered_count"], "stale": cov["stale"],
                 "status": s_status, "reasons": s_reasons}
        if _RANK[s_status] > _RANK[worst]:
            worst = s_status
        if s_status != OK:
            reasons += [f"local run: {r}" for r in s_reasons]

    if not sources and stale is None:
        worst, reasons = NOT_MONITORED, ["no docket layer"]

    return {
        "date": run_date,
        "county": county_id,
        "status": worst,
        "reasons": reasons or ["healthy"],
        "sources": sources,
        "local_run": stale,
    }


# ═════════════════════════════════════════════════════════════════════════
# Report (docs/data/health.json + summary.json block)
# ═════════════════════════════════════════════════════════════════════════
def feed_cases_from_loader() -> dict[str, list[str]]:
    """{county_id: [case_number, …]} of the loader-qualified in-window leads —
    the same set the dashboard publishes, so 'uncovered' means a lead the user
    can see whose docket the local run never touched. Pure (committed data)."""
    from core.loader import load_all_leads
    out: dict[str, list[str]] = {}
    for l in load_all_leads():
        out.setdefault(l.county_id, []).append(l.case_number)
    return out


def build_report(run_date: Optional[str] = None, *, history: Optional[list[dict]] = None,
                 today: Optional[date] = None, raw_dir: Path = RAW_DIR,
                 dockets_dir: Path = DOCKETS_DIR, cron_day: bool = True,
                 auction_cases_by_county: Optional[dict] = None,
                 judge_staleness: bool = True) -> dict:
    """Evaluate every county for run_date against prior history. Pure — the
    caller decides whether to persist. Returns {report, entries}."""
    today = today or date.today()
    run_date = run_date or today.isoformat()
    history = load_history() if history is None else history
    prior = [h for h in history if h.get("date", "") < run_date]
    if judge_staleness and auction_cases_by_county is None:
        auction_cases_by_county = feed_cases_from_loader()

    entries, counties = [], {}
    for cid in ALL_COUNTY_IDS:
        if cid not in MONITORED_COUNTIES:
            counties[cid] = {"status": NOT_MONITORED, "reasons": ["no docket layer"],
                             "sources": {}, "local_run": None}
            continue
        ac = (auction_cases_by_county or {}).get(cid, []) if judge_staleness else None
        e = evaluate_county_run(cid, run_date, prior, today=today, raw_dir=raw_dir,
                                dockets_dir=dockets_dir, auction_cases=ac, cron_day=cron_day)
        entries.append(e)
        counties[cid] = {k: e[k] for k in ("status", "reasons", "sources", "local_run")}

    for cid, c in counties.items():
        c["chip"] = chip_for(c)
        c["reason"] = "; ".join(c["reasons"])
        c["local_run_county"] = cid in LOCAL_RUN_COUNTIES
    overall = OK
    for c in counties.values():
        if _RANK[c["status"]] > _RANK[overall]:
            overall = c["status"]
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_date": run_date,
        "overall": overall,
        "critical": sorted(c for c, v in counties.items() if v["status"] == CRITICAL),
        "warn": sorted(c for c, v in counties.items() if v["status"] == WARN),
        "counties": counties,
        "thresholds": {
            "baseline_runs": BASELINE_RUNS, "min_baseline_runs": MIN_BASELINE_RUNS,
            "warn_fraction": WARN_FRACTION, "critical_fraction": CRITICAL_FRACTION,
            "critical_streak": CRITICAL_STREAK, "stale_soft_days": STALE_SOFT_DAYS,
            "stale_hard_days": STALE_HARD_DAYS, "summary_stale_hours": SUMMARY_STALE_HOURS,
        },
    }
    return {"report": report, "entries": entries}


def chip_for(county: dict) -> dict:
    """Dashboard chip {label, level}. A local-run county whose ONLY problem is
    staleness reads as an action item ("Needs local run"), never as a system
    fault; content collapse reads Degraded/Collapsed."""
    st = county["status"]
    if st == NOT_MONITORED:
        return {"label": "No docket layer", "level": "none"}
    if st == OK:
        return {"label": "Healthy", "level": "ok"}
    sources_bad = any(m.get("status") not in (OK, None) for m in (county.get("sources") or {}).values())
    lr = county.get("local_run") or {}
    if not sources_bad and lr.get("status") in (WARN, CRITICAL):
        return ({"label": "Needs local run", "level": "warn"} if st == WARN
                else {"label": "Local run overdue", "level": "critical"})
    return ({"label": "Degraded", "level": "warn"} if st == WARN
            else {"label": "Collapsed", "level": "critical"})


def summary_block(report: dict) -> dict:
    """Compact block embedded in docs/data/summary.json."""
    return {
        "overall": report["overall"],
        "run_date": report["run_date"],
        "critical": report["critical"],
        "warn": report["warn"],
        "counties": {cid: {"status": v["status"], "reason": v["reason"], "chip": v["chip"]}
                     for cid, v in report["counties"].items()},
        "summary_stale_hours": SUMMARY_STALE_HOURS,
    }


# ═════════════════════════════════════════════════════════════════════════
# Replay / backtest over committed history
# ═════════════════════════════════════════════════════════════════════════
def _all_run_dates(dockets_dir: Path = DOCKETS_DIR) -> list[str]:
    dates = set()
    for f in dockets_dir.glob("*_20??-??-??.jsonl"):
        m = re.search(r"_(\d{4}-\d{2}-\d{2})\.jsonl$", f.name)
        if m:
            dates.add(m.group(1))
    return sorted(dates)


def replay(since: Optional[str] = None, until: Optional[str] = None, *,
           raw_dir: Path = RAW_DIR, dockets_dir: Path = DOCKETS_DIR) -> list[dict]:
    """Rebuild history from the committed docket files, oldest→newest, each
    day evaluated against the replayed days before it. A county-day is a run
    only if it wrote a file (a missing cron file in the past cannot be told
    apart from 'the docket step was not run'). Local-run staleness is NOT
    replayed (it is judged against the loader's live lead feed, which has no
    historical form). Never touches disk."""
    history: list[dict] = []
    for d in _all_run_dates(dockets_dir):
        if (since and d < since) or (until and d > until):
            continue
        day_entries = []
        for cid in MONITORED_COUNTIES:
            has_file = any(load_source_rows(cid, s, d, dockets_dir)[0]
                           for s in CRON_SOURCES.get(cid, []) + LOCAL_SOURCES.get(cid, []))
            if not has_file:
                continue          # no run that day (or a local county between runs)
            e = evaluate_county_run(cid, d, history, today=date.fromisoformat(d),
                                    raw_dir=raw_dir, dockets_dir=dockets_dir, cron_day=False)
            day_entries.append(e)
        history = upsert_history(day_entries, history)
    return history


def backtest(since: Optional[str] = None) -> int:
    """Acceptance gate: MUST fire CRITICAL on broward-fl 2026-08-08. Prints
    every non-OK county-day for review and returns 0/1."""
    hist = replay(since)
    alarms = [h for h in hist if h["status"] in (WARN, CRITICAL)]
    runs = len(hist)
    print(f"Replayed {runs} county-runs over {len(_all_run_dates())} dates; "
          f"{len(alarms)} non-OK ({sum(h['status'] == CRITICAL for h in alarms)} CRITICAL, "
          f"{sum(h['status'] == WARN for h in alarms)} WARN)\n")
    print(f"{'date':10}  {'county':14} {'status':8} {'att':>3} {'rows':>4} {'cont':>4} "
          f"{'fail':>4} {'skip':>4} {'rate':>5} {'cov':>5} {'base':>5}  reason")
    for h in hist:
        for s, m in h["sources"].items():
            flag = h["status"] if h["status"] != OK else ""
            if not flag and not os.environ.get("HEALTH_BACKTEST_VERBOSE"):
                continue
            base = f"{m['baseline']:.2f}" if m["baseline"] is not None else "  —"
            print(f"{h['date']:10}  {h['county']:14} {flag:8} {m['attempted']:3} {m['rows']:4} "
                  f"{m['content_rows']:4} {m['failed_rows']:4} {m['skipped_rows']:4} {m['content_rate']:5.2f} "
                  f"{m['coverage']:5.2f} {base:>5}  {s}: {'; '.join(m['reasons'])}")
        if h["local_run"] and h["local_run"]["status"] != OK:
            print(f"{h['date']:10}  {h['county']:14} {h['status']:8} {'':37} "
                  f"local: {'; '.join(h['local_run']['reasons'])}")

    target = next((h for h in hist if h["county"] == "broward-fl" and h["date"] == "2026-08-08"), None)
    ok = bool(target) and target["status"] == CRITICAL
    print(f"\nACCEPTANCE — broward-fl 2026-08-08 fires CRITICAL: {'PASS' if ok else 'FAIL'}"
          + (f"  ({'; '.join(target['reasons'])})" if target else "  (no run found)"))
    return 0 if ok else 1


# ═════════════════════════════════════════════════════════════════════════
# CLI: gate / backfill
# ═════════════════════════════════════════════════════════════════════════
def is_cron_day() -> bool:
    """True when the FULL daily pipeline ran today (the workflow exports
    HEALTH_CRON_DAY=1 on that path). Locally / on partial runs it is unset."""
    return os.environ.get("HEALTH_CRON_DAY", "").strip() in ("1", "true", "yes")


def _gh_annotate(level: str, msg: str) -> None:
    print(f"::{level}::{msg}")


def _step_summary(report: dict, entries: list[dict]) -> str:
    lines = [f"## Data health gate — {report['run_date']} — **{report['overall']}**", "",
             "| County | Status | Source | Attempted | Rows | Content | Failed | Skipped | Rate | Coverage | Baseline | Reason |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    icon = {OK: "🟢", WARN: "🟡", CRITICAL: "🔴", NOT_MONITORED: "⚪"}
    for e in entries:
        if not e["sources"] and not e["local_run"]:
            continue
        for s, m in e["sources"].items():
            base = f"{m['baseline']:.0%}" if m["baseline"] is not None else "—"
            lines.append(f"| {e['county']} | {icon[m['status']]} {m['status']} | {s} | {m['attempted']} | "
                         f"{m['rows']} | {m['content_rows']} | {m['failed_rows']} | {m['skipped_rows']} | {m['content_rate']:.0%} | "
                         f"{m['coverage']:.0%} | {base} | {'; '.join(m['reasons'])} |")
        if e["local_run"]:
            lr = e["local_run"]
            lines.append(f"| {e['county']} | {icon[lr['status']]} {lr['status']} | local run | | | | | | | | | "
                         f"{'; '.join(lr['reasons'])} (last {lr['last_scraped'] or 'never'}) |")
    return "\n".join(lines) + "\n"


def run_gate(run_date: Optional[str], persist: bool = True, cron_day: bool = True,
             drill: bool = False) -> int:
    """The cron 'Data health gate'. Persists history + docs/data/health.json,
    prints ::error::/::warning:: annotations + the step summary, writes
    overall/critical/warn to GITHUB_OUTPUT, exits 2 on CRITICAL.
    --drill: everything real is computed and persisted, then the verdict is
    FORCED to CRITICAL (exit 2, annotation) to exercise the publish-then-fail
    ordering + auto-issue path end to end. health.json keeps the real status."""
    history = load_history()
    out = build_report(run_date, history=history, cron_day=cron_day)
    report, entries = out["report"], out["entries"]

    if persist:
        write_history(upsert_history(entries, history))
        DOCS_HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        DOCS_HEALTH_FILE.write_text(json.dumps(report, indent=1))

    print(f"Data health — run {report['run_date']} — overall {report['overall']}")
    for e in entries:
        print(f"  {e['county']:14} {e['status']:8} {'; '.join(e['reasons'])}")
    for cid in report["critical"]:
        _gh_annotate("error", f"[data-health] {cid} CRITICAL — "
                              f"{'; '.join(report['counties'][cid]['reasons'])}")
    for cid in report["warn"]:
        _gh_annotate("warning", f"[data-health] {cid} WARN — "
                                f"{'; '.join(report['counties'][cid]['reasons'])}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as fh:
            fh.write(_step_summary(report, entries))
    overall, critical = report["overall"], list(report["critical"])
    if drill:
        overall, critical = CRITICAL, critical or ["DRILL"]
        _gh_annotate("error", "[data-health] DRILL — verdict forced to CRITICAL to prove "
                              "publish-then-fail ordering; the published health.json is real")
        print("DRILL: forcing CRITICAL exit (real status above was "
              f"{report['overall']})")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as fh:
            fh.write(f"overall={overall}\n")
            fh.write(f"critical={','.join(critical)}\n")
            fh.write(f"warn={','.join(report['warn'])}\n")
            fh.write(f"drill={'true' if drill else 'false'}\n")
    return 2 if overall == CRITICAL else 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="run date to gate (default: today)")
    ap.add_argument("--backtest", action="store_true", help="replay committed history, print alarms, check the acceptance gate")
    ap.add_argument("--backfill", action="store_true", help="replay and WRITE data/health/history.jsonl")
    ap.add_argument("--since", help="earliest date for --backtest/--backfill")
    ap.add_argument("--no-persist", action="store_true", help="gate without writing history/health.json")
    ap.add_argument("--cron-day", dest="cron_day", action="store_true", default=None,
                    help="full daily pipeline ran today: a missing cron file IS a failed run "
                         "(default: HEALTH_CRON_DAY=1 in the environment)")
    ap.add_argument("--not-cron-day", dest="cron_day", action="store_false",
                    help="partial/local run: a missing cron file carries the last known run")
    ap.add_argument("--drill", action="store_true",
                    help="force a CRITICAL verdict after a real evaluation (ordering/auto-issue drill)")
    a = ap.parse_args(argv)
    cron_day = is_cron_day() if a.cron_day is None else a.cron_day

    if a.backtest:
        return backtest(a.since)
    if a.backfill:
        hist = replay(a.since)
        write_history(upsert_history(hist, load_history()))
        print(f"Backfilled {len(hist)} county-run lines into {HISTORY_FILE.relative_to(PROJECT_ROOT)}")
        return 0
    return run_gate(a.date, persist=not a.no_persist, cron_day=cron_day, drill=a.drill)


if __name__ == "__main__":
    sys.exit(main())
