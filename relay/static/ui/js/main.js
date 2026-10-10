/* Relay browser UI — the app body: session, feed, sidebar, post and edit
 * modals, live updates. Self-contained concerns live in sibling modules
 * (status, lint, history, recovery, themes, modal plumbing in dialog.js).
 *
 * `marked` and `DOMPurify` are globals from vendored classic scripts in <head>,
 * which run before this deferred module.
 */

import { apiFetch, apiSend, clearApiKey, clearCallerScope, getCallerScope, setApiKey, setCallerScope, tagsAllowedByScope } from './api.js';
import { closeStatusModal, fetchInitStatus, isStatusOpen } from './status.js';
import { query, resetPaging } from './feed-query.js';
import { initPostHistory, openPostHistory } from './post-history.js';
import { anyModalOpen, closeAllModals, dismissTopModal, topModal, wireModal } from './dialog.js';
import { showToast } from './toast.js';
import { initDeleted } from './deleted.js';
import { initLint, reopenLintModal, tryCloseLintModal } from './lint.js';
import { buildEditForm, confirmDeleteAttachment, wireAttachments } from './edit-form.js';
import { idForTitle, postExists, refreshLinks } from './links.js';
import { closeThemeMenu, isThemeMenuOpen } from './theme.js';
import { applySort, initViewPrefs, isDefaultSort, prefs } from './view-prefs.js';
import { ICON_CLOCK, ICON_FOLDER, ICON_IMAGE, ICON_PENCIL, ICON_TRASH } from './icons.js';
import { CODE_SPAN_RE, cleanTag, escHtml, fmtBytes, parseTags, relativeTime, sourceParts, toDatetimeLocal, toUtcIso } from './util.js';

const LIMIT = 20;
let authed = false;         // true once a session exists (cookie or key)
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
const a11yAnnouncer = document.getElementById('a11yAnnouncer');
const apiKeyInput  = document.getElementById('apiKeyInput');
const connectForm  = document.getElementById('connectForm');
const oidcLogin    = document.getElementById('oidcLogin');
const oidcLoginBtn = document.getElementById('oidcLoginBtn');
const useKeyBtn    = document.getElementById('useKeyBtn');
const newPostBtn   = document.getElementById('newPostBtn');
const collapseBtn  = document.getElementById('collapseBtn');
const composePanel = document.getElementById('composePanel');
const newTagBtn    = document.getElementById('newTagBtn');
const tagNewWrap   = document.getElementById('tagNew');
const tagNewInput  = document.getElementById('tagNewInput');
const disconnectBtn   = document.getElementById('disconnectBtn');
const menuBtn         = document.getElementById('menuBtn');
const sidebarEl       = document.getElementById('sidebarEl');
const sidebarOverlay  = document.getElementById('sidebarOverlay');
const searchBar       = document.getElementById('searchBar');
const attachmentsView = document.getElementById('attachmentsView');
const lightbox        = document.getElementById('lightbox');
const searchInput     = document.getElementById('searchInput');
const searchClear     = document.getElementById('searchClear');
const modeSelect      = document.getElementById('modeSelect');
const searchScope     = document.getElementById('searchScope');
// The ranking a fresh search starts from: 'hybrid' once /status confirms
// embeddings are on (it beats keyword alone on this relay's eval, #253),
// 'keyword' otherwise.
let defaultMode = 'keyword';
// Whether a delete can be undone (vault history on) — set from /status in init().
// Until that answers, deletes ask first, the safe assumption.
let historyOn = false;

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

// Login problems are shown under the form, not in a blocking alert() that
// leaves nothing on the page once dismissed.
const loginError = document.getElementById('loginError');
function showLoginError(msg) { loginError.textContent = msg; loginError.hidden = false; }

document.getElementById('connectForm').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  loginError.hidden = true;
  const val = apiKeyInput.value.trim();
  if (!val) return;
  try {
    const res = await fetch('/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: val }),
      credentials: 'same-origin',
    });
    if (!res.ok) { showLoginError('Invalid API key.'); return; }
    setApiKey(val);
    authed = true;
    init();
  } catch (e) {
    showLoginError('Connection failed: ' + e.message);
  }
});

// Forward `?post=<id>` (an /id/<id> deep link landed here logged out) so
// /auth/login can stash it and the OIDC callback can restore it — see
// routes/auth.py's auth_login/auth_callback.
oidcLoginBtn.addEventListener('click', () => { window.location.href = '/auth/login' + location.search; });
useKeyBtn.addEventListener('click', () => {
  oidcLogin.style.display = 'none';
  connectForm.style.display = '';
  apiKeyInput.focus();
});

// New Post's one visibility rule — hidden on the Files tab and for a read-only key.
function applyNewPostVisibility() {
  const scope = getCallerScope();
  const hiddenByTab = sidebarMode === 'files';
  const hiddenByScope = !!scope && scope.mode === 'read';
  newPostBtn.style.display = (hiddenByTab || hiddenByScope) ? 'none' : '';
}

// On load, ask the server whether a session cookie is already live (OIDC or a
// prior key-paste). If so, boot straight into the app cookie-only — no re-paste.
// Otherwise show the right login control based on whether OIDC is configured.
async function bootstrap() {
  const params = new URLSearchParams(location.search);
  if (params.get('auth_error') === 'forbidden') showLoginError('Your account is not authorized for relay.');
  else if (params.get('auth_error')) showLoginError('Login failed. Please try again.');
  if (params.has('auth_error')) history.replaceState(null, '', location.pathname);

  let me = { authenticated: false, oidc: false };
  try { me = await (await fetch('/auth/me', { credentials: 'same-origin' })).json(); } catch {}
  if (me.authenticated) { authed = true; init(); return; }
  document.body.classList.add('signed-out');
  if (me.oidc) { oidcLogin.style.display = ''; connectForm.style.display = 'none'; }
  else { connectForm.style.display = ''; oidcLogin.style.display = 'none'; }
}

/* Back to the login card — from Disconnect, or because the server stopped
   accepting the session (any 401, or the live stream refused). `reason` is
   shown under the form. */
async function signOut(reason = '') {
  if (!authed) return;
  authed = false;
  clearApiKey();
  clearCallerScope();
  // Await so the cookie is cleared before bootstrap() re-checks /auth/me below.
  await fetch('/session', { method: 'DELETE', credentials: 'same-origin' }).catch(() => {});
  if (es) { es.close(); es = null; }
  if (sseErrorTimer) { clearTimeout(sseErrorTimer); sseErrorTimer = null; }
  closeAllModals();
  closeSidebar();
  feed.innerHTML = '';
  tagList.innerHTML = '';
  loadMoreWrap.style.display = 'none';
  query.search = null; searchInput.value = ''; searchBar.classList.remove('active');
  apiKeyInput.value = '';
  setDot('');
  await bootstrap();   // shows whichever login control the deployment uses
  if (reason) showLoginError(reason);
}
disconnectBtn.addEventListener('click', () => signOut());
window.addEventListener('relay:unauthorized', () => signOut('Your session has ended — sign in again.'));



