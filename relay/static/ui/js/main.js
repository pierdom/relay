/* Relay browser UI — application entry point.
 *
 * Stage 2 of splitting index.html: the whole script moved out of the markup and
 * became an ES module. Leaf concerns are being lifted into ./util.js, ./api.js
 * and ./status.js; what remains here is the app body, still to be split along the
 * section markers below.
 *
 * Module scope matters: nothing here is on `window` any more. That is safe
 * because the markup carries no inline `onclick` handlers and nothing assigned to
 * `window.*` — both checked before the move. `marked` and `DOMPurify` are still
 * read as globals; they are classic CDN scripts in <head> and so run before this
 * deferred module.
 */

import { apiFetch, apiSend, clearApiKey, setApiKey } from './api.js';
import { closeStatusModal, isStatusOpen } from './status.js';   // also self-wires its own controls
import { query, resetPaging } from './feed-query.js';
import { closeHistoryModal, initPostHistory, isHistoryOpen, openPostHistory } from './post-history.js';
import { attachSheetDismiss } from './sheet.js';
import { initDeleted } from './deleted.js';
import { closeThemeMenu, isThemeMenuOpen } from './theme.js';
import { applySort, initViewPrefs, isDefaultSort, prefs } from './view-prefs.js';
import { escHtml, fmtBytes, relativeTime, toDatetimeLocal, toUtcIso } from './util.js';
const LIMIT = 20;
// The break-glass API key now lives in ./api.js (setApiKey/clearApiKey).
let authed = false;    // true once a session exists (cookie or key) — the real "logged in" flag
let sidebarMode = 'tags';   // 'tags' | 'tree' | 'files' | 'deleted'
let attachCache = [];       // last-fetched attachment list (for the gallery)
let attachFolder = null;    // active gallery folder filter (null = all)
let searchDebounce = null;
let loadingMore = false;   // guards the infinite-scroll auto-load
let es = null;
let sseErrorTimer = null;

const feed         = document.getElementById('feed');
const tagList      = document.getElementById('tagList');
const loadMoreWrap = document.getElementById('loadMore');
const loadMoreBtn  = document.getElementById('loadMoreBtn');
const liveDot      = document.getElementById('liveDot');
const liveLabel    = document.getElementById('liveLabel');
const a11yAnnouncer = document.getElementById('a11yAnnouncer');
const apiKeyInput  = document.getElementById('apiKeyInput');
const connectBtn   = document.getElementById('connectBtn');
const connectForm  = document.getElementById('connectForm');
const oidcLogin    = document.getElementById('oidcLogin');
const oidcLoginBtn = document.getElementById('oidcLoginBtn');
const useKeyBtn    = document.getElementById('useKeyBtn');
const newPostBtn   = document.getElementById('newPostBtn');
const statusBtn    = document.getElementById('statusBtn');
const collapseBtn  = document.getElementById('collapseBtn');
const composePanel = document.getElementById('composePanel');
const newTagBtn    = document.getElementById('newTagBtn');
const tagNewWrap   = document.getElementById('tagNew');
const tagNewInput  = document.getElementById('tagNewInput');
const connectedBar    = document.getElementById('connectedBar');
const disconnectBtn   = document.getElementById('disconnectBtn');
const loginView       = document.getElementById('loginView');
const menuBtn         = document.getElementById('menuBtn');
const sidebarEl       = document.getElementById('sidebarEl');
const sidebarOverlay  = document.getElementById('sidebarOverlay');
const searchBar       = document.getElementById('searchBar');
const attachmentsView = document.getElementById('attachmentsView');
const lightbox        = document.getElementById('lightbox');
const searchInput     = document.getElementById('searchInput');
const searchClear     = document.getElementById('searchClear');
const vtList          = document.getElementById('vtList');
const vtGrid          = document.getElementById('vtGrid');

// View/sort preferences live in ./view-prefs.js; reloading on a sort change is
// this module's job, so it is passed in.
function reloadSorted() { resetPaging(); applySort(); loadPosts(true); }
initViewPrefs(reloadSorted);

const newPostsPill  = document.getElementById('newPostsPill');
const newPostsLabel = document.getElementById('newPostsLabel');
let pendingNew = 0;
function bumpNewPostsPill() {
  pendingNew++;
  newPostsLabel.textContent = pendingNew === 1 ? '1 new post' : `${pendingNew} new posts`;
  newPostsPill.style.display = '';
}
function clearNewPostsPill() {
  pendingNew = 0;
  newPostsPill.style.display = 'none';
}
newPostsPill.addEventListener('click', () => { clearNewPostsPill(); reloadSorted(); });

// Themes live in ./theme.js, which self-wires its picker (see status.js for the
// same pattern). Only the Escape handler below needs anything from it.

function openSidebar()  { sidebarEl.classList.add('open'); sidebarOverlay.classList.add('visible'); menuBtn.classList.add('active'); }
function closeSidebar() { sidebarEl.classList.remove('open'); sidebarOverlay.classList.remove('visible'); menuBtn.classList.remove('active'); }
menuBtn.addEventListener('click', () => sidebarEl.classList.contains('open') ? closeSidebar() : openSidebar());
sidebarOverlay.addEventListener('click', closeSidebar);

// Desktop sidebar collapse — width animates to 0, state persisted.
function applySidebarCollapsed(collapsed) {
  sidebarEl.classList.toggle('collapsed', collapsed);
  collapseBtn.classList.toggle('collapsed', collapsed);
  collapseBtn.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
  collapseBtn.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
}
collapseBtn.addEventListener('click', () => {
  const collapsed = !sidebarEl.classList.contains('collapsed');
  applySidebarCollapsed(collapsed);
  try { localStorage.setItem('relay-sidebar-collapsed', collapsed ? '1' : '0'); } catch (e) {}
});
try { applySidebarCollapsed(localStorage.getItem('relay-sidebar-collapsed') === '1'); } catch (e) {}

document.getElementById('connectForm').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const val = apiKeyInput.value.trim();
  if (!val) return;
  try {
    const res = await fetch('/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: val }),
      credentials: 'same-origin',
    });
    if (!res.ok) { alert('Invalid API key'); return; }
    setApiKey(val);
    authed = true;
    init();
  } catch (e) {
    alert('Connection failed: ' + e.message);
  }
});

oidcLoginBtn.addEventListener('click', () => { window.location.href = '/auth/login'; });
useKeyBtn.addEventListener('click', () => {
  oidcLogin.style.display = 'none';
  connectForm.style.display = '';
  apiKeyInput.focus();
});

// On load, ask the server whether a session cookie is already live (OIDC or a
// prior key-paste). If so, boot straight into the app cookie-only — no re-paste.
// Otherwise show the right login control based on whether OIDC is configured.
async function bootstrap() {
  const params = new URLSearchParams(location.search);
  if (params.get('auth_error') === 'forbidden') alert('Your account is not authorized for relay.');
  else if (params.get('auth_error')) alert('Login failed. Please try again.');
  if (params.has('auth_error')) history.replaceState(null, '', location.pathname);

  let me = { authenticated: false, oidc: false };
  try { me = await (await fetch('/auth/me', { credentials: 'same-origin' })).json(); } catch {}
  if (me.authenticated) { authed = true; init(); return; }
  // Logged out: hide the feed, show the centered login card.
  feed.style.display = 'none';
  loginView.style.display = '';
  if (me.oidc) { oidcLogin.style.display = ''; connectForm.style.display = 'none'; }
  else { connectForm.style.display = ''; oidcLogin.style.display = 'none'; }
}

disconnectBtn.addEventListener('click', async () => {
  clearApiKey();
  authed = false;
  // Await so the cookie is cleared before bootstrap() re-checks /auth/me below.
  await fetch('/session', { method: 'DELETE', credentials: 'same-origin' }).catch(() => {});
  if (es) { es.close(); es = null; }
  if (sseErrorTimer) { clearTimeout(sseErrorTimer); sseErrorTimer = null; }
  closeSidebar();
  closeCompose();
  feed.innerHTML = '';
  tagList.innerHTML = '';
  loadMoreWrap.style.display = 'none';
  newPostBtn.style.display = 'none';
  newTagBtn.style.display = 'none';
  statusBtn.style.display = 'none';
  searchBar.style.display = 'none';
  query.search = null; searchInput.value = ''; searchBar.classList.remove('active');
  connectedBar.style.display = 'none';
  collapseBtn.style.display = 'none';
  apiKeyInput.value = '';
  setDot('');
  // Show whichever login control fits the deployment (OIDC button vs key form).
  bootstrap();
});



