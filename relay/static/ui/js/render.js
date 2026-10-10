/* Markdown → sanitised HTML for posts: wikilinks, #id refs, Obsidian embeds,
 * and the table fix-ups every rendered body gets. Shared by the feed, the post
 * modal and the editor's Preview, so a note reads the same in all three.
 *
 * `marked` and `DOMPurify` are globals from vendored classic scripts in <head>.
 */

import { idForTitle, postExists } from './links.js';
import { CODE_SPAN_RE, escHtml } from './util.js';

// DOMPurify config: keep the attrs our attachment embeds/links add (img loading,
// link target/rel). marked + preprocessLinks output is sanitized through this.
const SANITIZE_OPTS = { ADD_ATTR: ['target', 'rel', 'loading'] };
// A link out of the vault opens in a new tab: in this tab it would replace the
// whole app, mid-read, with no way back to the open post.
DOMPurify.addHook('afterSanitizeAttributes', node => {
  if (node.tagName === 'A' && /^https?:/i.test(node.getAttribute('href') || '')) {
    node.setAttribute('target', '_blank');
    node.setAttribute('rel', 'noopener noreferrer');
  }
});
export const renderBody = (md) => DOMPurify.sanitize(marked.parse(preprocessLinks(md)), SANITIZE_OPTS);

// A titled post's body usually opens with that title as a heading; cards, the
// modal and link previews all show the title already, so they drop it — only
// when it *is* the title: a note opening on "## Part 1" keeps its first section.
// Compared on letters and digits alone, since the title is a filename and lost
// the heading's ":" or "/" on the way.
const LEADING_HEADING_RE = /^\s*#{1,6}\s+([^\n]*)\n*/;
const bare = (text) => text.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
export function stripTitleHeading(md, title) {
  const m = md.match(LEADING_HEADING_RE);
  return m && title && bare(m[1]) === bare(title) ? md.slice(m[0].length) : md;
}

/* Every rendered table scrolls sideways inside its own box when it cannot fit
   — see `.table-scroll` in app.css — in feed cards as well as the modal. */
export function wrapTables(root) {
  root.querySelectorAll('.post-body table').forEach(t => {
    const wrap = document.createElement('div');
    wrap.className = 'table-scroll';
    t.replaceWith(wrap);
    wrap.appendChild(t);
    t.querySelectorAll('td code').forEach(addPathBreaks);
    floorProseColumns(t);
  });
}

/* Once a table overflows, automatic layout shrinks *every* column to its
   longest word — on a phone a paragraph column ended up 98px wide beside short
   ones. A column whose longest cell reads as prose keeps a floor instead, and
   the table scrolls rather than crushing it. Short columns are left tight. */
const PROSE_CHARS = 30;
function floorProseColumns(table) {
  const longest = [];
  for (const row of table.rows) {
    [...row.cells].forEach((cell, i) => {
      // Prose only: code already wraps at its joints (addPathBreaks).
      const code = [...cell.querySelectorAll('code')].reduce((n, c) => n + c.textContent.length, 0);
      longest[i] = Math.max(longest[i] || 0, cell.textContent.trim().length - code);
    });
  }
  for (const row of table.rows) {
    [...row.cells].forEach((cell, i) => { if (longest[i] >= PROSE_CHARS) cell.style.minWidth = '10em'; });
  }
}

/* A path or dotted name in a table cell has no break opportunity, so one
   `/var/lib/node_exporter/fleet.prom` sets its column's minimum width and
   squeezes the prose column beside it. `<wbr>` after each separator lets it
   wrap at its joints; unlike a zero-width space it adds no character, so a
   copied command stays exact. */
const PATH_JOINT_RE = /(?<=[/._=])/;
function addPathBreaks(code) {
  if (code.children.length || code.textContent.length < 16) return;
  const parts = code.textContent.split(PATH_JOINT_RE);
  if (parts.length < 2) return;
  code.replaceChildren(...parts.flatMap((part, i) => (i ? [document.createElement('wbr'), part] : [part])));
}

// Convert wikilinks / id-refs to anchors, leaving fenced + inline code untouched.
function preprocessLinks(md) {
  return md.split(CODE_SPAN_RE)
    .map((seg, i) => (i % 2 === 1) ? seg : linkifySegment(seg)).join('');
}

