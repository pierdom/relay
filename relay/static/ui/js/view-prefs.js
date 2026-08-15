/* Feed view preferences: list ⇄ grid layout, sort field and direction.
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
const sortFieldEl = document.getElementById('sortField');
const sortDirEl   = document.getElementById('sortDir');

export const prefs = {
  view:      localStorage.getItem('relay-view')  === 'grid'    ? 'grid'    : 'list',
  sortField: localStorage.getItem('relay-sort')  === 'created' ? 'created' : 'updated',
  sortOrder: localStorage.getItem('relay-order') === 'asc'     ? 'asc'     : 'desc',
};

export function applyViewMode() {
  feed.classList.toggle('grid', prefs.view === 'grid');
  vtList.classList.toggle('active', prefs.view === 'list');
  vtGrid.classList.toggle('active', prefs.view === 'grid');
  localStorage.setItem('relay-view', prefs.view);
}

export function applySort() {
  sortFieldEl.value = prefs.sortField;
  sortDirEl.textContent = prefs.sortOrder === 'asc' ? '↑' : '↓';
  sortDirEl.title = 'Sort direction: ' + (prefs.sortOrder === 'asc' ? 'oldest first' : 'newest first');
  localStorage.setItem('relay-sort', prefs.sortField);
  localStorage.setItem('relay-order', prefs.sortOrder);
}

// A live post belongs at the top of the feed only under the default sort
// (last-modified, newest first). Under any other order it would land mid-list,
// so the caller surfaces it via the "new posts" pill instead.
export function isDefaultSort() {
  return prefs.sortField === 'updated' && prefs.sortOrder === 'desc';
}

/** Wire the toggles. `onSortChange` re-runs the feed query. */
export function initViewPrefs(onSortChange) {
  vtList.addEventListener('click', () => { prefs.view = 'list'; applyViewMode(); });
  vtGrid.addEventListener('click', () => { prefs.view = 'grid'; applyViewMode(); });
  applyViewMode();

  sortFieldEl.addEventListener('change', () => { prefs.sortField = sortFieldEl.value; onSortChange(); });
  sortDirEl.addEventListener('click', () => {
    prefs.sortOrder = prefs.sortOrder === 'asc' ? 'desc' : 'asc';
    onSortChange();
  });
  applySort();
}
