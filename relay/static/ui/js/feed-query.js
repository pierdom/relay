/* What the feed is currently showing.
 *
 * These six values were separate top-level `let`s spread across the search, tags,
 * sidebar, files, posts, SSE and modal sections — which is why almost every part
 * of main.js could reach almost every other part. They are not really shared
 * globals though: together they are one thing, the query the feed is rendering.
 * Naming that is the point of this module.
 *
 * Exported as an object so properties stay mutable across module boundaries (an
 * imported binding itself is read-only).
 *
 * `tag` and `folder` are mutually exclusive — the UI never applies both — and
 * `search` combines with either.
 */

export const query = {
  tag: null,       // active tag filter, or null
  folder: null,    // active folder filter, or null
  search: null,    // active search term, or null
  mode: 'keyword', // search ranking mode — 'keyword' (default), 'semantic', or 'hybrid'
                    // (relay #253, proof of concept). Mutually exclusive with tag/folder,
                    // same as tag/folder are with each other — the server 400s the combination.
  offset: 0,       // paging cursor into the current result set
  total: 0,        // result count reported by the last response
};

/** Back to the first page. Call whenever the filters change. */
export function resetPaging() {
  query.offset = 0;
  query.total = 0;
}
