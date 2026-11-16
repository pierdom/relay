"""The TUI's fourth catch-up: live posts held back from a filtered feed, file
attachments from the editor, related posts, the outline, filtering by a
post's tag, copying, and the semantic-search switches in the status panel."""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from textual.widgets import Input, TextArea

from relay_tui import api
from relay_tui import app as tui_app
from relay_tui.completion import PathSuggester
from relay_tui.render import code_blocks
from relay_tui.widgets.edit_form import AttachFileModal, EditForm
from relay_tui.widgets.picker import PickerModal
from relay_tui.widgets.post_panel import PostPanel
from relay_tui.widgets.reader import ReaderPane
from relay_tui.widgets.status import StatusModal, fmt_backfill

from .test_tui_reader import WIDE, FakeApi, _pane_title, _post, _settle
from .test_tui_status import STATUS

# ── pure ──────────────────────────────────────────────────────────────────────


def test_code_blocks_are_found_in_order_with_their_language():
    text = "intro\n```bash\nls -la\npwd\n```\nmid\n~~~\nplain\n~~~\n```py\nunclosed"
    assert code_blocks(text) == [("bash", "ls -la\npwd"), ("", "plain")]
    assert code_blocks("no code") == []


@pytest.mark.asyncio
async def test_paths_complete_from_the_filesystem(tmp_path):
    (tmp_path / "diagram.png").write_bytes(b"x")
    (tmp_path / "docs").mkdir()
    (tmp_path / ".hidden").write_text("")
    s = PathSuggester()
    assert await s.get_suggestion(f"{tmp_path}/dia") == f"{tmp_path}/diagram.png"
    assert await s.get_suggestion(f"{tmp_path}/do") == f"{tmp_path}/docs/"
    assert await s.get_suggestion(f"{tmp_path}/.hi") == f"{tmp_path}/.hidden"
    assert await s.get_suggestion(f"{tmp_path}/zz") is None


def test_backfill_reads_like_the_browsers():
    assert fmt_backfill({}) == "never run"
    assert fmt_backfill({"running": True, "checked": 3, "total": 9}) == "running — 3/9 checked"


# ── in the app ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake(monkeypatch) -> FakeApi:
    f = FakeApi(monkeypatch)
    f.posts[1] = _post(1, "Alpha", "see [[Gamma]]\n\n## One\n\ntext\n\n## Two\n\n```sh\necho hi\n```\n",
                       ["notes", "ops"])
    f.related_calls = []
    f.uploads = []
    f.embedding_calls = []

    def get_related(post_id):
        f.related_calls.append(post_id)
        return [(2, "Beta")]

    def upload_attachment(path, *, post_id=None, tags=None):
        f.uploads.append({"path": path, "post_id": post_id, "tags": tags})
        return {"filename": os.path.basename(path), "ref": f"![[{os.path.basename(path)}]]"}

    monkeypatch.setattr(api, "get_related", get_related)
    monkeypatch.setattr(api, "upload_attachment", upload_attachment)
    monkeypatch.setattr(api, "get_status_report", lambda: STATUS)
    monkeypatch.setattr(api, "get_lint", lambda: {"items": [], "checked_posts": 0, "skipped_rules": []})
    monkeypatch.setattr(api, "list_changes", lambda limit=12: [])
    monkeypatch.setattr(api, "set_embeddings_enabled", lambda on: f.embedding_calls.append(on) or {})
    return f


def _feed_ids(app) -> list[int]:
    return [c.post.id for c in app.query_one("#post-listview").children]


