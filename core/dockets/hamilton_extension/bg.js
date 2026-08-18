// SurplusIQ Hamilton Docket Bridge — background service worker.
//
// Relays the content script's messages to the local runner over
// http://127.0.0.1:<PORT>. The fetch is made from the EXTENSION context (with
// host_permissions), not the https page, so it is not subject to the page's
// mixed-content / Private Network Access gate. PORT matches the runner
// (HAMILTON_BRIDGE_PORT in core/dockets/hamilton.py).
//
//   GET  /next            -> { case: "<CASENUMBER>" }  or  { done: true }
//   POST /result {json}   -> { ok: true }
//   POST /log    {json}   -> { ok: true }   (best-effort)

const PORT = 8800;
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
  relay(msg).then((res) => sendResponse(res)).catch((e) => sendResponse({ error: String(e) }));
  return true; // keep the channel open for the async response
});
