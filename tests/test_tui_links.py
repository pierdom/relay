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


# ── attachment / embed handling ───────────────────────────────────────────────


def test_embed_image_becomes_attachment_link():
    out = _linkify_markdown("![[diagram.png]]", INDEX)
    assert "](http" in out and "/attachments/diagram.png)" in out
    assert out.startswith("[\U0001F4CE diagram.png]")


def test_embed_image_size_spec_is_not_used_as_label():
    out = _linkify_markdown("![[diagram.png|300]]", INDEX)
    # |300 is an Obsidian size, not a label — filename stays the label.
    assert "[\U0001F4CE diagram.png]" in out
    assert "300" not in out.split("](")[0]


def test_embed_pdf_becomes_attachment_link():
    out = _linkify_markdown("![[notes.pdf]]", INDEX)
    assert "/attachments/notes.pdf)" in out


def test_embed_note_transclusion_links_to_note():
    out = _linkify_markdown("![[QTH]]", INDEX)
    assert out == "[QTH](relay:155)"  # resolved note, not a bogus attachment link


def test_embed_unresolved_note_degrades_to_text():
    out = _linkify_markdown("![[No Such Note]]", INDEX)
    assert out == "No Such Note"


def test_bare_file_wikilink_becomes_attachment_link():
    out = _linkify_markdown("see [[report.pdf]]", INDEX)
    assert "/attachments/report.pdf)" in out
