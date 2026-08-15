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
import { applySort, initViewPrefs, isDefaultSort, prefs } from './view-prefs.js';
import { escHtml, fmtBytes, relativeTime, toDatetimeLocal, toUtcIso } from './util.js';
const LIMIT = 20;
// The break-glass API key now lives in ./api.js (setApiKey/clearApiKey).
let authed = false;    // true once a session exists (cookie or key) — the real "logged in" flag
let sidebarMode = 'tags';   // 'tags' | 'tree' | 'files'
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

// Light ⇄ dark theme — CSS var override on <html>, persisted locally. Default: dark.
const themeBtn = document.getElementById('themeBtn');
const brandMark = document.querySelector('.brand-mark');
function applyTheme() {
  const light = document.documentElement.getAttribute('data-theme') === 'light';
  themeBtn.textContent = light ? '☾' : '☀';
  if (brandMark) brandMark.src = light ? '/assets/relay-mark.svg' : '/assets/relay-mark-on-dark.svg';
}
themeBtn.addEventListener('click', () => {
  const light = document.documentElement.getAttribute('data-theme') === 'light';
  if (light) document.documentElement.removeAttribute('data-theme');
  else       document.documentElement.setAttribute('data-theme', 'light');
  try { localStorage.setItem('relay-theme', light ? 'dark' : 'light'); } catch (e) {}
  applyTheme();
});
applyTheme();

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
  feed.style.display = ''; attachmentsView.style.display = 'none';
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
  tagList.innerHTML = '';
  tagList.appendChild(makeTagItem('all', null, tags.reduce((s, t) => s + t.count, 0)));
  tags.forEach(t => tagList.appendChild(makeTagItem(t.tag, t.tag, t.count)));
}

