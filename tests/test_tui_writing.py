"""The TUI's second catch-up with the browser: the Ctrl+K switcher, search
modes, the writer filter, Undo after a delete, and the editor's writing aids
(link and tag completion, preview, the Compose draft)."""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from textual.widgets import Input, OptionList, Select, TextArea

from relay_tui import app as tui_app
from relay_tui import drafts
from relay_tui.completion import TagSuggester, accepted_text, link_span, match_posts
from relay_tui.widgets.edit_form import EditForm
from relay_tui.widgets.modals import ConfirmModal, EditModal, SearchModal
from relay_tui.widgets.reader import ReaderPane
from relay_tui.widgets.switcher import SwitcherModal

from .test_tui_reader import WIDE, FakeApi, _pane_title, _settle

TITLES = {1: "Alpha", 2: "Beta", 3: "Gamma", 12: "Alphabet Soup", 40: "The Alpha Plan"}


# ── pure matching ─────────────────────────────────────────────────────────────


def test_match_ranks_exact_id_then_prefix_then_substring():
    assert [i for i, _ in match_posts(TITLES, "alpha")] == [1, 12, 40]
    assert [i for i, _ in match_posts(TITLES, "#12")][0] == 12
    assert match_posts(TITLES, "zzz") == []
    assert len(match_posts(TITLES, "", limit=2)) == 2


def test_link_span_finds_wikilinks_and_id_refs_but_not_headings():
    span = link_span("see [[Ga", "")
    assert span and span.kind == "wiki" and span.query == "Ga" and span.pick
    assert accepted_text(span, 3, "Gamma") == "Gamma]]"
    closing = link_span("see [[Ga", "]] more")
    assert closing and closing.end == len("see [[Ga") + 2   # replaces the closing brackets too
    ref = link_span("as in #alp", "")
    assert ref and ref.kind == "id" and not ref.pick and accepted_text(ref, 1, "Alpha") == "#1"
    assert link_span("# Heading", "") is None
    assert link_span("a#1", "") is None


@pytest.mark.asyncio
async def test_tag_suggestions_complete_the_tag_being_typed():
    s = TagSuggester(["homelab", "home", "finance"])
    assert await s.get_suggestion("fin") == "finance"
    assert await s.get_suggestion("finance, hom") == "finance, home"
    assert await s.get_suggestion("home, hom") == "home, homelab"   # skips one already given
    assert await s.get_suggestion("finance, ") is None


def test_the_draft_round_trips_and_an_empty_one_is_dropped():
    assert drafts.load() is None
    drafts.save({"title": "Half", "content": "written", "tags": ""})
    assert drafts.load() == {"title": "Half", "content": "written", "tags": ""}
    drafts.save({"title": " ", "content": ""})
    assert drafts.load() is None


# ── in the app ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake(monkeypatch) -> FakeApi:
    return FakeApi(monkeypatch)


