"""relay.markdown_scan.strip_code — shared by relay.links (extract_links) and
relay.lint (the H1 scanner), which had no code-exclusion at all before this
(relay #198, N-5 follow-up: L-2/L-3/L-6)."""
from __future__ import annotations

from relay.markdown_scan import strip_code


def test_a_fenced_block_is_blanked_but_its_newlines_survive():
    content = "before\n```\n[[inputs.cpu]]\nmore code\n```\nafter"
    out = strip_code(content)
    assert "[[inputs.cpu]]" not in out
    assert out.count("\n") == content.count("\n")
    assert out.splitlines()[0] == "before"
    assert out.splitlines()[-1] == "after"


def test_an_inline_code_span_is_blanked_in_place():
    content = "See `[[Title]]` for the syntax."
    out = strip_code(content)
    assert "[[Title]]" not in out
    assert out.startswith("See ")
    assert out.endswith(" for the syntax.")
    assert len(out) == len(content)


def test_inline_code_never_crosses_a_newline():
    """A single backtick with no closing partner on the same line must not
    swallow the rest of the document looking for one further down."""
    content = "a stray ` backtick\n\nreal [[Target]] link"
    out = strip_code(content)
    assert "[[Target]]" in out


def test_content_with_no_code_is_untouched():
    content = "Plain prose with [[Title]] and #42, no code anywhere."
    assert strip_code(content) == content


def test_length_is_always_preserved():
    content = "x```\ncode here\n```y `inline` z"
    assert len(strip_code(content)) == len(content)
