/* The vault's (title → id) index, behind every [[wikilink]] and #id.
 *
 * Rendering resolves links against it and the editor highlights the ones it
 * cannot find — one copy for both, refreshed after anything that can add,
 * remove or rename a post (main.js).
 */

import { apiFetch } from './api.js';

let byTitle = new Map();
let ids = new Set();

const norm = (title) => title.trim().toLowerCase();

export async function refreshLinks() {
  try {
    const { items } = await apiFetch('/links');
    byTitle = new Map(items.map(i => [norm(i.title), i.id]));
    ids = new Set(items.map(i => i.id));
  } catch { /* keep the last good index */ }
}

/** The id a [[Title]] resolves to (case-insensitive), or undefined. */
export const idForTitle = (title) => byTitle.get(norm(title));

export const postExists = (id) => ids.has(Number(id));
