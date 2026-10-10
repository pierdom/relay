/* Vault lint (relay #198, N-5): GET /lint rendered as its own two-pane modal.
 *
 * Checks the vault against the rules already written down in #0 — tag axes,
 * folder placement, the H1/title convention, broken cross-links, embedding
 * coverage — instead of relying on someone reading every post. See
 * relay/lint.py for the rule set and what each one means.
 *
 * Structure mirrors post-history.js: a fixed-height shell with two panes
 * built once, list on the left and a detail pane on the right, so selecting
 * a finding never resizes the panel or loses the one on screen a moment ago.
 * The pane hosts the *editor* (buildEditForm, shared with the standalone Edit
 * modal — see ./edit-form.js) rather than a read-only preview: the whole
 * point of this panel is fixing what a finding flags, and a preview plus a
 * button that left the list behind (an even earlier version's design) meant
 * doing that always cost you your place in it. "Open post" survives as a
 * smaller, secondary action for what the inline editor doesn't cover —
 * Delete, History, backlinks.
 */

import { apiFetch } from './api.js';
import { wireModal } from './dialog.js';
import { el, note, placeholder, setPane } from './dom.js';
import { buildEditForm, scrollToMatch } from './edit-form.js';

const lintModal = document.getElementById('lintModal');
const lmVersion = document.getElementById('lmVersion');
const lmBody = document.getElementById('lmBody');

// Fixed, readable order — not sorted by count, so the chip row doesn't
// reshuffle every time the vault changes underneath it.
const RULE_LABELS = {
  zero_tags: 'Zero tags',
  missing_domain_tag: 'Missing domain tag',
  missing_type_tag: 'Missing type tag',
  stale_inbox: 'Stuck in Inbox',
  h1_missing: 'H1 missing',
  h1_title_mismatch: 'H1 / title mismatch',
  broken_link: 'Broken link',
  link_to_deleted_post: 'Links to a deleted post',
  broken_attachment_embed: 'Broken attachment embed',
  wikilink_to_filename: 'Wikilink to a filename',
  master_doc_post_count: '#0 post count is stale',
  stale_last_updated: 'Stale hub/plan',
  zero_backlinks: 'Zero backlinks',
  zero_chunks: 'Zero embedding chunks',
  empty_tag_config: 'Unused tag config',
};

let report = null;
let filter = null;         // null = every rule
let selected = null;       // the finding currently shown in the pane
let currentEditHandle = null;   // { isDirty } from the pane's current buildEditForm
let onOpenPost = () => {};
let onPostSaved = () => {};

/** main.js supplies the callbacks that leave this modal to open the real
 * post ("Open post"), and that refresh the feed/sidebar after a save made
 * from inside this pane's editor. */
export function initLint({ openPost, onSaved }) {
  onOpenPost = openPost;
  onPostSaved = onSaved;
}

/** True unless the pane's editor has unsaved changes the user declines to
 * throw away. Guards every way the pane's content can change out from under
 * an in-progress edit: picking another finding, switching filters, and
 * closing the modal outright. */
function confirmDiscardCurrentEdit() {
  return !currentEditHandle?.isDirty() || confirm('Discard your changes to this post?');
}

/** Pure close — no confirmation (wireModal asks first on user dismissals). */
function closeLintModal() {
  lintModal.classList.remove('open');
  lmBody.innerHTML = '';
  currentEditHandle = null;
  // report/filter/selected deliberately survive a close — reopenLintModal
  // (the post modal's "← Vault lint" breadcrumb) puts you back exactly where
  // you left off rather than a fresh full list. openLintModal (the status
  // panel's own entry point) resets them itself before this ever matters.
}

/** Close, asking first if the pane's editor was touched. Returns whether it
 * actually closed, so "Open post" can bail out if the user declined. */
export function tryCloseLintModal() {
  if (!confirmDiscardCurrentEdit()) return false;
  closeLintModal();
  return true;
}

/** Run the lint. Exported separately so the status panel's headline count
 * can fetch it without opening this modal. */
export async function fetchLint() {
  return apiFetch('/lint');
}

export function issueCount(r) {
  return r ? r.items.length : 0;
}

/* The panes are built once and only their contents change afterward — a
 * fixed-height shell, same as the history modal's buildLayout. */
