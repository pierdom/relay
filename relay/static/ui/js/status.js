/* Status / about panel.
 *
 * Surfaces GET /status. The counts are incidental; the point is the health block,
 * which reports what is *working* — relay can quietly lose vault history (no git),
 * full-text search (no FTS5), or external-edit pickup (watcher off), and none of
 * that is otherwise visible from the browser.
 *
 * Self-contained: it owns its own elements and needs nothing from main.js beyond
 * apiFetch, which is why it was among the first modules lifted out.
 */

import { apiFetch } from './api.js';
import { fmtBytes, fmtUptime } from './util.js';
import { attachSheetDismiss } from './sheet.js';
import { fetchDeleted, recoverableCount, renderDeleted } from './deleted.js';

// ── Status / about modal ─────────────────────────────────────────────────────
const statusModal = document.getElementById('statusModal');
const statusBtn = document.getElementById('statusBtn');
const smBody = document.getElementById('smBody');
const smVersion = document.getElementById('smVersion');


// Built with textContent throughout — no innerHTML for server-provided values
// like the vault path.
function smSection(title, node) {
  const wrap = document.createElement('div');
  const h = document.createElement('div');
  h.className = 'sm-section-title';
  h.textContent = title;
  wrap.appendChild(h);
  wrap.appendChild(node);
  return wrap;
}

function smRows(pairs) {
  const dl = document.createElement('dl');
  dl.className = 'sm-rows';
  for (const [label, value] of pairs) {
    const dt = document.createElement('dt'); dt.textContent = label;
    const dd = document.createElement('dd'); dd.textContent = value;
    dl.append(dt, dd);
  }
  return dl;
}

// `display: contents` on the row, so the dot+label and the note land in the same
// two grid columns `.sm-rows` uses. Health used to be a flex row with the note
// pushed right by `margin-left: auto`, which put the value at the far edge of a
// 560px panel while Vault and Server sat their values next to the label — two
// alignment systems in one panel, and the reason it read as unbalanced.
function smFeature(label, state, note) {
  const row = document.createElement('div');
  row.className = 'sm-feat';
  const name = document.createElement('span');
  name.className = 'sm-feat-name';
  const dot = document.createElement('span');
  dot.className = `sm-dot ${state}`;
  const text = document.createElement('span');
  text.textContent = label;
  name.append(dot, text);
  const hint = document.createElement('span');
  hint.className = 'sm-feat-note';
  hint.textContent = note;
  row.append(name, hint);
  return row;
}

/* Recovery lives here because this is the panel that already answers "does
 * vault history work". When it does not, there is nothing to recover, and this
 * section says so rather than offering a button that cannot help. */
function renderRecovery(historyWorks) {
  const wrap = document.createElement('div');
  const line = document.createElement('div');
  line.className = 'sm-recovery-line';
  wrap.appendChild(line);

  if (!historyWorks) {
    line.textContent = 'Vault history is off — deleted posts cannot be recovered.';
    return smSection('Recovery', wrap);
  }

  line.textContent = 'Checking…';
  const btn = document.createElement('button');
  btn.className = 'btn-edit';
  btn.id = 'smBrowseDeleted';
  btn.textContent = 'Browse deleted →';
  btn.disabled = true;
  wrap.appendChild(btn);

  // One request serves both the headline and the browser; the headline excludes
  // TTL expiries, which are routine, while the browser offers them as a filter.
  fetchDeleted().then(list => {
    const n = recoverableCount(list);
    line.textContent = n
      ? `${n} deleted post${n === 1 ? '' : 's'} can be restored`
      : 'Nothing deleted — or nothing left to recover.';
    btn.disabled = !list.length;
    btn.onclick = () => showDeleted();
  }).catch(() => {
    line.textContent = 'Could not read deleted posts.';
  });

  return smSection('Recovery', wrap);
}