@pytest.mark.asyncio
async def test_a_live_post_waits_behind_a_count_in_a_filtered_feed(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        app._search = "alp"
        app._reload()
        await _settle(pilot)
        before = _feed_ids(app)
        app._prepend_post(_post(50, "Unrelated", "nothing to do with it"))
        await _settle(pilot)
        assert _feed_ids(app) == before   # not dropped into a search it may not match
        assert "1 new" in str(app.query_one(PostPanel).border_subtitle)
        app._search = None
        app._reload()
        await _settle(pilot)
        assert "new" not in str(app.query_one(PostPanel).border_subtitle)
        app._prepend_post(_post(51, "Fresh"))
        await _settle(pilot)
        assert 51 in _feed_ids(app)       # the plain feed takes it at the top
        app._active_tag = "other"
        app._prepend_post(_post(52, "Elsewhere", tags=["notes"]))
        await _settle(pilot)
        assert 52 not in _feed_ids(app) and not app._pending_ids   # another tag's post: never this feed's


@pytest.mark.asyncio
async def test_a_post_is_counted_once_however_often_it_arrives(fake):
    """A create under a filter is followed by its own echo on the stream, and
    an agent may save one post several times: one post, one count."""
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        app._search = "alp"
        app._reload()
        await _settle(pilot)
        created = _post(60, "Mine")
        app._on_post_created(created)
        app._prepend_post(created)            # the stream's echo of it
        app._prepend_post(_post(61, "Other"))
        app._prepend_post(_post(61, "Other, saved again"))
        await _settle(pilot)
        assert "2 new" in str(app.query_one(PostPanel).border_subtitle)


@pytest.mark.asyncio
async def test_an_upload_that_outlives_its_editor_still_says_where_it_went(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("e")
        await _settle(pilot)
        form = app.query_one(ReaderPane).query_one(EditForm)
        await pilot.press("escape")      # unchanged, so it closes at once
        await _settle(pilot)
        assert not form.is_attached
        form._insert_ref("![[late.png]]")   # what the upload worker calls when it finishes
        await _settle(pilot)
        assert any("![[late.png]]" in str(n.message) for n in app._notifications)


@pytest.mark.asyncio
async def test_related_posts_show_with_semantic_search_on_and_f_follows_them(fake):
    fake.embeddings = True
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.pause(0.4)   # the backlink/related fetch waits for the selection to settle
        await _settle(pilot)
        view = app.query_one(ReaderPane).view
        assert fake.related_calls and view._related == [(2, "Beta")]
        await pilot.press("f")
        await _settle(pilot)
        labels = [o[2] for o in app.screen._items]
        assert "≈" in labels


@pytest.mark.asyncio
async def test_related_posts_are_not_asked_for_with_semantic_search_off(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.pause(0.4)
        await _settle(pilot)
        assert fake.related_calls == []


@pytest.mark.asyncio
async def test_g_lists_the_outline(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("g")
        await _settle(pilot)
        assert isinstance(app.screen, PickerModal)
        assert [label for label, _ in app.screen._items] == ["One", "Two"]
        await pilot.press("enter")
        await _settle(pilot)
        assert not isinstance(app.screen, PickerModal)


@pytest.mark.asyncio
async def test_hash_filters_by_one_of_the_posts_tags(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("number_sign")
        await _settle(pilot)
        assert isinstance(app.screen, PickerModal)   # Alpha has two tags: which?
        await pilot.press(*"ops", "enter")
        await _settle(pilot)
        assert fake.listed[-1]["tag"] == "ops"
        assert _pane_title(app) == "Alpha"


@pytest.mark.asyncio
async def test_y_copies_a_code_block_or_the_whole_post(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("y")
        await _settle(pilot)
        assert isinstance(app.screen, PickerModal)
        await pilot.press("down", "enter")
        await _settle(pilot)
        assert app.clipboard == "echo hi"


@pytest.mark.asyncio
async def test_ctrl_o_attaches_a_file_and_inserts_its_embed(fake, tmp_path):
    f = tmp_path / "diagram.png"
    f.write_bytes(b"\x89PNG")
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("e")
        await _settle(pilot)
        form = app.query_one(ReaderPane).query_one(EditForm)
        ta = form.query_one("#content-input", TextArea)
        ta.move_cursor((0, len(ta.document.get_line(0))))
        await pilot.press("ctrl+o")
        await _settle(pilot)
        assert isinstance(app.screen, AttachFileModal)
        app.screen.query_one("#af-path", Input).value = str(f)
        await pilot.press("enter")
        await _settle(pilot)
        assert fake.uploads == [{"path": str(f), "post_id": 1, "tags": None}]
        assert ta.document.get_line(1) == "![[diagram.png]]"


@pytest.mark.asyncio
async def test_a_new_post_attaches_by_its_tags(fake, tmp_path):
    f = tmp_path / "notes.pdf"
    f.write_bytes(b"%PDF")
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("n")
        await _settle(pilot)
        form = app.screen.query_one(EditForm)
        form.query_one("#tags-input", Input).value = "finance, reading"
        form.query_one("#content-input", TextArea).focus()
        await pilot.press("ctrl+o")
        await _settle(pilot)
        app.screen.query_one("#af-path", Input).value = str(f)
        await pilot.press("enter")
        await _settle(pilot)
        assert fake.uploads[-1]["tags"] == ["finance", "reading"] and fake.uploads[-1]["post_id"] is None


@pytest.mark.asyncio
async def test_s_in_the_status_panel_switches_semantic_search(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("i")
        await _settle(pilot)
        assert isinstance(app.screen, StatusModal)
        await pilot.press("s")
        await _settle(pilot)
        assert fake.embedding_calls == [True]   # STATUS has it off, so s turns it on