function makeTagItem(label, value, count) {
  const el = document.createElement('div');
  el.className = 'tag-item' + (query.tag === value ? ' active' : '');
  const renameBtn = value !== null ? `<button class="tag-rename" title="Rename">✏︎</button>` : '';
  const configBtn = value !== null ? `<button class="tag-config-btn" title="Configure">⚙</button>` : '';
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
function setSidebarMode(mode) {
  sidebarMode = mode;
  document.getElementById('tabTags').classList.toggle('active', mode === 'tags');
  document.getElementById('tabTree').classList.toggle('active', mode === 'tree');
  document.getElementById('tabFiles').classList.toggle('active', mode === 'files');
  newTagBtn.style.display = mode === 'tags' ? '' : 'none';
  document.getElementById('tagNew').style.display = 'none';
  const files = mode === 'files';
  // Files mode swaps the post feed for the attachment gallery.
  feed.style.display = files ? 'none' : '';
  attachmentsView.style.display = files ? '' : 'none';
  newPostBtn.style.display = files ? 'none' : '';
  if (files) loadMoreWrap.style.display = 'none';
  else if (query.offset < query.total) loadMoreWrap.style.display = 'block';
  if (mode === 'tags') loadTags();
  else if (mode === 'tree') loadFolders();
  else loadAttachments();
}
document.getElementById('tabTags').addEventListener('click', () => setSidebarMode('tags'));
document.getElementById('tabTree').addEventListener('click', () => setSidebarMode('tree'));
document.getElementById('tabFiles').addEventListener('click', () => setSidebarMode('files'));

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
  const ico = value === null ? '' : '<span class="folder-ico">▸</span>';
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
  all.innerHTML = `<span class="tag-name">All files</span><span class="tag-count">${attachCache.length}</span>`;
  all.addEventListener('click', () => selectAttachFolder(null));
  tagList.appendChild(all);
  [...counts.keys()].sort().forEach(folder => {
    const el = document.createElement('div');
    el.className = 'tag-item folder-item' + (attachFolder === folder ? ' active' : '');
    el.innerHTML = `<span class="tag-name"><span class="folder-ico">▸</span>${escHtml(folder)}</span><span class="tag-count">${counts.get(folder)}</span>`;
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

function startTagRename(el, oldName) {
  const nameSpan = el.querySelector('.tag-name');
  const input = document.createElement('input');
  input.className = 'tag-rename-input';
  input.value = oldName;
  nameSpan.replaceWith(input);
  input.focus(); input.select();

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
    committed = true;
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
  const savedHtml = el.innerHTML;
  const form = document.createElement('div');
  form.className = 'tag-config-form';
  form.innerHTML = `
    <input type="number" class="tc-ttl" placeholder="TTL hours (optional)" min="1">
    <input type="datetime-local" class="tc-expires">`;
  el.innerHTML = '';
  el.appendChild(form);

  const ttlInput = form.querySelector('.tc-ttl');
  const expiresInput = form.querySelector('.tc-expires');
  ttlInput.focus();

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
    committed = true;
    el.innerHTML = savedHtml;
    el.querySelector('.tag-rename')?.addEventListener('click', ev => {
      ev.stopPropagation(); startTagRename(el, tagName);
    });
    el.querySelector('.tag-config-btn')?.addEventListener('click', ev => {
      ev.stopPropagation(); startTagConfig(el, tagName);
    });
  }

  form.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') cancel();
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

function enterEditMode(el, post) {
  el.classList.add('editing');
  const savedHtml = el.innerHTML;
  el.innerHTML = `
    <div class="edit-form">
      <div><label>Title</label><input class="ef-title" type="text" value="${escHtml(post.title || '')}"></div>
      <div><label>Content</label><textarea class="ef-content">${escHtml(post.content)}</textarea>
        <div class="attach-row">
          <input type="file" class="ef-file" multiple style="display:none">
          <button type="button" class="btn-attach ef-attach">📎 Attach</button>
          <span class="attach-status ef-attach-status"></span>
        </div>
        <div class="ef-attachments"></div>
      </div>
      <div><label>Tags</label><input class="ef-tags" type="text" value="${escHtml(post.tags.join(', '))}"></div>
      <div><label>Source</label><input class="ef-source" type="text" value="${escHtml(post.source || '')}"></div>
      <div><label>Expires</label><input class="ef-expires" type="datetime-local" value="${toDatetimeLocal(post.expires_at || '')}"></div>
      <div class="edit-actions">
        <button class="btn-cancel">Cancel</button>
        <button class="btn-save">Save</button>
      </div>
    </div>`;

  wireAttachments(
    el.querySelector('.ef-content'), el.querySelector('.ef-file'),
    el.querySelector('.ef-attach'), el.querySelector('.ef-attach-status'),
    () => ({ post_id: post.id, embed: false }),
  );
  renderEditAttachments(el, post.id);

  el.querySelector('.btn-cancel').addEventListener('click', () => { el.classList.remove('editing'); el.innerHTML = savedHtml; rewirePost(el, post); });
  el.querySelector('.btn-save').addEventListener('click', async () => {
    const newTitle = el.querySelector('.ef-title').value.trim();
    if (!newTitle) { alert('Title is required'); return; }
    const body = {
      title:      newTitle,
      content:    el.querySelector('.ef-content').value,
      tags:       el.querySelector('.ef-tags').value.split(',').map(s => s.trim()).filter(Boolean),
      source:     el.querySelector('.ef-source').value.trim() || null,
      expires_at: toUtcIso(el.querySelector('.ef-expires').value) || null,
    };
    const btn = el.querySelector('.btn-save');
    btn.disabled = true; btn.textContent = 'Saving…';
    try {
      const updated = await apiFetch(`/posts/${post.id}`, { method: 'PATCH', body: JSON.stringify(body) });
      el.replaceWith(renderPost(updated));
      refreshSidebarCounts();
    } catch (e) {
      alert(`Save failed: ${e.message}`);
      btn.disabled = false; btn.textContent = 'Save';
    }
  });
}

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
      openPostModal(post);
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
    } else {
      // Non-default sort: the new post doesn't belong at the top — count it and
      // let the user pull it in via the pill rather than misplacing the card.
      query.total++;
      bumpNewPostsPill();
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
let _modalPost   = null;

function openPostModal(post) {
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
  renderBacklinks(post.id);

  postModal.classList.add('open');
  document.body.style.overflow = 'hidden';
  pmBody.scrollTop = 0;
  requestAnimationFrame(updateModalFade);
}

function updateModalFade() {
  const atBottom = pmBody.scrollHeight - pmBody.scrollTop <= pmBody.clientHeight + 2;
  pmBodyFade.classList.toggle('hidden', atBottom);
}


function closePostModal() {
  postModal.classList.remove('open');
  document.body.style.overflow = '';
  pmBody.innerHTML = '';
  _modalPost = null;
}

pmBody.addEventListener('scroll', updateModalFade);
pmClose.addEventListener('click', closePostModal);
pmBack.addEventListener('click', closePostModal);
postModal.addEventListener('click', (e) => { if (!e.target.closest('.pm-inner')) closePostModal(); });

/* Swipe-down-to-dismiss on the modal header (mobile bottom-sheet) */
let _swipeStartY = 0, _swipeDeltaY = 0;
pmHeader.addEventListener('touchstart', e => {
  _swipeStartY = e.touches[0].clientY;
  _swipeDeltaY = 0;
  pmInner.style.transition = 'none';
}, { passive: true });
pmHeader.addEventListener('touchmove', e => {
  _swipeDeltaY = e.touches[0].clientY - _swipeStartY;
  if (_swipeDeltaY > 0) pmInner.style.transform = `translateY(${_swipeDeltaY}px)`;
}, { passive: true });
pmHeader.addEventListener('touchend', () => {
  pmInner.style.transition = '';
  pmInner.style.transform = '';
  if (_swipeDeltaY > 72) closePostModal();
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
  closePostModal();
  const card = feed.querySelector(`[data-id="${post.id}"]`);
  if (card) enterEditMode(card, post);
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
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && isStatusOpen()) { closeStatusModal(); return; }
  if (e.key === 'Escape' && isHistoryOpen()) { closeHistoryModal(); return; }
  if (e.key === 'Escape' && postModal.classList.contains('open')) closePostModal();
});

/* ── Helpers ──────────────────────────────────────────────── */




// Kick off: restore an existing session (cookie) or show the login control.
bootstrap();
