/* Quick switcher (Ctrl/Cmd-K): jump to a post by title or `#id`.
 *
 * Filters the in-memory link index (./links.js), so it answers as fast as you
 * type and costs no request until a post is chosen.
 */

import { wireModal } from './dialog.js';
import { matchPosts } from './links.js';

const modal = document.getElementById('switcherModal');
const input = document.getElementById('swInput');
const list = document.getElementById('swList');

let results = [];
let active = 0;
let openPost = () => {};

function paint() {
  list.replaceChildren(...results.map((p, i) => {
    const li = document.createElement('li');
    li.id = `swItem${i}`;
    li.className = 'sw-item';
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', String(i === active));
    li.textContent = p.title;
    const id = document.createElement('span');
    id.className = 'ac-hint';
    id.textContent = `#${p.id}`;
    li.appendChild(id);
    li.addEventListener('click', () => choose(i));
    return li;
  }));
  if (!results.length) {
    const none = document.createElement('li');
    none.className = 'sw-empty';
    none.textContent = 'No post matches.';
    list.appendChild(none);
  }
  if (results.length) input.setAttribute('aria-activedescendant', `swItem${active}`);
  else input.removeAttribute('aria-activedescendant');
  list.children[active]?.scrollIntoView({ block: 'nearest' });
}

function search() {
  results = matchPosts(input.value, 20);
  active = 0;
  paint();
}

function choose(i) {
  const post = results[i];
  if (!post) return;
  close();
  openPost(post.id);
}

function close() {
  modal.classList.remove('open');
  input.value = '';
}

input.addEventListener('input', search);
input.addEventListener('keydown', e => {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    if (!results.length) return;
    active = (active + (e.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length;
    paint();
  } else if (e.key === 'Enter') {
    e.preventDefault();
    choose(active);
  }
});
wireModal(modal, { close });

/** main.js supplies how a chosen post is opened. */
export function initSwitcher(open) { openPost = open; }

export function openSwitcher() {
  modal.classList.add('open');
  search();
  input.focus();
}
