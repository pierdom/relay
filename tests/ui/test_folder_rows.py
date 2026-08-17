"""Folder rows are marked with a folder, and every label starts on one edge.

The Tree and Files lists used `▸` (U+25B8), a right-pointing triangle, which
reads as "expandable" — these rows are filters, not a tree that opens. They now
carry U+1F4C1.

The icon is **drawn, not typed**. U+1F4C1 shipped here first and was wrong for
the same reason `✏︎` was wrong in the tag row: a colour emoji ignores `color`, so
it painted the same manila tab in all fifteen themes — the one thing on screen
that did not answer to the palette. An inline SVG stroked in `currentColor` puts
it back under `--accent`.

That is also why this asserts the **computed colour against the active theme's
accent** rather than that an icon is present: "there is an icon" was true of the
emoji too. Font coverage is a related trap — measured in the CI browser the
original `▸` had exactly the width of a notdef box, so any check of the form
"does something render" reports on the font stack rather than the markup.

The alignment half is the part that is easy to get wrong: the `all` / `All files`
row has no icon, so with the element simply omitted its label sat left of every
folder beneath it. `.folder-ico` is a fixed-width gutter present in every row,
empty on that one.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.request

import pytest

from .conftest import API_KEY

pytestmark = pytest.mark.ui

ARROW = "▸"

MEASURE = """() => {
  const accent = getComputedStyle(document.documentElement)
    .getPropertyValue('--accent').trim();
  return {
    accent,
    rows: [...document.querySelectorAll('#tagList .folder-item')].map(r => {
      const name = r.querySelector('.tag-name');
      const ico = r.querySelector('.folder-ico');
      return {
        label: name.textContent.trim(),
        drawn: !!(ico && ico.querySelector('svg')),
        // The SVG's own resolved `stroke`, not the span's `color`. Reading the
        // span only proves the CSS variable is plumbed to the wrapper; an icon
        // with a hardcoded `stroke="#e0af68"` still passed that, and would not
        // follow a single theme. `currentColor` resolves here.
        stroke: ico && ico.querySelector('svg')
          ? getComputedStyle(ico.querySelector('svg')).stroke : null,
        gutter: ico ? Math.round(ico.getBoundingClientRect().width) : null,
        textLeft: ico ? Math.round(ico.getBoundingClientRect().right) : null,
      };
    }),
  };
}"""


def _to_rgb(value: str) -> tuple[int, int, int] | None:
    value = value.strip()
    if value.startswith("#"):
        raw = value[1:]
        if len(raw) == 3:
            raw = "".join(c * 2 for c in raw)
        return tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))
    nums = re.findall(r"[\d.]+", value)
    return tuple(int(float(n)) for n in nums[:3]) if len(nums) >= 3 else None


def _seed_attachment(base_url: str) -> None:
    """One attachment, so the Files tab has a folder row to draw at all."""
    png = base64.b64encode(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6300010000050001" "0d0a2db4" "0000000049454e44ae426082"
    )).decode()
    req = urllib.request.Request(
        f"{base_url}/attachments",
        data=json.dumps({"filename": "probe.png", "data": png, "folder": "Homelab"}).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:      # already seeded by an earlier test
        if exc.code not in (400, 409):
            raise


def _assert_folder_rows(page):
    m = page.evaluate(MEASURE)
    rows = m["rows"]
    assert rows, "no folder rows rendered"

    drawn = [r for r in rows if r["drawn"]]
    assert drawn, f"no row carries a drawn folder icon: {[r['label'] for r in rows]}"
    assert not any(ARROW in r["label"] for r in rows), "a row still uses the old ▸ arrow"
    assert not any("\U0001F4C1" in r["label"] for r in rows), (
        "a row still uses the emoji, which cannot take the accent colour"
    )

    # The point of drawing it: the icon answers to the theme.
    accent = _to_rgb(m["accent"])
    for r in drawn:
        assert _to_rgb(r["stroke"]) == accent, (
            f"{r['label'][:14]!r} icon is {r['stroke']}, not the theme accent {m['accent']}"
        )

    # Every row reserves the gutter, including the icon-less "all" row.
    assert all(r["gutter"] for r in rows), f"a row has no icon gutter: {rows}"
    assert len({r["textLeft"] for r in rows}) == 1, (
        f"labels do not share a left edge: {[(r['label'][:12], r['textLeft']) for r in rows]}"
    )


def test_the_tree_list_marks_folders_and_aligns_them(page):
    page.locator("#tabTree").click()
    page.wait_for_timeout(600)
    _assert_folder_rows(page)


def test_the_files_list_marks_folders_and_aligns_them(page, relay_server):
    _seed_attachment(relay_server)
    page.reload()
    page.locator("#tabFiles").click()
    page.wait_for_timeout(800)
    _assert_folder_rows(page)


def test_the_icon_follows_a_theme_change(page):
    """The whole reason it is drawn rather than typed: switch theme, and the
    folder recolours with everything else. The emoji it replaced could not."""
    page.locator("#tabTree").click()
    page.wait_for_timeout(500)
    seen = set()
    for theme in ("nord", "gruvbox", "catppuccin-mocha"):
        page.locator("#themeBtn").click()
        page.locator(f'.theme-opt[data-theme-id="{theme}"]').click()
        page.wait_for_timeout(600)
        m = page.evaluate(MEASURE)
        drawn = [r for r in m["rows"] if r["drawn"]]
        assert drawn, f"{theme}: no drawn icon"
        assert _to_rgb(drawn[0]["stroke"]) == _to_rgb(m["accent"]), (
            f"{theme}: icon {drawn[0]['stroke']} is not the accent {m['accent']}"
        )
        seen.add(drawn[0]["stroke"])
    assert len(seen) == 3, f"the icon rendered the same colour across themes: {seen}"