async function init() {
  resetPaging();
  feed.innerHTML = '';
  // Reset to the default Tags + feed view (in case we reconnect from Files/Tree mode).
  sidebarMode = 'tags'; attachFolder = null;
  document.getElementById('tabTags').classList.add('active');
  document.getElementById('tabTree').classList.remove('active');
  document.getElementById('tabFiles').classList.remove('active');
  feed.style.display = '';
  attachmentsView.style.display = 'none';
  loginView.style.display = 'none';
  loadMoreWrap.style.display = 'none';
  connectForm.style.display = 'none';
  oidcLogin.style.display = 'none';
  connectedBar.style.display = '';
  collapseBtn.style.display = '';
  newPostBtn.style.display = '';
  newTagBtn.style.display = '';
  statusBtn.style.display = '';
  searchBar.style.display = '';
  await Promise.all([loadTags(), loadPosts(true), loadLinkIndex()]);
  setDot('connected');
  connectSSE();
}

// ── Wikilinks: [[Title]] / [[Title|alias]] and #NNN cross-references ──────────
let linkIndex = new Map();   // normalised title -> id
let linkIds = new Set();     // existing post ids

async function loadLinkIndex() {
  try {
    const d = await apiFetch('/links');
    linkIndex = new Map(d.items.map(i => [i.title.trim().toLowerCase(), i.id]));
    linkIds = new Set(d.items.map(i => i.id));
  } catch {}
}

// DOMPurify config: keep the attrs our attachment embeds/links add (img loading,
// link target/rel). marked + preprocessLinks output is sanitized through this.
const SANITIZE_OPTS = { ADD_ATTR: ['target', 'rel', 'loading'] };
const renderBody = (md) => DOMPurify.sanitize(marked.parse(preprocessLinks(md)), SANITIZE_OPTS);

// Convert wikilinks / id-refs to anchors, leaving fenced + inline code untouched.
function preprocessLinks(md) {
  return md.split(/(```[\s\S]*?```|`[^`\n]*`)/g)
    .map((seg, i) => (i % 2 === 1) ? seg : linkifySegment(seg)).join('');
}

const IMAGE_EXT_RE  = /\.(png|jpe?g|gif|webp|svg|avif|bmp)$/i;
// Any file extension. Only used on the ![[…]] embed path, which is *always* a file
// in Obsidian — so a bare note title (no `!`) can never be mistaken for a file.
const HAS_EXT_RE    = /\.[a-z0-9]{1,12}$/i;
// Curated types for the plain [[…]] link path, where a dotted note title like
// [[Section 2.1]] must NOT be treated as a file.
const ATTACH_EXT_RE = /\.(png|jpe?g|gif|webp|svg|avif|bmp|pdf|canvas|docx?|xlsx?|pptx?|csv|txt|rtf|odt|ods|zip|epub|mp3|m4a|wav|flac|ogg|aac|opus|mp4|mov|webm|mkv|avi)$/i;

// /attachments/ URL, encoding each path segment (bare filenames stay bare).
const attUrl = (name) => '/attachments/' + name.split('/').map(encodeURIComponent).join('/');
const attLink = (name, label) =>
  `<a class="attachment-link" href="${attUrl(name)}" target="_blank" rel="noopener noreferrer">${escHtml(label)}</a>`;

function linkifySegment(text) {
  // Obsidian embeds: ![[target(|opts)]] — image, other-file link, or note transclusion.
  text = text.replace(/!\[\[([^\]|#]+?)(?:\|([^\]]+))?\]\]/g, (m, target, opts) => {
    const name = target.trim(), o = (opts || '').trim();
    if (IMAGE_EXT_RE.test(name)) {
      const dim = o.match(/^(\d+)(?:x(\d+))?$/);   // Obsidian sizing: |W or |WxH
      const size = dim ? ` width="${dim[1]}"${dim[2] ? ` height="${dim[2]}"` : ''}` : '';
      const alt = dim || !o ? name : o;
      return `<img class="attachment" src="${attUrl(name)}" alt="${escHtml(alt)}" loading="lazy"${size}>`;
    }
    if (HAS_EXT_RE.test(name)) return attLink(name, o || name);   // any file (pdf/zip/…) → link
    // No extension → note transclusion; relay doesn't transclude, so link to the note.
    const id = linkIndex.get(name.toLowerCase());
    return (id !== undefined)
      ? `<a class="wikilink" data-post-id="${id}">${escHtml(o || name)}</a>`
      : `<span class="wikilink broken" title="unresolved embed">${escHtml(o || name)}</span>`;
  });
  text = text.replace(/\[\[([^\]|#]+?)(#[^\]|]+)?(?:\|([^\]]+))?\]\]/g, (m, target, heading, alias) => {
    const label = escHtml((alias || target).trim());
    const t = target.trim();
    const id = linkIndex.get(t.toLowerCase());
    if (id !== undefined) return `<a class="wikilink" data-post-id="${id}">${label}</a>`;
    // Unresolved but a known attachment type (e.g. [[doc.pdf]]) → attachment link, not broken.
    if (ATTACH_EXT_RE.test(t)) return attLink(t, (alias || target).trim());
    return `<span class="wikilink broken" title="unresolved link">${label}</span>`;
  });
  text = text.replace(/(^|[^\w#])#(\d{1,5})\b/g, (m, pre, n) =>
    linkIds.has(Number(n)) ? `${pre}<a class="wikilink" data-post-id="${n}">#${n}</a>` : m);
  return text;
}

// First image embed → thumbnail URL + image count, plus the content with image
// embeds removed so a card's text preview shows prose instead of an image slice.
// Non-image embeds (pdf, note transclusions) are left in place.
function extractMedia(content) {
  let thumb = null, count = 0;
  const stripped = content.replace(/!\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]/g, (m, target) => {
    const name = target.trim();
    if (!IMAGE_EXT_RE.test(name)) return m;
    count++;
    if (!thumb) thumb = attUrl(name);
    return '';
  });
  return { thumb, count, stripped };
}

async function openPostById(id) {
  try { openPostModal(await apiFetch(`/posts/${id}`)); } catch {}
}

// Delegated: any rendered wikilink opens its target post.
document.addEventListener('click', e => {
  const a = e.target.closest('a.wikilink[data-post-id]');
  if (!a) return;
  e.preventDefault(); e.stopPropagation();
  openPostById(Number(a.dataset.postId));
});

// A missing/unauthorised attachment image degrades to a labelled link (built as a
// DOM node, not innerHTML, so it bypasses the sanitiser). error doesn't bubble → capture.
document.addEventListener('error', e => {
  const img = e.target;
  if (img.tagName !== 'IMG' || !img.classList.contains('attachment')) return;
  const a = document.createElement('a');
  a.className = 'attachment-link broken';
  a.href = img.getAttribute('src'); a.target = '_blank'; a.rel = 'noopener noreferrer';
  a.textContent = img.getAttribute('alt') || 'attachment';
  img.replaceWith(a);
}, true);

