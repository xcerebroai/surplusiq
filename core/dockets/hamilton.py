"""
SurplusIQ — Hamilton County (OH) Docket Scraper — LOCAL-RUN, genuine-Chrome bridge.

Hamilton's clerk portal (courtclerk.org) puts case search behind Cloudflare
Turnstile. The token auto-issues to a GENUINE residential browser (proven
2026-08-17) but NOT from the CI datacenter IP (Actions probe 32083163234) and NOT
under any Playwright/CDP automation — the same shape as Broward. So Hamilton is a
LOCAL-RUN county driven by the unpacked bridge extension (core/dockets/
hamilton_extension), exactly like Broward: the runner serves a localhost queue and
the extension's content script runs the whole search flow in real Chrome and posts
each docket back.

FLOW (see the extension): search page (records-search/common-pleas-civil-case-
search) → auto-issued Turnstile token → inject sec=history → submit → case_summary.
php?sec=history → the docket-event table (Date|Description|Notes|Amount).

DEBT: Hamilton exposes NO judgment amount and gates documents, so this NEVER emits
a debt figure — leads stay apparent_surplus, debt_source empty. Classification is
kill-signal detection only, via core.dockets.hamilton_classify (Hamilton's OWN
"EXCESS FUNDS" vocabulary, ground-truthed on 6 real dockets). Owner comes from the
"PLAINTIFF vs. DEFENDANT" caption.

One-time setup (Chrome 151 removed CLI --load-extension):
  python3 -m core.dockets.hamilton --selftest   # launches Chrome on the profile
  chrome://extensions → Developer mode → Load unpacked → core/dockets/hamilton_extension
Then:  python3 -m core.dockets.hamilton
"""
from __future__ import annotations
import argparse
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from .base import DocketScraper, DocketResult, DocketEvent
from .hamilton_classify import classify_hamilton

PORTAL_ROOT = "https://www.courtclerk.org/"
SEARCH_URL = "https://www.courtclerk.org/records-search/common-pleas-civil-case-search/"
FORECLOSURE_MORTGAGE = "mortgage_foreclosure"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = PROJECT_ROOT / "data" / "browser_profiles" / "hamilton-oh"
EXTENSION_DIR = Path(__file__).resolve().parent / "hamilton_extension"
BRIDGE_PORT = int(os.environ.get("HAMILTON_BRIDGE_PORT", "8800"))
_MAX_ATTEMPTS = 2

_CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


def _find_chrome() -> str:
    for c in _CHROME_CANDIDATES:
        if c and os.path.exists(c):
            return c
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "Google Chrome not found. Install it or set CHROME_PATH (Hamilton is "
        "local-run and requires a genuine Chrome for Turnstile).")