@pytest.mark.asyncio
async def test_ctrl_k_jumps_to_a_post_and_never_opens_over_an_editor(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("ctrl+k")
        await _settle(pilot)
        assert isinstance(app.screen, SwitcherModal)
        await pilot.press(*"gam", "enter")
        await _settle(pilot)
        assert _pane_title(app) == "Gamma"
        await pilot.press("e")
        await _settle(pilot)
        app.action_switcher()   # the TextArea keeps Ctrl+K for itself; the action refuses too
        await _settle(pilot)
        assert not isinstance(app.screen, SwitcherModal)


@pytest.mark.asyncio
async def test_search_offers_modes_only_with_semantic_search_on(fake):
    fake.embeddings = True
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("slash")
        await _settle(pilot)
        modal = app.screen
        assert isinstance(modal, SearchModal)
        assert modal.query_one("#search-mode", Select).value == "hybrid"
        modal.query_one("#search-input", Input).value = "wire"
        modal.query_one("#search-mode", Select).value = "semantic"
        await pilot.press("enter")
        await _settle(pilot)
        assert fake.listed[-1]["search"] == "wire" and fake.listed[-1]["mode"] == "semantic"


@pytest.mark.asyncio
async def test_search_without_semantic_search_is_keyword(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("slash")
        await _settle(pilot)
        assert not app.screen.query(Select)
        await pilot.press(*"alp", "enter")
        await _settle(pilot)
        assert fake.listed[-1]["mode"] == "keyword"


@pytest.mark.asyncio
async def test_b_filters_by_the_open_posts_writer_and_again_clears(fake):
    fake.posts[1].updated_by = "agent"
    fake.posts[3].updated_by = "agent"
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("b")
        await _settle(pilot)
        assert fake.listed[-1]["author"] == "agent"
        assert app.query_one("#post-listview").children[1].post.id == 1
        assert len(app.query_one("#post-listview").children) == 3   # master + the two by agent
        await pilot.press("b")
        await _settle(pilot)
        assert fake.listed[-1]["author"] is None


@pytest.mark.asyncio
async def test_with_history_a_delete_asks_nothing_and_u_undoes_it(fake):
    fake.history = True
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("d")
        await _settle(pilot)
        assert not isinstance(app.screen, ConfirmModal)
        assert 1 not in fake.posts and _pane_title(app) == "Beta"
        await pilot.press("u")
        await _settle(pilot)
        assert 1 in fake.posts
        assert app.query_one("#post-listview").children[1].post.id == 1


@pytest.mark.asyncio
async def test_without_history_a_delete_asks_by_name(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("d")
        await _settle(pilot)
        assert isinstance(app.screen, ConfirmModal)
        assert "Alpha" in app.screen._message and 1 in fake.posts


@pytest.mark.asyncio
async def test_compose_keeps_a_draft_across_esc_and_drops_it_on_publish(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("n")
        await _settle(pilot)
        assert isinstance(app.screen, EditModal)
        form = app.screen.query_one(EditForm)
        form.query_one("#title-input", Input).value = "Fresh Note"
        form.query_one("#content-input", TextArea).text = "half written"
        await pilot.press("escape")
        await _settle(pilot)
        assert not isinstance(app.screen, EditModal)
        assert drafts.load()["title"] == "Fresh Note"
        await pilot.press("n")
        await _settle(pilot)
        form = app.screen.query_one(EditForm)
        assert form.query_one("#content-input", TextArea).text == "half written"
        await pilot.press("ctrl+s")
        await _settle(pilot)
        assert drafts.load() is None
        assert _pane_title(app) == "Fresh Note"


@pytest.mark.asyncio
async def test_link_completion_in_the_editor(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("e")
        await _settle(pilot)
        pane = app.query_one(ReaderPane)
        form = pane.query_one(EditForm)
        ta = form.query_one("#content-input", TextArea)
        ta.text = "see "
        ta.move_cursor((0, 4))
        await pilot.press("left_square_bracket", "left_square_bracket", "G", "a")
        await _settle(pilot)
        assert form.has_class("-completing")
        assert form.query_one("#ef-complete", OptionList).option_count == 1
        await pilot.press("tab")
        await _settle(pilot)
        assert ta.text == "see [[Gamma]]"
        assert not form.has_class("-completing")
        # A #word preselects nothing: Enter stays a new line.
        await pilot.press("space", "#", "a", "l")
        await _settle(pilot)
        assert form.has_class("-completing")
        await pilot.press("enter")
        await _settle(pilot)
        assert ta.text.endswith("#al\n")


@pytest.mark.asyncio
async def test_ctrl_t_previews_the_text(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("e")
        await _settle(pilot)
        form = app.query_one(ReaderPane).query_one(EditForm)
        await pilot.press("ctrl+t")
        await _settle(pilot)
        assert form.has_class("-preview")
        assert not form.query_one("#content-input", TextArea).display
        await pilot.press("ctrl+t")
        await _settle(pilot)
        assert not form.has_class("-preview")
