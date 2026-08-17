"""Folder rows are marked with a folder, and every label starts on one edge.

The Tree and Files lists used `▸` (U+25B8), a right-pointing triangle, which
reads as "expandable" — these rows are filters, not a tree that opens. They now
carry U+1F4C1.

⚠️ **The glyph is asserted by codepoint, never by whether it looks right.** Font
coverage differs between a developer's machine and CI: measured in the CI browser
the *old* `▸` had exactly the width of a notdef box, so a test that checked "does
something render" would have been reporting on the font stack, not the markup.

The alignment half is the part that is easy to get wrong: the `all` / `All files`
row has no icon, so with the element simply omitted its label sat left of every
folder beneath it. `.folder-ico` is a fixed-width gutter present in every row,
empty on that one.
"""
from __future__ import annotations

import base64
import json
import urllib.request

import pytest

from .conftest import API_KEY

pytestmark = pytest.mark.ui

FOLDER = "\U0001F4C1"
ARROW = "▸"

MEASURE = """() => [...document.querySelectorAll('#tagList .folder-item')].map(r => {
  const name = r.querySelector('.tag-name');
  const ico = r.querySelector('.folder-ico');
  return {
    label: name.textContent.trim(),
    ico: ico ? ico.textContent : null,
    gutter: ico ? Math.round(ico.getBoundingClientRect().width) : null,
    textLeft: ico ? Math.round(ico.getBoundingClientRect().right) : null,
  };
})"""


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
    rows = page.evaluate(MEASURE)
    assert rows, "no folder rows rendered"

    folders = [r for r in rows if r["label"].startswith(FOLDER)]
    assert folders, f"no row carries the folder glyph: {[r['label'] for r in rows]}"
    assert not any(ARROW in r["label"] for r in rows), "a row still uses the old ▸ arrow"

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
