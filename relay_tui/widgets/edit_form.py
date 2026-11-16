"""The post editor, shared by the edit modal and the reader pane.

It saves itself, sending the etag it opened on as ``if_match``: a post that
changed meanwhile (an agent's append, an Obsidian save) is not overwritten.
The form says so and keeps the edit, and a second save overwrites on purpose.
"""
from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Input, Label, TextArea

from .. import api
from ..theme import ACCENT, BORDER


def _tags(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


class EditForm(Vertical):
    class Saved(Message):
        def __init__(self, post: api.Post) -> None:
            super().__init__()
            self.post = post

    class Cancelled(Message):
        """The user left the editor (after confirming, when it had changes)."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    DEFAULT_CSS = f"""
    EditForm {{ height: auto; }}
    EditForm .modal-title {{ color: {ACCENT}; text-style: bold; margin-bottom: 1; }}
    EditForm Input {{ margin-bottom: 1; width: 1fr; border: solid {BORDER}; }}
    EditForm TextArea {{ height: 12; width: 1fr; margin-bottom: 1; border: solid {BORDER}; }}
    EditForm .ef-conflict {{ color: $warning; height: auto; display: none; }}
    EditForm.-conflict .ef-conflict {{ display: block; }}
    EditForm .modal-actions {{ height: 3; align: right middle; margin-top: 1; }}
    EditForm .modal-actions Button {{ margin-left: 1; }}
    """

    def __init__(self, post: api.Post, **kwargs) -> None:
        super().__init__(**kwargs)
        self.post = post
        self._etag = post.etag
        self._saving = False

    def compose(self) -> ComposeResult:
        post = self.post
        yield Label(f"Edit Post #{post.id}", classes="modal-title")
        yield Input(value=post.title or "", placeholder="Title (required — becomes the filename)", id="title-input")
        yield Input(value=", ".join(post.tags), placeholder="Tags (comma-separated)", id="tags-input")
        yield Input(value=post.source or "", placeholder="Source (optional)", id="source-input")
        yield TextArea(post.content, id="content-input", language="markdown")
        yield Input(value=post.expires_at or "", placeholder="Expires at (ISO, optional)", id="expires-input")
        yield Label("", classes="ef-conflict", id="ef-conflict")
        with Horizontal(classes="modal-actions"):
            yield Button("Cancel", id="cancel-btn", variant="default")
            yield Button("Save", id="submit-btn", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#content-input", TextArea).focus()

    def values(self) -> dict:
        return {
            "title": self.query_one("#title-input", Input).value.strip(),
            "tags": _tags(self.query_one("#tags-input", Input).value),
            "source": self.query_one("#source-input", Input).value.strip() or None,
            "content": self.query_one("#content-input", TextArea).text,
            "expires_at": self.query_one("#expires-input", Input).value.strip() or None,
        }

    @property
    def is_dirty(self) -> bool:
        v, p = self.values(), self.post
        return (v["title"], v["tags"], v["source"], v["content"], v["expires_at"]) != (
            p.title or "", p.tags, p.source, p.content, p.expires_at
        )

    def action_cancel(self) -> None:
        if not self.is_dirty:
            self.post_message(self.Cancelled())
            return
        from .modals import ConfirmModal  # modals imports this module

        def _confirmed(ok: bool | None) -> None:
            if ok:
                self.post_message(self.Cancelled())

        self.app.push_screen(ConfirmModal("Discard your changes?"), _confirmed)

    def action_save(self) -> None:
        if self._saving:
            return
        v = self.values()
        if not v["title"]:
            self.app.notify("Title is required", severity="warning")
            return
        if not v["content"].strip():
            self.app.notify("Content cannot be empty", severity="warning")
            return
        self._saving = True
        self._save(v, self._etag)

    @work(thread=True)
    def _save(self, v: dict, etag: str) -> None:
        try:
            post = api.update_post(self.post.id, if_match=etag or None, **v)
        except api.Conflict as exc:
            self.app.call_from_thread(self._conflict, exc.current)
            return
        except Exception as exc:
            self.app.call_from_thread(self._failed, exc)
            return
        self.app.call_from_thread(self._saved, post)

    def _conflict(self, current: api.Post | None) -> None:
        self._saving = False
        # The next save overwrites on purpose: it carries the version it was told about.
        if current is not None:
            self._etag = current.etag
        who = f" by {current.updated_by}" if current is not None and current.updated_by else ""
        self.query_one("#ef-conflict", Label).update(
            f"This post changed{who} since you opened it. Save again to overwrite it."
        )
        self.add_class("-conflict")

    def _failed(self, exc: Exception) -> None:
        self._saving = False
        self.app.notify(f"Update failed: {exc}", severity="error")

    def _saved(self, post: api.Post) -> None:
        self._saving = False
        self.post_message(self.Saved(post))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel-btn":
            self.action_cancel()
        elif event.button.id == "submit-btn":
            self.action_save()
