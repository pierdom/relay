/* Transient notices at the bottom of the page — "Deleted …" with Undo, and
 * failures that used to be alert() boxes.
 *
 * One at a time: a new toast replaces the current one, so a burst of actions
 * never stacks a column of them. Announced through a polite live region, and
 * the action is a real button, reachable by Tab, while the toast is up.
 */

const DEFAULT_MS = 6000;
const ACTION_MS = 10000;   // long enough to notice a mistake and reach Undo

let el = null;
let timer = null;

function ensure() {
  if (el) return el;
  el = document.createElement('div');
  el.className = 'toast';
  el.setAttribute('role', 'status');
  el.setAttribute('aria-live', 'polite');
  el.hidden = true;
  // Hovering or focusing the toast pauses it, so Undo never vanishes under the pointer.
  el.addEventListener('mouseenter', () => clearTimeout(timer));
  el.addEventListener('focusin', () => clearTimeout(timer));
  el.addEventListener('mouseleave', () => arm(DEFAULT_MS));
  document.body.appendChild(el);
  return el;
}

function arm(ms) {
  clearTimeout(timer);
  timer = setTimeout(hideToast, ms);
}

function hideToast() {
  clearTimeout(timer);
  if (el) { el.hidden = true; el.replaceChildren(); }
}

/**
 * @param {string} message plain text (never parsed as HTML)
 * @param {{ action?: { label: string, onClick: () => (void|Promise<void>) }, error?: boolean }} [opts]
 */
export function showToast(message, { action, error = false } = {}) {
  const t = ensure();
  t.replaceChildren();
  t.classList.toggle('toast-error', error);
  const text = document.createElement('span');
  text.className = 'toast-text';
  text.textContent = message;
  t.appendChild(text);
  if (action) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'toast-action';
    btn.textContent = action.label;
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try { await action.onClick(); }
      catch (e) { showToast(`${action.label} failed: ${e.message}`, { error: true }); return; }
      // The action's own outcome toast (if any) has replaced this one by now.
    });
    t.appendChild(btn);
  }
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'toast-close';
  close.setAttribute('aria-label', 'Dismiss');
  close.textContent = '×';
  close.addEventListener('click', hideToast);
  t.appendChild(close);
  t.hidden = false;
  arm(action ? ACTION_MS : DEFAULT_MS);
}
