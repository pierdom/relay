/* The sidebar's Files tab: every attachment in the vault, grouped by folder,
 * with a lightbox for images. main.js owns the sidebar's rows and drawer, so
 * it passes those helpers in (`initGallery`). */

import { apiFetch } from './api.js';
import { confirmDeleteAttachment } from './edit-form.js';
import { IMAGE_EXT_RE } from './render.js';
import { escHtml, fmtBytes } from './util.js';

const tagList         = document.getElementById('tagList');
const attachmentsView = document.getElementById('attachmentsView');
const lightbox        = document.getElementById('lightbox');

let attachCache = [];       // last-fetched attachment list
let attachFolder = null;    // active folder filter (null = all)
let deps;

/** `{ isAuthed, sidebarRow, folderLabel, closeSidebar }` from main.js. */
export function initGallery(d) { deps = d; }

/** A new session starts on "All files". */
export function resetGallery() { attachFolder = null; }

const attPath = (a) => '/attachments/' + [a.folder, 'assets', a.filename].map(encodeURIComponent).join('/');

export async function loadAttachments() {
  if (!deps.isAuthed()) return;
  try { attachCache = (await apiFetch('/attachments')).items; } catch { attachCache = []; }
  renderAttachSidebar();
  renderAttachGallery();
}

function selectAttachFolder(folder) {
  attachFolder = folder;
  renderAttachSidebar();
  renderAttachGallery();
  deps.closeSidebar();
}

function renderAttachSidebar() {
  const counts = new Map();
  attachCache.forEach(a => counts.set(a.folder, (counts.get(a.folder) || 0) + 1));
  const row = (folder, label, count) => deps.sidebarRow({
    labelHtml: deps.folderLabel(label, folder !== null), count, className: 'tag-item folder-item',
    active: attachFolder === folder, onClick: () => selectAttachFolder(folder),
  });
  tagList.replaceChildren(
    row(null, 'All files', attachCache.length),
    ...[...counts.keys()].sort().map(folder => row(folder, folder, counts.get(folder))),
  );
}

function renderAttachGallery() {
  const items = attachFolder === null ? attachCache : attachCache.filter(a => a.folder === attachFolder);
  if (!items.length) { attachmentsView.innerHTML = '<div class="att-empty">No attachments yet.</div>'; return; }
  const groups = new Map();
  items.forEach(a => { if (!groups.has(a.folder)) groups.set(a.folder, []); groups.get(a.folder).push(a); });
  let html = '';
  [...groups.keys()].sort().forEach(folder => {
    html += `<div class="att-folder-head">${escHtml(folder)}/assets</div><div class="att-grid">`;
    groups.get(folder).forEach(a => {
      const p = attPath(a);
      const thumb = IMAGE_EXT_RE.test(a.filename)
        ? `<div class="att-thumb" data-src="${p}" data-name="${escHtml(a.filename)}"><img src="${p}" alt="${escHtml(a.filename)}" loading="lazy"></div>`
        : `<a class="att-thumb" href="${p}" target="_blank" rel="noopener noreferrer"><span class="att-ext">${escHtml(a.filename.split('.').pop() || 'file')}</span></a>`;
      html += `<div class="att-card" data-name="${escHtml(a.filename)}">${thumb}
        <div class="att-meta"><span class="att-name">${escHtml(a.filename)}</span>
          <span class="att-sub"><span>${fmtBytes(a.bytes)}</span></span></div>
        <button class="att-del" title="Delete file from vault">×</button></div>`;
    });
    html += '</div>';
  });
  attachmentsView.innerHTML = html;
}

attachmentsView.addEventListener('click', async e => {
  const thumb = e.target.closest('.att-thumb[data-src]');
  if (thumb) { openLightbox(thumb.dataset.src, thumb.dataset.name); return; }
  const del = e.target.closest('.att-del');
  if (!del) return;
  const name = del.closest('.att-card').dataset.name;
  if (await confirmDeleteAttachment(name)) await loadAttachments();
});

function openLightbox(src, name) {
  document.getElementById('lightboxImg').src = src;
  document.getElementById('lightboxCap').textContent = name || '';
  lightbox.style.display = 'flex';
}
function closeLightbox() { lightbox.style.display = 'none'; document.getElementById('lightboxImg').src = ''; }
lightbox.addEventListener('click', closeLightbox);
document.addEventListener('keydown', e => { if (e.key === 'Escape' && lightbox.style.display === 'flex') closeLightbox(); });
