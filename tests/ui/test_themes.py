"""Themes: the picker, and the audit that proves the token layer is airtight.

`test_every_rendered_colour_belongs_to_the_active_theme` is the point of this
file. The static check in `tests/test_css_tokens.py` proves no colour *literal*
sits outside the token blocks; it cannot see a token used for the wrong role, a
UA default leaking in, or a value that resolves from somewhere unexpected. This
one renders the real UI in each theme and asks a harder question: does every
colour on screen trace back to *this* theme's palette?

It earned its place immediately. Run against the first draft of the gruvbox
theme it found three things, none of which the static check could:

  * `<button>` takes `buttontext` from the user agent, which `color-scheme`
    resolves to white on a dark theme and black on a light one — a colour owned
    by no theme, invisible only because the one button that never sets its own
    colour draws bars instead of text;
  * `<html>` had no `color` at all, so it fell back the same way, and passed on
    the light theme purely by coinciding with `--on-accent: #000`;
  * the picker's "Relay Dark" swatch painted itself in whatever theme was
    active, because dark was the bare `:root` block and had no name to select.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parent.parent.parent / "relay" / "static" / "ui" / "app.css"

# A theme's colour transitions have to finish before the page is sampled —
# `.live-dot` fades over 0.3s, and reading it mid-transition yields an
# interpolated value that belongs to no palette. That looked like a real leak
# on first run.
SETTLE_MS = 600


def _rgb(value: str) -> tuple[int, int, int] | None:
    """Best-effort RGB triple from any CSS colour form used in the stylesheet."""
    value = value.strip()
    if m := re.fullmatch(r"#([0-9a-fA-F]{3})", value):
        return tuple(int(c * 2, 16) for c in m.group(1))
    if m := re.fullmatch(r"#([0-9a-fA-F]{6})", value):
        return tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4))
    if m := re.fullmatch(r"rgba?\(([^)]*)\)", value):
        parts = [p.strip() for p in m.group(1).replace("/", " ").split(",")]
        if len(parts) >= 3:
            try:
                return tuple(int(float(p)) for p in parts[:3])
            except ValueError:
                return None
    if m := re.fullmatch(r"(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", value):   # --scrim-rgb
        return tuple(int(g) for g in m.groups())
    return None


def _palettes() -> dict[str, set[tuple[int, int, int]]]:
    """Every RGB triple each theme declares, keyed by theme id.

    Alpha is dropped on purpose: `color-mix(… var(--accent) 40%, transparent)`
    renders as the accent's own RGB at 0.4 alpha, so comparing triples accepts
    every derived value without having to enumerate them.
    """
    css = CSS.read_text(encoding="utf-8")
    out: dict[str, set[tuple[int, int, int]]] = {}
    block = r"^(:root(?:\[[^\]]*\])?)(?:\s*,\s*\[([^\]]*)\])*\s*\{(.*?)^\}"
    for match in re.finditer(block, css, re.S | re.M):
        name, body = match.group(1), match.group(3)
        key = "dark" if name == ":root" else re.search(r'"([^"]+)"', name).group(1)
        values: set[tuple[int, int, int]] = set()
        for _, raw in re.findall(r"^\s*(--[\w-]+):\s*([^;]+);", body, re.M):
            for token in re.findall(r"#[0-9a-fA-F]{3,6}|rgba?\([^)]*\)|\d+\s*,\s*\d+\s*,\s*\d+", raw):
                if (c := _rgb(token)) is not None:
                    values.add(c)
        out.setdefault(key, set()).update(values)
    return out


_WALK = """
() => {
  const seen = {};
  const props = ['color', 'backgroundColor', 'borderTopColor', 'borderRightColor',
                 'borderBottomColor', 'borderLeftColor', 'outlineColor'];
  document.querySelectorAll('*').forEach(el => {
    // A swatch deliberately carries another theme's tokens — showing a foreign
    // palette is its entire job. This is the one documented exemption.
    if (el.closest('.theme-swatch')) return;
    const cs = getComputedStyle(el);
    for (const p of props) {
      const v = cs[p];
      if (!v || v === 'rgba(0, 0, 0, 0)' || v === 'transparent') continue;
      (seen[v] ||= []).push((el.tagName + '.' + (el.className || '')).slice(0, 48) + ' ' + p);
    }
  });
  return Object.entries(seen).map(([v, who]) => [v, who.length, who[0]]);
}
"""


# Derived from the stylesheet rather than listed here: a new theme is a token
# block plus a registry entry, and it should not also be a test edit. Every test
# below then covers theme #5 the moment it exists.
THEMES = sorted(_palettes())


def _select(page, theme: str) -> None:
    page.locator("#themeBtn").click()
    page.locator(f'.theme-opt[data-theme-id="{theme}"]').click()
    page.wait_for_timeout(SETTLE_MS)


@pytest.mark.parametrize("theme", THEMES)
def test_every_rendered_colour_belongs_to_the_active_theme(page, theme):
    page.set_viewport_size({"width": 1440, "height": 950})
    palette = _palettes()[theme]
    assert palette, f"no palette parsed for {theme}"

    _select(page, theme)
    foreign = []
    for value, count, example in page.evaluate(_WALK):
        rgb = _rgb(value)
        if rgb is None or rgb in palette:
            continue
        foreign.append(f"{value} x{count} (e.g. {example})")

    assert not foreign, f"{theme}: colours from outside the palette:\n  " + "\n  ".join(foreign)


@pytest.mark.parametrize("theme", THEMES)
def test_the_picker_applies_and_persists_each_theme(page, theme):
    _select(page, theme)
    # `dark` is the bare `:root` block, so it is represented by no attribute.
    expected = None if theme == "dark" else theme
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == expected

    page.reload()
    page.locator("#themeBtn").wait_for(timeout=10_000)
    page.wait_for_timeout(200)
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == expected, (
        f"{theme} did not survive a reload — the before-paint script dropped it"
    )
    assert page.evaluate("() => localStorage.getItem('relay-theme')") == theme


def test_each_swatch_shows_its_own_theme_not_the_active_one(page):
    """A swatch is painted by the theme it names, via its own `data-theme`.

    This is why theme blocks match a bare attribute selector as well as `:root`,
    and why `dark` had to stop being anonymous: with no `[data-theme="dark"]` to
    select, the Relay Dark swatch inherited the active theme and rendered itself
    gruvbox while sitting next to the gruvbox entry.
    """
    _select(page, "gruvbox")
    page.locator("#themeBtn").click()
    page.wait_for_timeout(200)

    swatches = page.evaluate(
        """() => Object.fromEntries([...document.querySelectorAll('.theme-swatch')]
             .map(s => [s.getAttribute('data-theme'), getComputedStyle(s).backgroundColor]))"""
    )
    assert len(swatches) == len(THEMES), f"expected a swatch per theme, got {sorted(swatches)}"
    assert len(set(swatches.values())) == len(THEMES), f"swatches are not distinct: {swatches}"

    palettes = _palettes()
    for theme, colour in swatches.items():
        assert _rgb(colour) in palettes[theme], f"{theme} swatch painted {colour}, not its own --bg"


def test_the_picker_orders_by_family_group_then_alphabetically(page):
    """Relay Dark and Relay Light lead; named families follow in group order;
    ungrouped singletons trail alphabetically.

    Named families: Relay → ANSI → Catppuccin → Gruvbox → Solarized.
    Singletons (Dracula, Everforest Dark, Molokai, Nord, Tokyo Night) come last.
    Within each group themes are alphabetical.
    """
    page.locator("#themeBtn").click()
    page.wait_for_timeout(200)
    labels = page.evaluate(
        "() => [...document.querySelectorAll('.theme-opt .theme-label')].map(e => e.textContent)"
    )
    assert labels[:2] == ["Relay Dark", "Relay Light"], f"signature themes do not lead: {labels}"
    assert len(labels) == len(THEMES), f"picker shows {len(labels)} themes, stylesheet has {len(THEMES)}"

    # Named family blocks appear contiguously and before ungrouped singles.
    families = {
        'ANSI':       ['ANSI Dark', 'ANSI Light'],
        'Catppuccin': ['Catppuccin Frappé', 'Catppuccin Latte', 'Catppuccin Macchiato', 'Catppuccin Mocha'],
        'Gruvbox':    ['Gruvbox', 'Gruvbox Light'],
        'Solarized':  ['Solarized Dark', 'Solarized Light'],
    }
    singles = sorted(['Dracula', 'Everforest Dark', 'Molokai', 'Nord', 'Tokyo Night'])

    for family, members in families.items():
        positions = [labels.index(m) for m in members]
        assert positions == sorted(positions), f"{family} members are not in order: {positions}"
        span = labels[min(positions):max(positions) + 1]
        assert max(positions) - min(positions) == len(members) - 1, (
            f"{family} members are not contiguous in the picker: {span}"
        )

    # All named-family themes come before all singles.
    family_members = [m for members in families.values() for m in members]
    last_family_pos = max(labels.index(m) for m in family_members)
    first_single_pos = min(labels.index(m) for m in singles)
    assert last_family_pos < first_single_pos, (
        f"a named-family theme appears after a singleton: last family at {last_family_pos}, "
        f"first single at {first_single_pos}"
    )

    # Ungrouped singles are alphabetical among themselves.
    single_positions = [labels.index(m) for m in singles]
    assert single_positions == sorted(single_positions), f"singles are not alphabetical: {singles}"


def test_the_picker_closes_on_escape_and_on_a_click_away(page):
    menu = page.locator("#themeMenu")

    page.locator("#themeBtn").click()
    page.wait_for_timeout(150)
    assert "open" in (menu.get_attribute("class") or "")
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    assert "open" not in (menu.get_attribute("class") or ""), "Escape did not close the picker"

    page.locator("#themeBtn").click()
    page.wait_for_timeout(150)
    page.locator(".feed").click(position={"x": 5, "y": 5})
    page.wait_for_timeout(150)
    assert "open" not in (menu.get_attribute("class") or ""), "clicking away did not close the picker"


def test_the_picker_rows_are_uniform_and_fit_on_screen(page):
    """No wrapping, no overflow, and the tick costs no layout.

    At fifteen themes and 168px the menu wrapped "Catppuccin Macchiato" onto two
    lines, making those rows taller than their neighbours — a list of one-line
    rows with two-line exceptions reads as broken rather than long. The tick was
    `::after` on the label, so it joined the wrap and landed on a line of its
    own; it is now a reserved column, present in every row and merely invisible
    on the inactive ones, which also stops labels shifting as the selection
    moves.
    """
    page.set_viewport_size({"width": 900, "height": 900})
    page.locator("#themeBtn").click()
    page.wait_for_timeout(250)

    m = page.evaluate(
        """() => {
          const menu = document.getElementById('themeMenu');
          const r = menu.getBoundingClientRect();
          const opts = [...menu.querySelectorAll('.theme-opt')];
          const labels = opts.map(o => {
            const lr = o.querySelector('.theme-label').getBoundingClientRect();
            return { left: Math.round(lr.left), width: Math.round(lr.width) };
          });
          return {
            width: Math.round(r.width),
            heights: [...new Set(opts.map(o => Math.round(o.getBoundingClientRect().height)))],
            rows: opts.length,
            overflows: r.bottom > window.innerHeight + 1,
            scrollable: menu.scrollHeight > menu.clientHeight + 1,
            labelLefts: [...new Set(labels.map(l => l.left))],
            labelWidths: [...new Set(labels.map(l => l.width))],
          };
        }"""
    )

    assert m["rows"] >= 10, f"expected the full theme list, got {m['rows']}"
    assert len(m["heights"]) == 1, f"rows are not a uniform height — a label wrapped: {m['heights']}"
    assert not m["overflows"] or m["scrollable"], "the menu runs off the screen instead of scrolling"
    # The tick occupies a reserved column, so every label starts and ends alike.
    assert len(m["labelLefts"]) == 1, f"labels do not share a left edge: {m['labelLefts']}"
    assert len(m["labelWidths"]) == 1, f"the tick is taking width from one label: {m['labelWidths']}"


def test_the_picker_scrolls_rather_than_running_off_a_short_window(page):
    """A window shorter than the list must not put themes out of reach."""
    page.set_viewport_size({"width": 900, "height": 420})
    page.locator("#themeBtn").click()
    page.wait_for_timeout(250)
    m = page.evaluate(
        """() => {
          const menu = document.getElementById('themeMenu');
          const r = menu.getBoundingClientRect();
          return { bottom: Math.round(r.bottom), vh: window.innerHeight,
                   scrollable: menu.scrollHeight > menu.clientHeight + 1 };
        }"""
    )
    assert m["bottom"] <= m["vh"] + 1, f"menu extends to {m['bottom']} in a {m['vh']}px window"
    assert m["scrollable"], "the list is taller than the window but does not scroll"
