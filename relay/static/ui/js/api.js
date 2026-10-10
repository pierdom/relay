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

/* Every request: the session cookie authenticates by default, and the bearer
   header is added only on the API-key break-glass path. A non-ok status always
   throws — a blocked DELETE (the protected master document) once looked
   exactly like a successful one because a helper here returned it silently.
   A 401 also announces itself, so an expired session goes back to the login
   card (main.js) instead of every panel failing on its own. */
async function send(path, opts = {}, json = true) {
  const headers = { ...(json ? { 'Content-Type': 'application/json' } : {}), ...(opts.headers || {}) };
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
  const res = await fetch(path, { credentials: 'same-origin', ...opts, headers });
  if (res.status === 401) window.dispatchEvent(new Event('relay:unauthorized'));
  if (!res.ok) throw await failure(res);
  return res;
}

/* The server's `detail` is the message worth showing ("A backfill is already
   running" beats "409 Conflict"); a structured one (a 409's `{error, current}`)
   rides along whole on `err.detail`, and `err.status` lets a caller branch on
   the status without matching wording. */
async function failure(res) {
  let detail;
  try { detail = (await res.json())?.detail; } catch { /* not JSON, or no body */ }
  const message = detail && typeof detail === 'object' ? detail.error : detail;
  return Object.assign(new Error(message || `${res.status} ${res.statusText}`), { detail, status: res.status });
}

/** A JSON request, answered with the parsed JSON body. */
export async function apiFetch(path, opts = {}) {
  return (await send(path, opts)).json();
}

/** A request whose body isn't JSON (a raw upload) or whose reply is empty (a
 *  204 DELETE, which `.json()` would turn into a failure). Returns the Response. */
export function apiSend(path, opts = {}) {
  return send(path, opts, false);
}
