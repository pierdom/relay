/* Writing aids shared by Compose and the Edit form: completion for `[[Title]]`,
 * `#id` and tags, and a Write/Preview toggle.
 *
 * Completion reads the in-memory link index (./links.js) and the tag list, so
 * a suggestion costs no request per keystroke. The list is a listbox the field
 * drives through `aria-activedescendant`; focus never leaves the field.
 */

import { apiFetch } from './api.js';
import { matchPosts } from './links.js';
import { renderBody, wrapTables } from './render.js';
import { parseTags } from './util.js';

let menuSeq = 0;

/* Where the caret sits on screen. A textarea cannot report it, so a hidden
   copy of the field lays the text out the same way and a marker is measured. */
function caretPoint(ta) {
  const cs = getComputedStyle(ta);
  const mirror = document.createElement('div');
  // Longhands: a computed shorthand (`font`, `padding`) can read back empty.
  for (const prop of ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'letterSpacing', 'lineHeight',
    'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'borderTopWidth', 'borderRightWidth',
    'borderBottomWidth', 'borderLeftWidth', 'boxSizing', 'tabSize', 'width']) {
    mirror.style[prop] = cs[prop];
  }
  Object.assign(mirror.style, {
    position: 'fixed', visibility: 'hidden', top: '0', left: '0', borderStyle: 'solid',
    whiteSpace: 'pre-wrap', overflowWrap: 'break-word',
  });
  mirror.textContent = ta.value.slice(0, ta.selectionEnd);
  const mark = document.createElement('span');
  mark.textContent = '​';
  mirror.appendChild(mark);
  document.body.appendChild(mirror);
  const box = ta.getBoundingClientRect();
  const point = {
    top: box.top + mark.offsetTop + mark.offsetHeight - ta.scrollTop,
    left: box.left + mark.offsetLeft - ta.scrollLeft,
  };
  mirror.remove();
  return point;
}

/**
 * A suggestion list for one text field.
 *
 * @param {HTMLInputElement|HTMLTextAreaElement} field
 * @param {(before: string, after: string) => ({from: number, to?: number, query: string, pick?: boolean} | null)} find
 *        what is being typed, from the text either side of the caret — `from`
 *        and `to` bound what accepting replaces (`to` counts into `after`).
 *        `pick: false` preselects nothing, so Enter keeps its own meaning until
 *        an arrow key or Tab chooses
 * @param {(query: string) => {label: string, hint?: string, text: string}[]} suggest
 */
function attachCompletion(field, find, suggest) {
  const menu = document.createElement('ul');
  menu.className = 'ac-menu';
  menu.id = `acMenu${++menuSeq}`;
  menu.setAttribute('role', 'listbox');
  menu.hidden = true;
  field.after(menu);
  field.setAttribute('aria-autocomplete', 'list');
  field.setAttribute('aria-controls', menu.id);

  let items = [], active = 0, span = null, accepting = false;

  function close() {
    menu.hidden = true;
    items = [];
    field.removeAttribute('aria-activedescendant');
    field.setAttribute('aria-expanded', 'false');
  }

  function paint() {
    menu.replaceChildren(...items.map((item, i) => {
      const li = document.createElement('li');
      li.id = `${menu.id}-${i}`;
      li.setAttribute('role', 'option');
      li.setAttribute('aria-selected', String(i === active));
      li.className = 'ac-item';
      li.textContent = item.label;
      if (item.hint) {
        const hint = document.createElement('span');
        hint.className = 'ac-hint';
        hint.textContent = item.hint;
        li.appendChild(hint);
      }
      // mousedown, not click: a click would blur the field first and close the list.
      li.addEventListener('mousedown', e => { e.preventDefault(); accept(i); });
      return li;
    }));
    if (active < 0) field.removeAttribute('aria-activedescendant');
    else field.setAttribute('aria-activedescendant', `${menu.id}-${active}`);
    menu.children[active]?.scrollIntoView({ block: 'nearest' });
  }

  function update() {
    if (accepting) return;   // our own input event: what was just inserted is not a new query
    const caret = field.selectionEnd;
    span = field.selectionStart === caret ? find(field.value.slice(0, caret), field.value.slice(caret)) : null;
    items = span ? suggest(span.query) : [];
    if (!items.length) { close(); return; }
    active = span.pick === false ? -1 : 0;
    paint();
    const at = field.tagName === 'TEXTAREA' ? caretPoint(field) : (({ bottom, left }) => ({ top: bottom, left }))(field.getBoundingClientRect());
    menu.style.top = `${at.top + 4}px`;
    menu.style.left = `${Math.max(8, Math.min(at.left, window.innerWidth - 288))}px`;
    menu.hidden = false;
    field.setAttribute('aria-expanded', 'true');
  }

  function accept(i) {
    const { text } = items[i];
    const caret = field.selectionEnd;
    const end = caret + (span.to || 0);
    field.value = field.value.slice(0, span.from) + text + field.value.slice(end);
    field.selectionStart = field.selectionEnd = span.from + text.length;
    close();
    accepting = true;
    field.dispatchEvent(new Event('input', { bubbles: true }));   // the highlighter, scope gates and draft listen
    accepting = false;
  }

  field.addEventListener('input', update);
  field.addEventListener('blur', close);
  field.addEventListener('scroll', close);   // it is placed at the caret, which just moved
  field.addEventListener('keydown', e => {
    if (menu.hidden || e.isComposing) return;   // an IME owns Enter mid-composition
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const step = e.key === 'ArrowDown' ? 1 : -1;
      active = active < 0 ? (step > 0 ? 0 : items.length - 1) : (active + step + items.length) % items.length;
      paint();
    } else if (e.key === 'Tab' || (e.key === 'Enter' && active >= 0)) {
      e.preventDefault();
      accept(Math.max(active, 0));
    } else if (e.key === 'Enter') {
      close();   // nothing chosen: Enter is still a new line
    } else if (e.key === 'Escape') {
      e.stopPropagation();   // close the list, not the dialog around it
      close();
    }
  });
}

