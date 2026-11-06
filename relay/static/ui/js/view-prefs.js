/* Feed view preferences: list, grid or three-column layout, sort field and direction.
 *
 * Cohesive enough to own outright — these three values are read by the feed and
 * nothing else, and this module is the only place that touches their
 * localStorage keys.
 *
 * State lives on an exported *object* rather than as exported `let`s: an imported
 * binding is read-only, so `prefs.view = 'grid'` works where `view = 'grid'`
 * across a module boundary would not.
 *
 * Reloading the feed when the sort changes is main.js's job, so it passes that in
 * — this module knows when to reload, not how.
 */

const feed        = document.getElementById('feed');
const vtList      = document.getElementById('vtList');
const vtGrid      = document.getElementById('vtGrid');
const vtReader    = document.getElementById('vtReader');
const sortFieldEl = document.getElementById('sortField');
const sortDirEl   = document.getElementById('sortDir');

// Storage can throw (blocked site data, some private modes). This module runs
// at import time, so an unguarded read here took the whole app down with it —
// preferences are a convenience and fall back to the defaults instead.
function load(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function save(key, value) {
  try { localStorage.setItem(key, value); } catch { /* not persisted this session */ }
}

export const prefs = {
  view:      ['grid', 'reader'].includes(load('relay-view')) ? load('relay-view') : 'list',
  sortField: load('relay-sort')  === 'created' ? 'created' : 'updated',
  sortOrder: load('relay-order') === 'asc'     ? 'asc'     : 'desc',
};

let onViewChange = () => {};

function applyViewMode() {
  feed.classList.toggle('grid', prefs.view === 'grid');
  // The three-column layout is CSS on this class (it falls back to the list on
  // a phone); docking the note panel is main.js's, through `onViewChange`.
  document.body.classList.toggle('view-reader', prefs.view === 'reader');
  vtList.classList.toggle('active', prefs.view === 'list');
  vtGrid.classList.toggle('active', prefs.view === 'grid');
  vtReader.classList.toggle('active', prefs.view === 'reader');
  save('relay-view', prefs.view);
  onViewChange();
}

export function applySort() {
  sortFieldEl.value = prefs.sortField;
  sortDirEl.textContent = prefs.sortOrder === 'asc' ? '↑' : '↓';
  sortDirEl.title = 'Sort direction: ' + (prefs.sortOrder === 'asc' ? 'oldest first' : 'newest first');
  save('relay-sort', prefs.sortField);
  save('relay-order', prefs.sortOrder);
}

// A live post belongs at the top of the feed only under the default sort
// (last-modified, newest first). Under any other order it would land mid-list,
// so the caller surfaces it via the "new posts" pill instead.
export function isDefaultSort() {
  return prefs.sortField === 'updated' && prefs.sortOrder === 'desc';
}

/** Wire the toggles. `onSortChange` re-runs the feed query; `viewChanged` runs
 *  after every layout switch (not the initial one — main.js isn't ready yet). */
export function initViewPrefs(onSortChange, viewChanged) {
  vtList.addEventListener('click', () => { prefs.view = 'list'; applyViewMode(); });
  vtGrid.addEventListener('click', () => { prefs.view = 'grid'; applyViewMode(); });
  vtReader.addEventListener('click', () => { prefs.view = 'reader'; applyViewMode(); });
  applyViewMode();
  onViewChange = viewChanged;

  sortFieldEl.addEventListener('change', () => { prefs.sortField = sortFieldEl.value; onSortChange(); });
  sortDirEl.addEventListener('click', () => {
    prefs.sortOrder = prefs.sortOrder === 'asc' ? 'desc' : 'asc';
    onSortChange();
  });
  applySort();
}
