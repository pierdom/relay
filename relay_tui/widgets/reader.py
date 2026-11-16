"""The three-column layout's note column: the open post, in place of a modal.

A pick in the feed shows that post and forgets the link trail; a followed link
moves the pane and can be walked back (``[``) and forward (``]``). Editing
happens here too, in the same `EditForm` the edit modal uses.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static

from .. import api
from .edit_form import EditForm
from .post_view import PostView


class ReaderPane(Vertical):
    class Saved(Message):
        def __init__(self, post: api.Post) -> None:
            super().__init__()
            self.post = post

    class EditorClosed(Message):
        """The pane's editor closed, saved or not."""

    class Moved(Message):
        """The pane shows another post, or none, or opened or closed its
        editor — the trail and the footer's keys follow it."""

    DEFAULT_CSS = """
    ReaderPane { padding: 0 2; }
    ReaderPane > .reader-empty {
        height: 1fr; width: 1fr; content-align: center middle;
        color: $relay-muted; text-style: italic; display: none;
    }
    ReaderPane.-empty > .reader-empty { display: block; }
    ReaderPane.-empty > PostView, ReaderPane.-editing > PostView { display: none; }
    ReaderPane > EditForm { height: 1fr; }
    ReaderPane > EditForm TextArea, ReaderPane > EditForm #ef-preview { height: 1fr; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._back: list[api.Post] = []
        self._forward: list[api.Post] = []
        self.link_index: dict[str, int] = {}
        self.link_titles: dict[int, str] = {}

    def compose(self) -> ComposeResult:
        yield Static("No note selected\n\nj / k to move through the list", classes="reader-empty")
        yield PostView()

    def on_mount(self) -> None:
        self.add_class("-empty", "panel")

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def view(self) -> PostView:
        return self.query_one(PostView)

    @property
    def post(self) -> api.Post | None:
        return None if self.has_class("-empty") else self.view.post

    @property
    def editing(self) -> bool:
        return self.has_class("-editing")

    @property
    def trail(self) -> list[api.Post]:
        """The posts followed through to reach this one, oldest first."""
        return list(self._back)

    @property
    def can_go_forward(self) -> bool:
        return bool(self._forward)

    def back_to(self, depth: int) -> None:
        """Walk the trail back until ``depth`` posts precede the open one."""
        while len(self._back) > depth and self.go_back():
            pass

    def _display(self, post: api.Post) -> None:
        self.remove_class("-empty")
        self.view.show(post, self.link_index, self.link_titles)
        self.post_message(self.Moved())

    def show(self, post: api.Post) -> None:
        """A pick from the feed: the link trail starts over."""
        self._back.clear()
        self._forward.clear()
        self._display(post)

    def navigate(self, post: api.Post) -> None:
        """A followed link: the current post goes on the back trail."""
        current = self.post
        if current is not None and current.id != post.id:
            self._back.append(current)
            self._forward.clear()
        self._display(post)

    def go_back(self) -> bool:
        current = self.post
        if not self._back or current is None:
            return False
        self._forward.append(current)
        self._display(self._back.pop())
        return True

    def go_forward(self) -> bool:
        current = self.post
        if not self._forward or current is None:
            return False
        self._back.append(current)
        self._display(self._forward.pop())
        return True

    def refresh_post(self, post: api.Post) -> None:
        """A newer copy of a post (a live edit, a restore): redraw it if open."""
        self._back = [post if p.id == post.id else p for p in self._back]
        self._forward = [post if p.id == post.id else p for p in self._forward]
        if self.post is not None and self.post.id == post.id and not self.editing:
            self._display(post)

    def set_empty(self) -> None:
        self._back.clear()
        self._forward.clear()
        self.add_class("-empty")
        self.post_message(self.Moved())

    # ── editing ──────────────────────────────────────────────────────────────

    def edit(self) -> bool:
        post = self.post
        if post is None or self.editing:
            return False
        self.add_class("-editing")
        self.mount(EditForm(post))
        self.post_message(self.Moved())
        return True

    def _close_editor(self) -> None:
        self.remove_class("-editing")
        for form in self.query(EditForm):
            form.remove()
        self.post_message(self.EditorClosed())
        self.post_message(self.Moved())

    def on_edit_form_saved(self, event: EditForm.Saved) -> None:
        event.stop()
        self._close_editor()
        self._display(event.post)
        self.post_message(self.Saved(event.post))

    def on_edit_form_cancelled(self, event: EditForm.Cancelled) -> None:
        event.stop()
        self._close_editor()