async function init() {
  document.body.classList.remove('signed-out');
  syncSearchScope();
  resetPaging();
  feed.innerHTML = '';
  attachFolder = null;
  showSidebarMode('tags');   // a reconnect may come from the Tree or Files tab
  loadMoreWrap.style.display = 'none';
  // Not carried across sessions: a stale 'semantic' would 503 the first
  // search on a relay with embeddings off.
  query.mode = 'keyword';
  modeSelect.value = 'keyword';
  modeSelect.style.display = 'none';
  fetchInitStatus().then(({ embeddingsEnabled: on, caller, historyEnabled }) => {
    historyOn = historyEnabled;
    modeSelect.style.display = on ? '' : 'none';
    defaultMode = on ? 'hybrid' : 'keyword';
    query.mode = modeSelect.value = defaultMode;
    setCallerScope(caller);
    applyNewPostVisibility();   // scope was unknown until now
    applyComposeScopeGate();
  });
  // A deep-linked post renders once, so it waits for the link index (but not
  // for tags or the feed) — otherwise its [[links]] would all show as broken.
  const linkIndexReady = refreshLinks();
  linkIndexReady.then(openPostFromUrl);
  await Promise.all([loadTags(), loadPosts(true), linkIndexReady]);
  setDot('connected');
  connectSSE();
}

// /id/<id> deep links land here as `/?post=<id>` (see relay/main.py's
// redirect). Consumed once per load — stripped from the URL immediately so a
// later reload or back-navigation doesn't reopen it.
async function openPostFromUrl() {
  const params = new URLSearchParams(location.search);
  const raw = params.get('post');
  if (raw === null) return;
  history.replaceState(null, '', location.pathname);
  if (!/^\d+$/.test(raw)) return;
  // Unlike a silent broken wikilink, opening this post is what the visit is for.
  await openPostById(raw, { onError: (e) => showToast(`Couldn’t open post #${raw}: ${e.message}`, { error: true }) });
}

// ── Wikilinks: [[Title]] / [[Title|alias]] and #NNN cross-references ──────────
// DOMPurify config: keep the attrs our attachment embeds/links add (img loading,
// link target/rel). marked + preprocessLinks output is sanitized through this.
const SANITIZE_OPTS = { ADD_ATTR: ['target', 'rel', 'loading'] };
// A link out of the vault opens in a new tab: in this tab it would replace the
// whole app, mid-read, with no way back to the open post.
DOMPurify.addHook('afterSanitizeAttributes', node => {
  if (node.tagName === 'A' && /^https?:/i.test(node.getAttribute('href') || '')) {
    node.setAttribute('target', '_blank');
    node.setAttribute('rel', 'noopener noreferrer');
  }
});
const renderBody = (md) => DOMPurify.sanitize(marked.parse(preprocessLinks(md)), SANITIZE_OPTS);

