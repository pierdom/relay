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

function renderRevisions(data) {
  hmBody.innerHTML = '';
  if (!data.items.length) {
    hmBody.appendChild(note('No revisions recorded for this post yet.'));
    return;
  }
  if (!data.exists) hmBody.appendChild(note('This post is deleted — restoring brings it back.'));

  const list = document.createElement('div');
  list.className = 'hm-list';
  data.items.forEach((rev, i) => {
    const row = document.createElement('button');
    row.className = 'hm-rev';
    row.type = 'button';
    const sha = document.createElement('span');
    sha.className = 'hm-sha';
    sha.textContent = rev.short_sha;
    const msg = document.createElement('span');
    msg.className = 'hm-msg';
    msg.textContent = rev.message;
    const when = document.createElement('span');
    when.className = 'hm-when';
    when.textContent = rev.when.replace('T', ' ').slice(0, 16);
    row.append(sha, msg, when);
    if (i === 0 && data.exists) row.classList.add('hm-current');
    row.addEventListener('click', () => selectRevision(rev, row));
    list.appendChild(row);
  });
  hmBody.appendChild(list);
}

async function selectRevision(rev, row) {
  for (const el of hmBody.querySelectorAll('.hm-rev.active')) el.classList.remove('active');
  row.classList.add('active');

  let preview = hmBody.querySelector('.hm-preview');
  if (preview) preview.remove();
  preview = document.createElement('div');
  preview.className = 'hm-preview';
  preview.appendChild(note('loading…'));
  hmBody.appendChild(preview);

  try {
    const d = await apiFetch(`/posts/${currentPostId}/history/${rev.sha}`);
    preview.innerHTML = '';
    preview.appendChild(note(`${d.title} — as of ${rev.short_sha}`));
    const body = document.createElement('pre');
    body.className = 'hm-body-text';
    body.textContent = d.content;          // never innerHTML: this is vault content
    preview.appendChild(body);

    const restore = document.createElement('button');
    restore.className = 'btn-edit hm-restore';
    restore.type = 'button';
    restore.textContent = `Restore this version (${rev.short_sha})`;
    restore.addEventListener('click', () => restoreRevision(rev, restore));
    preview.appendChild(restore);
  } catch (err) {
    preview.innerHTML = '';
    preview.appendChild(note(`Could not load that revision: ${err.message}`, 'sm-error'));
  }
}

async function restoreRevision(rev, button) {
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
    hmBody.appendChild(note(`Restore failed: ${err.message}`, 'sm-error'));
  }
}

export async function openPostHistory(postId, title) {
  currentPostId = postId;
  historyModal.classList.add('open');
  document.body.style.overflow = 'hidden';
  hmTitle.textContent = `#${postId}${title ? ' · ' + title : ''}`;
  hmBody.innerHTML = '';
  hmBody.appendChild(note('loading…'));
  try {
    renderRevisions(await apiFetch(`/posts/${postId}/history`));
  } catch (err) {
    hmBody.innerHTML = '';
    // 503 is the one expected failure — the server has history switched off, or no
    // git binary — and deserves plain words rather than a bare status line.
    hmBody.appendChild(err.message.startsWith('503')
      ? note('Vault history is not enabled on this server, so there is nothing to restore from.', 'sm-error')
      : note(`Could not load history: ${err.message}`, 'sm-error'));
  }
}

document.getElementById('hmClose').onclick = closeHistoryModal;
document.getElementById('hmBackdrop').onclick = closeHistoryModal;