/** Drill down into the recovery browser, replacing the panel's contents.
 *
 * The panel keeps its 560px width: the cards are built for it (the full path is
 * dropped — the title *is* the filename, so only the folder adds anything), and
 * a modal that changes size as you navigate inside it is the jump the history
 * panel's fixed height exists to prevent. */
function showDeleted() {
  renderDeleted(smBody, { onBack: openStatusModal });
}

function renderStatus(d) {
  smVersion.textContent = d.version;
  smBody.innerHTML = '';

  const health = document.createElement('div');
  health.className = 'sm-rows sm-health';
  const h = d.features.history;
  health.appendChild(smFeature(
    'Vault history',
    h.effective ? 'ok' : 'bad',                       // not recoverable is a fault, not a warning
    h.effective ? `git ${h.git}` : (h.git ? 'disabled' : 'git missing'),
  ));
  health.appendChild(smFeature(
    'Full-text search',
    d.features.search.fts5 ? 'ok' : 'warn',           // degraded but still functional
    d.features.search.fts5 ? 'FTS5' : 'substring fallback',
  ));
  health.appendChild(smFeature(
    'Semantic search',
    d.features.search.embeddings ? 'ok' : 'off',      // off by default everywhere — not a fault or a degradation
    d.features.search.embeddings ? 'enabled' : 'disabled (proof of concept)',
  ));
  health.appendChild(smFeature(
    'External edits',
    d.features.watcher.running ? 'ok' : 'warn',
    d.features.watcher.running ? 'watching' : (d.features.watcher.enabled ? 'not running' : 'disabled'),
  ));
  smBody.appendChild(smSection('Health', health));

  const v = d.vault;
  smBody.appendChild(smSection('Vault', smRows([
    ['Path', v.path],
    ['Posts', String(v.posts)],
    ['Tags', String(v.tags)],
    ['Folders', String(v.folders)],
    ['Attachments', `${v.attachments} (${fmtBytes(v.attachment_bytes)})`],
  ])));

  smBody.appendChild(smSection('Server', smRows([
    ['Uptime', fmtUptime(d.uptime_seconds)],
    ['Started', d.started_at || '—'],
    ['Live clients', String(d.sse_clients)],
    ['OIDC login', d.features.auth.oidc ? 'enabled' : 'off'],
    ['MCP OAuth', d.features.auth.mcp_oauth ? 'enabled' : 'off'],
  ])));

  // Last, and deliberately so. Health/Vault/Server are what you open this panel
  // *for*; recovery is the one section that acts rather than reports, and it is
  // reached on purpose rather than stumbled into. It still belongs in this panel
  // because Health above it is what decides whether recovery is possible at all.
  smBody.appendChild(renderRecovery(h.effective));
}

async function openStatusModal() {
  statusModal.classList.add('open');
  document.body.style.overflow = 'hidden';
  smVersion.textContent = '';
  smBody.innerHTML = '';
  const loading = document.createElement('div');
  loading.className = 'sm-section-title';
  loading.textContent = 'loading…';
  smBody.appendChild(loading);
  try {
    renderStatus(await apiFetch('/status'));
  } catch (err) {
    smBody.innerHTML = '';
    const e = document.createElement('div');
    e.className = 'sm-error';
    e.textContent = `Could not load status: ${err.message}`;
    smBody.appendChild(e);
  }
}

export function closeStatusModal() {
  statusModal.classList.remove('open');
  document.body.style.overflow = '';
  smBody.innerHTML = '';
}

// Exposed so main.js's single Escape handler can keep giving this panel priority
// over the post modal, exactly as it did when both lived in one scope.
export function isStatusOpen() {
  return statusModal.classList.contains('open');
}

statusBtn.onclick = openStatusModal;
document.getElementById('smClose').onclick = closeStatusModal;
const smBackdrop = document.getElementById('smBackdrop');
smBackdrop.onclick = closeStatusModal;
attachSheetDismiss({
  inner: statusModal.querySelector('.sm-inner'),
  handle: statusModal.querySelector('.sm-head'),
  backdrop: smBackdrop,
  onDismiss: closeStatusModal,
});