function buildLayout() {
  lmBody.innerHTML = '';
  const filters = el('div', 'lm-filters');
  const layout = el('div', 'lm-layout');
  const list = el('div', 'lm-list');
  const pane = el('div', 'lm-pane');
  layout.append(list, pane);
  lmBody.append(filters, layout);
  return { filters, list, pane };
}

function renderFilters(panes) {
  const { filters, list, pane } = panes;
  filters.innerHTML = '';
  if (report.skipped_rules.length) {
    filters.appendChild(note(`Skipped — ${report.skipped_rules.join('; ')}`, 'lint-skipped'));
  }
  const counts = {};
  for (const i of report.items) counts[i.rule] = (counts[i.rule] || 0) + 1;

  const bar = el('div', 'lm-chips');
  const chip = (key, label, count) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'lm-filter' + (filter === key ? ' active' : '');
    b.textContent = `${label} ${count}`;
    b.addEventListener('click', () => {
      if (!confirmDiscardCurrentEdit()) return;
      filter = key;
      renderList(panes);
    });
    return b;
  };
  bar.appendChild(chip(null, 'all', report.items.length));
  for (const [rule, label] of Object.entries(RULE_LABELS)) {
    if (counts[rule]) bar.appendChild(chip(rule, label, counts[rule]));
  }
  filters.appendChild(bar);
}

function findingRow(item, panes) {
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'hm-rev lm-finding';
  if (item.post_id != null) row.dataset.postId = String(item.post_id);

  const head = el('span', 'lm-finding-head');
  const dot = el('span', `sm-dot ${item.severity === 'error' ? 'bad' : 'warn'}`);
  const rule = el('span', 'hm-msg');
  // occurrences > 1: the same broken target mentioned several times in one
  // post is one finding, not one per mention (relay #198 N-5 follow-up) —
  // the count still needs to be visible somewhere, or "fixed the one ref"
  // could leave the other mentions behind unnoticed.
  rule.textContent = (RULE_LABELS[item.rule] || item.rule) + (item.occurrences > 1 ? ` (×${item.occurrences})` : '');
  head.append(dot, rule);

  const sub = el('span', 'hm-meta', item.post_id != null ? `#${item.post_id} ${item.title || ''}`.trim() : 'vault-wide');

  row.append(head, sub);
  row.addEventListener('click', () => {
    if (!confirmDiscardCurrentEdit()) return;
    selectFinding(item, row, panes);
  });
  return row;
}

function renderList(panes) {
  const { list, pane } = panes;
  // A fix can clear the last finding of the filtered rule; its chip then goes
  // away, and a filter nobody can see or unset would leave an empty list.
  if (filter && !report.items.some(i => i.rule === filter)) filter = null;
  renderFilters(panes);
  list.innerHTML = '';

  const rows = filter ? report.items.filter(i => i.rule === filter) : report.items;
  if (!rows.length) {
    list.appendChild(note(
      report.items.length
        ? 'No matches.'
        : `Nothing found — ${report.checked_posts} post${report.checked_posts === 1 ? '' : 's'} checked.`,
    ));
    setPane(pane, placeholder('Nothing to show.'));
    selected = null;
    currentEditHandle = null;
    return;
  }
  const rowEls = rows.map(item => {
    const row = findingRow(item, panes);
    list.appendChild(row);
    return row;
  });

  // Keep whatever was selected in view (e.g. after a filter click) if it is
  // still in the filtered list; otherwise fall back to the first row rather
  // than leaving the pane pointed at something no longer listed.
  const idx = selected ? rows.indexOf(selected) : -1;
  const targetIdx = idx === -1 ? 0 : idx;
  selectFinding(rows[targetIdx], rowEls[targetIdx], panes);
}

/** Detail box for the finding itself — kept visible above the editor so the
 * reason you opened this pane never scrolls out of sight. */
function findingDetail(item) {
  const box = el('div', `lint-card lint-${item.severity}`);
  const head = el('div', 'del-head');
  const dot = el('span', `sm-dot ${item.severity === 'error' ? 'bad' : 'warn'}`);
  const rule = el('span', 'lint-rule', RULE_LABELS[item.rule] || item.rule);
  head.append(dot, rule);
  const detail = el('div', 'lint-detail', item.detail);
  box.append(head, detail);
  return box;
}

