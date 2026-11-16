"""The TUI's three-column layout and its editor, driven through Textual's Pilot
against a fake API (no server, no SSE): the open post shows in a pane beside
the feed, links move the pane with a back trail, editing happens in place and
a save sends `if_match`, so a post changed meanwhile is not overwritten."""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from textual.widgets import Label, TextArea

from relay_tui import api
from relay_tui import app as tui_app
from relay_tui.widgets.edit_form import EditForm
from relay_tui.widgets.post_view import PostDetailModal, PostView
from relay_tui.widgets.reader import ReaderPane

WIDE, NARROW = (150, 40), (90, 40)


def _post(id: int, title: str, content: str = "", tags: list[str] | None = None) -> api.Post:
    return api.Post(id=id, title=title, content=content or f"body of {title}", tags=tags or ["notes"],
                    source=None, created_at="2026-11-01T00:00:00Z", etag=f"e{id}")


class FakeApi:
    def __init__(self, monkeypatch) -> None:
        self.posts = {
            0: _post(0, "Master Document"),
            1: _post(1, "Alpha", "see [[Gamma]]"),
            2: _post(2, "Beta"),
            3: _post(3, "Gamma", tags=["other"]),
        }
        self.saves: list[dict] = []
        self.listed: list[dict] = []
        self.deleted: dict[int, api.Post] = {}
        self.conflict_once = False
        self.history = False
        self.embeddings = False
        for name in ("list_posts", "list_tags", "list_folders", "link_targets", "get_backlinks",
                     "get_status", "get_post", "update_post", "delete_post", "create_post",
                     "list_deleted_posts", "restore_post"):
            monkeypatch.setattr(api, name, getattr(self, name))
        monkeypatch.setattr(tui_app.SSESubscriber, "start", lambda self: None)
        monkeypatch.setattr(tui_app.SSESubscriber, "stop", lambda self: None)

    def list_posts(self, tag=None, folder=None, search=None, limit=50, offset=0, sort="updated", order="desc",
                   author=None, mode="keyword"):
        self.listed.append({"tag": tag, "search": search, "author": author, "mode": mode})
        items = [p for i, p in sorted(self.posts.items())
                 if i != 0 and (tag is None or tag in p.tags) and (author is None or p.updated_by == author)]
        return items, len(items), self.posts[0]

    def list_tags(self):
        return [api.Tag(name="notes", count=2), api.Tag(name="other", count=1)]

    def list_folders(self):
        return []

    def link_targets(self):
        return [(i, p.title) for i, p in self.posts.items()]

    def get_backlinks(self, post_id):
        return []

    def get_status(self):
        return api.ServerStatus(api.CallerScope(mode="full"), history=self.history, embeddings=self.embeddings)

    def create_post(self, *, title, content, tags=None, source=None, expires_at=None):
        post = _post(max(self.posts) + 1, title, content, tags)
        self.posts[post.id] = post
        return post

    def list_deleted_posts(self, limit=50, include_expiry=False):
        return [api.DeletedPost(id=p.id, title=p.title, sha="abc", short_sha="abc", when="", reason="deleted",
                                path=f"{p.title}.md") for p in self.deleted.values()]

    def restore_post(self, post_id, sha):
        post = self.deleted.pop(post_id)
        self.posts[post_id] = post
        return post

    def get_post(self, post_id):
        return self.posts[post_id]

    def update_post(self, post_id, *, if_match=None, **fields):
        self.saves.append({"id": post_id, "if_match": if_match, **fields})
        if self.conflict_once:
            self.conflict_once = False
            current = _post(post_id, self.posts[post_id].title, "changed elsewhere")
            current.etag, current.updated_by = "e-new", "agent"
            raise api.Conflict(current)
        saved = _post(post_id, fields["title"], fields["content"], fields["tags"])
        saved.etag = f"{if_match}+1"
        self.posts[post_id] = saved
        return saved

    def delete_post(self, post_id):
        self.deleted[post_id] = self.posts.pop(post_id)


@pytest.fixture
def fake(monkeypatch) -> FakeApi:
    return FakeApi(monkeypatch)


def _pane_title(app) -> str | None:
    pane = app.query_one(ReaderPane)
    return pane.post.title if pane.post is not None else None


async def _settle(pilot) -> None:
    for _ in range(4):
        await pilot.pause()


