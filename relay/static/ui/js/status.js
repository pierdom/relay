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
import { fetchLint, issueCount, openLintModal } from './lint.js';

/** The two pieces of /status main.js's init() gates UI on, fetched together so
 * startup makes one request instead of two: whether mode='semantic'/'hybrid'
 * is actually usable (relay #253, proof of concept, off by default
 * everywhere — a control for a search mode that might not work is worse than
 * not offering it), and the caller's own effective scope (relay #198, B-8),
 * which main.js uses to gate Compose/Save. Both fall back to their safest
 * default (no ranked-search control, no assumed scope) on any fetch failure. */
export async function fetchInitStatus() {
  try {
    const d = await apiFetch('/status');
    return { embeddingsEnabled: !!d.features?.search?.embeddings, caller: d.caller || null };
  } catch {
    return { embeddingsEnabled: false, caller: null };
  }
}

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

/** "never run" / "running — N/M checked" / "N/M checked, finished <time>" —
 * the three states vault.backfill_status() (relay #253) can report. */
function fmtBackfill(b) {
  if (b.running) return `running — ${b.checked}/${b.total} checked`;
  if (b.completed_at) return `${b.checked}/${b.total} checked, finished ${b.completed_at}`;
  return 'never run';
}

/* The health block above only ever answers on/off. Every production question
 * this session's embedding work actually hit — which model, is the backfill
 * still going, how much of the vault is covered — needed a shell or a log
 * tail before /status grew the `embeddings` object (relay #253, v1.2.1) to
 * answer them directly. This section is that object, plus the two controls
 * (v1.3.0) that used to mean editing .env and restarting: pause/resume, and
 * re-trigger a catch-up on demand. */
