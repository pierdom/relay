"""Pure-function tests for the TUI's pinned-post bookkeeping (no Textual
runtime) — the id != 0 assumptions both of these replaced only held while the
master document was the only thing `pinned` could ever be. A bare-id search
(relay #(this PR)) can pin any post, so both need the actual pinned id, not a
hardcoded 0."""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

from relay_tui import api
from relay_tui.app import _dated_stream_offset
from relay_tui.widgets.post_panel import _skip_pinned


def _post(id: int) -> api.Post:
    return api.Post(id=id, title="", content="", tags=[], source=None, created_at="")


# ── _dated_stream_offset ──────────────────────────────────────────────────────


def test_offset_excludes_the_master_pin():
    posts = [_post(0), _post(5), _post(6)]
    assert _dated_stream_offset(posts, pinned_id=0) == 2


def test_offset_excludes_a_bare_id_search_pin():
    # Regression: an id != 0 check here counted a non-master pin as a real
    # dated-stream post, inflating the offset and skipping a real post on
    # the next page.
    posts = [_post(42), _post(5), _post(6)]
    assert _dated_stream_offset(posts, pinned_id=42) == 2


def test_offset_counts_everything_when_nothing_is_pinned():
    posts = [_post(5), _post(6)]
    assert _dated_stream_offset(posts, pinned_id=None) == 2


def test_offset_of_an_id_lookup_with_no_dated_stream_is_zero():
    # `_id_lookup` on the server always returns an empty `items` alongside the
    # pin — posts is just `[pinned]`.
    assert _dated_stream_offset([_post(42)], pinned_id=42) == 0


# ── _skip_pinned ───────────────────────────────────────────────────────────────


def test_skip_pinned_skips_the_master_doc():
    assert _skip_pinned(first_child_id=0, pinned_id=0) == 1


def test_skip_pinned_skips_a_bare_id_search_pin():
    # Regression: a hardcoded `== 0` check here let a live SSE arrival insert
    # itself above a non-master pin, displacing it from the top slot.
    assert _skip_pinned(first_child_id=42, pinned_id=42) == 1


def test_skip_pinned_is_zero_when_nothing_is_pinned():
    assert _skip_pinned(first_child_id=5, pinned_id=None) == 0


def test_skip_pinned_is_zero_on_an_empty_feed():
    assert _skip_pinned(first_child_id=None, pinned_id=42) == 0


def test_skip_pinned_is_zero_when_the_first_child_is_not_the_pin():
    assert _skip_pinned(first_child_id=5, pinned_id=42) == 0
