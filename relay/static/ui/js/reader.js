/* Three-column view: sidebar · a narrow note list · the open note, in place.
 *
 * The note column is the post modal's own panel (`.pm-inner`), moved into
 * `#readerPane` while the view is on and back when it is off — one renderer for
 * both, so the pane cannot drift from the modal. main.js decides which note is
 * shown; this module docks and undocks the panel and hosts the in-pane editor.
 * Below the phone breakpoint the view falls back to the list (CSS) and the
 * panel stays a modal.
 */

import { buildEditForm } from './edit-form.js';
import { prefs } from './view-prefs.js';

export const READER_MQ = window.matchMedia('(min-width: 769px)');

const pane      = document.getElementById('readerPane');
const postModal = document.getElementById('postModal');
const panel     = postModal.querySelector('.pm-inner');
const reTitle   = document.getElementById('reTitle');
const reBody    = document.getElementById('reBody');
let docked = false;
let editor = null;   // { isDirty } from buildEditForm while editing in the pane

/** The three-column view is chosen and the screen is wide enough for it. */
export const wantsReader = () => prefs.view === 'reader' && READER_MQ.matches;
export const isDocked = () => docked;
export const isEditing = () => editor !== null;

export function dock() {
  if (docked) return;
  pane.prepend(panel);
  // A region in the page now, not a dialog: no focus trap, no aria-modal.
  panel.setAttribute('role', 'region');
  panel.removeAttribute('aria-modal');
  docked = true;
}

export function undock() {
  if (!docked) return;
  closeEditor();
  postModal.appendChild(panel);
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  pane.classList.add('empty');
  docked = false;
}

/** Show the "no note selected" state, or the panel. */
export function setEmpty(empty) { pane.classList.toggle('empty', empty); }

/** Edit `post` in the pane; `onSave(updated)` runs after the editor closes. */
export function openEditor(post, { onSave }) {
  closeEditor();
  pane.classList.add('editing');
  reTitle.textContent = `#${post.id}`;
  editor = buildEditForm(reBody, post, {
    onCancel: closeEditor,   // buildEditForm already confirmed the discard
    onSave: updated => { closeEditor(); onSave(updated); },
  });
}

/** Leave the editor, asking first when it has changes. False when the user stays. */
export function confirmLeaveEditor() {
  if (!editor) return true;
  if (editor.isDirty() && !confirm('Discard your changes to this post?')) return false;
  closeEditor();
  return true;
}

function closeEditor() {
  if (!editor) return;
  editor = null;
  pane.classList.remove('editing');
  reBody.innerHTML = '';
}
