// SurplusIQ Broward Docket Bridge — background service worker.
//
// Sole job: relay the content script's messages to the local runner over
// http://127.0.0.1:<PORT>. The fetch is made from the EXTENSION context (with
// host_permissions), which is not subject to the page's mixed-content / Private
// Network Access gate the way a content-script fetch from an https page would be.
//
// Protocol with the runner (core/dockets/broward.py):
//   GET  /next            -> { case: "<NODASH>" }  or  { done: true }
//   POST /result {json}   -> { ok: true }
//   POST /log    {json}   -> { ok: true }   (best-effort diagnostics)
// PORT is fixed; the runner uses the same constant (BROWARD_BRIDGE_PORT).

const PORT = 8799;
const BASE = `http://127.0.0.1:${PORT}`;

async function relay(msg) {
  if (msg.type === "next") {
    const r = await fetch(`${BASE}/next`, { cache: "no-store" });
    return await r.json();
  }
  if (msg.type === "result") {
    const r = await fetch(`${BASE}/result`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(msg.payload || {}),
    });
    return await r.json().catch(() => ({ ok: true }));
  }
  if (msg.type === "log") {
    await fetch(`${BASE}/log`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(msg.payload || {}),
    }).catch(() => {});
    return { ok: true };
  }
  return { error: "unknown message type" };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  relay(msg)
    .then((res) => sendResponse(res))
    .catch((e) => sendResponse({ error: String(e) }));
  return true; // keep the channel open for the async response
});
