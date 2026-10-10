/* Pure helpers — formatting and escaping.
 *
 * No DOM, no network, no shared state, which is why this is the first module
 * lifted out of main.js: nothing can depend on it in the wrong direction.
 */

// Past this, "412d ago" stops meaning anything; a date does.
const RELATIVE_DAYS = 30;

export function relativeTime(iso) {
  const when = new Date(iso);
  const s = Math.floor((Date.now() - when.getTime()) / 1000);
  if (Math.abs(s) < 60)    return s < 0 ? 'in a moment' : 'just now';
  if (Math.abs(s) < 3600)  return s < 0 ? `in ${Math.floor(-s/60)}m` : `${Math.floor(s/60)}m ago`;
  if (Math.abs(s) < 86400) return s < 0 ? `in ${Math.floor(-s/3600)}h` : `${Math.floor(s/3600)}h ago`;
  const days = Math.floor(Math.abs(s) / 86400);
  if (days <= RELATIVE_DAYS) return s < 0 ? `in ${days}d` : `${days}d ago`;
  return (s < 0 ? 'on ' : '') + shortDate(when);
}

/** "12 Mar", or "12 Mar 2025" outside the current year. */
function shortDate(d) {
  const opts = { day: 'numeric', month: 'short' };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return d.toLocaleDateString(undefined, opts);
}

/** A post's `source` as shown on a card: a URL's host ("pve.proxmox.com"),
 *  anything else as written. `href` is set only for an http(s) URL, so a
 *  stored `javascript:` source can never become a link. */
export function sourceParts(source) {
  try {
    const u = new URL(source);
    if (u.protocol === 'http:' || u.protocol === 'https:') {
      return { label: u.hostname.replace(/^www\./, ''), href: u.href };
    }
  } catch { /* not a URL */ }
  return { label: source, href: null };
}

export function toUtcIso(localDatetimeStr) {
  if (!localDatetimeStr) return '';
  const d = new Date(localDatetimeStr);
  if (isNaN(d.getTime())) return '';
  return d.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

export function toDatetimeLocal(utcIso) {
  if (!utcIso) return '';
  const d = new Date(utcIso);
  if (isNaN(d.getTime())) return '';
  // Format as YYYY-MM-DDTHH:MM in local time for datetime-local input
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** A tag in the server's normal form (models.clean_tag). */
export const cleanTag = (value) => value.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '');

/** A comma-separated Tags field as a list ("a, b,," → ["a", "b"]). */
export const parseTags = (value) => value.split(',').map(t => t.trim()).filter(Boolean);

export function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export const fmtBytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`;

// A fenced block or a single-line inline code span. Shared by main.js
// (preprocessLinks, extractMedia) and edit-form.js (the broken-link
// highlighter) — every place in the UI that must not mistake a syntax
// example for a live wikilink/embed/id-ref splits on this same pattern,
// rather than each maintaining its own copy that can drift out of sync
// with what GET /lint considers code (relay #198 N-5 follow-up).
export const CODE_SPAN_RE = /(```[\s\S]*?```|`[^`\n]*`)/g;

export function fmtUptime(sec) {
  if (sec < 60) return `${sec}s`;
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}
