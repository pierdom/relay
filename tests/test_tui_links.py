"""Pure-function tests for the TUI wikilink helpers (no Textual runtime)."""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

from relay_tui.widgets.modals import _linkify_markdown, _outbound_link_ids

INDEX = {"qth": 155, "homelab inventory": 83, "audio setup": 63}


def test_linkify_wiki_alias_broken_idref_and_code():
    out = _linkify_markdown(
        "see [[QTH]], [[Homelab Inventory|gear]], [[Ghost]], #83, #999 and `[[QTH]] #83`",
        INDEX,
    )
    assert "[QTH](relay:155)" in out
    assert "[gear](relay:83)" in out
    assert "Ghost" in out and "[Ghost]" not in out  # broken → plain text
    assert "[#83](relay:83)" in out
    assert "#999" in out and "[#999]" not in out     # unknown id untouched
    assert "`[[QTH]] #83`" in out                     # inline code left raw


def test_outbound_ids_order_dedup_and_skips():
    ids = _outbound_link_ids(
        "see [[QTH]] then #83 and [[Audio Setup]] again [[QTH]] and #999 and ```\n[[QTH]] #83\n```",
        INDEX,
    )
    assert ids == [155, 83, 63]  # document order, de-duped, unknown + fenced skipped


def test_outbound_ids_empty_when_no_links():
    assert _outbound_link_ids("plain text, no links", INDEX) == []
