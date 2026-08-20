/* Theme registry and picker.
 *
 * A theme is a block of token overrides in `app.css` and an entry here. Nothing
 * else — that is the claim the token refactor makes, and adding gruvbox is what
 * tested it.
 *
 * `dark` is the only per-theme fact JavaScript needs, because the brand mark
 * ships as two files rather than as CSS. Every colour, including the picker's
 * own swatches, comes from the stylesheet: a swatch is an element carrying
 * `data-theme`, so the theme's own block paints it. Listing hex values here
 * would reintroduce exactly the drift the token layer removed.
 */

import { closeStatusModal, isStatusOpen } from './status.js';

/* Families group the picker for scannability. Named groups appear in GROUP_ORDER
   before ungrouped singletons; within each group themes sort alphabetically.
   Signature themes (Relay) always lead regardless of group order. */
const CATALOGUE = [
  { id: 'dark',                label: 'Relay Dark',          dark: true,  signature: true, group: 'relay' },
  { id: 'light',               label: 'Relay Light',         dark: false, signature: true, group: 'relay' },
  { id: 'ansi-dark',           label: 'ANSI Dark',           dark: true,  group: 'ansi' },
  { id: 'ansi-light',          label: 'ANSI Light',          dark: false, group: 'ansi' },
  { id: 'catppuccin-latte',    label: 'Catppuccin Latte',    dark: false, group: 'catppuccin' },
  { id: 'catppuccin-frappe',   label: 'Catppuccin Frappé',   dark: true,  group: 'catppuccin' },
  { id: 'catppuccin-macchiato',label: 'Catppuccin Macchiato',dark: true,  group: 'catppuccin' },
  { id: 'catppuccin-mocha',    label: 'Catppuccin Mocha',    dark: true,  group: 'catppuccin' },
  { id: 'dracula',             label: 'Dracula',             dark: true  },
  { id: 'everforest-dark',     label: 'Everforest Dark',     dark: true  },
  { id: 'gruvbox',             label: 'Gruvbox',             dark: true,  group: 'gruvbox' },
  { id: 'gruvbox-light',       label: 'Gruvbox Light',       dark: false, group: 'gruvbox' },
  { id: 'molokai',             label: 'Molokai',             dark: true  },
  { id: 'nord',                label: 'Nord',                dark: true  },
  { id: 'solarized-dark',      label: 'Solarized Dark',      dark: true,  group: 'solarized' },
  { id: 'solarized-light',     label: 'Solarized Light',     dark: false, group: 'solarized' },
  { id: 'tokyo-night',         label: 'Tokyo Night',         dark: true  },
];

const GROUP_ORDER = ['relay', 'ansi', 'catppuccin', 'gruvbox', 'solarized'];
const GROUP_LABELS = { relay: 'Relay', ansi: 'ANSI', catppuccin: 'Catppuccin', gruvbox: 'Gruvbox', solarized: 'Solarized' };

export const THEMES = [
  // Signature themes (Relay Dark then Relay Light) always lead in declaration order.
  ...CATALOGUE.filter(t => t.signature),
  // Named family groups in GROUP_ORDER (skipping 'relay' which already led).
  ...GROUP_ORDER.slice(1).flatMap(g =>
    CATALOGUE.filter(t => t.group === g).sort((a, b) => a.label.localeCompare(b.label))
  ),
  // Ungrouped singletons last, alphabetically.
  ...CATALOGUE.filter(t => !t.group && !t.signature).sort((a, b) => a.label.localeCompare(b.label)),
];

const STORAGE_KEY = 'relay-theme';
const DEFAULT_ID = 'dark';

const themeBtn = document.getElementById('themeBtn');
const themeMenu = document.getElementById('themeMenu');
const brandMark = document.querySelector('.brand-mark');

function current() {
  const id = document.documentElement.getAttribute('data-theme') || DEFAULT_ID;
  return THEMES.find(t => t.id === id) || THEMES[0];
}

/** Paint the parts of the theme that live outside CSS. */
function reflect() {
  const theme = current();
  // Two mark files, not one recoloured by CSS — the SVGs differ in more than
  // colour, so `dark` has to be known here.
  if (brandMark) {
    brandMark.src = theme.dark ? '/assets/relay-mark-on-dark.svg' : '/assets/relay-mark.svg';
  }
  // The icon is a static palette in the markup: this control opens a menu, so
  // nothing about it should look like it flips between two states. Only the
  // label changes, and it names the theme actually in use.
  themeBtn.setAttribute('aria-label', `Theme: ${theme.label}`);
  themeBtn.setAttribute('title', `Theme: ${theme.label}`);
  themeMenu.querySelectorAll('.theme-opt').forEach(opt => {
    const isCurrent = opt.dataset.themeId === theme.id;
    opt.classList.toggle('active', isCurrent);
    opt.setAttribute('aria-checked', isCurrent ? 'true' : 'false');
  });
}

