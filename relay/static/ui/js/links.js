/* The vault's (title → id) index, behind every [[wikilink]] and #id.
 *
 * Rendering resolves links against it and the editor highlights the ones it
 * cannot find — one copy for both, refreshed after anything that can add,
 * remove or rename a post (main.js).
 */

import { apiFetch } from './api.js';

let posts = [];   // [{ id, title }], by title
let byTitle = new Map();
let ids = new Set();

const norm = (title) => title.trim().toLowerCase();

export async function refreshLinks() {
  try {
    const { items } = await apiFetch('/links');
    posts = [...items].sort((a, b) => a.title.localeCompare(b.title));
    byTitle = new Map(items.map(i => [norm(i.title), i.id]));
    ids = new Set(items.map(i => i.id));
  } catch { /* keep the last good index */ }
}

/** The id a [[Title]] resolves to (case-insensitive), or undefined. */
export const idForTitle = (title) => byTitle.get(norm(title));

export const postExists = (id) => ids.has(Number(id));

/** Up to `limit` posts whose title (or `#id`) matches `text`, best first:
 *  an exact id, then title prefixes, then titles containing it. Feeds the
 *  quick switcher and the editor's `[[`/`#` completion. */
export function matchPosts(text, limit = 8) {
  const q = norm(text).replace(/^#/, '');
  if (!q) return posts.slice(0, limit);
  const rank = (p) => (String(p.id) === q ? 0 : norm(p.title).startsWith(q) ? 1 : norm(p.title).includes(q) ? 2 : 3);
  return posts.map(p => [rank(p), p]).filter(([r]) => r < 3)
    .sort((a, b) => a[0] - b[0])   // stable: ties stay alphabetical
    .slice(0, limit).map(([, p]) => p);
}
