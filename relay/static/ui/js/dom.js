/* Small DOM builders shared by the panels.
 *
 * Text goes in through `textContent`, never markup, so nothing built here can
 * carry HTML from a server value.
 */

/** A new element with an optional class and text. */
export function el(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

/** A one-line status message in a panel (loading…, errors, empty states). */
export const note = (text, className = 'sm-section-title') => el('div', className, text);

/** Grey filler for an empty detail pane. */
export const placeholder = (text) => el('div', 'hm-placeholder', text);

/** Swap only a pane's contents, so its fixed-size box never resizes. */
export function setPane(pane, ...nodes) {
  pane.replaceChildren(...nodes);
}
