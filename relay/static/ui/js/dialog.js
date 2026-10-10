/* Every modal's shared behaviour, wired once per modal by `wireModal`.
 *
 * - Dismissal: the × button, the backdrop, the mobile swipe (./sheet.js) and
 *   Escape — Escape always goes to the top-most open modal, so a panel stacked
 *   on another (edit over a post, lint over status) closes first.
 * - Unsaved work: given `confirmDiscard`, every one of those paths asks first.
 * - Dialog semantics: `role="dialog"`, `aria-modal`, a label, focus moved in on
 *   open, Tab kept inside, and focus handed back to whatever had it on close.
 *
 * Open/closed is the `open` class the modules already toggle; this watches it,
 * so nothing else needs to know about focus or stacking.
 */

import { attachSheetDismiss } from './sheet.js';

const FOCUSABLE = [
  'a[href]', 'button:not([disabled])', 'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])', 'textarea:not([disabled])', '[tabindex]:not([tabindex="-1"])',
].join(',');

const stack = [];               // open modals, most recently opened last
const returnTo = new Map();     // modal -> element focused before it opened
const dismissers = new Map();   // modal -> what × and Escape do
const closers = new Map();      // modal -> its unconditional close

const panelOf = (modal) => modal.querySelector('.pm-inner, .sm-inner');
const visible = (node) => node.getClientRects().length > 0;

/**
 * @param {HTMLElement} modal  the `.status-modal` / `.post-modal` element
 * @param {object} opts
 * @param {() => void} opts.close            closes for good, no questions
 * @param {() => boolean} [opts.confirmDiscard]  false vetoes a user dismissal
 * @param {() => void} [opts.back]           what × and Escape do instead of
 *        closing (the post modal steps back through followed links)
 * @returns {() => void} the guarded close — asks `confirmDiscard`, then closes
 */
export function wireModal(modal, { close, confirmDiscard, back }) {
  const tryClose = () => { if (!confirmDiscard || confirmDiscard()) close(); };
  const dismiss = back || tryClose;
  const panel = panelOf(modal);
  const backdrop = modal.querySelector('.pm-backdrop');

  modal.querySelector('.sm-close, .pm-close')?.addEventListener('click', dismiss);
  backdrop?.addEventListener('click', tryClose);
  attachSheetDismiss({
    inner: panel,
    handle: modal.querySelector('.sm-head, .pm-header'),
    backdrop,
    canDismiss: confirmDiscard,
    onDismiss: close,
  });
  dismissers.set(modal, dismiss);
  closers.set(modal, close);

  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  panel.tabIndex = -1;
  const label = modal.querySelector('.pm-title, .sm-name');
  if (label) {
    label.id ||= `${modal.id}Label`;
    panel.setAttribute('aria-labelledby', label.id);
  }
  new MutationObserver(() => (modal.classList.contains('open') ? opened : closed)(modal))
    .observe(modal, { attributes: true, attributeFilter: ['class'] });
  return tryClose;
}

export const anyModalOpen = () => stack.length > 0;
export const topModal = () => stack[stack.length - 1] || null;

/** Escape: dismiss the top-most modal. Returns whether there was one. */
export function dismissTopModal() {
  const top = topModal();
  if (top) dismissers.get(top)();
  return !!top;
}

/** Close every open modal, no questions asked (the session ended). */
export function closeAllModals() {
  for (const modal of [...stack].reverse()) closers.get(modal)();
}

function opened(modal) {
  if (stack.includes(modal)) return;
  stack.push(modal);
  const active = document.activeElement;
  if (active && active !== document.body && !modal.contains(active)) returnTo.set(modal, active);
  // The opener may already have placed focus (the edit form focuses Title);
  // otherwise land on the panel, which a screen reader announces by its label.
  if (!modal.contains(document.activeElement)) panelOf(modal).focus({ preventScroll: true });
}

function closed(modal) {
  const at = stack.indexOf(modal);
  if (at === -1) return;
  stack.splice(at, 1);
  const back = returnTo.get(modal);
  returnTo.delete(modal);
  // Only reclaim focus that was inside this modal (or dropped to <body> when its
  // content was torn down) — never steal it from wherever the user went next.
  const active = document.activeElement;
  if (active && active !== document.body && !modal.contains(active)) return;
  const top = topModal();
  if (back?.isConnected && visible(back) && (!top || top.contains(back))) back.focus({ preventScroll: true });
  else if (top) panelOf(top).focus({ preventScroll: true });
}

// Tab cycles inside the top-most open modal instead of escaping behind it.
document.addEventListener('keydown', (e) => {
  const top = topModal();
  if (e.key !== 'Tab' || !top) return;
  const panel = panelOf(top);
  const items = [...panel.querySelectorAll(FOCUSABLE)].filter(visible);
  const first = items[0], last = items[items.length - 1];
  const active = document.activeElement;
  if (!items.length) { e.preventDefault(); panel.focus(); }
  else if (!panel.contains(active)) { e.preventDefault(); first.focus(); }
  else if (e.shiftKey && (active === first || active === panel)) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && active === last) { e.preventDefault(); first.focus(); }
});
