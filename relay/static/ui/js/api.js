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
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// Like apiFetch but returns the raw Response (no JSON parse) — for DELETEs.
export async function apiSend(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
  return fetch(path, { credentials: 'same-origin', ...opts, headers });
}
