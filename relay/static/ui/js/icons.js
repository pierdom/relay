/* Inline SVG icons, drawn rather than typed.
 *
 * Never emoji or dingbats: a colour emoji ignores `color` and paints the same
 * picture in every theme, and a host without an emoji font renders a blank box
 * — the Edit/Delete/History/Attach buttons all shipped as tofu that way. A
 * dingbat at this size is worse still: ✏︎ (U+270F) renders as a thin horizontal
 * stroke that read as "remove tag", not "rename". Every icon here is one 16px
 * stroke drawing on `currentColor`, so it follows the palette and the text
 * weight beside it.
 */
const svg = (paths) => `<svg class="icon" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"
  stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${paths}</svg>`;

export const ICON_FOLDER = svg('<path d="M1.9 12.6V3.4h4l1.5 1.9h6.7v7.3z"/>');
export const ICON_PENCIL = svg('<path d="M11.4 2.4l2.2 2.2L6 12.2l-2.9.7.7-2.9z"/><path d="M10 3.8l2.2 2.2"/>');
/* A clock, not a gear: the tag button sets TTL/expiry, and a gear at 13px reads
 * as a brightness control. */
export const ICON_CLOCK = svg('<circle cx="8" cy="8" r="5.8"/><path d="M8 4.6V8l2.3 1.7"/>');
export const ICON_HISTORY = svg('<path d="M2.6 8a5.4 5.4 0 1 0 1.6-3.8"/><path d="M2.4 2.4v2.4h2.4"/><path d="M8 5.2v2.9l1.9 1.2"/>');
export const ICON_TRASH = svg('<path d="M2.6 4.4h10.8"/><path d="M6.2 4.4V2.8h3.6v1.6"/><path d="M3.9 4.4l.7 8.8h6.8l.7-8.8"/>');
export const ICON_CLIP = svg('<path d="M13 7.4l-5.1 5.1a3 3 0 0 1-4.3-4.3l5.3-5.3a2 2 0 0 1 2.8 2.8l-5.1 5.1a1 1 0 0 1-1.4-1.4L10 4.6"/>');
export const ICON_IMAGE = svg('<rect x="2" y="3" width="12" height="10" rx="1.5"/><circle cx="5.8" cy="6.4" r="1.1"/><path d="M14 11l-3.6-3.6L4 13"/>');
export const ICON_EYE = svg('<path d="M1.5 8S3.9 3.5 8 3.5 14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/>');