async function selectFinding(item, row, panes) {
  currentEditHandle = null;
  selected = item;
  for (const el of panes.list.querySelectorAll('.lm-finding.active')) el.classList.remove('active');
  row.classList.add('active');

  const { pane } = panes;
  if (item.post_id == null) {
    setPane(pane, findingDetail(item), placeholder('This finding is not about one specific post.'));
    return;
  }

  setPane(pane, findingDetail(item), placeholder('loading…'));
  try {
    const post = await apiFetch(`/posts/${item.post_id}`);
    // Another row can be picked while this fetch is in flight; a stale
    // response landing after that must not clobber the pane it left behind.
    if (selected !== item) return;

    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'lm-open';
    open.textContent = 'Open post →';
    open.addEventListener('click', () => onOpenPost(post.id));

    const editorWrap = el('div', 'lm-editor');
    setPane(pane, findingDetail(item), open, editorWrap);
    currentEditHandle = buildEditForm(editorWrap, post, {
      // The pane opens on every row click, not on a deliberate "start
      // editing" action — autofocus would steal the keyboard from someone
      // who is still just browsing the list.
      focus: false,
      onSave: updated => handleSaved(item, updated, panes),
      // buildEditForm already confirmed the discard before calling this.
      // Re-rendering the list reloads the same finding fresh rather than
      // trying to reset the form fields by hand.
      onCancel: () => renderList(panes),
    });
    // broken_link/link_to_deleted_post carry the exact broken text (relay.lint's
    // `match`) — jump straight to it instead of leaving it to be found by eye
    // in a long post. Other rules have no single in-content spot to point at,
    // so `item.match` is absent and this is a no-op (the `focus: false` above
    // stands, and the pane opens without stealing the keyboard).
    scrollToMatch(editorWrap, item.match);
  } catch (err) {
    if (selected !== item) return;
    setPane(pane, findingDetail(item), placeholder(`Could not load #${item.post_id}: ${err.message}`));
  }
}

async function handleSaved(item, updated, panes) {
  onPostSaved(updated);
  try {
    report = await fetchLint();
  } catch {
    // The save itself already succeeded; keep serving the stale report
    // rather than losing the list entirely over a second request failing.
    report = report ?? { items: [], checked_posts: 0, skipped_rules: [] };
  }
  lmVersion.textContent = `${report.items.length} issue${report.items.length === 1 ? '' : 's'}`;
  // Prefer another finding still pointing at the same post — likely the next
  // thing worth fixing — before falling back to the first remaining row.
  selected = report.items.find(i => i.rule === item.rule && i.post_id === item.post_id)
          || report.items.find(i => i.post_id === item.post_id)
          || null;
  renderList(panes);
}

function showModal() {
  lintModal.classList.add('open');
}

/** Entry point from the status panel's "Browse issues" button — always a
 * fresh run: resets any filter/selection left over from a previous visit. */
export async function openLintModal() {
  filter = null;
  selected = null;
  showModal();
  lmVersion.textContent = '';
  const panes = buildLayout();
  panes.list.appendChild(note('loading…'));
  setPane(panes.pane, placeholder('Select a finding to see its detail.'));
  try {
    report = await fetchLint();
    lmVersion.textContent = `${report.items.length} issue${report.items.length === 1 ? '' : 's'}`;
    renderList(panes);
  } catch (err) {
    panes.list.innerHTML = '';
    panes.list.appendChild(note(`Could not load the lint report: ${err.message}`, 'sm-error'));
    setPane(panes.pane, placeholder(''));
  }
}

/** Entry point from the post modal's "← Vault lint" breadcrumb (main.js,
 * wired via initLint below): back to exactly the filter and finding a prior
 * "Open post" left off on, not a fresh full list — restating the report
 * rather than re-fetching it, since nothing about the vault's lint state
 * changed just from looking at one post. Falls back to a fresh run if there
 * is nothing to restore (e.g. the tab was reloaded in between). */
export function reopenLintModal() {
  if (!report) { openLintModal(); return; }
  showModal();
  const panes = buildLayout();
  lmVersion.textContent = `${report.items.length} issue${report.items.length === 1 ? '' : 's'}`;
  renderList(panes);
}

wireModal(lintModal, { close: closeLintModal, confirmDiscard: confirmDiscardCurrentEdit });
