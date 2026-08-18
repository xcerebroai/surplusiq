// SurplusIQ Hamilton Docket Bridge — content script (the whole search flow).
//
// Runs on every courtclerk.org page. A URL-keyed state machine drives one case
// at a time through the flow the Phase-1 recon proved works in genuine Chrome
// (the ONLY environment where Cloudflare Turnstile issues its token):
//
//   Search page (records-search/common-pleas-civil-case-search/) ->
//       ask runner for next case -> fill #cc_frm casenumber -> wait for the
//       auto-issued Turnstile token -> inject sec=history (a GET form drops the
//       action's ?sec=history on serialize) -> submit
//   case_summary.php?sec=history ->
//       set the DataTables docket to all rows, extract Date|Description|Notes|
//       Amount, read the "Case Caption", POST rows to the runner -> loop.
//
// The current case survives navigation via sessionStorage. A case that can't be
// reached is reported ok:false and left docket-not-verified (never clean). When
// the runner says done, the script idles. Gentle: one case at a time, no retries
// here (the runner caps retries) — the site ToS forbids bulk mining.

(function () {
  const SEARCH = "https://www.courtclerk.org/records-search/common-pleas-civil-case-search/";
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

  // The docket table: header carries Date + Description + Amount.
  function findDocketTable() {
    for (const t of document.querySelectorAll("table")) {
      const h = (t.innerText || "").toLowerCase();
      if (h.includes("date") && h.includes("description") && h.includes("amount")) return t;
    }
    return null;
  }

  function extractRows(tbl) {
    const head = tbl.querySelector("thead tr") || tbl.rows[0];
    const H = Array.from(head.cells).map((c) => norm(c.innerText).toLowerCase());
    const di = H.findIndex((h) => h.includes("date"));
    const de = H.findIndex((h) => h.includes("desc"));
    const no = H.findIndex((h) => h.includes("note"));
    const am = H.findIndex((h) => h.includes("amount"));
    const body = tbl.querySelector("tbody")
      ? Array.from(tbl.querySelector("tbody").querySelectorAll("tr"))
      : Array.from(tbl.rows).slice(1);
    const out = [];
    for (const r of body) {
      const c = Array.from(r.cells).map((x) => norm(x.innerText));
      const date = di >= 0 ? c[di] || "" : "";
      const description = de >= 0 ? c[de] || "" : "";
      if (!description || !/\d/.test(date)) continue;
      out.push({
        date, description,
        notes: no >= 0 ? c[no] || "" : "",
        amount: am >= 0 ? c[am] || "" : "",
      });
    }
    return out;
  }

  function getCaption() {
    const m = (document.body.innerText || "").match(/Case Caption:\s*(.+)/);
    return m ? norm(m[1]) : "";
  }

  async function onSearch() {
    const job = await ask({ type: "next" });
    if (!job || job.done) { document.title = "SurplusIQ Hamilton: idle (no more cases)"; return; }
    const cn = job.case;
    sessionStorage.setItem("ham_case", cn);

    const f = await waitFor(() => document.querySelector("#cc_frm"), 15000);
    if (!f) {
      await ask({ type: "result", payload: { case: cn, ok: false, error: "search form not rendered" } });
      return void (location.href = SEARCH);
    }
    const inp = f.querySelector("[name=casenumber]");
    if (inp) inp.value = cn;

    // wait for the Turnstile token to auto-issue on #cc_frm (never read its value).
    const tokened = await waitFor(() => {
      for (const i of f.querySelectorAll('input[name*="cf-chl-widget"],input[name="cf-turnstile-response"]')) {
        if ((i.value || "").length > 20) return true;
      }
      return false;
    }, 30000);
    if (!tokened) {
      await ask({ type: "result", payload: { case: cn, ok: false, error: "turnstile token not issued" } });
      return void (location.href = SEARCH);
    }

    // A GET form drops its action's ?sec=history — inject it as a field so the
    // history (docket) section renders instead of the summary-only view.
    if (!f.querySelector('input[name="sec"]')) {
      const s = document.createElement("input");
      s.type = "hidden"; s.name = "sec"; s.value = "history";
      f.appendChild(s);
    }
    let b = f.querySelector("input[type=submit],button[type=submit]");
    if (b) b.click(); else f.submit(); // -> case_summary.php?...&sec=history
  }

  async function onDocket() {
    const cn = sessionStorage.getItem("ham_case") || "";
    if (!/sec=history/i.test(location.search)) {
      await ask({ type: "result", payload: { case: cn, ok: false, error: "landed on summary (sec=history missing)" } });
      sessionStorage.removeItem("ham_case");
      return void (location.href = SEARCH);
    }
    const tbl = await waitFor(findDocketTable, 20000);
    if (!tbl) {
      const body = (document.body.innerText || "").toLowerCase();
      const norec = /no records|not found|no matching|invalid case/.test(body);
      await ask({ type: "result", payload: { case: cn, ok: false, error: norec ? "no case found" : "docket table not found" } });
      sessionStorage.removeItem("ham_case");
      return void (location.href = SEARCH);
    }
    // Show every docket row (DataTables paginates to 10 by default).
    try {
      if (window.jQuery && jQuery.fn.dataTable && jQuery.fn.dataTable.isDataTable(tbl)) {
        jQuery(tbl).DataTable().page.len(-1).draw();
      }
    } catch (e) {}
    await sleep(400);

    const rows = extractRows(tbl);
    const compact = (document.body.innerHTML || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
    const casePresent = !!(cn && compact.includes(cn.toUpperCase()));
    await ask({
      type: "result",
      payload: {
        case: cn,
        ok: rows.length > 0 && casePresent,
        rows,
        caption: getCaption(),
        case_present: casePresent,
        url: location.href.split("cf-turnstile-response")[0], // drop the token from the URL
        error: rows.length ? (casePresent ? "" : "case number not on detail page") : "docket table empty",
      },
    });
    sessionStorage.removeItem("ham_case");
    await sleep(400);
    location.href = SEARCH;
  }

  const path = location.pathname;
  if (/common-pleas-civil-case-search/i.test(path)) onSearch();
  else if (/case_summary\.php/i.test(path)) onDocket();
})();
