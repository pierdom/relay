/* Pure helpers — formatting and escaping.
 *
 * No DOM, no network, no shared state, which is why this is the first module
 * lifted out of main.js: nothing can depend on it in the wrong direction.
 */

export function relativeTime(iso) {
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (Math.abs(s) < 60)    return s < 0 ? 'in a moment' : 'just now';
  if (Math.abs(s) < 3600)  return s < 0 ? `in ${Math.floor(-s/60)}m` : `${Math.floor(s/60)}m ago`;
  if (Math.abs(s) < 86400) return s < 0 ? `in ${Math.floor(-s/3600)}h` : `${Math.floor(s/3600)}h ago`;
  return s < 0 ? `in ${Math.floor(-s/86400)}d` : `${Math.floor(s/86400)}d ago`;
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

export function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export const fmtBytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`;

export function fmtUptime(sec) {
  if (sec < 60) return `${sec}s`;
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}