export function setTheme(id) {
  // `dark` stays the bare `:root` block rather than a stamped attribute, so an
  // absent or unrecognised stored value degrades to a complete theme instead of
  // an unstyled page.
  if (id === DEFAULT_ID) document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', id);
  try { localStorage.setItem(STORAGE_KEY, id); } catch (e) {}
  reflect();
}

function buildMenu() {
  themeMenu.replaceChildren();
  let prevGroup;
  for (const theme of THEMES) {
    if (theme.group !== prevGroup) {
      // Separator before every group boundary (but not before the very first).
      if (prevGroup !== undefined) {
        const sep = document.createElement('div');
        sep.className = 'theme-sep';
        sep.setAttribute('role', 'separator');
        themeMenu.appendChild(sep);
      }
      // Named groups get a label; the ungrouped trailing section gets only the hairline.
      if (theme.group) {
        const gl = document.createElement('div');
        gl.className = 'theme-group-label';
        gl.setAttribute('aria-hidden', 'true');
        gl.textContent = GROUP_LABELS[theme.group];
        themeMenu.appendChild(gl);
      }
      prevGroup = theme.group;
    }

    const opt = document.createElement('button');
    opt.className = 'theme-opt';
    opt.dataset.themeId = theme.id;
    opt.setAttribute('role', 'menuitemradio');

    const swatch = document.createElement('span');
    swatch.className = 'theme-swatch';
    // The swatch is painted by the theme's own token block; see app.css.
    swatch.setAttribute('data-theme', theme.id);
    swatch.setAttribute('aria-hidden', 'true');

    const label = document.createElement('span');
    label.className = 'theme-label';
    label.textContent = theme.label;

    // Reserved in every row, shown only on the active one — see app.css.
    const check = document.createElement('span');
    check.className = 'theme-check';
    check.textContent = '✓';
    check.setAttribute('aria-hidden', 'true');

    opt.append(swatch, label, check);
    opt.addEventListener('click', (e) => {
      // Hand focus back to the trigger only for a keyboard user, who is mid-flow
      // and needs somewhere sane to tab on from. After a tap there is nobody to
      // hand it to, and a focused trigger paints itself `:focus-visible` (accent
      // text, accent border — see app.css), so the theme button stayed lit after
      // the menu closed. The focus *was* the highlight.
      //
      // ⚠️ "Is focus inside the menu?" is not the test, though it reads like it:
      // tapping a <button> focuses it, so that is true after a tap as well.
      // `detail === 0` distinguishes an Enter/Space-synthesised click from a real
      // pointer, which is the actual question being asked.
      setTheme(theme.id);
      closeThemeMenu();
      if (e.detail === 0) themeBtn.focus();
    });
    themeMenu.appendChild(opt);
  }
}

export function isThemeMenuOpen() {
  return themeMenu.classList.contains('open');
}

export function closeThemeMenu() {
  themeMenu.classList.remove('open');
  themeBtn.setAttribute('aria-expanded', 'false');
}

function openThemeMenu({ viaKeyboard = false } = {}) {
  // The status panel is the only other thing that can be open up here, and two
  // overlapping popovers in a header this tight reads as a glitch.
  if (isStatusOpen()) closeStatusModal();
  themeMenu.classList.add('open');
  themeBtn.setAttribute('aria-expanded', 'true');
  // Moving focus into the menu is for keyboard users — it is what makes the
  // arrow keys below land somewhere. Doing it on every open also focuses things
  // for a tap, which is the other half of the stuck-highlight bug.
  if (viaKeyboard) themeMenu.querySelector('.theme-opt.active')?.focus();
}

themeBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  // `detail === 0` is a click synthesised from Enter/Space rather than a real
  // pointer — the standard way to tell a keyboard activation from a tap without
  // guessing from media queries about the device.
  if (isThemeMenuOpen()) closeThemeMenu();
  else openThemeMenu({ viaKeyboard: e.detail === 0 });
});

// Click-away, matching the tag editors' dismissal. Bound on document, so the
// button's own handler stops propagation to avoid closing what it just opened.
document.addEventListener('click', (e) => {
  if (isThemeMenuOpen() && !e.target.closest('#themeMenu')) closeThemeMenu();
});

// Arrow keys inside the menu; Escape is handled by main.js's single handler so
// the priority order against the modals stays in one place.
themeMenu.addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  e.preventDefault();
  const opts = [...themeMenu.querySelectorAll('.theme-opt')];
  const at = opts.indexOf(document.activeElement);
  const next = e.key === 'ArrowDown' ? at + 1 : at - 1;
  opts[(next + opts.length) % opts.length].focus();
});

buildMenu();
reflect();
