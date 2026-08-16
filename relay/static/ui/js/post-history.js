/* Post history panel — the browser side of GET /posts/{id}/history.
 *
 * Recovery already worked over REST and MCP, but only if you knew a post had been
 * clobbered and were willing to reach for a shell. This surfaces it where the
 * damage is noticed: a History button in the post modal.
 *
 * Deliberately preview-then-restore. The listing carries only metadata, so
 * choosing a sha from it blind is a poor way to undo something; selecting a
 * revision fetches its body first, and only then is Restore offered.
 *
 * Structure mirrors status.js: owns its own elements, wires its own controls, and
 * exports only what main.js genuinely calls.
 */

import { apiFetch } from './api.js';
import { attachSheetDismiss } from './sheet.js';

const historyModal = document.getElementById('historyModal');
const hmTitle = document.getElementById('hmTitle');
const hmBody = document.getElementById('hmBody');

let currentPostId = null;
let onRestored = () => {};

/** main.js supplies the callback that refreshes the feed after a restore. */
export function initPostHistory(refresh) {
  onRestored = refresh;
}

export function isHistoryOpen() {
  return historyModal.classList.contains('open');
}

export function closeHistoryModal() {
  historyModal.classList.remove('open');
  document.body.style.overflow = '';
  hmBody.innerHTML = '';
  currentPostId = null;
}

function note(text, className = 'sm-section-title') {
  const el = document.createElement('div');
  el.className = className;
  el.textContent = text;
  return el;
}

/* The panes are built once and then only their *contents* change.
 *
 * They used to be created per selection, which meant the panel was sized by
 * whatever was in it: picking a revision collapsed it to the height of the
 * "loading…" line and re-inflated when the body arrived, jumping on every click.
 * A fixed-height shell with two internally-scrolling panes cannot do that. */
function buildLayout() {
  hmBody.innerHTML = '';
  const layout = document.createElement('div');
  layout.className = 'hm-layout';
  const list = document.createElement('div');
  list.className = 'hm-list';
  const pane = document.createElement('div');
  pane.className = 'hm-pane';
  layout.append(list, pane);
  hmBody.appendChild(layout);
  return { list, pane };
}

function placeholder(text) {
  const el = document.createElement('div');
  el.className = 'hm-placeholder';
  el.textContent = text;
  return el;
}

/** Swap only what is inside the preview pane, so its box never changes size. */
function setPane(pane, ...nodes) {
  pane.innerHTML = '';
  pane.append(...nodes);
}

function renderRevisions(data, panes) {
  const { list, pane } = panes;
  list.innerHTML = '';
  if (!data.items.length) {
    list.appendChild(note('No revisions recorded yet.'));
    setPane(pane, placeholder('Nothing to preview.'));
    return;
  }
  if (!data.exists) list.appendChild(note('Deleted — restoring brings it back.'));

  data.items.forEach((rev, i) => {
    // Two lines: the message gets the full width, with sha/date/badge beneath.
    // On one line the message was squeezed between them and ellipsized to a
    // couple of characters — "post 86 update: …" became "va…", which identifies
    // nothing, and picking a revision to restore is the entire job here.
    const row = document.createElement('button');
    row.className = 'hm-rev';
    row.type = 'button';

    const msg = document.createElement('span');
    msg.className = 'hm-msg';
    msg.textContent = rev.message;
    msg.title = rev.message;

    const meta = document.createElement('span');
    meta.className = 'hm-meta';
    const sha = document.createElement('span');
    sha.className = 'hm-sha';
    sha.textContent = rev.short_sha;
    const when = document.createElement('span');
    when.className = 'hm-when';
    when.textContent = rev.when.replace('T', ' ').slice(0, 16);
    meta.append(sha, when);
    if (i === 0 && data.exists) {
      const badge = document.createElement('span');
      badge.className = 'hm-badge';
      badge.textContent = 'current';
      meta.appendChild(badge);
    }

    row.append(msg, meta);
    row.addEventListener('click', () => selectRevision(rev, row, pane));
    list.appendChild(row);
  });
  setPane(pane, placeholder('Select a revision to see how the post looked.'));
}

async function selectRevision(rev, row, pane) {
  for (const el of hmBody.querySelectorAll('.hm-rev.active')) el.classList.remove('active');
  row.classList.add('active');
  setPane(pane, placeholder('loading…'));

  try {
    const d = await apiFetch(`/posts/${currentPostId}/history/${rev.sha}`);
    const head = note(`${d.title} — as of ${rev.short_sha}`, 'sm-section-title hm-pane-head');
    const body = document.createElement('pre');
    body.className = 'hm-body-text';
    body.textContent = d.content;          // never innerHTML: this is vault content

    const restore = document.createElement('button');
    restore.className = 'btn-edit hm-restore';
    restore.type = 'button';
    restore.textContent = `Restore this version (${rev.short_sha})`;
    restore.addEventListener('click', () => restoreRevision(rev, restore, pane));

    setPane(pane, head, body, restore);
  } catch (err) {
    setPane(pane, placeholder(`Could not load that revision: ${err.message}`));
  }
}

async function restoreRevision(rev, button, pane) {
  if (!confirm(`Restore this post to ${rev.short_sha}?\n\nThe current version stays in history, so this can be undone.`)) return;
  button.disabled = true;
  button.textContent = 'Restoring…';
  try {
    await apiFetch(`/posts/${currentPostId}/restore`, {
      method: 'POST',
      body: JSON.stringify({ sha: rev.sha }),
    });
    closeHistoryModal();
    onRestored();
  } catch (err) {
    button.disabled = false;
    button.textContent = `Restore this version (${rev.short_sha})`;
    pane.appendChild(note(`Restore failed: ${err.message}`, 'sm-error'));
  }
}

export async function openPostHistory(postId, title) {
  currentPostId = postId;
  historyModal.classList.add('open');
  document.body.style.overflow = 'hidden';
  hmTitle.textContent = `#${postId}${title ? ' · ' + title : ''}`;
  // Build the shell before fetching so the panel opens at its final size.
  const panes = buildLayout();
  panes.list.appendChild(note('loading…'));
  setPane(panes.pane, placeholder(''));
  try {
    renderRevisions(await apiFetch(`/posts/${postId}/history`), panes);
  } catch (err) {
    panes.list.innerHTML = '';
    // 503 is the one expected failure — the server has history switched off, or no
    // git binary — and deserves plain words rather than a bare status line.
    panes.list.appendChild(err.message.startsWith('503')
      ? note('Vault history is not enabled on this server, so there is nothing to restore from.', 'sm-error')
      : note(`Could not load history: ${err.message}`, 'sm-error'));
    setPane(panes.pane, placeholder(''));
  }
}

document.getElementById('hmClose').onclick = closeHistoryModal;
const hmBackdrop = document.getElementById('hmBackdrop');
hmBackdrop.onclick = closeHistoryModal;
attachSheetDismiss({
  inner: historyModal.querySelector('.sm-inner'),
  handle: historyModal.querySelector('.sm-head'),
  backdrop: hmBackdrop,
  onDismiss: closeHistoryModal,
});
