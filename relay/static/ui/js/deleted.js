/* Recovering a post the UI could not previously reach.
 *
 * Every primitive already existed: list a post's revisions (which answers for a
 * deleted post), read its body at one, and restore it keeping its original id.
 * What was missing was *discovery* — the post modal is the only entry point to
 * history, so the browser could only recover something that still existed. You
 * can restore anything if you know its id; there was no way to learn the id of
 * something you deleted.
 *
 * This is deliberately **not** a trash can. Nothing is moved anywhere on delete
 * and there is nothing to purge — `GET /posts/deleted` is a read over the git
 * history that already records every removal.
 *
 * **It lives inside the status panel, and that is a placement decision, not an
 * accident.** It was first built as a fourth sidebar tab beside Tags/Tree/Files,
 * which was wrong twice over: those three are ways to browse what *exists* and
 * this is a different corpus, and a fourth tab did not fit the 220px strip
 * anyway. Recovery belongs with the panel that already reports whether vault
 * history works at all — because when it does not, there is nothing to recover
 * and this section says so instead of offering a button that cannot help.
 */

import { apiFetch } from './api.js';
import { relativeTime } from './util.js';

// Why a post went away. The API reports all three; the filter row uses them.
// `expiry` is excluded from the headline count on purpose — a vault that sheds
// fourteen digests a week would otherwise report a permanent backlog of
// "recoverable" posts nobody wants back, burying the one accident.
const REASONS = {
  deleted: { label: 'Deleted', hint: 'Removed through the UI, API or an agent' },
  external: { label: 'External', hint: 'Removed in Obsidian or another editor' },
  expiry: { label: 'Expired', hint: 'Swept by a tag TTL — usually intentional' },
};

let items = [];
let filter = null;          // null = every reason
let onRestored = () => {};

export function initDeleted(refresh) {
  onRestored = refresh;
}

/** Deleted posts, newest first. Expiries included — the caller decides. */
export async function fetchDeleted() {
  const data = await apiFetch('/posts/deleted?limit=100&include_expiry=true');
  items = data.items || [];
  return items;
}

/** How many are worth surfacing in the status panel's headline. */
export function recoverableCount(list = items) {
  return list.filter(d => d.reason !== 'expiry').length;
}

/**
 * Render the browser into `container`, replacing its contents.
 * `onBack` gets a "← Status" control; omit it and none is drawn.
 */
export function renderDeleted(container, { onBack } = {}) {
  filter = null;
  const draw = () => paint(container, onBack, draw);
  draw();
}

function paint(container, onBack, draw) {
  container.replaceChildren();

  if (onBack) {
    const back = document.createElement('button');
    back.className = 'sm-back';
    back.id = 'delBack';
    back.textContent = '← Status';
    back.addEventListener('click', onBack);
    container.appendChild(back);
  }

  const counts = {};
  for (const d of items) counts[d.reason] = (counts[d.reason] || 0) + 1;

  const bar = document.createElement('div');
  bar.className = 'del-filters';
  const chip = (key, label, count) => {
    const b = document.createElement('button');
    b.className = 'del-filter' + (filter === key ? ' active' : '');
    b.dataset.reason = key ?? 'all';
    b.textContent = `${label} ${count}`;
    if (key) b.title = REASONS[key].hint;
    b.addEventListener('click', () => { filter = key; draw(); });
    return b;
  };
  bar.appendChild(chip(null, 'all', items.length));
  for (const [key, meta] of Object.entries(REASONS)) {
    if (counts[key]) bar.appendChild(chip(key, meta.label.toLowerCase(), counts[key]));
  }
  if (items.length) container.appendChild(bar);

  const rows = filter ? items.filter(d => d.reason === filter) : items;
  if (!rows.length) {
    const empty = document.createElement('div');
    empty.className = 'sm-section-title';
    empty.textContent = 'Nothing deleted — or nothing left to recover.';
    container.appendChild(empty);
    return;
  }
  for (const d of rows) container.appendChild(card(d, draw));
}

function card(d, draw) {
  const el = document.createElement('div');
  el.className = 'del-card';
  el.dataset.id = String(d.id);

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

  // The panel is 560px, so the full path does not earn its line here — the
  // title is the filename, and the folder is the only part that adds anything.
  const meta = document.createElement('div');
  meta.className = 'del-meta';
  const folder = d.path.includes('/') ? d.path.slice(0, d.path.lastIndexOf('/')) : '';
  meta.textContent = [relativeTime(d.when), d.short_sha, folder].filter(Boolean).join(' · ');

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
      // apiFetch, not apiSend: apiSend is the raw-Response helper for DELETEs —
      // it sends no Content-Type and never throws on a non-ok status, so a
      // failed restore would look exactly like a successful one.
      await apiFetch(`/posts/${d.id}/restore`, {
        method: 'POST', body: JSON.stringify({ sha: d.sha }),
      });
      items = items.filter(x => x.id !== d.id);
      draw();
      onRestored();
    } catch (err) {
      restore.disabled = false;
      restore.textContent = '⤺ Restore';
      const failed = document.createElement('div');
      failed.className = 'del-meta del-failed';
      failed.textContent = `Restore failed: ${err.message}`;
      el.appendChild(failed);
    }
  });

  el.append(head, meta, actions, body);
  return el;
}
