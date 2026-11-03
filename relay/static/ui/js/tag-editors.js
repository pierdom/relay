/* The sidebar's per-tag editors: rename and expiry/retention. main.js owns the
 * tag list, so it passes in what an editor needs to redraw it (`initTagEditors`). */

import { apiFetch } from './api.js';
import { query } from './feed-query.js';
import { showToast } from './toast.js';
import { cleanTag, toDatetimeLocal, toUtcIso } from './util.js';

let deps;

/** `{ renderTags, postCount, selectTag, loadTags, wireTagControls }` from main.js. */
export function initTagEditors(d) { deps = d; }

/** The tag list is about to be redrawn: the open editor's DOM goes with it. */
export function forgetTagEditor() { openTagEditor = null; }

/* One tag editor (rename or expiry) open at a time: opening another, clicking
   away or Escape closes the current one; re-clicking its control toggles it. */
let openTagEditor = null;   // { el, kind, cancel }

function closeTagEditor() {
  if (!openTagEditor) return;
  const { cancel } = openTagEditor;
  openTagEditor = null;
  cancel();
}

/** True when this control was already open, meaning the click should just close it. */
function toggledTagEditor(el, kind) {
  if (openTagEditor && openTagEditor.el === el && openTagEditor.kind === kind) {
    closeTagEditor();
    return true;
  }
  closeTagEditor();
  return false;
}

// A click anywhere outside the open editor dismisses it. Tag controls are
// exempt: closing on mousedown shifts the rows below before mouseup, so the
// click would land on the wrong row; they close the previous editor themselves.
document.addEventListener('mousedown', e => {
  if (!openTagEditor) return;
  if (openTagEditor.el.contains(e.target)) return;
  if (e.target.closest?.('.tag-config-btn, .tag-rename')) return;
  closeTagEditor();
});

export function startTagRename(el, oldName) {
  if (toggledTagEditor(el, 'rename')) return;
  const nameSpan = el.querySelector('.tag-name');
  const input = document.createElement('input');
  input.className = 'tag-rename-input';
  input.value = oldName;
  nameSpan.replaceWith(input);
  input.focus(); input.select();
  openTagEditor = { el, kind: 'rename', cancel: () => cancelRename() };

  let committed = false;
  async function commit() {
    if (committed) return; committed = true;
    // Normalised as the server stores it, so an active filter follows the rename.
    const newName = cleanTag(input.value);
    if (!newName || newName === oldName) { cancelRename(); return; }
    try {
      const data = await apiFetch(`/tags/${encodeURIComponent(oldName)}`, {
        method: 'PATCH', body: JSON.stringify({ new_name: newName }),
      });
      deps.renderTags(data.tags, await deps.postCount());
      if (query.tag === oldName) deps.selectTag(newName);   // feed and live stream follow the rename
    } catch (e) { showToast(`Couldn’t rename #${oldName}: ${e.message}`, { error: true }); committed = false; cancelRename(); }
  }

  function cancelRename() {
    if (committed) return;
    committed = true;
    if (openTagEditor && openTagEditor.el === el) openTagEditor = null;
    const label = document.createElement('button');
    label.type = 'button'; label.className = 'tag-name'; label.textContent = oldName;
    if (query.tag === oldName) label.setAttribute('aria-current', 'true');
    input.replaceWith(label);
    label.focus();   // focus was on the input that just went away
  }

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') cancelRename();
  });
  input.addEventListener('blur', commit);
}