const postItems = (query, text) => matchPosts(query).map(p => ({ label: p.title, hint: `#${p.id}`, text: text(p) }));

/** `[[Ti` → titles; `#pro` or `#4` → posts by title or id, inserted as `#id`.
 *  A `#word` may just be prose (or an Obsidian tag), so that list preselects
 *  nothing and Enter stays a new line. */
function wireLinkCompletion(ta) {
  let kind = null;
  attachCompletion(ta, (before, after) => {
    let m = before.match(/\[\[([^\][|#\n]*)$/);
    if (m) {
      kind = 'wiki';
      return { from: before.length - m[1].length, to: after.startsWith(']]') ? 2 : 0, query: m[1] };
    }
    // Not a heading (`# `), and only at a word start: `a#1` is no reference.
    m = before.match(/(?:^|[\s([])#([^\s#[\]()]{1,40})$/);
    if (m) {
      kind = 'id';
      return { from: before.length - m[1].length - 1, query: m[1], pick: false };
    }
    return null;
  }, query => postItems(query, kind === 'wiki' ? p => `${p.title}]]` : p => `#${p.id}`));
}

/** The Tags field completes the tag being typed from the vault's tags. */
function wireTagCompletion(input) {
  let known = [];
  // Fetched on focus, never at wiring time: Compose is wired before sign-in,
  // and a 401 there would bounce the login card.
  input.addEventListener('focus', () => {
    apiFetch('/tags').then(d => { known = d.tags.map(t => t.tag); }).catch(() => {});
  });
  attachCompletion(input, (before) => {
    const query = before.slice(before.lastIndexOf(',') + 1).trimStart();
    return query ? { from: before.length - query.length, query } : null;
  }, query => {
    const have = new Set(parseTags(input.value));
    const q = query.toLowerCase();
    return known.filter(t => t.includes(q) && !have.has(t))
      .sort((a, b) => b.startsWith(q) - a.startsWith(q))
      .slice(0, 8)
      .map(t => ({ label: t, text: t }));
  });
}

/** A Write/Preview toggle: the button swaps the editing surface for the
 *  rendered body, exactly as the post will read. */
function wirePreview(ta, button) {
  const surface = ta.closest('.ef-content-highlight') || ta;
  const preview = document.createElement('div');
  preview.className = 'ef-preview post-body';
  preview.hidden = true;
  surface.after(preview);
  button.setAttribute('aria-pressed', 'false');
  button.addEventListener('click', () => {
    const show = preview.hidden;
    if (show) {
      preview.style.minHeight = `${surface.offsetHeight}px`;
      preview.innerHTML = renderBody(ta.value);
      wrapTables(preview);
    }
    preview.hidden = !show;
    surface.hidden = show;
    button.textContent = show ? 'Write' : 'Preview';
    button.setAttribute('aria-pressed', String(show));
    if (!show) ta.focus();
  });
  // A form reset (Compose closing) must not leave the next post in Preview.
  return () => { if (!preview.hidden) button.click(); };
}

/** Wire the writing aids into one editor. Returns a reset for the preview. */
export function enhanceEditor({ content, tags, previewBtn }) {
  wireLinkCompletion(content);
  wireTagCompletion(tags);
  return wirePreview(content, previewBtn);
}