def _launch_chrome(url: str) -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [_find_chrome(), f"--user-data-dir={PROFILE_DIR}",
         "--no-first-run", "--no-default-browser-check",
         "--window-size=1400,900", "--lang=en-US", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _kill_chrome() -> None:
    subprocess.run(["pkill", "-f", f"user-data-dir={PROFILE_DIR}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _chrome_running_on_profile() -> bool:
    return subprocess.run(["pgrep", "-f", f"user-data-dir={PROFILE_DIR}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def parse_hamilton_case_number(raw: str):
    """Hamilton Common Pleas Civil case number → the search-box token, or None.
    Format: 'A' + 2-digit year + 6-digit sequence, optional trailing parcel letter
    (e.g. 'A2500210', 'A2504403A'). Strips the '(NNNNN)' auction suffix."""
    if not raw:
        return None
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", raw.strip())
    compact = re.sub(r"[^A-Za-z0-9]", "", cleaned).upper()
    return compact if re.match(r"^A\d{6,8}[A-Z]?$", compact) else None


class HamiltonDocketScraper(DocketScraper):
    county_id = "hamilton-oh"
    county_name = "Hamilton"

    def classify(self, result: DocketResult, final_sale_price: float) -> tuple:
        # No-op: parse_docket already ran the full Hamilton evidence model. The
        # base prayer-vs-sale math is wrong for Hamilton (no prayer, no debt).
        return (result.classification, result.classification_reason)

    def parse_docket(self, rows: list, result: DocketResult,
                     caption: str = "", sale_date=None) -> None:
        """Rows (from the bridge extension) → DocketResult via the Hamilton
        classifier. NEVER sets a debt figure (Hamilton exposes none)."""
        rows = rows or []
        verdict = classify_hamilton(rows, caption, sale_date)

        result.foreclosure_type = FORECLOSURE_MORTGAGE
        result.debt_source = ""            # Hamilton has NO judgment amount
        result.prayer_amount = 0.0
        result.owner_name = verdict["owner_name"]
        result.kill_signals = verdict["kill_signals"]
        result.competing_filers = verdict["competing_filers"]
        result.evidence_level = verdict["evidence_level"]
        result.classification_reason = verdict["classification_reason"]
        result.events = [
            DocketEvent(filing_date=r.get("date", ""),
                        description=(r.get("description") or "")[:200],
                        document_type="").__dict__
            for r in rows[:120]
        ]
        if verdict["classification"] == "killed":
            result.classification = "killed"
            result.lead_status = "not_pursuable"
        else:
            # docket-checked, no kill, NO debt → stays apparent_surplus. NOT green:
            # a missing excess-funds line means not-yet-processed, never verified.
            result.classification = ""
            result.lead_status = "pursuable_with_caution"

    async def scrape_case(self, case_number: str) -> DocketResult:
        result = DocketResult(
            county_id=self.county_id, case_number=case_number,
            scraped_at=datetime.now().isoformat(),
            foreclosure_type=FORECLOSURE_MORTGAGE, debt_source="", prayer_amount=0.0)
        result.case_url = SEARCH_URL
        result.classification = "unknown"
        result.evidence_level = "auction_only"
        result.classification_reason = (
            "Hamilton is extension-driven local-run (Turnstile blocks headless/CDP). "
            "Run `python -m core.dockets.hamilton` (needs local Chrome + the bridge "
            "extension).")
        return result


# ─────────────────────────────────────────────────────────────────────────────
# AUTONOMOUS LOCAL RUN — genuine-Chrome bridge (Broward pattern, LOCAL_RUN).

def _load_hamilton_cases() -> list:
    from .enrich import load_cases_from_raw
    return load_cases_from_raw("hamilton-oh")


class _Bridge:
    """Thread-safe shared state for the localhost queue the extension talks to.
    The classifier runs unchanged in the HTTP handler thread on posted rows."""

    def __init__(self, cases, only_case):
        from .manual_runner import _progress_paths, _load_progress, _write_outputs
        self.lock = threading.Lock()
        self.scraper = HamiltonDocketScraper()
        self._write_outputs = _write_outputs
        day = datetime.now().strftime("%Y-%m-%d")
        self.out_file, self.progress_file = _progress_paths("hamilton-oh", day)
        self.progress = _load_progress(self.progress_file)
        self.total = len(cases)

        self.raw_of, self.auction_of, self.order, self.skipped = {}, {}, [], []
        for rec in cases:
            raw = rec.get("case_number", "")
            if only_case and not raw.upper().startswith(only_case.upper()[:8]):
                continue
            nd = parse_hamilton_case_number(raw)
            if not nd:
                self.skipped.append(raw); continue
            self.raw_of[nd] = raw
            self.auction_of[nd] = rec
            if raw not in self.progress:
                self.order.append(nd)
        self.attempts = {}
        self.done = set()
        self.results_ok = 0

    def next_case(self):
        with self.lock:
            for nd in self.order:
                if nd in self.done:
                    continue
                if self.attempts.get(nd, 0) >= _MAX_ATTEMPTS:
                    self.done.add(nd); continue
                self.attempts[nd] = self.attempts.get(nd, 0) + 1
                return {"case": nd}
            return {"done": True}

    def _sale_date(self, rec) -> str:
        for k in ("auction_date", "sale_date", "sale_datetime"):
            v = rec.get(k)
            if v:
                return str(v)
        return ""

    def record(self, payload):
        nd = (payload or {}).get("case", "")
        raw = self.raw_of.get(nd, nd)
        ok = bool(payload.get("ok") and payload.get("rows") and payload.get("case_present"))
        if not ok:
            with self.lock:
                capped = self.attempts.get(nd, 0) >= _MAX_ATTEMPTS
                if capped:
                    self.done.add(nd)
            print(f"  · {raw}: not verified — {payload.get('error') or 'no rows'}"
                  + ("" if capped else " (will retry)"))
            return {"ok": False}

        rec = self.auction_of.get(nd, {})
        result = DocketResult(
            county_id="hamilton-oh", case_number=raw,
            scraped_at=datetime.now().isoformat(),
            foreclosure_type=FORECLOSURE_MORTGAGE, debt_source="", prayer_amount=0.0)
        result.case_url = payload.get("url") or SEARCH_URL
        if payload.get("caption"):
            result.case_title = payload["caption"][:200]
        self.scraper.parse_docket(payload["rows"], result,
                                  caption=payload.get("caption", ""),
                                  sale_date=self._sale_date(rec))

        out_rec = result.to_dict()
        out_rec["_auction_data"] = {
            "final_sale_price": float(rec.get("final_sale_price") or 0.0),
            "opening_bid":      float(rec.get("opening_bid") or 0.0),
            "apparent_surplus": float(rec.get("gross_surplus") or 0.0),
            "address":          rec.get("address", ""),
            "sale_date":        self._sale_date(rec),
        }
        with self.lock:
            self.progress[raw] = out_rec
            self.done.add(nd)
            self.results_ok += 1
            self._write_outputs(self.out_file, self.progress_file, self.progress)
        tag = result.classification.upper() or "DOCKET-CHECKED"
        extra = f"  \U0001F6A8 {', '.join(result.kill_signals)}" if result.kill_signals else ""
        print(f"  ✓ {raw}: {tag} | owner={result.owner_name or '(none)'} "
              f"| {len(payload['rows'])} rows{extra}")
        return {"ok": True, "classification": result.classification}

    def all_done(self):
        with self.lock:
            return all(nd in self.done for nd in self.order)


def _make_handler(bridge, hits):
    class _H(http.server.BaseHTTPRequestHandler):
        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            hits.append(1)
            if self.path.startswith("/next"):
                self._send(bridge.next_case())
            elif self.path.startswith("/ping"):
                self._send({"ok": True})
            else:
                self._send({"error": "unknown"}, 404)

        def do_POST(self):
            hits.append(1)
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                payload = json.loads(raw.decode() or "{}")
            except Exception:
                payload = {}
            if self.path.startswith("/result"):
                self._send(bridge.record(payload))
            elif self.path.startswith("/log"):
                self._send({"ok": True})
            else:
                self._send({"error": "unknown"}, 404)

        def log_message(self, *a):
            pass
    return _H


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _serve(bridge, hits):
    httpd = _Server(("127.0.0.1", BRIDGE_PORT), _make_handler(bridge, hits))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _wait(fn, timeout, step=0.5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(step)
    return False


def _run_local(cases, only_case=None):
    bridge = _Bridge(cases, only_case)
    print("\n" + "=" * 64)
    print("  LOCAL DOCKET RUN — Hamilton (hamilton-oh)  [genuine-Chrome bridge]")
    print("=" * 64)
    print(f"  in-window cases : {bridge.total}")
    print(f"  already scraped : {len(bridge.progress)}")
    print(f"  to do this run  : {len(bridge.order)}")
    if bridge.skipped:
        print(f"  unparseable     : {len(bridge.skipped)} (stay docket-not-verified)")
    if not bridge.order:
        print("  ✓ nothing to do — all in-window cases already scraped this cycle.")
        return {"county_id": "hamilton-oh", "scraped": len(bridge.progress), "remaining": 0}

    hits = []
    httpd = _serve(bridge, hits)
    if _chrome_running_on_profile():
        print("  ⚠ a Chrome is already running on the hamilton-oh profile — opening "
              "a tab in it. Close it first if the run stalls.")
    print(f"\n  Launching genuine Chrome (profile: {PROFILE_DIR.name}) → the bridge "
          f"extension drives {len(bridge.order)} case(s). Gentle pacing (ToS).\n")
    _launch_chrome(SEARCH_URL)

    if not _wait(lambda: bool(hits), 25):
        _kill_chrome(); httpd.shutdown()
        print("\n  ✖ The bridge extension never contacted the runner. Confirm it is "
              "loaded:\n    chrome://extensions → Developer mode → Load unpacked → "
              f"{EXTENSION_DIR}\n    (loaded INTO the hamilton-oh profile), then re-run.")
        return {"county_id": "hamilton-oh", "scraped": len(bridge.progress),
                "remaining": len(bridge.order), "error": "extension not detected"}

    deadline = time.time() + max(180, bridge.total * 45 + 60)
    while not bridge.all_done() and time.time() < deadline:
        time.sleep(2)

    _kill_chrome()
    httpd.shutdown()
    remaining = len(bridge.order) - bridge.results_ok
    print("\n" + "=" * 64)
    print(f"  DONE: {bridge.results_ok} verified this run, "
          f"{len(bridge.progress)}/{bridge.total} total.")
    if remaining > 0:
        print(f"  {remaining} case(s) unverified — DOCKET-NOT-VERIFIED. Re-run to finish.")
    print(f"  Output: {bridge.out_file}")
    print("=" * 64)
    return {"county_id": "hamilton-oh", "scraped": len(bridge.progress), "remaining": remaining}


def _selftest():
    print(f"Bridge self-test on port {BRIDGE_PORT}. Extension dir: {EXTENSION_DIR}")

    class _Empty:
        total = 0; order = []; progress = {}; skipped = []
        def next_case(self): return {"done": True}
        def record(self, p): return {"ok": True}
        def all_done(self): return True

    hits = []
    httpd = _serve(_Empty(), hits)
    _launch_chrome(SEARCH_URL)
    ok = _wait(lambda: bool(hits), 30)
    _kill_chrome(); httpd.shutdown()
    if ok:
        print("✓ Channel OK — the extension reached the runner over localhost.")
    else:
        print("✖ No contact. Either the extension isn't loaded in the hamilton-oh "
              f"profile (chrome://extensions → Load unpacked → {EXTENSION_DIR}), or "
              "localhost is gated.")
    return ok


def main():
    ap = argparse.ArgumentParser(
        prog="python -m core.dockets.hamilton",
        description="Autonomous LOCAL Hamilton docket run via the genuine-Chrome "
                    "bridge extension. One-time setup: chrome://extensions → "
                    "Developer mode → Load unpacked → core/dockets/hamilton_extension "
                    "(in the hamilton-oh profile).")
    ap.add_argument("--selftest", action="store_true",
                    help="validate the extension⇄runner localhost channel; run no cases")
    ap.add_argument("--case", help="run one specific case-number prefix only")
    args = ap.parse_args()

    if args.selftest:
        _selftest(); return

    cases = _load_hamilton_cases()
    if not cases:
        print("No Hamilton auction data in data/raw/hamilton-oh_*.jsonl. "
              "Run the auction scraper first (or wait for the daily cron).")
        return
    _run_local(cases, only_case=args.case)


if __name__ == "__main__":
    main()