// A titled post's body usually opens with that title as a heading; cards, the
// modal and link previews all show the title already, so they drop it.
const stripTitleHeading = (md) => md.replace(/^\s*#{1,6}\s+[^\n]*\n*/, '');
const postName = (post) => post.title || `#${post.id}`;
const tagPills = (tags) => tags.map(t => `<span class="tag-pill" data-tag="${escHtml(t)}">${escHtml(t)}</span>`).join('');

/* Every rendered table scrolls sideways inside its own box when it cannot fit
   — see `.table-scroll` in app.css — in feed cards as well as the modal. */
function wrapTables(root) {
  root.querySelectorAll('.post-body table').forEach(t => {
    const wrap = document.createElement('div');
    wrap.className = 'table-scroll';
    t.replaceWith(wrap);
    wrap.appendChild(t);
    t.querySelectorAll('td code').forEach(addPathBreaks);
    floorProseColumns(t);
  });
}

/* Once a table overflows, automatic layout shrinks *every* column to its
   longest word — on a phone a paragraph column ended up 98px wide beside short
   ones. A column whose longest cell reads as prose keeps a floor instead, and
   the table scrolls rather than crushing it. Short columns are left tight. */
const PROSE_CHARS = 30;
function floorProseColumns(table) {
  const longest = [];
  for (const row of table.rows) {
    [...row.cells].forEach((cell, i) => {
      // Prose only: code already wraps at its joints (addPathBreaks).
      const code = [...cell.querySelectorAll('code')].reduce((n, c) => n + c.textContent.length, 0);
      longest[i] = Math.max(longest[i] || 0, cell.textContent.trim().length - code);
    });
  }
  for (const row of table.rows) {
    [...row.cells].forEach((cell, i) => { if (longest[i] >= PROSE_CHARS) cell.style.minWidth = '10em'; });
  }
}

/* A path or dotted name in a table cell has no break opportunity, so one
   `/var/lib/node_exporter/fleet.prom` sets its column's minimum width and
   squeezes the prose column beside it. `<wbr>` after each separator lets it
   wrap at its joints; unlike a zero-width space it adds no character, so a
   copied command stays exact. */
const PATH_JOINT_RE = /(?<=[/._=])/;
function addPathBreaks(code) {
  if (code.children.length || code.textContent.length < 16) return;
  const parts = code.textContent.split(PATH_JOINT_RE);
  if (parts.length < 2) return;
  code.replaceChildren(...parts.flatMap((part, i) => (i ? [document.createElement('wbr'), part] : [part])));
}

// Convert wikilinks / id-refs to anchors, leaving fenced + inline code untouched.
function preprocessLinks(md) {
  return md.split(CODE_SPAN_RE)
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
    const id = idForTitle(name);
    return (id !== undefined)
      ? `<a class="wikilink" data-post-id="${id}">${escHtml(o || name)}</a>`
      : `<span class="wikilink broken" title="unresolved embed">${escHtml(o || name)}</span>`;
  });
  text = text.replace(/\[\[([^\]|#]+?)(#[^\]|]+)?(?:\|([^\]]+))?\]\]/g, (m, target, heading, alias) => {
    const label = escHtml((alias || target).trim());
    const t = target.trim();
    const id = idForTitle(t);
    if (id !== undefined) return `<a class="wikilink" data-post-id="${id}">${label}</a>`;
    // Unresolved but a known attachment type (e.g. [[doc.pdf]]) → attachment link, not broken.
    if (ATTACH_EXT_RE.test(t)) return attLink(t, (alias || target).trim());
    return `<span class="wikilink broken" title="unresolved link">${label}</span>`;
  });
  text = text.replace(/(^|[^\w#])#(\d{1,5})\b/g, (m, pre, n) =>
    postExists(n) ? `${pre}<a class="wikilink" data-post-id="${n}">#${n}</a>` : m);
  return text;
}

// First image embed → thumbnail URL + image count, plus the content with image
// embeds removed so a card's text preview shows prose instead of an image slice.
// Non-image embeds (pdf, note transclusions) are left in place.
function extractMedia(content) {
  let thumb = null, count = 0;
  const stripped = content.split(CODE_SPAN_RE).map((seg, i) => {
    if (i % 2 === 1) return seg;   // code — never a real embed, leave untouched
    return seg.replace(/!\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]/g, (m, target) => {
      const name = target.trim();
      if (!IMAGE_EXT_RE.test(name)) return m;
      count++;
      if (!thumb) thumb = attUrl(name);
      return '';
    });
  }).join('');
  return { thumb, count, stripped };
}

async function openPostById(id, { onError, origin, pushHistory = true } = {}) {
  try { openPostModal(await apiFetch(`/posts/${id}`), { origin, pushHistory }); }
  catch (e) { if (onError) onError(e); }
}

// Delegated: any rendered wikilink opens its target post.
document.addEventListener('click', e => {
  const a = e.target.closest('a.wikilink[data-post-id]');
  if (!a) return;
  e.preventDefault(); e.stopPropagation();
  hideLinkPreview();   // also cancels a hover preview still on its timer
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
const cpTitle = document.getElementById('cpTitle');
const cpContent = document.getElementById('cpContent');
const cpTags = document.getElementById('cpTags');
const cpPublish = document.getElementById('cpPublish');
const cpGateMsg = document.createElement('div');
cpGateMsg.className = 'ef-gate-msg';
cpPublish.insertAdjacentElement('beforebegin', cpGateMsg);

// Publish follows the key's scope, live as Tags is edited. Scope arrives after
// login (fetchInitStatus), and until it does Publish stays disabled — treating
// "unknown" as "full access" was a race CI caught.
function applyComposeScopeGate() {
  // Closed, there is nothing to gate — and a message parked in the hidden
  // form is still text in the page (a "Read-only" lookup found two).
  if (!isComposeOpen()) return;
  const scope = getCallerScope();
  if (!scope) {
    cpGateMsg.textContent = 'Checking access…';
    cpPublish.disabled = true;
    return;
  }
  if (scope.mode === 'full') { cpGateMsg.textContent = ''; cpPublish.disabled = false; return; }
  if (scope.mode === 'read') {   // New Post is hidden then, but may be open from before scope arrived
    cpGateMsg.textContent = 'This key is read-only — publishing is disabled.';
    cpPublish.disabled = true;
    return;
  }
  const tags = parseTags(cpTags.value);
  if (tagsAllowedByScope(tags, scope)) {
    cpGateMsg.textContent = '';
    cpPublish.disabled = false;
  } else {
    cpGateMsg.textContent = `This key can only write tags: ${(scope.tags || []).join(', ') || '(none)'}.`;
    cpPublish.disabled = true;
  }
}
cpTags.addEventListener('input', applyComposeScopeGate);

// What the panel held when it opened (Tags may be prefilled from the active
// filter), so leaving it can ask before throwing a draft away — the same guard
// the Edit modal has. Toggling New Post shut used to wipe a draft unasked.
const COMPOSE_FIELDS = ['cpTitle', 'cpContent', 'cpTags', 'cpSource', 'cpExpires'];
let composeBaseline = null;
const composeValues = () => COMPOSE_FIELDS.map(id => document.getElementById(id).value);
function isComposeDirty() {
  return !!composeBaseline && composeValues().some((v, i) => v !== composeBaseline[i]);
}
function isComposeOpen() { return composePanel.classList.contains('open'); }

newPostBtn.addEventListener('click', () => {
  composePanel.classList.add('open');
  if (query.tag) cpTags.value = query.tag;
  composeBaseline = composeValues();
  applyComposeScopeGate();
  cpTitle.focus();
});

const tryCloseCompose = wireModal(composePanel, {
  close: closeCompose,
  confirmDiscard: () => !isComposeDirty() || confirm('Discard this unpublished post?'),
});
document.getElementById('cpCancel').addEventListener('click', tryCloseCompose);

// Said where the user is looking, beside Publish, instead of in an alert() —
// or, for an empty body, not at all.
function composeProblem(msg, field) {
  cpGateMsg.textContent = msg;
  field.focus();
}

cpPublish.addEventListener('click', async () => {
  const title = cpTitle.value.trim();
  if (!title) { composeProblem('Add a title — it becomes the file name.', cpTitle); return; }
  const content = cpContent.value.trim();
  if (!content) { composeProblem('Write something before publishing.', cpContent); return; }
  const body = {
    title,
    content,
    tags:   parseTags(cpTags.value),
    source: document.getElementById('cpSource').value.trim() || null,
    expires_at: toUtcIso(document.getElementById('cpExpires').value) || null,
  };
  cpPublish.disabled = true; cpPublish.textContent = 'Publishing…';
  try {
    await apiFetch('/posts', { method: 'POST', body: JSON.stringify(body) });
    closeCompose();
    refreshSidebarCounts();
    await refreshLinks();
  } catch (e) { cpGateMsg.textContent = `Publish failed: ${e.message}`; }
  finally { cpPublish.textContent = 'Publish'; cpPublish.disabled = false; applyComposeScopeGate(); }
});

function closeCompose() {
  composePanel.classList.remove('open');
  composeBaseline = null;
  COMPOSE_FIELDS.forEach(id => { document.getElementById(id).value = ''; });
  const st = document.getElementById('cpAttachStatus');
  st.textContent = ''; st.classList.remove('error');
  cpGateMsg.textContent = '';
}

wireAttachments(
  cpContent, document.getElementById('cpFile'),
  document.getElementById('cpAttach'), document.getElementById('cpAttachStatus'),
  () => ({ tags: parseTags(cpTags.value) }),
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
  query.mode = defaultMode;
  modeSelect.value = query.mode;
  searchBar.classList.remove('active');
  resetPaging();
  await loadPosts(true);
  searchInput.focus();
});

modeSelect.addEventListener('change', async () => {
  query.mode = modeSelect.value;
  resetPaging();
  await loadPosts(true);
});

/* ── New tag ──────────────────────────────────────────────── */
newTagBtn.addEventListener('click', () => {
  const visible = tagNewWrap.style.display !== 'none';
  tagNewWrap.style.display = visible ? 'none' : '';
  if (!visible) { tagNewInput.value = ''; tagNewInput.focus(); }
});

/* A tag exists while a post carries it — there is nothing to create on its
   own. This used to POST an empty config, which the server reads as "remove
   this tag's config" (set_tag_config), so the new tag silently never
   appeared. It now starts a post carrying the tag, the one way a tag begins. */
tagNewInput.addEventListener('keydown', e => {
  if (e.key === 'Escape') { tagNewWrap.style.display = 'none'; newTagBtn.focus(); return; }
  if (e.key !== 'Enter') return;
  const name = cleanTag(tagNewInput.value);
  if (!name) return;
  tagNewWrap.style.display = 'none';
  closeSidebar();
  if (!isComposeOpen()) newPostBtn.click();
  const current = parseTags(cpTags.value);
  if (!current.includes(name)) cpTags.value = [...current, name].join(', ');
  cpTags.dispatchEvent(new Event('input'));   // re-run the scope gate
  cpTitle.focus();
});

/* ── Tags ─────────────────────────────────────────────────── */
// The "all" row counts posts, not tag or folder memberships: summing per-tag
// counts counted a multi-tag post once per tag and an untagged post not at all,
// and summing folders skipped the root master document. The unfiltered feed's
// `total` excludes the pinned master document, so add it back.
async function postCount() {
  const d = await apiFetch('/posts?limit=1&summary=true');
  return d.total + (d.pinned ? 1 : 0);
}

async function loadTags() {
  try {
    const [data, count] = await Promise.all([apiFetch('/tags'), postCount()]);
    renderTags(data.tags, count);
  } catch {}
}

// Refresh whichever count view is showing (Tags or Tree). Use this, not
// loadTags(), after a local change: loadTags() on the Tree tab swaps the
// sidebar to the tag list. Files has no post-driven counts.
function refreshSidebarCounts() {
  if (sidebarMode === 'tags') loadTags();
  else if (sidebarMode === 'tree') loadFolders();
}

// After live updates, debounced so a burst of SSE events (a reconnect replay)
// costs one round of fetches, not one per event.
let _refreshTimer = null;
function scheduleRefresh() {
  clearTimeout(_refreshTimer);
  _refreshTimer = setTimeout(() => { refreshSidebarCounts(); refreshLinks(); }, 250);
}

function renderTags(tags, allCount) {
  openTagEditor = null;   // the DOM these forms lived in is about to be replaced
  tagList.innerHTML = '';
  tagList.appendChild(makeTagItem('all', null, allCount));
  tags.forEach(t => tagList.appendChild(makeTagItem(t.tag, t.tag, t.count, t)));
}

/* One sidebar row — a tag, a folder, or a Files group. Its label is a real
   <button> so Tab reaches it; the click bubbles to the row, which keeps the
   whole row a target. `labelHtml` and `controls` are already escaped. */
function sidebarRow({ labelHtml, count, active, onClick, className = 'tag-item', controls = '' }) {
  const row = document.createElement('div');
  row.className = className + (active ? ' active' : '');
  row.innerHTML = `<button type="button" class="tag-name"${active ? ' aria-current="true"' : ''}>${labelHtml}</button>`
    + `${controls}<span class="tag-count">${count}</span>`;
  row.addEventListener('click', onClick);
  return row;
}

// Folder rows reserve the icon's gutter even without one ("all"), so every
// label starts on the same left edge.
const folderLabel = (name, icon = true) =>
  `<span class="folder-ico">${icon ? ICON_FOLDER : ''}</span>${escHtml(name)}`;

/** Mark the active sidebar row, for sight (`.active`) and for screen readers. */
function markActiveRow(el, active) {
  el.classList.toggle('active', active);
  const btn = el.querySelector('.tag-name');
  if (btn) { if (active) btn.setAttribute('aria-current', 'true'); else btn.removeAttribute('aria-current'); }
}

/** "48h after posting", "at <date>", both, or '' — a tag's expiry config. */
function expirySummary(cfg) {
  if (!cfg) return '';
  const parts = [];
  if (cfg.ttl_hours) parts.push(`${cfg.ttl_hours}h after posting`);
  if (cfg.expires_at) parts.push(`at ${new Date(cfg.expires_at).toLocaleString()}`);
  return parts.join(', ');
}

function makeTagItem(label, value, count, cfg = null) {
  const expiry = cfg && (cfg.ttl_hours || cfg.expires_at) ? { ttl_hours: cfg.ttl_hours, expires_at: cfg.expires_at } : null;
  // A tag that expires its posts keeps its clock visible (.has-expiry).
  const summary = escHtml(expirySummary(expiry));
  const controls = value === null ? '' :
    `<button class="tag-rename" title="Rename tag" aria-label="Rename tag">${ICON_PENCIL}</button>`
    + `<button class="tag-config-btn${summary ? ' has-expiry' : ''}" title="${summary ? `Posts expire ${summary}` : 'Expiry settings'}"`
    + ` aria-label="Expiry settings${summary ? ` (posts expire ${summary})` : ''}">${ICON_CLOCK}</button>`;
  const row = sidebarRow({
    labelHtml: escHtml(label), count, controls, active: query.tag === value, onClick: () => selectTag(value),
  });
  row._expiry = expiry;
  row.dataset.tag = value ?? '';   // by value: a tag may well be called "all"
  if (value !== null) wireTagControls(row, value);
  return row;
}

function wireTagControls(row, tag) {
  row.querySelector('.tag-rename').addEventListener('click', e => { e.stopPropagation(); startTagRename(row, tag); });
  row.querySelector('.tag-config-btn').addEventListener('click', e => { e.stopPropagation(); startTagConfig(row, tag); });
}

/* ── Sidebar: Tags ⇄ Tree toggle ───────────────────────────── */
// One tab replaces the feed with a different listing; the other two filter it.
// Naming that split is what keeps a new tab from adding another set of
// near-identical toggles — and the strip has no room for one anyway
// (`tests/ui/test_sidebar_tabs.py`), which is why recovering a deleted post
// lives in the status panel rather than here.
const SIDEBAR_TABS = { tags: 'tabTags', tree: 'tabTree', files: 'tabFiles' };
const FEED_REPLACING = { files: 'attachmentsView' };

/** Switch tabs: the UI part, without loading anything (init loads its own). */
function showSidebarMode(mode) {
  sidebarMode = mode;
  for (const [name, id] of Object.entries(SIDEBAR_TABS)) {
    document.getElementById(id).classList.toggle('active', mode === name);
  }
  newTagBtn.style.display = mode === 'tags' ? '' : 'none';
  document.getElementById('tagNew').style.display = 'none';

  const replacing = mode in FEED_REPLACING;
  feed.style.display = replacing ? 'none' : '';
  // Search, sort and list/grid all drive the post feed; beside the Files grid
  // they were live controls acting on a hidden feed.
  searchBar.style.display = replacing ? 'none' : '';
  for (const [name, id] of Object.entries(FEED_REPLACING)) {
    document.getElementById(id).style.display = mode === name ? '' : 'none';
  }
  applyNewPostVisibility();
  if (replacing) loadMoreWrap.style.display = 'none';
  else if (query.offset < query.total) loadMoreWrap.style.display = 'block';
}

function setSidebarMode(mode) {
  showSidebarMode(mode);
  if (mode === 'tags') loadTags();
  else if (mode === 'tree') loadFolders();
  else loadAttachments();
}
for (const [name, id] of Object.entries(SIDEBAR_TABS)) {
  document.getElementById(id).addEventListener('click', () => setSidebarMode(name));
}
// A restore puts a post back in the feed, so the feed has to hear about it.
// The recovery browser itself lives in the status panel (`js/status.js`).
initDeleted(() => { resetPaging(); loadPosts(true); refreshSidebarCounts(); });
initLint({
  // Leave lint (asking about unsaved edits — a "no" cancels) and status, which
  // would cover the post modal; its "← Vault lint" breadcrumb returns to the
  // same filter and finding.
  openPost: id => {
    if (!tryCloseLintModal()) return;
    if (isStatusOpen()) closeStatusModal();
    openPostById(id, { origin: { label: 'Vault lint', onBack: reopenLintModal } });
  },
  // The pane's editor saved the post itself; this is the feed/sidebar half.
  onSaved: updated => {
    replaceCard(updated);
    refreshSidebarCounts();
    refreshLinks();
  },
});

async function loadFolders() {
  if (!authed) return;
  try {
    const [data, count] = await Promise.all([apiFetch('/folders'), postCount()]);
    renderFolders(data.folders, count);
  } catch {}
}

function renderFolders(folders, allCount) {
  tagList.innerHTML = '';
  tagList.appendChild(makeFolderItem('all', null, allCount));
  folders.forEach(f => tagList.appendChild(makeFolderItem(f.folder, f.folder, f.count)));
}

function makeFolderItem(label, value, count) {
  const row = sidebarRow({
    labelHtml: folderLabel(label, value !== null), count, className: 'tag-item folder-item',
    active: query.folder === value, onClick: () => selectFolder(value),
  });
  row.dataset.folder = value === null ? '__all__' : value;
  return row;
}

/** Point the feed at a tag or a folder (never both) and start it over. */
function setFilter({ tag = null, folder = null }) {
  closeSidebar();
  const tagChanged = query.tag !== tag;
  query.tag = tag; query.folder = folder; resetPaging();
  if (tagChanged) connectSSE();   // the stream is filtered by tag server-side
  syncSearchScope();
  feed.innerHTML = '';
  loadMoreWrap.style.display = 'none';
}

async function selectFolder(folder) {
  setFilter({ folder });
  const key = folder === null ? '__all__' : folder;
  tagList.querySelectorAll('.folder-item').forEach(el => markActiveRow(el, el.dataset.folder === key));
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
  const row = (folder, label, count) => sidebarRow({
    labelHtml: folderLabel(label, folder !== null), count, className: 'tag-item folder-item',
    active: attachFolder === folder, onClick: () => selectAttachFolder(folder),
  });
  tagList.replaceChildren(
    row(null, 'All files', attachCache.length),
    ...[...counts.keys()].sort().map(folder => row(folder, folder, counts.get(folder))),
  );
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
    // Normalised as the server stores it, so an active filter follows the rename.
    const newName = cleanTag(input.value);
    if (!newName || newName === oldName) { cancelRename(); return; }
    try {
      const data = await apiFetch(`/tags/${encodeURIComponent(oldName)}`, {
        method: 'PATCH', body: JSON.stringify({ new_name: newName }),
      });
      renderTags(data.tags, await postCount());
      if (query.tag === oldName) selectTag(newName);   // feed and live stream follow the rename
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

function startTagConfig(el, tagName) {
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
    <div class="tc-actions">
      <button type="button" class="tc-save">Save</button>
      <button type="button" class="tc-cancel">Cancel</button>
      ${current ? '<button type="button" class="tc-remove">Remove expiry</button>' : ''}
    </div>`;
  form.querySelector('.tc-label').textContent = current ? `Expiry for #${tagName}` : `Set an expiry for #${tagName}`;
  // What is set now — the form used to open empty whatever the tag carried.
  if (current?.ttl_hours) form.querySelector('.tc-ttl').value = current.ttl_hours;
  if (current?.expires_at) form.querySelector('.tc-expires').value = toDatetimeLocal(current.expires_at);
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
    // Both cleared on a tag that had an expiry means remove it; on one that
    // had none it means nothing was asked for.
    if (!ttlVal && !expiresVal) { if (current) { await save({}); } else { committed = false; cancel(); } return; }
    const body = {};
    if (ttlVal) body.ttl_hours = parseInt(ttlVal, 10);
    if (expiresVal) body.expires_at = toUtcIso(expiresVal);
    await save(body);
  }

  // `{}` removes the tag's config server-side (set_tag_config).
  async function save(body) {
    try {
      await apiFetch(`/tags/${encodeURIComponent(tagName)}/config`, {
        method: 'POST', body: JSON.stringify(body),
      });
      if (openTagEditor && openTagEditor.el === el) openTagEditor = null;
      await loadTags();
    } catch (e) { showToast(`Couldn’t save expiry for #${tagName}: ${e.message}`, { error: true }); committed = false; cancel(); }
  }

  function cancel() {
    if (committed) return;
    committed = true;
    if (openTagEditor && openTagEditor.el === el) openTagEditor = null;
    el.classList.remove('tag-editing');
    el.innerHTML = savedHtml;
    wireTagControls(el, tagName);
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

/* The active filter, shown beside the search box: a search runs inside it,
   and "No posts match" used to say nothing about the tag it had been limited
   to. Clicking the chip drops the filter. */
function syncSearchScope() {
  const label = query.tag ? `#${query.tag}` : query.folder ? `${query.folder}/` : '';
  searchScope.hidden = !label;
  searchScope.textContent = label ? `${label} ×` : '';
  searchScope.setAttribute('aria-label', label ? `Remove filter ${label}` : '');
  searchScope.title = label ? 'Show all posts' : '';
}
function clearScope() {
  if (query.tag) selectTag(null);
  else if (query.folder) selectFolder(null);
}
searchScope.addEventListener('click', clearScope);
feed.addEventListener('click', e => {
  if (e.target.closest('[data-action="clear-scope"]')) clearScope();
});

/** Empty-feed copy: says what was searched, where, and what to do next. */
function emptyFeedHtml() {
  const scope = query.tag ? `tagged #${escHtml(query.tag)}` : query.folder ? `in ${escHtml(query.folder)}/` : '';
  const widen = scope ? '<button type="button" class="empty-action" data-action="clear-scope">Search all posts</button>' : '';
  if (query.search) {
    const q = `“${escHtml(query.search)}”`;
    return scope ? `<p>No posts ${scope} match ${q}.</p>${widen}` : `<p>No posts match ${q}.</p>`;
  }
  if (scope) return `<p>No posts ${scope} yet.</p>${widen.replace('Search all posts', 'Show all posts')}`;
  return '<p>No posts yet. Write one with New Post, or publish from an agent over MCP or the API.</p>';
}

async function selectTag(tag) {
  setFilter({ tag });
  tagList.querySelectorAll('.tag-item').forEach(el => markActiveRow(el, el.dataset.tag === (tag ?? '')));
  await loadPosts(true);
}

/* ── Posts ────────────────────────────────────────────────── */
// Only the newest request may paint the feed. Without this a slow response
// landed after a newer one — clearing a search while it was still running
// repainted the stale results — and two overlapping "load more" requests read
// the same offset and appended the same page twice.
let loadSeq = 0;

async function loadPosts(replace = false) {
  if (!authed) return;
  const seq = ++loadSeq;
  try {
    const params = new URLSearchParams({ limit: LIMIT, offset: query.offset });
    params.set('sort', prefs.sortField);
    params.set('order', prefs.sortOrder);
    if (query.tag) params.set('tag', query.tag);
    if (query.folder) params.set('folder', query.folder);
    if (query.search) params.set('search', query.search);
    // Only meaningful alongside a search term — the server ignores mode without
    // one anyway, but omitting it here keeps a plain listing's request obviously
    // plain rather than carrying an inert param.
    if (query.search && query.mode !== 'keyword') params.set('mode', query.mode);
    const data = await apiFetch(`/posts?${params}`);
    if (seq !== loadSeq) return;
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
          ${emptyFeedHtml()}
        </div>`;
    } else {
      data.items.forEach((p, i) => {
        // Offsets drift when posts arrive or go while paging; a post already
        // on screen must not appear twice.
        if (!replace && feed.querySelector(`[data-id="${p.id}"]`)) return;
        const el = renderPost(p);
        if (replace) el.style.animationDelay = `${i * 35}ms`;
        feed.appendChild(el);
      });
    }
    loadMoreWrap.style.display = query.offset < query.total ? 'block' : 'none';
  } catch (e) {
    if (replace && seq === loadSeq) feed.innerHTML = `<div class="auth-prompt"><p>Could not load posts.</p><p>${escHtml(e.message)}</p></div>`;
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
  el.querySelector('.master-badge')?.setAttribute('aria-expanded', String(!collapsing));
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

/** Swap a card for a fresh render of `post`, keeping what its place in the
 *  feed gave it (the pinned master document stays pinned). */
function replaceCard(post) {
  const card = feed.querySelector(`[data-id="${post.id}"]`);
  if (!card) return null;
  const fresh = renderPost(post);
  if (card.classList.contains('pinned')) fresh.classList.add('pinned');
  card.replaceWith(fresh);
  return fresh;
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
    ? `<button type="button" class="master-badge" aria-expanded="false">✦ master document<span class="accordion-chevron" aria-hidden="true">▾</span></button>`
    : '';
  // A real link (the /id/<id> deep link), so a card is reachable by Tab and
  // opens in a new tab on Ctrl/Cmd-click; a plain click opens the modal as the
  // rest of the card does. The master document toggles in place instead.
  const titleHtml  = !post.title ? ''
    : post.id === 0 ? `<span class="post-title">${escHtml(post.title)}</span>`
    : `<a class="post-title" href="/id/${post.id}">${escHtml(post.title)}</a>`;
  const tagsHtml   = tagPills(post.tags);
  // Host only on a card — the full URL is its tooltip, and a link in the modal.
  const src        = post.source ? sourceParts(post.source) : null;
  const srcHtml    = src ? `<span class="post-source" title="${escHtml(post.source)}">via ${escHtml(src.label)}</span>` : '';
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
    contentToRender = stripTitleHeading(contentToRender);
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
    ? `<span class="post-media-count">${ICON_IMAGE} ${media.count}</span>`
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
        <button class="btn-edit" title="Edit">${ICON_PENCIL}<span class="btn-label">Edit</span></button>
        ${post.id === 0 ? '' : `<button class="btn-delete" title="Delete">${ICON_TRASH}<span class="btn-label">Delete</span></button>`}
      </div>
    </div>`;

  wrapTables(el);
  el.querySelectorAll('.tag-pill').forEach(pill =>
    pill.addEventListener('click', e => { e.stopPropagation(); selectTag(pill.dataset.tag); })
  );
  el.querySelector('a.post-title')?.addEventListener('click', e => {
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) { e.stopPropagation(); return; }   // browser's own
    e.preventDefault();   // bubble on to the card, which opens the modal
  });
  // A broken/unauthorised thumbnail just drops the media block (text stays).
  const mediaImg = el.querySelector('.post-media img');
  if (mediaImg) mediaImg.addEventListener('error', () => el.querySelector('.post-media')?.remove());
  el.querySelector('.btn-delete')?.addEventListener('click', (e) => {
    e.stopPropagation();
    deletePost(post);
  });
  el.querySelector('.btn-edit').addEventListener('click', (e) => { e.stopPropagation(); openEditModal(post); });
  if (post.id === 0) {
    // Master doc → inline accordion (collapsed by default → a few-line peek).
    // Clicking the card toggles it; body links/buttons still work via the guard.
    el.classList.add('accordion', 'collapsed');
    const wrap = el.querySelector('.post-body-wrap');
    wrap.style.maxHeight = MASTER_PEEK_PX + 'px';   // collapsed on first paint
    el.addEventListener('click', (e) => {
      if (e.target.closest('.master-badge')) { toggleMasterAccordion(el, wrap); return; }
      if (e.target.closest('a, button, .tag-pill, .post-actions')) return;
      toggleMasterAccordion(el, wrap);
    });
  } else {
    // A link inside the card (wikilink, external, attachment) is that link,
    // not the card — it used to open the card's post under the target too.
    el.addEventListener('click', e => { if (!e.target.closest('a:not(.post-title)')) openPostModal(post); });
  }
  return el;
}

/* ── Edit modal (the same form the lint pane hosts, edit-form.js) ─────────── */
const editModal = document.getElementById('editModal');
const emBody = document.getElementById('emBody');
const emTitle = document.getElementById('emTitle');
let editForm = null;   // { isDirty } from the current buildEditForm, or null

function closeEditModal() {
  editModal.classList.remove('open');
  emBody.innerHTML = '';
  editForm = null;
}

// × / backdrop / swipe / Escape ask first; the form's own Cancel asks itself.
wireModal(editModal, {
  close: closeEditModal,
  confirmDiscard: () => !editForm?.isDirty() || confirm('Discard your changes to this post?'),
});

function openEditModal(post) {
  editModal.classList.add('open');
  emTitle.textContent = `#${post.id}`;
  editForm = buildEditForm(emBody, post, {
    onCancel: closeEditModal,   // buildEditForm already confirmed the discard
    onSave: updated => {
      // The card is looked up rather than held: the feed may have re-rendered
      // (a filter, a sort, an SSE push) while the modal was open.
      replaceCard(updated);
      closeEditModal();
      if (isPostOpen()) openPostModal(updated, { pushHistory: false });
      refreshSidebarCounts();
      refreshLinks();   // a new title changes what [[links]] resolve to
    },
  });
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
    // A new post, or an edit (including one made outside relay). If it is the
    // post open in the modal, refresh that in place — only while it is open, so
    // a late event can't resurrect a modal just closed.
    if (_modalPost && _modalPost.id === post.id && isPostOpen())
      openPostModal(post, { pushHistory: false });
    const edited = replaceCard(post);   // an edit updates in place
    if (edited) {
      edited.classList.add('new');
    } else if (isDefaultSort() && !query.search && !query.folder) {
      const empty = feed.querySelector('.empty');
      if (empty) empty.remove();
      const el = renderPost(post);
      el.classList.add('new');
      const pinnedEl = feed.querySelector('.post.pinned');
      if (pinnedEl) pinnedEl.after(el); else feed.prepend(el);  // keep master on top
      query.total++;
      query.offset++;   // the server's list shifted too; the next page starts one later
      announce(`New post: ${post.title}`);
    } else {
      // Non-default sort, or a search/folder filter the stream can't apply (it
      // only filters by tag): the post may not belong here at all, let alone at
      // the top — count it and let the pill reload with the real filters.
      query.total++;
      bumpNewPostsPill();
      announce(`New post: ${post.title}`);
    }
    scheduleRefresh();
  });

  es.addEventListener('delete', e => {
    const { id } = JSON.parse(e.data);
    const card = feed.querySelector(`[data-id="${id}"]`);
    if (!card) return;            // idempotent: already gone (e.g. we deleted it)
    card.remove();
    query.total = Math.max(0, query.total - 1);
    query.offset = Math.max(0, query.offset - 1);   // or the next page skips a post
    if (_modalPost && _modalPost.id === id) closePostModal();
    scheduleRefresh();
  });

  es.onopen = () => {
    if (sseErrorTimer) { clearTimeout(sseErrorTimer); sseErrorTimer = null; }
    setDot('connected');
  };
  es.onerror = () => {
    if (sseErrorTimer) clearTimeout(sseErrorTimer);
    sseErrorTimer = setTimeout(() => setDot('error'), 3000);
    // EventSource retries on its own unless the server refused outright (a
    // 401 closes it for good) — then ask whether the session is still there.
    if (es?.readyState === EventSource.CLOSED) {
      fetch('/auth/me', { credentials: 'same-origin' }).then(r => r.json())
        .then(me => { if (!me.authenticated) signOut('Your session has ended — sign in again.'); })
        .catch(() => {});
    }
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
const pmBack     = document.getElementById('pmBack');
const pmTitle    = document.getElementById('pmTitle');
const pmMeta     = document.getElementById('pmMeta');
const pmBody     = document.getElementById('pmBody');
const pmBodyFade = document.getElementById('pmBodyFade');
const pmEdit     = document.getElementById('pmEdit');
const pmDelete   = document.getElementById('pmDelete');
const pmInner    = document.querySelector('.pm-inner');
let _modalPost        = null;
let _modalStack       = [];   // entries: { post, scrollTop }
let _historyDepth     = 0;
let _suppressPopstate = false;
// Where the modal was opened from when that wasn't another post — { label,
// onBack }, e.g. the lint pane. Once the followed-links stack is unwound, Back
// returns there instead of just closing. Survives any depth of link-following.
let _externalOrigin = null;
const isPostOpen = () => postModal.classList.contains('open');

function syncBackButton() {
  if (_modalStack.length === 0) {
    if (_externalOrigin) {
      pmBack.textContent = `← ${_externalOrigin.label}`;
      pmBack.style.display = 'inline-flex';
      pmInner.classList.add('has-back');
      return;
    }
    pmBack.style.display = '';
    pmBack.textContent = '← back';
    pmInner.classList.remove('has-back');
    return;
  }
  const { post } = _modalStack[_modalStack.length - 1];
  pmBack.textContent = `← ${postName(post)}`;
  pmBack.style.display = 'inline-flex';
  pmInner.classList.add('has-back');
}

function openPostModal(post, { pushHistory = true, origin } = {}) {
  if (pushHistory && _modalPost) {
    _modalStack.push({ post: _modalPost, scrollTop: pmBody.scrollTop });
    history.replaceState({ postId: _modalPost.id }, '');
    history.pushState({ postId: post.id }, '');
    _historyDepth++;
  } else if (pushHistory) {
    // The first post opened gets its own entry too, so Back (the Android
    // gesture, the browser button) closes the modal instead of leaving relay.
    history.pushState({ postId: post.id }, '');
    _historyDepth++;
  }
  hideLinkPreview();
  if (origin !== undefined) _externalOrigin = origin;   // only a fresh open passes one
  _modalPost = post;
  pmTitle.textContent = post.title || '';
  pmDelete.hidden = post.id === 0;   // the master document is undeletable
  pmTitle.style.display = post.title ? '' : 'none';

  const tagsHtml  = tagPills(post.tags);
  const src       = post.source ? sourceParts(post.source) : null;
  const srcHtml   = !src ? ''
    : src.href ? `<a class="post-source" href="${escHtml(src.href)}" target="_blank" rel="noopener noreferrer" title="${escHtml(post.source)}">via ${escHtml(src.label)}</a>`
    : `<span class="post-source">via ${escHtml(src.label)}</span>`;
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

  const pmContent = post.title ? stripTitleHeading(post.content) : post.content;
  pmBody.innerHTML = `<div class="post-body">${renderBody(pmContent)}</div><div class="pm-backlinks" id="pmBacklinks"></div>`;
  wrapTables(pmBody);
  pmBody.querySelectorAll('.post-body pre').forEach(pre => {
    const btn = document.createElement('button');
    btn.className = 'code-copy';
    btn.textContent = 'copy';
    btn.addEventListener('click', async () => {
      const ok = await copyText(pre.querySelector('code')?.textContent ?? pre.textContent);
      btn.classList.toggle('copied', ok); btn.textContent = ok ? 'copied' : 'copy failed';
      setTimeout(() => { btn.classList.remove('copied'); btn.textContent = 'copy'; }, 1500);
    });
    pre.appendChild(btn);
  });
  renderBacklinks(post.id);
  clearFeedFocus();

  postModal.classList.add('open');
  pmBody.scrollTop = 0;
  requestAnimationFrame(updateModalFade);
  syncBackButton();
}

/* The Clipboard API exists only in secure contexts, and relay is often served
   over plain HTTP on a LAN — there it falls back to a selected textarea. */
async function copyText(text) {
  try {
    if (navigator.clipboard) { await navigator.clipboard.writeText(text); return true; }
    const ta = Object.assign(document.createElement('textarea'), { value: text, readOnly: true });
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch { return false; }
}

function updateModalFade() {
  const atBottom = pmBody.scrollHeight - pmBody.scrollTop <= pmBody.clientHeight + 2;
  pmBodyFade.classList.toggle('hidden', atBottom);
}


/** Close and forget the whole session: followed links, origin, history. */
function closePostModal() {
  if (_historyDepth > 0) {
    _suppressPopstate = true;
    history.go(-_historyDepth);
  }
  resetPostModal();
}

/** The close itself, minus driving history — popstate calls this directly,
 *  since a history.go() there would fight the navigation in progress. */
function resetPostModal() {
  hideLinkPreview();
  _modalStack = [];
  _externalOrigin = null;
  _historyDepth = 0;
  _modalPost = null;
  postModal.classList.remove('open');
  pmBody.innerHTML = '';
  syncBackButton();
}

function popPostModal() {
  if (_modalStack.length === 0) {
    // Same "unwind one step" gesture that pops the in-modal stack below, just
    // leaving the post modal entirely for wherever the breadcrumb points —
    // the lint pane, currently — instead of a plain close.
    const origin = _externalOrigin;
    closePostModal();
    if (origin) origin.onBack();
    return;
  }
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
pmBack.addEventListener('click', popPostModal);
// × and Escape step back through followed links; backdrop and swipe close.
wireModal(postModal, { close: closePostModal, back: popPostModal });
// History opens over the post modal (which stays behind it), so returning from a
// revision leaves you where you were.
const pmHistory = document.getElementById('pmHistory');
pmHistory.addEventListener('click', () => {
  const post = _modalPost; if (!post) return;
  openPostHistory(post.id, post.title);
});
initPostHistory(() => { resetPaging(); loadPosts(true); });

pmEdit.addEventListener('click', () => { if (_modalPost) openEditModal(_modalPost); });
pmDelete.addEventListener('click', async () => {
  const post = _modalPost; if (!post) return;
  if (await deletePost(post)) closePostModal();
});

/* Delete, then offer Undo — not "Delete this post?" up front. With history on
   a delete is recoverable, so the confirm only taxed every deliberate delete
   while naming nothing (which post?). With history off it is final, and that
   is when to ask, by name. Returns whether the post was deleted. */
async function deletePost(post) {
  const name = postName(post);
  if (!historyOn && !confirm(`Delete “${name}”? Vault history is off, so this can’t be undone.`)) return false;
  try {
    await apiSend(`/posts/${post.id}`, { method: 'DELETE' });
  } catch (err) {
    showToast(`Couldn’t delete “${name}”: ${err.message}`, { error: true });
    return false;
  }
  const card = feed.querySelector(`[data-id="${post.id}"]`);
  if (card) { card.remove(); query.total--; query.offset = Math.max(0, query.offset - 1); }
  loadMoreWrap.style.display = query.offset < query.total ? 'block' : 'none';
  refreshSidebarCounts();
  showToast(`Deleted “${name}”.`, historyOn ? { action: { label: 'Undo', onClick: () => undoDelete(post) } } : {});
  return true;
}

async function undoDelete(post) {
  // The restorable revision is the one before the delete commit; the recovery
  // list already resolves it, so ask it rather than walk history here.
  const { items } = await apiFetch('/posts/deleted?limit=100');
  const gone = items.find(d => d.id === post.id);
  if (!gone) throw new Error('it is no longer in the recovery list');
  await apiFetch(`/posts/${post.id}/restore`, { method: 'POST', body: JSON.stringify({ sha: gone.sha }) });
  resetPaging(); loadPosts(true); refreshSidebarCounts(); refreshLinks();
  showToast(`Restored “${postName(post)}”.`);
}

/* ── Keyboard shortcuts modal ─────────────────────────────── */
const shortcutsModal = document.getElementById('shortcutsModal');
wireModal(shortcutsModal, { close: () => shortcutsModal.classList.remove('open') });

/* ── Feed keyboard focus ──────────────────────────────────── */
let _focusedCard = null;
const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)');

function getFeedCards() { return [...feed.querySelectorAll('.post')]; }

function setFocusedCard(card) {
  if (_focusedCard) _focusedCard.classList.remove('card-focused');
  _focusedCard = card;
  if (card) {
    card.classList.add('card-focused');
    card.scrollIntoView({ block: 'nearest', behavior: REDUCED_MOTION.matches ? 'auto' : 'smooth' });
  }
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
  // The body re-renders on navigation; a detached anchor measures as 0,0 and
  // nothing would ever fire its mouseout, so the preview stuck in the corner.
  if (!anchor.isConnected) return;
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
    const raw     = stripTitleHeading(post.content);
    // [[Target|alias]] reads as its alias, the way the body renders it.
    const snippet = raw.replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, '$2').replace(/[#*`_[\]]/g, '').trim();
    const clipped = snippet.length > 150 ? snippet.slice(0, 150) + '…' : snippet;
    el.innerHTML  =
      `<div class="lp-title">${escHtml(postName(post))}</div>` +
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
  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName) || e.target.isContentEditable;

  // Escape: the most transient thing first — the theme menu, then whichever
  // modal is on top (dialog.js), then the j/k selection.
  if (e.key === 'Escape') {
    if (isThemeMenuOpen()) closeThemeMenu();
    else if (!dismissTopModal()) clearFeedFocus();
    return;
  }

  // Single-key shortcuts only: Ctrl+J / Cmd+E belong to the browser and OS.
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;

  // Post modal shortcuts, while it is the modal on top.
  if (topModal() === postModal) {
    // preventDefault: the click moves focus into the editor's Title field
    // before the key's character is inserted, which typed an "e" into it.
    if (e.key === 'e') { e.preventDefault(); pmEdit.click(); return; }
    if (e.key === 'h') { e.preventDefault(); pmHistory.click(); return; }
  }

  // Global.
  if (e.key === '?') { e.preventDefault(); shortcutsModal.classList.toggle('open'); return; }

  // Feed navigation — only when no modal is open.
  if (!isThemeMenuOpen() && !anyModalOpen()) {
    if (e.key === 'j') { e.preventDefault(); moveFeedFocus(1);  return; }
    if (e.key === 'k') { e.preventDefault(); moveFeedFocus(-1); return; }
    // Not when Enter lands on a link or button: it activates that already, and
    // opening the j/k-selected card on top would stack a second post.
    if (e.key === 'Enter' && _focusedCard && !e.target.closest?.('a, button')) {
      e.preventDefault();
      openPostById(Number(_focusedCard.dataset.id));
      clearFeedFocus();
      return;
    }
  }
});

window.addEventListener('popstate', e => {
  if (_suppressPopstate) { _suppressPopstate = false; return; }
  if (e.state?.postId != null) {   // != null: the master document is #0
    if (_modalStack.length > 0) {
      const { post, scrollTop } = _modalStack.pop();
      _historyDepth--;
      openPostModal(post, { pushHistory: false });
      requestAnimationFrame(() => { pmBody.scrollTop = scrollTop; });
    } else {
      // Forward into a post's entry: that entry already exists, so reopening
      // it must not push another one.
      _historyDepth = Math.max(0, _historyDepth - 1);
      openPostById(e.state.postId, { pushHistory: false });
    }
  } else {
    resetPostModal();
  }
});

// Kick off: restore an existing session (cookie) or show the login control.
bootstrap();