async function renderBacklinks(id) {
  const el = document.getElementById('pmBacklinks');
  if (!el) return;
  try {
    const d = await apiFetch(`/posts/${id}/backlinks`);
    el.innerHTML = d.items.length
      ? `<h4>Linked mentions (${d.items.length})</h4><ul>${d.items.map(i =>
          `<li><a class="wikilink" data-post-id="${i.id}"><span class="bl-id">#${i.id}</span>${escHtml(i.title)}</a></li>`
        ).join('')}</ul>`
      : '';
  } catch { el.innerHTML = ''; }
}

/* ── Compose ──────────────────────────────────────────────── */
newPostBtn.addEventListener('click', () => {
  if (composePanel.classList.contains('open')) {
    closeCompose();
  } else {
    composePanel.classList.add('open');
    if (query.tag) document.getElementById('cpTags').value = query.tag;
    document.getElementById('cpContent').focus();
  }
});

document.getElementById('cpCancel').addEventListener('click', closeCompose);

document.getElementById('cpPublish').addEventListener('click', async () => {
  const content = document.getElementById('cpContent').value.trim();
  if (!content) return;
  const title = document.getElementById('cpTitle').value.trim();
  if (!title) { alert('Title is required'); return; }
  const body = {
    title,
    content,
    tags:   document.getElementById('cpTags').value.split(',').map(s => s.trim()).filter(Boolean),
    source: document.getElementById('cpSource').value.trim() || null,
    expires_at: toUtcIso(document.getElementById('cpExpires').value) || null,
  };
  const btn = document.getElementById('cpPublish');
  btn.disabled = true; btn.textContent = 'Publishing…';
  try {
    await apiFetch('/posts', { method: 'POST', body: JSON.stringify(body) });
    closeCompose();
    refreshSidebarCounts();
    await loadLinkIndex();
  } catch (e) { alert(`Publish failed: ${e.message}`); }
  finally { btn.disabled = false; btn.textContent = 'Publish'; }
});

function closeCompose() {
  composePanel.classList.remove('open');
  ['cpTitle', 'cpContent', 'cpTags', 'cpSource'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('cpExpires').value = '';
  const st = document.getElementById('cpAttachStatus');
  if (st) { st.textContent = ''; st.classList.remove('error'); }
}

/* ── Attachment upload ─────────────────────────────────────── */

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
function wireAttachments(ta, fileInput, attachBtn, statusEl, getExtra) {
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

const parseTagsField = (v) => v.split(',').map(s => s.trim()).filter(Boolean);

// Confirm + DELETE an attachment; alerts if posts still reference it. Returns
// true when the file was removed (callers refresh their own view).
async function confirmDeleteAttachment(name) {
  if (!confirm(`Delete "${name}" from the vault? This removes the file itself.`)) return false;
  try {
    const r = await apiFetch(`/attachments/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (r.referenced_by?.length)
      alert(`Deleted. Still referenced by ${r.referenced_by.map(i => '#' + i).join(', ')} — those embeds are now broken.`);
    return true;
  } catch (e) { alert(`Delete failed: ${e.message}`); return false; }
}

