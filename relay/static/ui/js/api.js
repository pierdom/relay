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

// Caller scope (relay #198, B-8) — the client-side mirror of GET /status's
// `caller` field. Session-scoped state, same pattern as apiKey above: set
// once by main.js's init() from its one /status fetch, read by main.js
// (New Post / compose gating) and edit-form.js (Save gating), cleared on
// disconnect so a stale scope never survives a switch to a different key.
let callerScope = null;

export function setCallerScope(scope) { callerScope = scope || null; }
export function clearCallerScope() { callerScope = null; }
export function getCallerScope() { return callerScope; }

// Whether `tags` is entirely within a write-restricted key's allowed set —
// the exact ALL-of, non-empty-required semantics of identity.Actor.can_write_tags,
// mirrored deliberately so this can never be more permissive than the
// server, only equally or more conservative. `scope` null or "full" always
// passes; "read" never does (defense in depth — callers should already be
// gating on mode === 'read' well before reaching this).
export function tagsAllowedByScope(tags, scope) {
  if (!scope || scope.mode === 'full') return true;
  if (scope.mode === 'read') return false;
  const allowed = new Set(scope.tags || []);
  return tags.length > 0 && tags.every(t => allowed.has(t));
}

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
