"""The TUI's lists, after qobit: the feed in date groups, Topics filtered as
you type, and a session that starts where the last one ended."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

os.environ.setdefault("API_KEY", "test-key")

import pytest

from relay_tui import app as tui_app
from relay_tui.theme import load_settings
from relay_tui.widgets.post_panel import PostItem, date_group
from relay_tui.widgets.tag_panel import TagItem, TagPanel

from .test_tui_reader import WIDE, FakeApi, _pane_title, _post, _settle

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_date_groups():
    assert date_group(_ago(hours=1), NOW) == "Today"
    assert date_group(_ago(days=1), NOW) == "Yesterday"
    assert date_group(_ago(days=4), NOW) == "Earlier this week"
    assert date_group(_ago(days=20), NOW) == "Earlier this month"
    assert date_group("2026-03-02T10:00:00Z", NOW) == "March"
    assert date_group("2024-06-01T10:00:00Z", NOW) == "2024"
    assert date_group(None, NOW) == "Undated"


@pytest.fixture
def fake(monkeypatch) -> FakeApi:
    return FakeApi(monkeypatch)


def _groups(app) -> list[tuple[int, str | None]]:
    return [(c.post.id, c.group) for c in app.query_one("#post-listview").children if isinstance(c, PostItem)]


@pytest.mark.asyncio
async def test_the_feed_is_grouped_by_the_date_it_is_sorted_on(fake):
    now = datetime.now(UTC)
    fake.posts[1].updated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    fake.posts[2].updated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    fake.posts[3].updated_at = "2020-01-01T00:00:00Z"
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        # The pinned master document opens no group; Alpha opens Today, Beta is
        # in it, Gamma opens its year.
        assert _groups(app) == [(0, None), (1, "Today"), (2, None), (3, "2020")]
        app._search = "a"
        app._reload()
        await _settle(pilot)
        assert all(group is None for _, group in _groups(app))   # ranked, not dated


def _tag_rows(app) -> list[str]:
    return [c.tag_name for c in app.query_one("#tag-listview").children if isinstance(c, TagItem)]


@pytest.mark.asyncio
async def test_topics_filter_as_you_type_and_esc_clears(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        app._active_tag = "notes"   # a feed filter Esc must leave alone here
        panel = app.query_one(TagPanel)
        app.query_one("#tag-listview").focus()
        await pilot.press("slash", "o", "t", "h")
        await _settle(pilot)
        assert _tag_rows(app) == ["other"]
        assert str(panel.border_subtitle) == "/ oth_"
        await pilot.press("backspace", "backspace", "backspace", "t")   # "t" types, not Tags/Tree
        await _settle(pilot)
        assert _tag_rows(app) == ["notes", "other"]
        await pilot.press("escape")
        await _settle(pilot)
        assert _tag_rows(app) == ["", "notes", "other"]
        assert app._active_tag == "notes"


@pytest.mark.asyncio
async def test_a_new_run_starts_where_the_last_one_ended(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        app._filter_by_tag("notes")
        await _settle(pilot)
        await pilot.press("j")              # Beta
        await _settle(pilot)
        await pilot.pause(1.2)              # the open post is saved once the reader settles
        app.action_cycle_order()
        await _settle(pilot)
    saved = load_settings()
    assert saved["feed"]["tag"] == "notes" and saved["feed"]["order"] == "asc"
    assert saved["open_post"] == 2

    again = tui_app.RelayTuiApp()
    async with again.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        assert again._active_tag == "notes" and again._order == "asc"
        assert _pane_title(again) == "Beta"


@pytest.mark.asyncio
async def test_a_remembered_post_no_longer_in_the_feed_still_opens(fake):
    from relay_tui.theme import save_setting

    fake.posts[40] = _post(40, "Elsewhere", tags=["gone"])
    save_setting("feed", {"tag": "notes"})
    save_setting("open_post", 40)
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await _settle(pilot)
        assert _pane_title(app) == "Elsewhere"


@pytest.mark.asyncio
async def test_the_layout_is_remembered(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("l")
        await _settle(pilot)
    again = tui_app.RelayTuiApp()
    async with again.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        assert not again._reader_active
