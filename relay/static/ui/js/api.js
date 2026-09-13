/* HTTP access to the relay API.
 *
 * Owns `apiKey` outright. It is only ever set by the break-glass login and
 * cleared on disconnect, so it becomes module-private state with setters rather
 * than a shared mutable global — a plain `export let` would not work, since
 * imported bindings are read-only in the importing module.
 *
 * The session cookie carries auth by default; the bearer header is added only on
 * the API-key path.
 */

let apiKey = '';

export function setApiKey(key) { apiKey = key || ''; }
export function clearApiKey() { apiKey = ''; }
export function hasApiKey() { return Boolean(apiKey); }

export async function apiFetch(path, opts = {}) {
  // Cookie carries the session by default; only add the bearer header on the
  // API-key break-glass path.
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
  const res = await fetch(path, { credentials: 'same-origin', ...opts, headers });
  if (!res.ok) {
    // FastAPI's HTTPException(detail=...) is the one thing worth showing a user
    // over the generic status text — "A backfill is already running" beats
    // "409 Conflict". Best-effort: a body that isn't JSON, or has no `detail`,
    // falls back to the status line exactly as before.
    let detail;
    try { detail = (await res.json())?.detail; } catch { /* not JSON, or no body */ }
    const err = new Error(detail || `${res.status} ${res.statusText}`);
    // Carried alongside the message so a caller can distinguish e.g. "history
    // disabled" (503) from any other failure without pattern-matching the
    // detail text itself — matching on `err.message` would silently break
    // again the moment the server's wording changes (K-12: the history
    // panel's own "friendly 503" branch checked `err.message.startsWith('503')`,
    // which never matched — `detail` is always a human sentence, never a
    // string starting with a status code).
    err.status = res.status;
    throw err;
  }
  return res.json();
}

// Like apiFetch but returns the raw Response (no JSON parse) — for DELETEs.
// Never throws on a non-ok status; callers that need to know whether the
// request actually succeeded should use apiSendChecked instead (K-11: three
// call sites used to `await apiSend(...)` and unconditionally treat that as
// success, so a blocked delete — e.g. the protected master document — looked
// identical to a real one in the UI).
export async function apiSend(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
  return fetch(path, { credentials: 'same-origin', ...opts, headers });
}

// Like apiSend, but throws on a non-ok status (with the server's `detail`
// when there is one) instead of silently returning the failed Response.
// Deliberately does NOT parse the body as JSON on success like apiFetch
// does — DELETE endpoints return 204 No Content, and apiFetch's own
// `res.json()` would throw on that empty body and turn every *successful*
// delete into an apparent failure.
export async function apiSendChecked(path, opts = {}) {
  const res = await apiSend(path, opts);
  if (!res.ok) {
    let detail;
    try { detail = (await res.json())?.detail; } catch { /* not JSON, or no body */ }
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res;
}