// Edit-form list of the post-folder's attachments, each with a delete (×) button.
async function renderEditAttachments(el, postId) {
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

wireAttachments(
  document.getElementById('cpContent'), document.getElementById('cpFile'),
  document.getElementById('cpAttach'), document.getElementById('cpAttachStatus'),
  () => ({ tags: parseTagsField(document.getElementById('cpTags').value) }),
);

/* ── Search ───────────────────────────────────────────────── */
searchInput.addEventListener('input', () => {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(async () => {
    const q = searchInput.value.trim();
    query.search = q || null;
    searchBar.classList.toggle('active', !!query.search);
    resetPaging();
    await loadPosts(true);
  }, 300);
});

searchInput.addEventListener('keydown', e => {
  if (e.key === 'Escape') { searchClear.click(); }
});

searchClear.addEventListener('click', async () => {
  searchInput.value = '';
  query.search = null;
  searchBar.classList.remove('active');
  resetPaging();
  await loadPosts(true);
  searchInput.focus();
});

/* ── New tag ──────────────────────────────────────────────── */
newTagBtn.addEventListener('click', () => {
  const visible = tagNewWrap.style.display !== 'none';
  tagNewWrap.style.display = visible ? 'none' : '';
  if (!visible) { tagNewInput.value = ''; tagNewInput.focus(); }
});

tagNewInput.addEventListener('keydown', async e => {
  if (e.key === 'Escape') { tagNewWrap.style.display = 'none'; return; }
  if (e.key !== 'Enter') return;
  const name = tagNewInput.value.trim().toLowerCase();
  if (!name) return;
  try {
    await apiFetch(`/tags/${encodeURIComponent(name)}/config`, {
      method: 'POST', body: JSON.stringify({}),
    });
    tagNewWrap.style.display = 'none';
    await loadTags();
  } catch (e) { alert(`Failed: ${e.message}`); }
});

/* ── Tags ─────────────────────────────────────────────────── */
async function loadTags() {
  try { const data = await apiFetch('/tags'); renderTags(data.tags); } catch {}
}

// Refresh whichever count view is active right now (Tags or Tree). Callers after
// a local create/edit/delete must use this rather than loadTags() directly, or a
// Tree-mode sidebar briefly flips to the Tags list before the SSE echo swaps it
// back. Files mode has no post-driven counts to refresh.
function refreshSidebarCounts() {
  if (sidebarMode === 'tags') loadTags();
  else if (sidebarMode === 'tree') loadFolders();
}

// Debounced sidebar-count refresh — a burst of SSE events (e.g. reconnect replay
// or an edit that retags/moves a post) must not trigger a storm of fetches +
// rebuilds. Refreshes only the *active* count view (Tags and Tree share the same
// sidebar element, so refreshing the inactive one would clobber what's shown):
// tag counts, or the Tree's folder counts (which go stale on an Inbox→domain move
// now streamed via a post edit).
let _tagsTimer = null;
function scheduleLoadTags() {
  clearTimeout(_tagsTimer);
  _tagsTimer = setTimeout(refreshSidebarCounts, 250);
}

function renderTags(tags) {
  openTagEditor = null;   // the DOM these forms lived in is about to be replaced
  tagList.innerHTML = '';
  tagList.appendChild(makeTagItem('all', null, tags.reduce((s, t) => s + t.count, 0)));
  tags.forEach(t => tagList.appendChild(makeTagItem(t.tag, t.tag, t.count)));
}

/* Inline SVG rather than ✏︎ / ⚙ glyphs.
 *
 * The pencil was U+270F with a text-presentation selector, which renders as a
 * thin *horizontal* stroke at this size — indistinguishable from a minus, and so
 * read as "remove tag" rather than "rename". A drawn, diagonal pencil cannot be
 * mistaken for one. The gear follows for consistency, and both now scale with the
 * icon size rather than the font's idea of a dingbat.
 */
/* A folder, drawn rather than typed. U+1F4C1 shipped here first and was the
 * wrong answer for the same reason the ✏︎ glyph was in the tag row: it is a
 * *colour* emoji, so it ignores `color` and paints the same manila tab in all
 * fifteen themes — conspicuously the one thing on screen that does not answer to
 * the palette. `currentColor` puts it back under `--accent`, and drawing it also
 * settles its weight against the 13px monospace labels beside it, which an
 * emoji's own metrics do not. */
const ICON_FOLDER = `<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"
  stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
  <path d="M1.9 12.6V3.4h4l1.5 1.9h6.7v7.3z"/></svg>`;

const ICON_PENCIL = `<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"
  stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
  <path d="M11.4 2.4l2.2 2.2L6 12.2l-2.9.7.7-2.9z"/><path d="M10 3.8l2.2 2.2"/></svg>`;

/* A clock, not a gear. The button sets TTL/expiry, so a clock says what it does —
 * and a gear at 13px renders as radiating spokes around a dot, which reads as a
 * brightness control rather than settings. */
const ICON_CLOCK = `<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"
  stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
  <circle cx="8" cy="8" r="5.8"/><path d="M8 4.6V8l2.3 1.7"/></svg>`;

function makeTagItem(label, value, count) {
  const el = document.createElement('div');
  el.className = 'tag-item' + (query.tag === value ? ' active' : '');
  const renameBtn = value !== null
    ? `<button class="tag-rename" title="Rename tag" aria-label="Rename tag">${ICON_PENCIL}</button>` : '';
  const configBtn = value !== null
    ? `<button class="tag-config-btn" title="Expiry settings" aria-label="Expiry settings">${ICON_CLOCK}</button>` : '';
  el.innerHTML = `<span class="tag-name">${escHtml(label)}</span>${renameBtn}${configBtn}<span class="tag-count">${count}</span>`;
  el.addEventListener('click', () => selectTag(value));
  if (value !== null) {
    el.querySelector('.tag-rename').addEventListener('click', e => {
      e.stopPropagation();
      startTagRename(el, value);
    });
    el.querySelector('.tag-config-btn').addEventListener('click', e => {
      e.stopPropagation();
      startTagConfig(el, value);
    });
  }
  return el;
}

/* ── Sidebar: Tags ⇄ Tree toggle ───────────────────────────── */
// One tab replaces the feed with a different listing; the other two filter it.
// Naming that split is what keeps a new tab from adding another set of
// near-identical toggles — and the strip has no room for one anyway
// (`tests/ui/test_sidebar_tabs.py`), which is why recovering a deleted post
// lives in the status panel rather than here.
const SIDEBAR_TABS = { tags: 'tabTags', tree: 'tabTree', files: 'tabFiles' };
const FEED_REPLACING = { files: 'attachmentsView' };

function setSidebarMode(mode) {
  sidebarMode = mode;
  for (const [name, id] of Object.entries(SIDEBAR_TABS)) {
    document.getElementById(id).classList.toggle('active', mode === name);
  }
  newTagBtn.style.display = mode === 'tags' ? '' : 'none';
  document.getElementById('tagNew').style.display = 'none';

  const replacing = mode in FEED_REPLACING;
  feed.style.display = replacing ? 'none' : '';
  for (const [name, id] of Object.entries(FEED_REPLACING)) {
    document.getElementById(id).style.display = mode === name ? '' : 'none';
  }
  newPostBtn.style.display = replacing ? 'none' : '';
  if (replacing) loadMoreWrap.style.display = 'none';
  else if (query.offset < query.total) loadMoreWrap.style.display = 'block';

  if (mode === 'tags') loadTags();
  else if (mode === 'tree') loadFolders();
  else loadAttachments();
}
for (const [name, id] of Object.entries(SIDEBAR_TABS)) {
  document.getElementById(id).addEventListener('click', () => setSidebarMode(name));
}
// A restore puts a post back in the feed, so the feed has to hear about it.
// The recovery browser itself lives in the status panel (`js/status.js`).
initDeleted(() => { resetPaging(); loadPosts(true); loadTags(); });

async function loadFolders() {
  if (!authed) return;
  try { renderFolders((await apiFetch('/folders')).folders); } catch {}
}

function renderFolders(folders) {
  tagList.innerHTML = '';
  // Local: the folder-count sum for the "all" row, unrelated to query.total
  // (which is the feed's result count). It shadowed the old global `total`.
  const allCount = folders.reduce((s, f) => s + f.count, 0);
  tagList.appendChild(makeFolderItem('all', null, allCount));
  folders.forEach(f => tagList.appendChild(makeFolderItem(f.folder, f.folder, f.count)));
}

function makeFolderItem(label, value, count) {
  const el = document.createElement('div');
  el.className = 'tag-item folder-item' + (query.folder === value ? ' active' : '');
  el.dataset.folder = value === null ? '__all__' : value;
  // Empty on the "all" row rather than absent: `.folder-ico` reserves a fixed
  // gutter, so every label starts on the same left edge whether or not its row
  // has an icon. With the icon simply omitted, "all" sat left of the folders.
  const ico = `<span class="folder-ico">${value === null ? '' : ICON_FOLDER}</span>`;
  el.innerHTML = `<span class="tag-name">${ico}${escHtml(label)}</span><span class="tag-count">${count}</span>`;
  el.addEventListener('click', () => selectFolder(value));
  return el;
}

async function selectFolder(folder) {
  closeSidebar();
  query.folder = folder; query.tag = null; resetPaging();
  feed.innerHTML = '';
  loadMoreWrap.style.display = 'none';
  const key = folder === null ? '__all__' : folder;
  tagList.querySelectorAll('.folder-item').forEach(el =>
    el.classList.toggle('active', el.dataset.folder === key));
  await loadPosts(true);
}

/* ── Attachment gallery (Files tab) ─────────────────────────── */
const attPath = (a) => '/attachments/' + [a.folder, 'assets', a.filename].map(encodeURIComponent).join('/');

async function loadAttachments() {
  if (!authed) return;
  try { attachCache = (await apiFetch('/attachments')).items; } catch { attachCache = []; }
  renderAttachSidebar();
  renderAttachGallery();
}

function selectAttachFolder(folder) {
  attachFolder = folder;
  renderAttachSidebar();
  renderAttachGallery();
  closeSidebar();
}

function renderAttachSidebar() {
  const counts = new Map();
  attachCache.forEach(a => counts.set(a.folder, (counts.get(a.folder) || 0) + 1));
  tagList.innerHTML = '';
  const all = document.createElement('div');
  all.className = 'tag-item folder-item' + (attachFolder === null ? ' active' : '');
  all.innerHTML = `<span class="tag-name"><span class="folder-ico"></span>All files</span><span class="tag-count">${attachCache.length}</span>`;
  all.addEventListener('click', () => selectAttachFolder(null));
  tagList.appendChild(all);
  [...counts.keys()].sort().forEach(folder => {
    const el = document.createElement('div');
    el.className = 'tag-item folder-item' + (attachFolder === folder ? ' active' : '');
    el.innerHTML = `<span class="tag-name"><span class="folder-ico">${ICON_FOLDER}</span>${escHtml(folder)}</span><span class="tag-count">${counts.get(folder)}</span>`;
    el.addEventListener('click', () => selectAttachFolder(folder));
    tagList.appendChild(el);
  });
}

function renderAttachGallery() {
  const items = attachFolder === null ? attachCache : attachCache.filter(a => a.folder === attachFolder);
  if (!items.length) { attachmentsView.innerHTML = '<div class="att-empty">No attachments yet.</div>'; return; }
  const groups = new Map();
  items.forEach(a => { if (!groups.has(a.folder)) groups.set(a.folder, []); groups.get(a.folder).push(a); });
  let html = '';
  [...groups.keys()].sort().forEach(folder => {
    html += `<div class="att-folder-head">${escHtml(folder)}/assets</div><div class="att-grid">`;
    groups.get(folder).forEach(a => {
      const p = attPath(a);
      const thumb = IMAGE_EXT_RE.test(a.filename)
        ? `<div class="att-thumb" data-src="${p}" data-name="${escHtml(a.filename)}"><img src="${p}" alt="${escHtml(a.filename)}" loading="lazy"></div>`
        : `<a class="att-thumb" href="${p}" target="_blank" rel="noopener noreferrer"><span class="att-ext">${escHtml(a.filename.split('.').pop() || 'file')}</span></a>`;
      html += `<div class="att-card" data-name="${escHtml(a.filename)}">${thumb}
        <div class="att-meta"><span class="att-name">${escHtml(a.filename)}</span>
          <span class="att-sub"><span>${fmtBytes(a.bytes)}</span></span></div>
        <button class="att-del" title="Delete file from vault">×</button></div>`;
    });
    html += '</div>';
  });
  attachmentsView.innerHTML = html;
}

attachmentsView.addEventListener('click', async e => {
  const thumb = e.target.closest('.att-thumb[data-src]');
  if (thumb) { openLightbox(thumb.dataset.src, thumb.dataset.name); return; }
  const del = e.target.closest('.att-del');
  if (!del) return;
  const name = del.closest('.att-card').dataset.name;
  if (await confirmDeleteAttachment(name)) await loadAttachments();
});

function openLightbox(src, name) {
  document.getElementById('lightboxImg').src = src;
  document.getElementById('lightboxCap').textContent = name || '';
  lightbox.style.display = 'flex';
}
function closeLightbox() { lightbox.style.display = 'none'; document.getElementById('lightboxImg').src = ''; }
lightbox.addEventListener('click', closeLightbox);
document.addEventListener('keydown', e => { if (e.key === 'Escape' && lightbox.style.display === 'flex') closeLightbox(); });

/* Only one tag editor may be open at a time.
 *
 * Both the rename and the TTL form replace a row's contents in place, and nothing
 * previously closed the last one — so a second click left two forms stacked over
 * the tag list, and the tag they belonged to was no longer readable. This tracks
 * the open one so opening another (or clicking away, or pressing Escape) closes
 * it first. Re-clicking the same control toggles it shut.
 */
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

// Clicking anywhere outside the open editor dismisses it. Without this the only
// way out of a form was Escape while it still had focus — click elsewhere first
// and the row was stuck open until a page reload.
//
// Tag controls are exempt: closing on mousedown collapses the open row, which
// shifts every row below it *between* mousedown and mouseup, so the click landed
// somewhere other than the gear that was pressed and appeared to do nothing.
// Those buttons close the previous editor themselves, after the click resolves.
document.addEventListener('mousedown', e => {
  if (!openTagEditor) return;
  if (openTagEditor.el.contains(e.target)) return;
  if (e.target.closest?.('.tag-config-btn, .tag-rename')) return;
  closeTagEditor();
});

function startTagRename(el, oldName) {
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
    const newName = input.value.trim().toLowerCase();
    if (!newName || newName === oldName) { cancelRename(); return; }
    try {
      const data = await apiFetch(`/tags/${encodeURIComponent(oldName)}`, {
        method: 'PATCH', body: JSON.stringify({ new_name: newName }),
      });
      if (query.tag === oldName) query.tag = newName;
      renderTags(data.tags);
    } catch (e) { alert(`Rename failed: ${e.message}`); cancelRename(); }
  }

  function cancelRename() {
    if (committed) return;
    committed = true;
    if (openTagEditor && openTagEditor.el === el) openTagEditor = null;
    const span = document.createElement('span');
    span.className = 'tag-name'; span.textContent = oldName;
    input.replaceWith(span);
  }

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') cancelRename();
  });
  input.addEventListener('blur', commit);
}

