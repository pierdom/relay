/* Shared "edit a post" form: markup, attachment upload wiring, save/cancel.
 *
 * Originally inline in main.js, only ever built into the standalone Edit
 * modal (`enterEditMode`). Pulled out so the vault-lint pane (lint.js) can
 * host the same editor directly instead of a read-only preview plus a
 * button that left the lint list behind — the whole reason that pane exists
 * is to fix what a finding flags without losing your place in the list.
 *
 * Self-contained like status.js/deleted.js: owns no DOM of its own (the
 * caller supplies the container to render into) and needs nothing from
 * main.js beyond apiFetch/apiSend.
 */

import { apiFetch, apiSend } from './api.js';
import { escHtml, fmtBytes, toDatetimeLocal, toUtcIso } from './util.js';

// ── Attachment upload (drag/drop, paste, file picker) ──────────────────────

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',', 2)[1] || '');
    r.onerror = () => reject(new Error('could not read file'));
    r.readAsDataURL(file);
  });
}

function insertAtCursor(ta, text) {
  const s = ta.selectionStart ?? ta.value.length, e = ta.selectionEnd ?? ta.value.length;
  ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
  ta.selectionStart = ta.selectionEnd = s + text.length;
  ta.focus();
}

// At/above this size, skip base64 (which inflates the JSON body ~33% and buffers
// the whole blob as a string) and stream the raw bytes through a presigned slot:
// POST a slot → PUT the file bytes → finalize with the upload_id.
const PRESIGNED_MIN_BYTES = 4 * 1024 * 1024;   // 4 MB

async function postAttachment(name, file, extra) {
  if (file.size >= PRESIGNED_MIN_BYTES) {
    const slot = await apiFetch('/attachments/uploads', { method: 'POST' });
    // Relative, same-origin path so the session cookie authenticates the PUT.
    const put = await apiSend(`/attachments/uploads/${encodeURIComponent(slot.upload_id)}`,
                              { method: 'PUT', body: file });
    if (!put.ok) throw new Error(`upload ${put.status} ${put.statusText}`);
    return apiFetch('/attachments', { method: 'POST',
      body: JSON.stringify({ upload_id: slot.upload_id, filename: name, ...extra }) });
  }
  const data = await fileToBase64(file);
  return apiFetch('/attachments', { method: 'POST',
    body: JSON.stringify({ filename: name, data, ...extra }) });
}

// Upload one file, then insert its ![[ref]] at the cursor. `getExtra` supplies the
// placement fields: an existing post ({post_id, embed:false} — server files it in
// the post's folder, UI places the ref) or a new note ({tags} — server derives the
// folder the note will use, so the image lands beside it instead of in Inbox).
async function uploadOne(file, ta, statusEl, getExtra) {
  const name = file.name || `pasted-${Date.now()}.png`;
  statusEl.classList.remove('error');
  statusEl.textContent = `Uploading ${name}…`;
  try {
    const res = await postAttachment(name, file, getExtra());
    // ![[…]] embed for everything: images render inline, other files as a 📎 link.
    insertAtCursor(ta, `\n![[${res.filename}]]\n`);
    statusEl.textContent = `Attached ${res.filename} → ${res.folder}/assets`;
  } catch (e) {
    statusEl.classList.add('error');
    statusEl.textContent = `Upload failed: ${e.message}`;
  }
}
async function uploadMany(files, ta, statusEl, getExtra) {
  for (const f of files) await uploadOne(f, ta, statusEl, getExtra);
}

export function wireAttachments(ta, fileInput, attachBtn, statusEl, getExtra) {
  attachBtn.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', async () => {
    await uploadMany([...fileInput.files], ta, statusEl, getExtra);
    fileInput.value = '';
  });
  ta.addEventListener('dragover', e => { e.preventDefault(); ta.classList.add('drag-over'); });
  ta.addEventListener('dragleave', () => ta.classList.remove('drag-over'));
  ta.addEventListener('drop', async e => {
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault(); ta.classList.remove('drag-over');
    await uploadMany([...e.dataTransfer.files], ta, statusEl, getExtra);
  });
  ta.addEventListener('paste', async e => {
    const files = [...(e.clipboardData?.items || [])]
      .filter(i => i.kind === 'file').map(i => i.getAsFile()).filter(Boolean);
    if (!files.length) return;   // let normal text paste through
    e.preventDefault();
    await uploadMany(files, ta, statusEl, getExtra);
  });
}

