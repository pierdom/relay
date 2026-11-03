/* A wikilink's target, previewed on hover: title, tags and the opening lines. */

import { apiFetch } from './api.js';
import { stripTitleHeading } from './render.js';
import { escHtml, postName } from './util.js';

let _previewTimer = null;
let _previewEl    = null;

export function hideLinkPreview() {
  clearTimeout(_previewTimer);
  _previewTimer = null;
  if (_previewEl) { _previewEl.remove(); _previewEl = null; }
}

function showLinkPreview(postId, anchor) {
  hideLinkPreview();
  // The body re-renders on navigation; a detached anchor measures as 0,0 and
  // nothing would ever fire its mouseout, so the preview stuck in the corner.
  if (!anchor.isConnected) return;
  const el = document.createElement('div');
  el.className = 'link-preview';
  const rect = anchor.getBoundingClientRect();
  el.style.top  = `${rect.bottom + 8}px`;
  el.style.left = `${Math.min(rect.left, window.innerWidth - 296)}px`;
  el.innerHTML = '<div class="lp-body">…</div>';
  document.body.appendChild(el);
  _previewEl = el;
  apiFetch(`/posts/${postId}`).then(post => {
    if (_previewEl !== el) return;
    const raw     = stripTitleHeading(post.content, post.title);
    // [[Target|alias]] reads as its alias, the way the body renders it.
    const snippet = raw.replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, '$2').replace(/[#*`_[\]]/g, '').trim();
    const clipped = snippet.length > 150 ? snippet.slice(0, 150) + '…' : snippet;
    el.innerHTML  =
      `<div class="lp-title">${escHtml(postName(post))}</div>` +
      (post.tags.length ? `<div class="lp-tags">${post.tags.map(t => `<span class="lp-tag">${escHtml(t)}</span>`).join('')}</div>` : '') +
      (clipped ? `<div class="lp-body">${escHtml(clipped)}</div>` : '');
  }).catch(hideLinkPreview);
}

/** Hover previews for the wikilinks inside `container` (the post modal's body). */
export function initLinkPreview(container) {
  container.addEventListener('mouseover', e => {
    const a = e.target.closest('a.wikilink[data-post-id]');
    if (!a) return;
    clearTimeout(_previewTimer);
    _previewTimer = setTimeout(() => showLinkPreview(Number(a.dataset.postId), a), 350);
  });
  container.addEventListener('mouseout', e => {
    if (!e.target.closest('a.wikilink[data-post-id]')) return;
    hideLinkPreview();
  });
}