function startTagConfig(el, tagName) {
  if (toggledTagEditor(el, 'config')) return;
  const savedHtml = el.innerHTML;
  const form = document.createElement('div');
  form.className = 'tag-config-form';
  // Explicit Save/Cancel, not just Enter/Escape: the keyboard-only version was
  // undiscoverable, and unreachable once focus had left the inputs.
  form.innerHTML = `
    <div class="tc-label"></div>
    <input type="number" class="tc-ttl" placeholder="TTL hours (optional)" min="1">
    <input type="datetime-local" class="tc-expires">
    <div class="tc-actions">
      <button type="button" class="tc-save">Save</button>
      <button type="button" class="tc-cancel">Cancel</button>
    </div>`;
  form.querySelector('.tc-label').textContent = `expiry for #${tagName}`;
  el.innerHTML = '';
  el.classList.add('tag-editing');
  el.appendChild(form);

  const ttlInput = form.querySelector('.tc-ttl');
  const expiresInput = form.querySelector('.tc-expires');
  ttlInput.focus();
  openTagEditor = { el, kind: 'config', cancel: () => cancel() };

  let committed = false;
  async function commit() {
    if (committed) return; committed = true;
    const ttlVal = ttlInput.value.trim();
    const expiresVal = expiresInput.value;
    if (!ttlVal && !expiresVal) { cancel(); return; }
    const body = {};
    if (ttlVal) body.ttl_hours = parseInt(ttlVal, 10);
    if (expiresVal) body.expires_at = toUtcIso(expiresVal);
    try {
      await apiFetch(`/tags/${encodeURIComponent(tagName)}/config`, {
        method: 'POST', body: JSON.stringify(body),
      });
      await loadTags();
    } catch (e) { alert(`Config failed: ${e.message}`); cancel(); }
  }

  function cancel() {
    if (committed) return;
    committed = true;
    if (openTagEditor && openTagEditor.el === el) openTagEditor = null;
    el.classList.remove('tag-editing');
    el.innerHTML = savedHtml;
    el.querySelector('.tag-rename')?.addEventListener('click', ev => {
      ev.stopPropagation(); startTagRename(el, tagName);
    });
    el.querySelector('.tag-config-btn')?.addEventListener('click', ev => {
      ev.stopPropagation(); startTagConfig(el, tagName);
    });
  }

  form.querySelector('.tc-save').addEventListener('click', e => { e.stopPropagation(); commit(); });
  form.querySelector('.tc-cancel').addEventListener('click', e => { e.stopPropagation(); cancel(); });
  // The row itself filters the feed on click; a click inside the form must not.
  form.addEventListener('click', e => e.stopPropagation());
  form.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { e.stopPropagation(); cancel(); }
  });
}

async function selectTag(tag) {
  closeSidebar();
  query.tag = tag; query.folder = null; resetPaging();
  feed.innerHTML = '';
  loadMoreWrap.style.display = 'none';
  tagList.querySelectorAll('.tag-item').forEach(el => {
    el.classList.remove('active');
    const name = el.querySelector('.tag-name').textContent;
    if ((tag === null && name === 'all') || name === tag) el.classList.add('active');
  });
  await loadPosts(true);
  connectSSE();
}

/* ── Posts ────────────────────────────────────────────────── */
async function loadPosts(replace = false) {
  if (!authed) return;
  try {
    const params = new URLSearchParams({ limit: LIMIT, offset: query.offset });
    params.set('sort', prefs.sortField);
    params.set('order', prefs.sortOrder);
    if (query.tag) params.set('tag', query.tag);
    if (query.folder) params.set('folder', query.folder);
    if (query.search) params.set('search', query.search);
    const data = await apiFetch(`/posts?${params}`);
    query.total = data.total;
    query.offset += data.items.length;

    if (replace) { feed.innerHTML = ''; clearNewPostsPill(); }

    if (replace && data.pinned) {
      const pin = renderPost(data.pinned);
      pin.classList.add('pinned');
      feed.appendChild(pin);
    }

    if (data.items.length === 0 && query.offset === 0 && !data.pinned) {
      feed.innerHTML = `
        <div class="empty">
          <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
          </svg>
          <p>No posts yet</p>
        </div>`;
    } else {
      data.items.forEach((p, i) => {
        const el = renderPost(p);
        if (replace) el.style.animationDelay = `${i * 35}ms`;
        feed.appendChild(el);
      });
    }
    loadMoreWrap.style.display = query.offset < query.total ? 'block' : 'none';
  } catch (e) {
    if (replace) feed.innerHTML = `<div class="auth-prompt"><p>Could not load posts.</p><p>${e.message}</p></div>`;
  }
}

loadMoreBtn.addEventListener('click', () => loadPosts(false));

/* Infinite scroll: auto-load the next page as the feed bottom nears view.
   The Load more button stays as a fallback (short feeds that never scroll). */
feed.addEventListener('scroll', () => {
  if (loadingMore || query.offset >= query.total) return;
  if (feed.scrollTop + feed.clientHeight >= feed.scrollHeight - 300) {
    loadingMore = true;
    loadPosts(false).finally(() => { loadingMore = false; });
  }
});

// Master-doc accordion: collapsed shows a single line (~1× the 1.55/13px body
// line-height); expanded animates max-height to the content's scrollHeight, then
// releases to `none` so late reflow (images, wraps) isn't clipped.
const MASTER_PEEK_PX = 21;
function toggleMasterAccordion(el, wrap) {
  const collapsing = !el.classList.contains('collapsed');
  if (collapsing) {
    wrap.style.maxHeight = wrap.scrollHeight + 'px';   // pin current height first
    requestAnimationFrame(() => {
      el.classList.add('collapsed');
      wrap.style.maxHeight = MASTER_PEEK_PX + 'px';
    });
  } else {
    el.classList.remove('collapsed');
    wrap.style.maxHeight = wrap.scrollHeight + 'px';
    const onEnd = (ev) => {
      if (ev.propertyName !== 'max-height') return;
      wrap.style.maxHeight = 'none';
      wrap.removeEventListener('transitionend', onEnd);
    };
    wrap.addEventListener('transitionend', onEnd);
  }
}