// Confirm + DELETE an attachment; alerts if posts still reference it. Returns
// true when the file was removed (callers refresh their own view).
export async function confirmDeleteAttachment(name) {
  if (!confirm(`Delete "${name}" from the vault? This removes the file itself.`)) return false;
  try {
    const r = await apiFetch(`/attachments/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (r.referenced_by?.length)
      alert(`Deleted. Still referenced by ${r.referenced_by.map(i => '#' + i).join(', ')} — those embeds are now broken.`);
    return true;
  } catch (e) { alert(`Delete failed: ${e.message}`); return false; }
}

// ── Broken-link highlighting ────────────────────────────────────────────────
//
// A textarea can't style individual substrings, so the content field is
// paired with a same-sized backdrop <pre> behind it: the backdrop paints a
// red background behind exactly the [[wikilink]]/#id spans that don't
// resolve (its own text is transparent, so characters are only ever drawn
// once, by the textarea on top) — see the CSS for the layering. Checked
// against /links (cached — see loadLinkIndex below); a link resolving or
// breaking *while* the form is open is rare enough that "current as of the
// last fetch" is an acceptable lag, the same tradeoff every other read in
// this app makes.
//
// "Broken" here means the same thing GET /lint's broken_link rule means —
// relay.links.WIKILINK_RE/IDREF_RE's definition, not the richer render-time
// rules main.js's post-viewer uses (which treat an unresolved ![[x.png]] as
// an attachment embed, not a broken link) — so what lights up here is
// exactly what a lint finding would flag, not a superset or subset of it.

const WIKILINK_RE = /\[\[([^\]|#]+?)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]/g;
const IDREF_RE = /(?<![\w#])#(\d{1,5})\b/g;

// Cached across calls: browsing the lint pane opens a fresh buildEditForm per
// finding clicked, and re-fetching the same /links index for every single one
// of them (nothing about it changes just from looking at a post) would be a
// GET per click on a panel whose whole point is a fast browse-and-fix loop.
// Invalidated after any successful save below, since a title change can
// change what other posts' [[wikilinks]] resolve to.
let linkIndexCache = null;

async function loadLinkIndex() {
  if (linkIndexCache) return linkIndexCache;
  try {
    const d = await apiFetch('/links');
    linkIndexCache = {
      titles: new Set(d.items.map(i => i.title.trim().toLowerCase())),
      ids: new Set(d.items.map(i => i.id)),
    };
    return linkIndexCache;
  } catch {
    return { titles: new Set(), ids: new Set() };
  }
}

/** Every [[wikilink]]/#id span in `text`, each marked broken or not against
 * `linkIndex`. Both regexes run independently (matching relay.links'
 * extract_links, which does the same over the very same content), so a
 * wikilink target shaped like a bare number could in principle also match
 * the id-ref pattern inside its own brackets — sorted by start and any span
 * starting before the previous one's end is dropped rather than nested,
 * since malformed markup is worse here than the one dropped highlight. */
function findLinkSpans(text, linkIndex) {
  const raw = [];
  let m;
  WIKILINK_RE.lastIndex = 0;
  while ((m = WIKILINK_RE.exec(text))) {
    raw.push({ start: m.index, end: m.index + m[0].length, broken: !linkIndex.titles.has(m[1].trim().toLowerCase()) });
  }
  IDREF_RE.lastIndex = 0;
  while ((m = IDREF_RE.exec(text))) {
    raw.push({ start: m.index, end: m.index + m[0].length, broken: !linkIndex.ids.has(Number(m[1])) });
  }
  raw.sort((a, b) => a.start - b.start);
  const spans = [];
  for (const s of raw) {
    if (spans.length && s.start < spans[spans.length - 1].end) continue;
    spans.push(s);
  }
  return spans;
}

function renderBackdrop(backdrop, text, linkIndex) {
  let out = '';
  let pos = 0;
  for (const span of findLinkSpans(text, linkIndex)) {
    out += escHtml(text.slice(pos, span.start));
    const chunk = escHtml(text.slice(span.start, span.end));
    out += span.broken ? `<span class="ef-broken-link">${chunk}</span>` : chunk;
    pos = span.end;
  }
  out += escHtml(text.slice(pos));
  backdrop.innerHTML = out;
}

function syncBackdropScroll(contentField, backdrop) {
  backdrop.scrollTop = contentField.scrollTop;
  backdrop.scrollLeft = contentField.scrollLeft;
}

/** Wires a content textarea to its highlight backdrop: fetches the link
 * index once, repaints on every keystroke, and keeps the backdrop's scroll
 * glued to the textarea's own (it is not independently scrollable). */
function wireBrokenLinkHighlight(contentField, backdrop) {
  let linkIndex = { titles: new Set(), ids: new Set() };
  const repaint = () => renderBackdrop(backdrop, contentField.value, linkIndex);
  repaint();
  loadLinkIndex().then(idx => { linkIndex = idx; repaint(); });
  contentField.addEventListener('input', repaint);
  contentField.addEventListener('scroll', () => syncBackdropScroll(contentField, backdrop));
}

/**
 * Focus the content field and select the first occurrence of `text` in it —
 * how a lint finding's `match` (the exact broken `[[wikilink]]`/`#id` text,
 * see relay/lint.py) turns into "jump to the spot that's actually wrong"
 * instead of leaving someone to search a long post for it by eye. A plain
 * `textarea.setSelectionRange` after `.focus()` already scrolls the
 * selection into view natively; the one thing that needs doing by hand is
 * carrying that same scroll position over to the highlight backdrop, which
 * has no scroll of its own to react to.
 *
 * Returns whether a match was found — a caller can fall back to nothing
 * (leave the field as opened) when the content changed since the finding
 * was computed and the exact substring no longer appears.
 */
export function scrollToMatch(container, text) {
  if (!text) return false;
  const contentField = container.querySelector('.ef-content');
  const idx = contentField?.value.indexOf(text) ?? -1;
  if (idx === -1) return false;
  contentField.focus();
  contentField.setSelectionRange(idx, idx + text.length);
  const backdrop = container.querySelector('.ef-content-backdrop');
  if (backdrop) requestAnimationFrame(() => syncBackdropScroll(contentField, backdrop));
  return true;
}

// Edit-form list of the post-folder's attachments, each with a delete (×) button.
export async function renderEditAttachments(el, postId) {
  const box = el.querySelector('.ef-attachments');
  if (!box) return;
  let d;
  try { d = await apiFetch(`/attachments?post_id=${postId}`); } catch { box.innerHTML = ''; return; }
  if (!d.items.length) { box.innerHTML = ''; return; }
  box.innerHTML = `<div class="ef-attach-head">Files in ${escHtml(d.items[0].folder)}/assets</div>` +
    d.items.map(a => `<div class="ef-attach-item" data-name="${escHtml(a.filename)}">
      <span class="ef-attach-name">${escHtml(a.filename)}</span>
      <span class="ef-attach-size">${fmtBytes(a.bytes)}</span>
      <button type="button" class="ef-attach-del" title="Delete file from vault">×</button></div>`).join('');
  box.querySelectorAll('.ef-attach-del').forEach(btn =>
    btn.addEventListener('click', async () => {
      const name = btn.closest('.ef-attach-item').dataset.name;
      if (await confirmDeleteAttachment(name)) await renderEditAttachments(el, postId);
    }));
}

// ── The form itself ─────────────────────────────────────────────────────────

/**
 * Render a post-editing form into `container`, wired to save/cancel.
 *
 * `onSave(updatedPost)` runs after a successful PATCH. `onCancel()` runs once
 * Cancel is confirmed — the dirty check happens here, inside this function,
 * not in the caller, so both the standalone Edit modal and the lint pane get
 * the same "discard your changes?" guard for free rather than each
 * reimplementing it.
 *
 * Returns `{ isDirty }` so a caller can guard a *different* way of leaving
 * (a swipe-to-dismiss, picking another row in a list) the same way Cancel is
 * guarded internally.
 */
export function buildEditForm(container, post, { onSave, onCancel, focus = true } = {}) {
  container.innerHTML = `
    <div class="edit-form">
      <div><label>Title<input class="ef-title" type="text" value="${escHtml(post.title || '')}"></label></div>
      <div class="ef-content-wrap"><label>Content</label>
        <div class="ef-content-highlight">
          <pre class="ef-content-backdrop" aria-hidden="true"></pre>
          <textarea class="ef-content" spellcheck="false">${escHtml(post.content)}</textarea>
        </div>
        <div class="attach-row">
          <input type="file" class="ef-file" multiple style="display:none">
          <button type="button" class="btn-attach ef-attach">📎 Attach</button>
          <span class="attach-status ef-attach-status"></span>
        </div>
        <div class="ef-attachments"></div>
      </div>
      <div><label>Tags<input class="ef-tags" type="text" value="${escHtml(post.tags.join(', '))}"></label></div>
      <div><label>Source<input class="ef-source" type="text" value="${escHtml(post.source || '')}"></label></div>
      <div><label>Expires<input class="ef-expires" type="datetime-local" value="${toDatetimeLocal(post.expires_at || '')}"></label></div>
      <div class="edit-actions">
        <button type="button" class="btn-cancel">Cancel</button>
        <button type="button" class="btn-save">Save</button>
      </div>
    </div>`;

  const contentField = container.querySelector('.ef-content');
  const titleField = container.querySelector('.ef-title');
  const tagsField = container.querySelector('.ef-tags');
  const sourceField = container.querySelector('.ef-source');
  const expiresField = container.querySelector('.ef-expires');
  // Every field the form can actually change, not just content — the lint
  // pane's whole reason to exist is fixing a missing/zero-tags finding by
  // editing *only* Tags, and a content-only check would treat that as
  // nothing to lose: picking another finding, switching a filter, or
  // closing the pane would silently discard it without asking. Baselines are
  // each field's own starting value (not `post.*` directly), so this stays
  // correct even where the two differ in shape (Tags starts as a joined
  // string, Expires as the datetime-local input's own text format).
  const initial = {
    title: titleField.value, content: contentField.value,
    tags: tagsField.value, source: sourceField.value, expires: expiresField.value,
  };
  const isDirty = () =>
    contentField.value !== initial.content ||
    titleField.value !== initial.title ||
    tagsField.value !== initial.tags ||
    sourceField.value !== initial.source ||
    expiresField.value !== initial.expires;
  wireBrokenLinkHighlight(contentField, container.querySelector('.ef-content-backdrop'));

  wireAttachments(
    contentField, container.querySelector('.ef-file'),
    container.querySelector('.ef-attach'), container.querySelector('.ef-attach-status'),
    () => ({ post_id: post.id, embed: false }),
  );
  renderEditAttachments(container, post.id);
  if (focus) titleField.focus();

  container.querySelector('.btn-cancel').addEventListener('click', () => {
    if (isDirty() && !confirm('Discard your changes to this post?')) return;
    onCancel?.();
  });
  container.querySelector('.btn-save').addEventListener('click', async () => {
    const newTitle = titleField.value.trim();
    if (!newTitle) { alert('Title is required'); return; }
    const body = {
      title:      newTitle,
      content:    contentField.value,
      tags:       tagsField.value.split(',').map(s => s.trim()).filter(Boolean),
      source:     sourceField.value.trim() || null,
      expires_at: toUtcIso(expiresField.value) || null,
    };
    const btn = container.querySelector('.btn-save');
    btn.disabled = true; btn.textContent = 'Saving…';
    try {
      const updated = await apiFetch(`/posts/${post.id}`, { method: 'PATCH', body: JSON.stringify(body) });
      linkIndexCache = null;   // a title change can change what other posts' [[wikilinks]] resolve to
      onSave?.(updated);
    } catch (e) {
      alert(`Save failed: ${e.message}`);
      btn.disabled = false; btn.textContent = 'Save';
    }
  });

  return { isDirty };
}
