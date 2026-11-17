"""The TUI's navigation, after qobit: a trail of where you are in the top bar,
one rule for Escape, a footer that lists only the keys that apply, an `i` menu
of every action for what's highlighted, and page keys that move the selection."""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest

from relay_tui import app as tui_app
from relay_tui.widgets.menu import ActionMenu
from relay_tui.widgets.modals import RenameTagModal
from relay_tui.widgets.post_view import PostView
from relay_tui.widgets.reader import ReaderPane
from relay_tui.widgets.topbar import Breadcrumb, StatusMark

from .test_tui_reader import WIDE, FakeApi, _pane_title, _post, _settle


@pytest.fixture
def fake(monkeypatch) -> FakeApi:
    return FakeApi(monkeypatch)


def _trail(app) -> str:
    return app.query_one(Breadcrumb).text


async def _follow_to_gamma(app, pilot) -> None:
    app.query_one(ReaderPane).view.post_message(PostView.OpenLink(3))
    await _settle(pilot)


@pytest.mark.asyncio
async def test_the_trail_names_the_filter_and_the_links_followed(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        assert _trail(app) == "All posts › Alpha"
        app._filter_by_tag("notes")
        await _settle(pilot)
        assert _trail(app) == "All posts › #notes › Alpha"
        await _follow_to_gamma(app, pilot)
        assert _trail(app) == "All posts › #notes › Alpha › Gamma"
        crumbs = app.query_one(Breadcrumb)._crumbs
        crumbs[2].go()               # "Alpha": back along the trail
        await _settle(pilot)
        assert _pane_title(app) == "Alpha"
        app.query_one(Breadcrumb)._crumbs[0].go()   # "All posts": off the filter
        await _settle(pilot)
        assert app._active_tag is None and _trail(app) == "All posts › Alpha"


@pytest.mark.asyncio
async def test_escape_steps_back_one_level_at_a_time(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        app.query_one("#post-listview").focus()
        app._filter_by_tag("notes")
        await _settle(pilot)
        app._search = "alp"
        app._reload()
        await _settle(pilot)
        await _follow_to_gamma(app, pilot)
        await pilot.press("escape")          # 1. back along the link
        await _settle(pilot)
        assert _pane_title(app) == "Alpha"
        await pilot.press("escape")          # 2. off the search
        await _settle(pilot)
        assert app._search is None and app._active_tag == "notes"
        await pilot.press("escape")          # 3. off the filter
        await _settle(pilot)
        assert app._active_tag is None


@pytest.mark.asyncio
async def test_the_footer_lists_only_keys_that_apply(fake):
    fake.history = True
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        assert app.check_action("follow_link", ()) is True
        assert app.check_action("pane_back", ()) is False     # no link followed yet
        assert app.check_action("undo_delete", ()) is False   # nothing deleted yet
        await _follow_to_gamma(app, pilot)
        assert app.check_action("pane_back", ()) is True
        await pilot.press("d")
        await _settle(pilot)
        assert app.check_action("undo_delete", ()) is True
        await pilot.press("e")
        await _settle(pilot)
        assert app.check_action("follow_link", ()) is False   # the editor is open


@pytest.mark.asyncio
async def test_i_offers_the_posts_actions_and_a_number_runs_one(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        app.query_one("#post-listview").focus()
        await pilot.press("i")
        await _settle(pilot)
        assert isinstance(app.screen, ActionMenu)
        assert app.screen._title == "Alpha"
        assert [i.action for i in app.screen._numbered][:2] == ["edit_post", "delete_post"]
        await pilot.press("1")
        await _settle(pilot)
        assert app.query_one(ReaderPane).editing


@pytest.mark.asyncio
async def test_i_on_a_tag_offers_the_tags_actions(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        tags = app.query_one("#tag-listview")
        tags.focus()
        tags.index = 1   # "notes" (0 is all posts)
        await _settle(pilot)
        await pilot.press("i")
        await _settle(pilot)
        assert app.screen._title == "#notes"
        await pilot.press("3")
        await _settle(pilot)
        assert isinstance(app.screen, RenameTagModal)


@pytest.mark.asyncio
async def test_page_keys_move_the_selection(fake):
    for i in range(10, 40):
        fake.posts[i] = _post(i, f"Post {i}")
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        feed = app.query_one("#post-listview")
        feed.focus()
        await pilot.press("end")
        await _settle(pilot)
        assert feed.index == len(feed.children) - 1
        await pilot.press("home")
        await _settle(pilot)
        assert feed.index == 0
        await pilot.press("pagedown")
        await _settle(pilot)
        assert feed.index > 1


@pytest.mark.asyncio
async def test_the_live_dot_pulses_as_changes_arrive(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        mark = app.query_one(StatusMark)
        app._set_live_status(True)
        assert mark.text.startswith("● live")
        app._prepend_post(_post(70, "Arrived"))
        assert mark._pulsing