function renderPost(post) {
  const el = document.createElement('div');
  el.className = 'post' + (post.id === 0 ? ' master-doc' : '');
  el.dataset.id = post.id;

  // Two spans, not one string: grid tiles hide the created stamp via CSS when an
  // edit stamp is present, and the separator is drawn by the list-mode rule so
  // the surviving chunk never starts with a stray "·".
  const timeLabel  = post.updated_at
    ? `<span class="t-created">${relativeTime(post.created_at)}</span>` +
      `<span class="t-edited">edited ${relativeTime(post.updated_at)}</span>`
    : `<span class="t-created">${relativeTime(post.created_at)}</span>`;
  const timeTitle  = post.updated_at
    ? `created ${post.created_at}\nedited ${post.updated_at}`
    : post.created_at;
  const expiresHtml = post.expires_at
    ? `<span class="post-expires">expires ${relativeTime(post.expires_at)}</span>`
    : '';
  const masterBadge = post.id === 0
    ? `<div class="master-badge">✦ master document<span class="accordion-chevron">▾</span></div>`
    : '';
  const titleHtml  = post.title ? `<span class="post-title">${escHtml(post.title)}</span>` : '';
  const tagsHtml   = post.tags.map(t => `<span class="tag-pill" data-tag="${escHtml(t)}">${escHtml(t)}</span>`).join('');
  const srcHtml    = post.source ? `<span class="post-source">via ${escHtml(post.source)}</span>` : '';
  const tagsRow    = (tagsHtml || srcHtml) ? `<div class="post-tags">${tagsHtml}${srcHtml}</div>` : '';
  const headerHtml = (titleHtml || tagsRow) ? `<div class="post-header">${titleHtml}${tagsRow}</div>` : '';

  // Pull images out first — to a card thumbnail (placed by CSS: right in list,
  // on top in grid) and out of the preview so text shows instead of an image
  // slice. Doing it before the strippers below also stops a leading ![[image]]
  // from hiding the title heading / "Last updated:" line, which only match at
  // the very start of the content.
  const media = extractMedia(post.content);
  let contentToRender = media.stripped;
  let extractedUpdated = null;
  if (post.title) {
    contentToRender = contentToRender.replace(/^\s*#{1,6}\s+[^\n]*\n*/, '');
    // Tolerate the line being wrapped in * / _ emphasis (*Last updated: …*).
    const lu = /^\s*[*_]*\s*Last updated:\s*([^\n]+?)\s*[*_]*\s*(?:\n|$)/i;
    const m = contentToRender.match(lu);
    if (m) {
      extractedUpdated = m[1].trim();
      contentToRender = contentToRender.replace(lu, '');
    }
  }

  const bodyHtml = `<div class="post-body">${renderBody(contentToRender)}</div>`;
  const mediaHtml = media.thumb
    ? `<div class="post-media"><img src="${escHtml(media.thumb)}" alt="" loading="lazy"></div>`
    : '';
  const mediaChip = media.count
    ? `<span class="post-media-count">🖼 ${media.count}</span>`
    : '';

  el.innerHTML = `
    ${masterBadge}
    ${headerHtml}
    <div class="post-body-wrap">${bodyHtml}</div>
    ${mediaHtml}
    <div class="post-footer">
      <div class="post-footer-left">
        <span class="post-id-pill">#${post.id}</span>
        <span class="post-time" title="${timeTitle}">${timeLabel}</span>
        ${extractedUpdated ? `<span class="post-time t-doc-updated">updated ${escHtml(extractedUpdated)}</span>` : ''}
        ${mediaChip}
        ${expiresHtml}
      </div>
      <div class="post-actions">
        <button class="btn-edit" title="Edit">✏️ <span class="btn-label">Edit</span></button>
        <button class="btn-delete" title="Delete">🗑️ <span class="btn-label">Delete</span></button>
      </div>
    </div>`;

  el.querySelectorAll('.tag-pill').forEach(pill =>
    pill.addEventListener('click', e => { e.stopPropagation(); selectTag(pill.dataset.tag); })
  );
  // A broken/unauthorised thumbnail just drops the media block (text stays).
  const mediaImg = el.querySelector('.post-media img');
  if (mediaImg) mediaImg.addEventListener('error', () => el.querySelector('.post-media')?.remove());
  el.querySelector('.btn-delete').addEventListener('click', async (e) => {
    e.stopPropagation();
    if (!confirm('Delete this post?')) return;
    try {
      await apiSend(`/posts/${post.id}`, { method: 'DELETE' });
      el.remove(); query.total--;
      loadMoreWrap.style.display = query.offset < query.total ? 'block' : 'none';
      refreshSidebarCounts();
    } catch {}
  });
  el.querySelector('.btn-edit').addEventListener('click', (e) => { e.stopPropagation(); enterEditMode(el, post); });
  if (post.id === 0) {
    // Master doc → inline accordion (collapsed by default → a few-line peek).
    // Clicking the card toggles it; body links/buttons still work via the guard.
    el.classList.add('accordion', 'collapsed');
    const wrap = el.querySelector('.post-body-wrap');
    wrap.style.maxHeight = MASTER_PEEK_PX + 'px';   // collapsed on first paint
    el.addEventListener('click', (e) => {
      if (el.classList.contains('editing')) return;
      if (e.target.closest('a, button, .tag-pill, .post-actions')) return;
      toggleMasterAccordion(el, wrap);
    });
  } else {
    el.addEventListener('click', () => { if (!el.classList.contains('editing')) openPostModal(post); });
  }
  return el;
}

/* Editing happens in its own modal, not inside the card.
 *
 * The form used to replace the post card's contents, which in grid view meant a
 * ~200px column: the textarea was a few words wide and a long note was unusable.
 * The markup is unchanged — it just gets the room the reading modal already had,
 * with the content field taking whatever height is left.
 */
const editModal = document.getElementById('editModal');
const emBody = document.getElementById('emBody');
const emTitle = document.getElementById('emTitle');
let editingPost = null;

function isEditOpen() {
  return editModal.classList.contains('open');
}

function closeEditModal() {
  editModal.classList.remove('open');
  if (!postModal.classList.contains('open')) document.body.style.overflow = '';
  emBody.innerHTML = '';
  editingPost = null;
}

/** True unless there are unsaved changes the user declines to throw away.
 *  Split out of tryCloseEditModal so the swipe gesture can ask *before* it
 *  animates the sheet away — a dismissal that gets vetoed has to spring back. */
function confirmDiscardEdit() {
  const field = emBody.querySelector('.ef-content');
  const dirty = editingPost && field && field.value !== editingPost.content;
  return !dirty || confirm('Discard your changes to this post?');
}

/** Close, asking first if the body was touched — the modal is easy to dismiss. */
function tryCloseEditModal() {
  if (!confirmDiscardEdit()) return;
  closeEditModal();
}

function enterEditMode(_el, post) {
  editingPost = post;
  editModal.classList.add('open');
  document.body.style.overflow = 'hidden';
  emTitle.textContent = `#${post.id}`;
  emBody.innerHTML = `
    <div class="edit-form">
      <div><label for="efTitle">Title</label><input id="efTitle" class="ef-title" type="text" value="${escHtml(post.title || '')}"></div>
      <div class="ef-content-wrap"><label for="efContent">Content</label><textarea id="efContent" class="ef-content">${escHtml(post.content)}</textarea>
        <div class="attach-row">
          <input type="file" class="ef-file" multiple style="display:none">
          <button type="button" class="btn-attach ef-attach">📎 Attach</button>
          <span class="attach-status ef-attach-status"></span>
        </div>
        <div class="ef-attachments"></div>
      </div>
      <div><label for="efTags">Tags</label><input id="efTags" class="ef-tags" type="text" value="${escHtml(post.tags.join(', '))}"></div>
      <div><label for="efSource">Source</label><input id="efSource" class="ef-source" type="text" value="${escHtml(post.source || '')}"></div>
      <div><label for="efExpires">Expires</label><input id="efExpires" class="ef-expires" type="datetime-local" value="${toDatetimeLocal(post.expires_at || '')}"></div>
      <div class="edit-actions">
        <button class="btn-cancel">Cancel</button>
        <button class="btn-save">Save</button>
      </div>
    </div>`;

  wireAttachments(
    emBody.querySelector('.ef-content'), emBody.querySelector('.ef-file'),
    emBody.querySelector('.ef-attach'), emBody.querySelector('.ef-attach-status'),
    () => ({ post_id: post.id, embed: false }),
  );
  renderEditAttachments(emBody, post.id);
  emBody.querySelector('.ef-title').focus();

  emBody.querySelector('.btn-cancel').addEventListener('click', tryCloseEditModal);
  emBody.querySelector('.btn-save').addEventListener('click', async () => {
    const newTitle = emBody.querySelector('.ef-title').value.trim();
    if (!newTitle) { alert('Title is required'); return; }
    const body = {
      title:      newTitle,
      content:    emBody.querySelector('.ef-content').value,
      tags:       emBody.querySelector('.ef-tags').value.split(',').map(s => s.trim()).filter(Boolean),
      source:     emBody.querySelector('.ef-source').value.trim() || null,
      expires_at: toUtcIso(emBody.querySelector('.ef-expires').value) || null,
    };
    const btn = emBody.querySelector('.btn-save');
    btn.disabled = true; btn.textContent = 'Saving…';
    try {
      const updated = await apiFetch(`/posts/${post.id}`, { method: 'PATCH', body: JSON.stringify(body) });
      // The card is looked up rather than held: the feed may have re-rendered
      // (a filter, a sort, an SSE push) while the modal was open.
      const card = feed.querySelector(`[data-id="${post.id}"]`);
      if (card) card.replaceWith(renderPost(updated));
      closeEditModal();
      if (postModal.classList.contains('open')) openPostModal(updated, { pushHistory: false });
      refreshSidebarCounts();
    } catch (e) {
      alert(`Save failed: ${e.message}`);
      btn.disabled = false; btn.textContent = 'Save';
    }
  });
}

document.getElementById('emClose').onclick = tryCloseEditModal;
document.getElementById('emBackdrop').onclick = tryCloseEditModal;
attachSheetDismiss({
  inner: editModal.querySelector('.sm-inner'),
  handle: editModal.querySelector('.sm-head'),
  backdrop: document.getElementById('emBackdrop'),
  canDismiss: confirmDiscardEdit,
  onDismiss: closeEditModal,
});

function rewirePost(el, post) {
  el.querySelectorAll('.tag-pill').forEach(pill =>
    pill.addEventListener('click', e => { e.stopPropagation(); selectTag(pill.dataset.tag); })
  );
  // A broken/unauthorised thumbnail just drops the media block (text stays).
  const mediaImg = el.querySelector('.post-media img');
  if (mediaImg) mediaImg.addEventListener('error', () => el.querySelector('.post-media')?.remove());
  el.querySelector('.btn-delete').addEventListener('click', async (e) => {
    e.stopPropagation();
    if (!confirm('Delete this post?')) return;
    try {
      await apiSend(`/posts/${post.id}`, { method: 'DELETE' });
      el.remove(); query.total--;
      loadMoreWrap.style.display = query.offset < query.total ? 'block' : 'none';
      refreshSidebarCounts();
    } catch {}
  });
  el.querySelector('.btn-edit').addEventListener('click', (e) => { e.stopPropagation(); enterEditMode(el, post); });
}

/* ── SSE ──────────────────────────────────────────────────── */
function connectSSE() {
  if (es) { es.close(); es = null; }
  if (!authed) return;
  const params = new URLSearchParams();
  if (query.tag) params.set('tag', query.tag);
  const qs = params.toString();
  es = new EventSource(`/events${qs ? '?' + qs : ''}`);

  es.addEventListener('post', e => {
    const post = JSON.parse(e.data);
    // A 'post' event is either a brand-new post or an edit streamed in from
    // outside relay (e.g. an Obsidian save picked up by the vault watcher).
    // If this post is *currently* open in the detail modal, refresh it in place.
    // Guard on the modal actually being open (not just _modalPost) so a late or
    // duplicate edit event that lands right after the user hits × can never
    // resurrect a modal they just closed.
    if (_modalPost && _modalPost.id === post.id && postModal.classList.contains('open'))
      openPostModal(post, { pushHistory: false });
    const existing = feed.querySelector(`[data-id="${post.id}"]`);
    // Don't clobber an inline edit the user has open on this card.
    if (existing && existing.classList.contains('editing')) { scheduleLoadTags(); return; }
    const el = renderPost(post);
    el.classList.add('new');
    if (existing) {
      if (existing.classList.contains('pinned')) el.classList.add('pinned');
      existing.replaceWith(el);   // edit: update in place (don't bump query.total)
    } else if (isDefaultSort()) {
      const empty = feed.querySelector('.empty');
      if (empty) empty.remove();
      const pinnedEl = feed.querySelector('.post.pinned');
      if (pinnedEl) pinnedEl.after(el); else feed.prepend(el);  // keep master on top
      query.total++;
      announce(`New post: ${post.title}`);
    } else {
      // Non-default sort: the new post doesn't belong at the top — count it and
      // let the user pull it in via the pill rather than misplacing the card.
      query.total++;
      bumpNewPostsPill();
      announce(`New post: ${post.title}`);
    }
    scheduleLoadTags();
  });

  es.addEventListener('delete', e => {
    const { id } = JSON.parse(e.data);
    const card = feed.querySelector(`[data-id="${id}"]`);
    if (!card) return;            // idempotent: already gone (e.g. we deleted it)
    card.remove();
    query.total = Math.max(0, query.total - 1);
    if (_modalPost && _modalPost.id === id) closePostModal();
    scheduleLoadTags();
  });

  es.onopen = () => {
    if (sseErrorTimer) { clearTimeout(sseErrorTimer); sseErrorTimer = null; }
    setDot('connected');
  };
  es.onerror = () => {
    if (sseErrorTimer) clearTimeout(sseErrorTimer);
    sseErrorTimer = setTimeout(() => setDot('error'), 3000);
  };
}

function announce(msg) {
  if (!a11yAnnouncer) return;
  a11yAnnouncer.textContent = '';
  requestAnimationFrame(() => { a11yAnnouncer.textContent = msg; });
}

function setDot(state) {
  liveDot.className = 'live-dot' + (state ? ' ' + state : '');
  liveLabel.textContent = state === 'connected' ? 'live' : state === 'error' ? 'error' : 'offline';
}

/* ── Post modal ───────────────────────────────────────────── */
const postModal  = document.getElementById('postModal');
const pmBackdrop = document.getElementById('pmBackdrop');
const pmClose    = document.getElementById('pmClose');
const pmBack     = document.getElementById('pmBack');
const pmTitle    = document.getElementById('pmTitle');
const pmMeta     = document.getElementById('pmMeta');
const pmBody     = document.getElementById('pmBody');
const pmBodyFade = document.getElementById('pmBodyFade');
const pmEdit     = document.getElementById('pmEdit');
const pmDelete   = document.getElementById('pmDelete');
const pmInner    = document.querySelector('.pm-inner');
const pmHeader   = document.querySelector('.pm-header');
let _modalPost        = null;
let _modalStack       = [];   // entries: { post, scrollTop }
let _historyDepth     = 0;
let _suppressPopstate = false;

function syncBackButton() {
  if (_modalStack.length === 0) {
    pmBack.style.display = '';
    pmBack.textContent = '← back';
    pmInner.classList.remove('has-back');
    return;
  }
  const { post } = _modalStack[_modalStack.length - 1];
  pmBack.textContent = `← ${post.title || `#${post.id}`}`;
  pmBack.style.display = 'inline-flex';
  pmInner.classList.add('has-back');
}

function openPostModal(post, { pushHistory = true } = {}) {
  if (pushHistory && _modalPost) {
    _modalStack.push({ post: _modalPost, scrollTop: pmBody.scrollTop });
    history.replaceState({ postId: _modalPost.id }, '');
    history.pushState({ postId: post.id }, '');
    _historyDepth++;
  }
  _modalPost = post;
  pmTitle.textContent = post.title || '';
  pmTitle.style.display = post.title ? '' : 'none';

  const tagsHtml  = post.tags.map(t => `<span class="tag-pill" data-tag="${escHtml(t)}">${escHtml(t)}</span>`).join('');
  const srcHtml   = post.source ? `<span class="post-source">via ${escHtml(post.source)}</span>` : '';
  const timeLabel = post.updated_at
    ? `${relativeTime(post.created_at)} · edited ${relativeTime(post.updated_at)}`
    : relativeTime(post.created_at);
  const pmExpiresHtml = post.expires_at
    ? `<div class="pm-time">expires ${relativeTime(post.expires_at)}</div>`
    : '';
  const pmMasterBadge = post.id === 0 ? `<div class="master-badge" style="margin-bottom:8px">✦ master document</div>` : '';
  const pmIdPill = `<span class="post-id-pill" style="margin-right:6px">#${post.id}</span>`;
  pmMeta.innerHTML = (tagsHtml || srcHtml)
    ? `${pmMasterBadge}<div class="post-tags">${tagsHtml}${srcHtml}</div><div class="pm-time">${pmIdPill}${timeLabel}</div>${pmExpiresHtml}`
    : `${pmMasterBadge}<div class="pm-time">${pmIdPill}${timeLabel}</div>${pmExpiresHtml}`;
  pmMeta.querySelectorAll('.tag-pill').forEach(pill =>
    pill.addEventListener('click', e => { e.stopPropagation(); closePostModal(); selectTag(pill.dataset.tag); })
  );

  const pmContent = post.title
    ? post.content.replace(/^\s*#{1,6}\s+[^\n]*\n*/, '')
    : post.content;
  pmBody.innerHTML = `<div class="post-body">${renderBody(pmContent)}</div><div class="pm-backlinks" id="pmBacklinks"></div>`;
  pmBody.querySelectorAll('.post-body table').forEach(t => {
    const wrap = document.createElement('div');
    wrap.className = 'table-scroll';
    t.parentNode.insertBefore(wrap, t);
    wrap.appendChild(t);
  });
  pmBody.querySelectorAll('.post-body pre').forEach(pre => {
    const btn = document.createElement('button');
    btn.className = 'code-copy';
    btn.textContent = 'copy';
    btn.addEventListener('click', () => {
      const text = pre.querySelector('code')?.textContent ?? pre.textContent;
      navigator.clipboard.writeText(text).then(() => {
        btn.classList.add('copied'); btn.textContent = 'copied';
        setTimeout(() => { btn.classList.remove('copied'); btn.textContent = 'copy'; }, 1500);
      }).catch(() => {});
    });
    pre.appendChild(btn);
  });
  renderBacklinks(post.id);
  clearFeedFocus();

  postModal.classList.add('open');
  document.body.style.overflow = 'hidden';
  pmBody.scrollTop = 0;
  requestAnimationFrame(updateModalFade);
  syncBackButton();
}

function updateModalFade() {
  const atBottom = pmBody.scrollHeight - pmBody.scrollTop <= pmBody.clientHeight + 2;
  pmBodyFade.classList.toggle('hidden', atBottom);
}


function closePostModal() {
  _modalStack = [];
  if (_historyDepth > 0) {
    _suppressPopstate = true;
    history.go(-_historyDepth);
    _historyDepth = 0;
  }
  postModal.classList.remove('open');
  document.body.style.overflow = '';
  pmBody.innerHTML = '';
  _modalPost = null;
  pmBack.style.display = '';
  pmBack.textContent = '← back';
  pmInner.classList.remove('has-back');
}

function popPostModal() {
  if (_modalStack.length === 0) { closePostModal(); return; }
  const { post, scrollTop } = _modalStack.pop();
  if (_historyDepth > 0) {
    _suppressPopstate = true;
    history.back();
    _historyDepth--;
  }
  openPostModal(post, { pushHistory: false });
  requestAnimationFrame(() => { pmBody.scrollTop = scrollTop; });
}

pmBody.addEventListener('scroll', updateModalFade);
pmClose.addEventListener('click', popPostModal);
pmBack.addEventListener('click', popPostModal);
postModal.addEventListener('click', (e) => { if (!e.target.closest('.pm-inner')) closePostModal(); });

/* Swipe-down-to-dismiss (mobile bottom-sheet) — shared with the other three
   sheets, which had no gesture at all before. */
attachSheetDismiss({
  inner: pmInner,
  handle: pmHeader,
  backdrop: pmBackdrop,
  onDismiss: closePostModal,
});
// History opens over the post modal (which stays behind it), so returning from a
// revision leaves you where you were.
const pmHistory = document.getElementById('pmHistory');
pmHistory.addEventListener('click', () => {
  const post = _modalPost; if (!post) return;
  openPostHistory(post.id, post.title);
});
initPostHistory(() => { resetPaging(); loadPosts(true); });

pmEdit.addEventListener('click', () => {
  const post = _modalPost; if (!post) return;
  // Edit modal (z-index 110) opens over the post modal (100) — post modal stays open behind it.
  enterEditMode(null, post);
});
pmDelete.addEventListener('click', async () => {
  const post = _modalPost; if (!post) return;
  if (!confirm('Delete this post?')) return;
  try {
    await apiSend(`/posts/${post.id}`, { method: 'DELETE' });
    closePostModal();
    const card = feed.querySelector(`[data-id="${post.id}"]`);
    if (card) { card.remove(); query.total--; }
    loadMoreWrap.style.display = query.offset < query.total ? 'block' : 'none';
    await loadTags();
  } catch {}
});
/* ── Keyboard shortcuts modal ─────────────────────────────── */
const shortcutsModal = document.getElementById('shortcutsModal');
document.getElementById('kbClose').onclick   = () => shortcutsModal.classList.remove('open');
document.getElementById('kbBackdrop').onclick = () => shortcutsModal.classList.remove('open');
function isShortcutsOpen() { return shortcutsModal.classList.contains('open'); }

/* ── Feed keyboard focus ──────────────────────────────────── */
let _focusedCard = null;

function getFeedCards() { return [...feed.querySelectorAll('.post:not(.editing)')]; }

function setFocusedCard(card) {
  if (_focusedCard) _focusedCard.classList.remove('card-focused');
  _focusedCard = card;
  if (card) { card.classList.add('card-focused'); card.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }
}

function moveFeedFocus(delta) {
  const cards = getFeedCards();
  if (!cards.length) return;
  const cur = _focusedCard ? cards.indexOf(_focusedCard) : -1;
  const next = Math.max(0, Math.min(cards.length - 1, cur + delta));
  setFocusedCard(cards[next === -1 ? 0 : next]);
}

function clearFeedFocus() {
  if (_focusedCard) _focusedCard.classList.remove('card-focused');
  _focusedCard = null;
}

/* ── Wikilink hover preview ───────────────────────────────── */
let _previewTimer = null;
let _previewEl    = null;

function hideLinkPreview() {
  clearTimeout(_previewTimer);
  _previewTimer = null;
  if (_previewEl) { _previewEl.remove(); _previewEl = null; }
}

function showLinkPreview(postId, anchor) {
  hideLinkPreview();
  const el = document.createElement('div');
  el.className = 'link-preview';
  const rect = anchor.getBoundingClientRect();
  el.style.top  = `${rect.bottom + 8}px`;
  el.style.left = `${Math.min(rect.left, window.innerWidth - 296)}px`;
  el.innerHTML = '<div class="lp-body">…</div>';
  document.body.appendChild(el);
  _previewEl = el;
  apiFetch(`/posts/${postId}`).then(post => {
    if (_previewEl !== el) return;
    const raw     = post.content.replace(/^\s*#{1,6}\s+[^\n]*\n*/, '');
    const snippet = raw.replace(/[#*`_[\]]/g, '').trim();
    const clipped = snippet.length > 150 ? snippet.slice(0, 150) + '…' : snippet;
    el.innerHTML  =
      `<div class="lp-title">${escHtml(post.title || `#${post.id}`)}</div>` +
      (post.tags.length ? `<div class="lp-tags">${post.tags.map(t => `<span class="lp-tag">${escHtml(t)}</span>`).join('')}</div>` : '') +
      (clipped ? `<div class="lp-body">${escHtml(clipped)}</div>` : '');
  }).catch(hideLinkPreview);
}

pmBody.addEventListener('mouseover', e => {
  const a = e.target.closest('a.wikilink[data-post-id]');
  if (!a) return;
  clearTimeout(_previewTimer);
  _previewTimer = setTimeout(() => showLinkPreview(Number(a.dataset.postId), a), 350);
});
pmBody.addEventListener('mouseout', e => {
  if (!e.target.closest('a.wikilink[data-post-id]')) return;
  hideLinkPreview();
});

document.addEventListener('keydown', e => {
  const typing = e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable;

  // Escape priority: most transient first.
  if (e.key === 'Escape' && isThemeMenuOpen())  { closeThemeMenu(); return; }
  if (e.key === 'Escape' && isEditOpen())        { tryCloseEditModal(); return; }
  if (e.key === 'Escape' && isShortcutsOpen())   { shortcutsModal.classList.remove('open'); return; }
  if (e.key === 'Escape' && isStatusOpen())      { closeStatusModal(); return; }
  if (e.key === 'Escape' && isHistoryOpen())     { closeHistoryModal(); return; }
  if (e.key === 'Escape' && postModal.classList.contains('open')) { popPostModal(); return; }
  if (e.key === 'Escape' && _focusedCard)        { clearFeedFocus(); return; }

  if (typing) return;

  // Post modal single-key shortcuts (reading mode only).
  if (postModal.classList.contains('open') && !isHistoryOpen() && !isEditOpen()) {
    if (e.key === 'e') { pmEdit.click(); return; }
    if (e.key === 'h') { pmHistory.click(); return; }
  }

  // Global.
  if (e.key === '?') { shortcutsModal.classList.toggle('open'); return; }

  // Feed navigation — only when no modal is open.
  const noModal = !isThemeMenuOpen() && !isEditOpen() && !isStatusOpen() && !isHistoryOpen()
               && !postModal.classList.contains('open') && !isShortcutsOpen();
  if (noModal) {
    if (e.key === 'j') { e.preventDefault(); moveFeedFocus(1);  return; }
    if (e.key === 'k') { e.preventDefault(); moveFeedFocus(-1); return; }
    if (e.key === 'Enter' && _focusedCard) {
      e.preventDefault();
      openPostById(Number(_focusedCard.dataset.id));
      clearFeedFocus();
      return;
    }
  }
});

window.addEventListener('popstate', e => {
  if (_suppressPopstate) { _suppressPopstate = false; return; }
  if (e.state?.postId) {
    if (_modalStack.length > 0) {
      const { post, scrollTop } = _modalStack.pop();
      _historyDepth--;
      openPostModal(post, { pushHistory: false });
      requestAnimationFrame(() => { pmBody.scrollTop = scrollTop; });
    } else {
      _historyDepth = Math.max(0, _historyDepth - 1);
      openPostById(e.state.postId);
    }
  } else {
    _historyDepth = 0;
    _modalStack = [];
    postModal.classList.remove('open');
    document.body.style.overflow = '';
    pmBody.innerHTML = '';
    _modalPost = null;
    pmBack.style.display = '';
    pmBack.textContent = '← back';
    pmInner.classList.remove('has-back');
  }
});

/* ── Helpers ──────────────────────────────────────────────── */




// Kick off: restore an existing session (cookie) or show the login control.
bootstrap();