export function startTagConfig(el, tagName) {
  if (toggledTagEditor(el, 'config')) return;
  const savedHtml = el.innerHTML;
  const form = document.createElement('div');
  form.className = 'tag-config-form';
  // Explicit Save/Cancel, not just Enter/Escape: the keyboard-only version was
  // undiscoverable, and unreachable once focus had left the inputs.
  const current = el._expiry;
  form.innerHTML = `
    <div class="tc-label"></div>
    <input type="number" class="tc-ttl" placeholder="Hours after posting" min="1" aria-label="Expire posts this many hours after posting">
    <input type="datetime-local" class="tc-expires" aria-label="Expire all posts at">
    <input type="number" class="tc-keep" placeholder="Keep only the newest N" min="1" aria-label="Keep only this many of the newest posts">
    <div class="tc-note" aria-live="polite"></div>
    <div class="tc-actions">
      <button type="button" class="tc-save">Save</button>
      <button type="button" class="tc-cancel">Cancel</button>
      ${current ? '<button type="button" class="tc-remove">Remove expiry</button>' : ''}
    </div>`;
  form.querySelector('.tc-label').textContent = current ? `Expiry for #${tagName}` : `Set an expiry for #${tagName}`;
  // What is set now — the form used to open empty whatever the tag carried.
  if (current?.ttl_hours) form.querySelector('.tc-ttl').value = current.ttl_hours;
  if (current?.expires_at) form.querySelector('.tc-expires').value = toDatetimeLocal(current.expires_at);
  if (current?.keep_last) form.querySelector('.tc-keep').value = current.keep_last;
  el.innerHTML = '';
  el.classList.add('tag-editing');
  el.appendChild(form);

  const ttlInput = form.querySelector('.tc-ttl');
  const expiresInput = form.querySelector('.tc-expires');
  const keepInput = form.querySelector('.tc-keep');
  // Say what a retention limit will do before it is saved: one typo'd digit
  // on a busy tag would otherwise expire most of it at the next sweep.
  const note = form.querySelector('.tc-note');
  const count = el._count || 0;
  // Whole numbers only: a number input accepts "1e3" and "2.5", which
  // parseInt read as 1 and 2 — "keep 1" on a typo.
  const wholeOrNull = (input) => {
    const raw = input.value.trim();
    if (!raw) return undefined;
    const n = Number(raw);
    return Number.isInteger(n) && n > 0 ? n : null;
  };
  const syncNote = () => {
    const keep = wholeOrNull(keepInput);
    const over = keep ? count - keep : 0;
    note.textContent = keep === null ? 'Keep only a whole number of posts, 1 or more.'
      : over > 0 ? `Up to ${over} of its ${count} posts, the oldest, will expire at the next cleanup`
        + ' (restorable while history is on).'
      : '';
  };
  keepInput.addEventListener('input', syncNote);
  syncNote();
  ttlInput.focus();
  openTagEditor = { el, kind: 'config', cancel: () => cancel() };

  let committed = false;
  async function commit() {
    if (committed) return; committed = true;
    const ttl = wholeOrNull(ttlInput);
    const expiresVal = expiresInput.value;
    const keep = wholeOrNull(keepInput);
    if (ttl === null || keep === null) {
      note.textContent = 'Hours and posts must be whole numbers, 1 or more.';
      committed = false;
      return;
    }
    // All cleared on a tag that had a rule means remove it; on one that had
    // none it means nothing was asked for.
    if (!ttl && !expiresVal && !keep) { if (current) { await save({}); } else { committed = false; cancel(); } return; }
    // The whole config is replaced, so every field shown goes back.
    const body = {};
    if (ttl) body.ttl_hours = ttl;
    if (expiresVal) body.expires_at = toUtcIso(expiresVal);
    if (keep) body.keep_last = keep;
    await save(body);
  }

  // `{}` removes the tag's config server-side (set_tag_config).
  async function save(body) {
    try {
      await apiFetch(`/tags/${encodeURIComponent(tagName)}/config`, {
        method: 'POST', body: JSON.stringify(body),
      });
      if (openTagEditor && openTagEditor.el === el) openTagEditor = null;
      await deps.loadTags();
    } catch (e) { showToast(`Couldn’t save expiry for #${tagName}: ${e.message}`, { error: true }); committed = false; cancel(); }
  }

  function cancel() {
    if (committed) return;
    committed = true;
    if (openTagEditor && openTagEditor.el === el) openTagEditor = null;
    el.classList.remove('tag-editing');
    el.innerHTML = savedHtml;
    deps.wireTagControls(el, tagName);
  }

  form.querySelector('.tc-save').addEventListener('click', e => { e.stopPropagation(); commit(); });
  form.querySelector('.tc-cancel').addEventListener('click', e => { e.stopPropagation(); cancel(); });
  form.querySelector('.tc-remove')?.addEventListener('click', e => {
    e.stopPropagation();
    if (committed) return;
    committed = true;
    save({});
  });
  // The row itself filters the feed on click; a click inside the form must not.
  form.addEventListener('click', e => e.stopPropagation());
  form.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { e.stopPropagation(); cancel(); }
  });
}