@pytest.mark.asyncio
async def test_the_first_post_opens_in_the_pane_and_j_steps(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        assert app.query_one(ReaderPane).display
        assert _pane_title(app) == "Alpha"  # the pinned master document is skipped
        await pilot.press("j")
        await _settle(pilot)
        assert _pane_title(app) == "Beta"
        await pilot.press("enter")
        await _settle(pilot)
        assert not isinstance(app.screen, PostDetailModal)


@pytest.mark.asyncio
async def test_a_narrow_terminal_falls_back_to_the_list_and_the_modal(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=NARROW) as pilot:
        await _settle(pilot)
        assert not app.query_one(ReaderPane).display
        app.query_one("#post-listview").focus()
        await pilot.press("j", "enter")
        await _settle(pilot)
        assert isinstance(app.screen, PostDetailModal)


@pytest.mark.asyncio
async def test_l_toggles_the_layout(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("l")
        await _settle(pilot)
        assert not app.query_one(ReaderPane).display
        await pilot.press("l")
        await _settle(pilot)
        assert app.query_one(ReaderPane).display


@pytest.mark.asyncio
async def test_a_link_moves_the_pane_and_back_returns(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        view = app.query_one(ReaderPane).view
        view.post_message(PostView.OpenLink(3))
        await _settle(pilot)
        assert _pane_title(app) == "Gamma"
        assert "Alpha" in str(app.query_one(".reader-back", Label).render())
        await pilot.press("left_square_bracket")
        await _settle(pilot)
        assert _pane_title(app) == "Alpha"
        await pilot.press("right_square_bracket")
        await _settle(pilot)
        assert _pane_title(app) == "Gamma"


@pytest.mark.asyncio
async def test_a_tag_opens_its_own_first_post(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        from relay_tui.widgets.tag_panel import TagPanel
        app.query_one(TagPanel).post_message(TagPanel.TagSelected("other"))
        await _settle(pilot)
        assert _pane_title(app) == "Gamma"


@pytest.mark.asyncio
async def test_editing_happens_in_the_pane_and_sends_if_match(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("e")
        await _settle(pilot)
        pane = app.query_one(ReaderPane)
        assert pane.editing and not app.screen.is_modal
        pane.query_one("#content-input", TextArea).text = "after the edit"
        await pilot.press("ctrl+s")
        await _settle(pilot)
        assert fake.saves[-1]["if_match"] == "e1"
        assert not pane.editing
        assert pane.post.content == "after the edit"


@pytest.mark.asyncio
async def test_a_conflicting_save_keeps_the_edit_and_a_second_save_overwrites(fake):
    fake.conflict_once = True
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("e")
        await _settle(pilot)
        pane = app.query_one(ReaderPane)
        pane.query_one("#content-input", TextArea).text = "mine"
        await pilot.press("ctrl+s")
        await _settle(pilot)
        form = pane.query_one(EditForm)
        assert form.has_class("-conflict")
        assert "by agent" in str(form.query_one("#ef-conflict", Label).render())
        assert pane.query_one("#content-input", TextArea).text == "mine"
        await pilot.press("ctrl+s")
        await _settle(pilot)
        assert fake.saves[-1]["if_match"] == "e-new"
        assert pane.post.content == "mine"


@pytest.mark.asyncio
async def test_deleting_opens_the_neighbour_then_the_empty_state(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        from relay_tui.widgets.tag_panel import TagPanel
        app.query_one(TagPanel).post_message(TagPanel.TagSelected("notes"))
        await _settle(pilot)
        assert _pane_title(app) == "Alpha"
        app._on_post_deleted(1)
        await _settle(pilot)
        assert _pane_title(app) == "Beta"
        app._on_post_deleted(2)
        await _settle(pilot)
        assert _pane_title(app) is None
        assert app.query_one(ReaderPane).has_class("-empty")


def test_a_409_becomes_a_conflict_carrying_the_current_post(monkeypatch):
    class Resp:
        status_code = 409

        def json(self):
            return {"detail": {"error": "changed", "current": {"id": 5, "title": "T", "etag": "now"}}}

    sent = {}

    def fake_patch(url, headers, json, timeout):
        sent.update(json)
        return Resp()

    monkeypatch.setattr(api.requests, "patch", fake_patch)
    with pytest.raises(api.Conflict) as exc:
        api.update_post(5, content="x", if_match="before")
    assert sent["if_match"] == "before"
    assert exc.value.current is not None and exc.value.current.etag == "now"
