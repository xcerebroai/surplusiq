// SurplusIQ Broward Docket Bridge — content script (the whole search flow).
//
// Runs on every browardclerk.org page. A per-tab state machine keyed on the URL
// path drives one case at a time through the flow the live recon proved works in
// genuine Chrome (the ONLY environment where Turnstile issues its token):
//
//   Index   -> ask runner for next case -> fill #CaseNumber -> wait for the
//              auto-issued Turnstile token -> submit caseSearchForm
//   Results -> click the case row (button.bc-casedetail-viewer -> GetCaseDetail)
//   Detail  -> extract the docket rows (Date | Description | Additional Text) from
//              the LIVE-rendered page (the events table is AJAX-built, so it only
//              exists after real navigation) -> POST rows to the runner -> loop.
//
// The current case survives navigations via sessionStorage. A case that can't be
// reached is reported ok:false and left for the runner to leave docket-not-verified
// (never written as clean). When the runner says done, the script idles.

(function () {
  const INDEX =
    "https://www.browardclerk.org/Web2/CaseSearchECA/Index/?AccessLevel=ANONYMOUS";
  const norm = (s) => (s || "").replace(/ /g, " ").replace(/\s+/g, " ").trim();
  const ask = (msg) => new Promise((res) => chrome.runtime.sendMessage(msg, res));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function waitFor(fn, timeout = 20000, step = 400) {
    return new Promise((resolve) => {
      const t0 = Date.now();
      const iv = setInterval(() => {
        let v = null;
        try { v = fn(); } catch (e) {}
        if (v) { clearInterval(iv); resolve(v); }
        else if (Date.now() - t0 > timeout) { clearInterval(iv); resolve(null); }
      }, step);
    });
  }

  // Same table-parse as the proven core extractor: scan every table whose header
  // has Description + Additional Text; emit {date, description, additional} rows.
  function extractRows() {
    const out = [];
    for (const t of document.querySelectorAll("table")) {
      const headRow = t.querySelector("thead tr") || t.rows[0];
      if (!headRow) continue;
      const cells = Array.from(headRow.cells).map((c) => norm(c.innerText).toLowerCase());
      const de = cells.findIndex((h) => h.includes("description"));
      const ad = cells.findIndex((h) => h.includes("additional"));
      const di = cells.findIndex((h) => h.includes("date"));
      if (de === -1 || ad === -1) continue;
      const body = t.querySelector("tbody")
        ? Array.from(t.querySelector("tbody").querySelectorAll("tr"))
        : Array.from(t.rows).slice(1);
      for (const r of body) {
        const c = Array.from(r.cells).map((x) => norm(x.innerText));
        if (!c.length) continue;
        const desc = de >= 0 ? c[de] || "" : "";
        const addl = ad >= 0 ? c[ad] || "" : "";
        if (!desc && !addl) continue;
        out.push({ date: di >= 0 ? c[di] || "" : "", description: desc, additional: addl });
      }
    }
    return out;
  }

  function getCaption() {
    const bt = document.body.innerText || "";
    const m = bt.match(/([A-Z][A-Za-z .,&'\-]{4,90}\s+vs\.?\s+[A-Z][A-Za-z .,&'\-]{3,90})/);
    return m ? norm(m[1]) : "";
  }

  async function onIndex() {
    const job = await ask({ type: "next" });
    if (!job || job.done) { document.title = "SurplusIQ Broward: idle (no more cases)"; return; }
    const nodash = job.case;
    sessionStorage.setItem("bro_case", nodash);

    // activate the Case Number tab (its anchor, not the <li>), then fill.
    const cands = [...document.querySelectorAll("a[data-toggle=tab],[role=tab],a,button")];
    const tab = cands.find((e) => norm(e.textContent) === "Case Number");
    if (tab) tab.click();
    const inp = await waitFor(() => document.getElementById("CaseNumber"), 15000);
    if (!inp) {
      await ask({ type: "result", payload: { case: nodash, ok: false, error: "case-number field not rendered" } });
      return void (location.href = INDEX);
    }
    inp.value = nodash;

    // wait for the Turnstile token to auto-issue on caseSearchForm (never read its value).
    const tokened = await waitFor(() => {
      const f = document.getElementById("caseSearchForm");
      const i = f && f.querySelector('input[name="cf-turnstile-response"]');
      return i && (i.value || "").length > 20;
    }, 30000);
    if (!tokened) {
      await ask({ type: "result", payload: { case: nodash, ok: false, error: "turnstile token not issued" } });
      return void (location.href = INDEX);
    }

    const f = document.getElementById("caseSearchForm");
    let b = f.querySelector("button[type=submit],input[type=submit]");
    if (!b) b = [...f.querySelectorAll("button,input")].find((x) => /search/i.test((x.textContent || "") + (x.value || "")));
    if (b) b.click(); else f.requestSubmit(); // -> navigates to /Results
  }

  async function onResults() {
    const nodash = sessionStorage.getItem("bro_case") || "";
    const btn = await waitFor(() => document.querySelector("button.bc-casedetail-viewer"), 20000);
    if (!btn) {
      const body = (document.body.innerText || "").toLowerCase();
      const norec = /no records|no cases|0 items|no matching/.test(body);
      await ask({ type: "result", payload: { case: nodash, ok: false, error: norec ? "no case found" : "results row not found" } });
      sessionStorage.removeItem("bro_case");
      return void (location.href = INDEX);
    }
    btn.click(); // ViewDetails(...) -> GetCaseDetail
  }

  async function onDetail() {
    const nodash = sessionStorage.getItem("bro_case") || "";
    const rows = (await waitFor(() => { const r = extractRows(); return r.length ? r : null; }, 20000)) || [];
    const compact = (document.body.innerHTML || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
    const casePresent = !!(nodash && compact.includes(nodash));
    await ask({
      type: "result",
      payload: {
        case: nodash,
        ok: rows.length > 0 && casePresent,
        rows,
        caption: getCaption(),
        case_present: casePresent,
        url: location.href,
        error: rows.length ? (casePresent ? "" : "case number not on detail page") : "docket table not found",
      },
    });
    sessionStorage.removeItem("bro_case");
    await sleep(400);
    location.href = INDEX;
  }

  const path = location.pathname;
  if (/CaseSearchECA\/Index/i.test(path)) onIndex();
  else if (/CaseSearchECA\/Results/i.test(path)) onResults();
  else if (/CaseSearchECA\/GetCaseDetail/i.test(path)) onDetail();
})();