function renderEmbeddings(e) {
  const wrap = document.createElement('div');
  wrap.appendChild(smRows([
    ['Model', e.model || '—'],
    ['Dimension', e.dimension != null ? `${e.dimension}d` : '—'],
    ['Model size', e.model_size_mb != null ? `${e.model_size_mb} MB` : '—'],
    ['Backend', e.backend_loaded ? 'loaded (resident)' : 'unloaded'],
    ['Idle unload', e.idle_unload_seconds > 0 ? `${e.idle_unload_seconds}s` : 'never'],
    ['Threads', String(e.threads)],
    ['Coverage', `${e.posts_embedded} / ${e.posts_total} posts (${e.posts_missing} missing)`],
    ['Chunks', String(e.chunks_total)],
    ['Cache entries', String(e.cache_entries)],
    ['Backfill', fmtBackfill(e.backfill)],
  ]));

  const err = document.createElement('div');
  err.className = 'sm-error';
  err.style.display = 'none';

  // Both actions re-fetch and re-render the whole panel on success rather than
  // patching this section alone — the health dot above and the Vault section's
  // post count can move too (an enable auto-triggers a backfill; the toggle
  // itself flips search.embeddings). The action and the refresh are caught
  // separately: fn() failing means nothing happened, so show the real error
  // and let the button be clicked again. refresh() failing means the action
  // *did* happen but this copy of the panel doesn't know it yet — re-enabling
  // the button there would invite a second, opposite-direction click against
  // state the user can no longer see, so it stays disabled and says so.
  const runAction = async (btn, fn) => {
    err.style.display = 'none';
    btn.disabled = true;
    try {
      await fn();
    } catch (ex) {
      err.textContent = ex.message;
      err.style.display = '';
      btn.disabled = false;
      return;
    }
    try {
      renderStatus(await apiFetch('/status'));
    } catch {
      err.textContent = 'Done, but the panel could not refresh — close and reopen it to see the latest state.';
      err.style.display = '';
    }
  };

  const toggleBtn = document.createElement('button');
  toggleBtn.className = 'btn-edit';
  toggleBtn.textContent = e.enabled ? 'Turn off' : 'Turn on';
  toggleBtn.title = e.enabled
    ? 'Pause semantic search until re-enabled or restarted'
    : 'Resume — a restart is not needed';
  toggleBtn.onclick = () => runAction(toggleBtn, () => apiFetch('/embeddings', {
    method: 'PATCH',
    body: JSON.stringify({ enabled: !e.enabled }),
  }));

  const backfillBtn = document.createElement('button');
  backfillBtn.className = 'btn-edit';
  backfillBtn.textContent = e.backfill.running ? 'Running…' : 'Re-run backfill';
  backfillBtn.disabled = !e.available || e.backfill.running;
  backfillBtn.title = e.available
    ? 'Re-embed anything the content-addressed cache does not already cover'
    : 'Enable semantic search first';
  backfillBtn.onclick = () => runAction(backfillBtn, () => apiFetch('/embeddings/backfill', { method: 'POST' }));

  const controls = document.createElement('div');
  controls.className = 'sm-embed-controls';
  controls.append(toggleBtn, backfillBtn);
  wrap.append(controls, err);

  return smSection('Semantic search', wrap);
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

/* The check itself is cheap (one pass over `posts` plus a few follow-up
 * queries — see relay/lint.py), so it runs on every panel open rather than on
 * demand behind a button. Skipped rules (a disabled feature, e.g. no
 * embeddings) fold into the headline count so a quiet vault doesn't need a
 * second look to notice something didn't run at all. */
function renderLintSummary() {
  const wrap = document.createElement('div');
  const line = document.createElement('div');
  // Not `.sm-recovery-line`: tests/ui/test_status_panel.py selects that class
  // expecting the Recovery section's line uniquely, and this section now sits
  // earlier in the DOM — a shared class would make it grab this one instead.
  line.className = 'lint-summary-line';
  wrap.appendChild(line);

  line.textContent = 'Checking…';
  const btn = document.createElement('button');
  btn.className = 'btn-edit';
  btn.id = 'smBrowseLint';
  btn.textContent = 'Browse issues →';
  btn.disabled = true;
  wrap.appendChild(btn);

  fetchLint().then(r => {
    const n = issueCount(r);
    let text = n
      ? `${n} issue${n === 1 ? '' : 's'} across ${r.checked_posts} posts`
      : `No issues — ${r.checked_posts} post${r.checked_posts === 1 ? '' : 's'} checked`;
    if (r.skipped_rules.length) {
      text += ` (${r.skipped_rules.length} rule${r.skipped_rules.length === 1 ? '' : 's'} skipped)`;
    }
    line.textContent = text;
    btn.disabled = false;
    // A separate modal (lint.js), not a drill-down replacing this panel's own
    // body — a finding needs its own two-pane list+preview, and stacking a
    // second modal on top (closed on its own) is what lets Escape or its
    // close button bring you back here instead of the main feed.
    btn.onclick = () => openLintModal();
  }).catch(() => {
    line.textContent = 'Could not run the vault lint.';
  });

  return smSection('Vault lint', wrap);
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

/* Always shown, even for full access — the panel should never have a
 * conspicuous gap where "what can I do here" belongs. Placed right after
 * Health: it answers the same kind of question (what's true about this
 * session right now), ahead of the feature-level diagnostics below it. */
function renderAccess(caller) {
  if (!caller) return null;
  if (caller.mode === 'full') {
    return smSection('Access', smRows([['Access', 'Full access']]));
  }
  if (caller.mode === 'read') {
    return smSection('Access', smRows([['Access', 'Read-only']]));
  }
  const tags = caller.tags && caller.tags.length ? caller.tags.join(', ') : '(none — every write will be denied)';
  return smSection('Access', smRows([
    ['Access', 'Write — restricted to tags'],
    ['Allowed tags', tags],
  ]));
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
  const access = renderAccess(d.caller);
  if (access) smBody.appendChild(access);
  smBody.appendChild(renderEmbeddings(d.embeddings));

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

  smBody.appendChild(renderLintSummary());

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