export const IMAGE_EXT_RE  = /\.(png|jpe?g|gif|webp|svg|avif|bmp)$/i;
// Any file extension. Only used on the ![[…]] embed path, which is *always* a file
// in Obsidian — so a bare note title (no `!`) can never be mistaken for a file.
const HAS_EXT_RE    = /\.[a-z0-9]{1,12}$/i;
// Curated types for the plain [[…]] link path, where a dotted note title like
// [[Section 2.1]] must NOT be treated as a file.
const ATTACH_EXT_RE = /\.(png|jpe?g|gif|webp|svg|avif|bmp|pdf|canvas|docx?|xlsx?|pptx?|csv|txt|rtf|odt|ods|zip|epub|mp3|m4a|wav|flac|ogg|aac|opus|mp4|mov|webm|mkv|avi)$/i;

// /attachments/ URL, encoding each path segment (bare filenames stay bare).
const attUrl = (name) => '/attachments/' + name.split('/').map(encodeURIComponent).join('/');
const attLink = (name, label) =>
  `<a class="attachment-link" href="${attUrl(name)}" target="_blank" rel="noopener noreferrer">${escHtml(label)}</a>`;

function linkifySegment(text) {
  // Obsidian embeds: ![[target(|opts)]] — image, other-file link, or note transclusion.
  text = text.replace(/!\[\[([^\]|#]+?)(?:\|([^\]]+))?\]\]/g, (m, target, opts) => {
    const name = target.trim(), o = (opts || '').trim();
    if (IMAGE_EXT_RE.test(name)) {
      const dim = o.match(/^(\d+)(?:x(\d+))?$/);   // Obsidian sizing: |W or |WxH
      const size = dim ? ` width="${dim[1]}"${dim[2] ? ` height="${dim[2]}"` : ''}` : '';
      const alt = dim || !o ? name : o;
      return `<img class="attachment" src="${attUrl(name)}" alt="${escHtml(alt)}" loading="lazy"${size}>`;
    }
    if (HAS_EXT_RE.test(name)) return attLink(name, o || name);   // any file (pdf/zip/…) → link
    // No extension → note transclusion; relay doesn't transclude, so link to the note.
    const id = idForTitle(name);
    return (id !== undefined)
      ? `<a class="wikilink" data-post-id="${id}">${escHtml(o || name)}</a>`
      : `<span class="wikilink broken" title="unresolved embed">${escHtml(o || name)}</span>`;
  });
  text = text.replace(/\[\[([^\]|#]+?)(#[^\]|]+)?(?:\|([^\]]+))?\]\]/g, (m, target, heading, alias) => {
    const label = escHtml((alias || target).trim());
    const t = target.trim();
    const id = idForTitle(t);
    if (id !== undefined) return `<a class="wikilink" data-post-id="${id}">${label}</a>`;
    // Unresolved but a known attachment type (e.g. [[doc.pdf]]) → attachment link, not broken.
    if (ATTACH_EXT_RE.test(t)) return attLink(t, (alias || target).trim());
    return `<span class="wikilink broken" title="unresolved link">${label}</span>`;
  });
  text = text.replace(/(^|[^\w#])#(\d{1,5})\b/g, (m, pre, n) =>
    postExists(n) ? `${pre}<a class="wikilink" data-post-id="${n}">#${n}</a>` : m);
  return text;
}

// First image embed → thumbnail URL + image count, plus the content with image
// embeds removed so a card's text preview shows prose instead of an image slice.
// Non-image embeds (pdf, note transclusions) are left in place.
export function extractMedia(content) {
  let thumb = null, count = 0;
  const stripped = content.split(CODE_SPAN_RE).map((seg, i) => {
    if (i % 2 === 1) return seg;   // code — never a real embed, leave untouched
    return seg.replace(/!\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]/g, (m, target) => {
      const name = target.trim();
      if (!IMAGE_EXT_RE.test(name)) return m;
      count++;
      if (!thumb) thumb = attUrl(name);
      return '';
    });
  }).join('');
  return { thumb, count, stripped };
}
