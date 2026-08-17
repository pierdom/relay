/* The Deleted view — recovering a post the UI could not previously reach.
 *
 * Every primitive already existed: list a post's revisions (which answers for a
 * deleted post), read its body at one, and restore it keeping its original id.
 * What was missing was *discovery* — the post modal is the only entry point to
 * history, so you could only recover something that still existed. You can
 * restore anything if you know its id; there was no way to learn the id of
 * something you deleted.
 *
 * This is deliberately **not** a trash can. Nothing is moved anywhere on delete
 * and there is nothing to purge — `GET /posts/deleted` is a read over the git
 * history that already records every removal. Calling it a wastebin would imply
 * a storage model relay does not have.
 */

import { apiFetch } from './api.js';
import { escHtml, relativeTime } from './util.js';

const view = document.getElementById('deletedView');
const tagList = document.getElementById('tagList');

// Why a post went away. The API reports all three; the sidebar filters on them.
// `expiry` is excluded server-side by default — this vault sheds fourteen
// digests a week and they would bury the one accident the view exists for.
const REASONS = {
  deleted: { label: 'Deleted', hint: 'Removed through the UI, API or an agent' },
  external: { label: 'External', hint: 'Removed in Obsidian or another editor' },
  expiry: { label: 'Expired', hint: 'Swept by a tag TTL — usually intentional' },
};

let items = [];
let filter = null;          // null = every reason present
let onRestored = () => {};

export function initDeleted(refresh) {
  onRestored = refresh;
}

export async function loadDeleted() {
  view.innerHTML = '<div class="att-empty">Reading history…</div>';
  try {
    // Ask for expiries too: the sidebar offers them as a filter, so the
    // decision of whether to look at them belongs to the reader, not the fetch.
    const data = await apiFetch('/posts/deleted?limit=100&include_expiry=true');
    items = data.items || [];
  } catch (err) {
    // History is optional (`features.history.effective`), and a 503 here means
    // it is off rather than that something broke.
    items = [];
    view.innerHTML = `<div class="att-empty">${escHtml(
      String(err.message || '').includes('503')
        ? 'Vault history is disabled, so deleted posts cannot be listed.'
        : 'Could not read deleted posts.')}</div>`;
    renderFilters();
    return;
  }
  renderFilters();
  render();
}

function visible() {
  return filter ? items.filter(d => d.reason === filter) : items;
}

function renderFilters() {
  tagList.replaceChildren();
  const counts = {};
  for (const d of items) counts[d.reason] = (counts[d.reason] || 0) + 1;

  const row = (key, label, count) => {
    const el = document.createElement('div');
    el.className = 'tag-item' + (filter === key ? ' active' : '');
    el.dataset.reason = key ?? '';
    const name = document.createElement('span');
    name.className = 'tag-name';
    name.textContent = label;
    const n = document.createElement('span');
    n.className = 'tag-count';
    n.textContent = String(count);
    el.append(name, n);
    el.addEventListener('click', () => { filter = key; renderFilters(); render(); });
    return el;
  };

  tagList.appendChild(row(null, 'all', items.length));
  for (const [key, meta] of Object.entries(REASONS)) {
    if (counts[key]) tagList.appendChild(row(key, meta.label.toLowerCase(), counts[key]));
  }
}

function render() {
  const rows = visible();
  if (!rows.length) {
    view.innerHTML = '<div class="att-empty">Nothing deleted — or nothing left to recover.</div>';
    return;
  }
  view.replaceChildren();
  for (const d of rows) {
    const card = document.createElement('div');
    card.className = 'del-card';
    card.dataset.id = String(d.id);

    const head = document.createElement('div');
    head.className = 'del-head';
    const pill = document.createElement('span');
    pill.className = 'post-id-pill';
    pill.textContent = `#${d.id}`;
    const title = document.createElement('span');
    title.className = 'del-title';
    title.textContent = d.title;
    const reason = document.createElement('span');
    reason.className = `del-reason del-reason-${d.reason}`;
    reason.textContent = (REASONS[d.reason] || { label: d.reason }).label;
    reason.title = (REASONS[d.reason] || {}).hint || '';
    head.append(pill, title, reason);

    const meta = document.createElement('div');
    meta.className = 'del-meta';
    meta.textContent = `${relativeTime(d.when)} · ${d.short_sha} · ${d.path}`;

    const actions = document.createElement('div');
    actions.className = 'del-actions';
    const preview = document.createElement('button');
    preview.className = 'btn-edit';
    preview.textContent = '👁 Preview';
    const restore = document.createElement('button');
    restore.className = 'btn-restore';
    restore.textContent = '⤺ Restore';
    actions.append(preview, restore);

    const body = document.createElement('pre');
    body.className = 'del-body';
    body.hidden = true;

    preview.addEventListener('click', async () => {
      if (!body.hidden) { body.hidden = true; return; }
      body.textContent = 'Loading…';
      body.hidden = false;
      try {
        const rev = await apiFetch(`/posts/${d.id}/history/${d.sha}`);
        // textContent, never innerHTML: this is an arbitrary note body.
        body.textContent = rev.content || '(empty)';
      } catch {
        body.textContent = 'Could not read this revision.';
      }
    });

    restore.addEventListener('click', async () => {
      if (!confirm(`Restore "${d.title}" as #${d.id}?`)) return;
      restore.disabled = true;
      restore.textContent = 'Restoring…';
      try {
        // apiFetch, not apiSend: apiSend is the raw-Response helper for
        // DELETEs — it sends no Content-Type and never throws on a non-ok
        // status, so a failed restore would look exactly like a successful one.
        await apiFetch(`/posts/${d.id}/restore`, {
          method: 'POST', body: JSON.stringify({ sha: d.sha }),
        });
        items = items.filter(x => x.id !== d.id);
        renderFilters();
        render();
        onRestored();
      } catch (err) {
        restore.disabled = false;
        restore.textContent = '⤺ Restore';
        const failed = document.createElement('div');
        failed.className = 'del-meta del-failed';
        failed.textContent = `Restore failed: ${err.message}`;
        card.appendChild(failed);
      }
    });

    card.append(head, meta, actions, body);
    view.appendChild(card);
  }
}
