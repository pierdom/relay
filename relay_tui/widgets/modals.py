from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    Markdown,
    Select,
    Static,
    TextArea,
)
from textual.containers import Horizontal, Vertical, VerticalScroll

from .. import api
from ..theme import ACCENT, BORDER, HEADER_BG
from .post_panel import _time_ago


# ── PostDetailModal ───────────────────────────────────────────────────────────


class PostDetailModal(ModalScreen[None]):
    """Full-screen modal showing the complete content of a post."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    DEFAULT_CSS = f"""
    PostDetailModal {{
        align: center middle;
    }}
    PostDetailModal > Vertical {{
        width: 92%;
        height: 92%;
        background: {HEADER_BG};
        border: solid {ACCENT};
        padding: 1 2;
    }}
    PostDetailModal .detail-title {{
        text-style: bold;
        color: {ACCENT};
        width: 1fr;
        height: auto;
    }}
    PostDetailModal .detail-meta {{
        color: #888888;
        width: 1fr;
        height: auto;
    }}
    PostDetailModal .detail-rule {{
        border-bottom: solid {BORDER};
        height: 1;
        width: 1fr;
    }}
    PostDetailModal .detail-actions {{
        height: 3;
        align: center middle;
        margin-top: 1;
    }}
    PostDetailModal VerticalScroll {{
        height: 1fr;
        width: 1fr;
        border: none;
        background: transparent;
    }}
    PostDetailModal Markdown {{
        width: 1fr;
        height: auto;
    }}
    """

    def __init__(self, post: api.Post) -> None:
        super().__init__()
        self._post = post

    def compose(self) -> ComposeResult:
        post = self._post
        title_text = post.title or post.content.split("\n")[0][:80]
        tags_str = (
            "  ".join(f"[{escape(t)}]" for t in post.tags) if post.tags else "—"
        )
        meta_parts = [
            f"#{post.id}",
            _time_ago(post.created_at),
        ]
        if post.updated_at:
            meta_parts.append(f"edited {_time_ago(post.updated_at)}")
        if post.source:
            meta_parts.append(post.source)
        meta_text = "  •  ".join(meta_parts)

        with Vertical():
            yield Label(escape(title_text), classes="detail-title")
            yield Label(
                f"[dim]{escape(meta_text)}[/dim]  {tags_str}",
                markup=True,
                classes="detail-meta",
            )
            yield Static("", classes="detail-rule")
            with VerticalScroll():
                if post.format == "markdown":
                    yield Markdown(post.content, classes="detail-content")
                else:
                    yield Static(post.content, classes="detail-content")
            with Horizontal(classes="detail-actions"):
                yield Button("Close", id="close-btn", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-btn":
            self.dismiss()


# ── ComposeModal ──────────────────────────────────────────────────────────────


class ComposeModal(ModalScreen[dict | None]):
    """Modal for composing a new post."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "submit", "Publish"),
    ]

    DEFAULT_CSS = f"""
    ComposeModal > Vertical {{
        width: 84;
        height: auto;
        max-height: 90%;
        background: {HEADER_BG};
        border: solid {ACCENT};
        padding: 1 2;
    }}
    ComposeModal .modal-title {{
        color: {ACCENT};
        text-style: bold;
        margin-bottom: 1;
    }}
    ComposeModal Input {{
        margin-bottom: 1;
        width: 1fr;
        border: solid {BORDER};
    }}
    ComposeModal Select {{
        margin-bottom: 1;
        width: 1fr;
        border: solid {BORDER};
    }}
    ComposeModal TextArea {{
        height: 12;
        width: 1fr;
        margin-bottom: 1;
        border: solid {BORDER};
    }}
    ComposeModal .modal-actions {{
        height: 3;
        align: right middle;
        margin-top: 1;
    }}
    ComposeModal .modal-actions Button {{
        margin-left: 1;
    }}
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("New Post", classes="modal-title")
            yield Input(placeholder="Title (optional)", id="title-input")
            yield Input(placeholder="Tags (comma-separated)", id="tags-input")
            yield Select(
                options=[
                    ("Markdown", "markdown"),
                    ("Plain text", "text"),
                    ("HTML", "html"),
                    ("JSON", "json"),
                ],
                value="markdown",
                id="fmt-select",
            )
            yield TextArea(id="content-input", language="markdown")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Publish", id="submit-btn", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        title = self.query_one("#title-input", Input).value.strip() or None
        tags_raw = self.query_one("#tags-input", Input).value.strip()
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
        fmt_select = self.query_one("#fmt-select", Select)
        fmt = str(fmt_select.value) if fmt_select.value is not Select.BLANK else "markdown"
        content = self.query_one("#content-input", TextArea).text
        if not content.strip():
            self.app.notify("Content cannot be empty", severity="warning")
            return
        self.dismiss(
            {
                "title": title,
                "tags": tags,
                "format": fmt,
                "content": content,
            }
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.action_cancel()
        elif event.button.id == "submit-btn":
            self.action_submit()


# ── EditModal ─────────────────────────────────────────────────────────────────


class EditModal(ModalScreen[dict | None]):
    """Modal for editing an existing post, pre-filled with current values."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "submit", "Save"),
    ]

    DEFAULT_CSS = f"""
    EditModal > Vertical {{
        width: 84;
        height: auto;
        max-height: 90%;
        background: {HEADER_BG};
        border: solid {ACCENT};
        padding: 1 2;
    }}
    EditModal .modal-title {{
        color: {ACCENT};
        text-style: bold;
        margin-bottom: 1;
    }}
    EditModal Input {{
        margin-bottom: 1;
        width: 1fr;
        border: solid {BORDER};
    }}
    EditModal Select {{
        margin-bottom: 1;
        width: 1fr;
        border: solid {BORDER};
    }}
    EditModal TextArea {{
        height: 12;
        width: 1fr;
        margin-bottom: 1;
        border: solid {BORDER};
    }}
    EditModal .modal-actions {{
        height: 3;
        align: right middle;
        margin-top: 1;
    }}
    EditModal .modal-actions Button {{
        margin-left: 1;
    }}
    """

    def __init__(self, post: api.Post) -> None:
        super().__init__()
        self._post = post

    def compose(self) -> ComposeResult:
        post = self._post
        tags_str = ", ".join(post.tags)
        with Vertical():
            yield Label(f"Edit Post #{post.id}", classes="modal-title")
            yield Input(
                value=post.title or "",
                placeholder="Title (optional)",
                id="title-input",
            )
            yield Input(
                value=tags_str,
                placeholder="Tags (comma-separated)",
                id="tags-input",
            )
            yield Select(
                options=[
                    ("Markdown", "markdown"),
                    ("Plain text", "text"),
                    ("HTML", "html"),
                    ("JSON", "json"),
                ],
                value=post.format,
                id="fmt-select",
            )
            yield TextArea(post.content, id="content-input", language="markdown")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Save", id="submit-btn", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        title = self.query_one("#title-input", Input).value.strip() or None
        tags_raw = self.query_one("#tags-input", Input).value.strip()
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
        fmt_select = self.query_one("#fmt-select", Select)
        fmt = str(fmt_select.value) if fmt_select.value is not Select.BLANK else self._post.format
        content = self.query_one("#content-input", TextArea).text
        if not content.strip():
            self.app.notify("Content cannot be empty", severity="warning")
            return
        self.dismiss(
            {
                "title": title,
                "tags": tags,
                "format": fmt,
                "content": content,
            }
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.action_cancel()
        elif event.button.id == "submit-btn":
            self.action_submit()


# ── ConfirmModal ──────────────────────────────────────────────────────────────


class ConfirmModal(ModalScreen[bool]):
    """Small confirmation dialog that returns True on confirm, False on cancel."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Confirm"),
    ]

    DEFAULT_CSS = f"""
    ConfirmModal > Vertical {{
        width: 50;
        height: auto;
        background: {HEADER_BG};
        border: solid {ACCENT};
        padding: 1 2;
    }}
    ConfirmModal .confirm-message {{
        width: 1fr;
        margin-bottom: 1;
        text-align: center;
    }}
    ConfirmModal .confirm-actions {{
        height: 3;
        align: center middle;
    }}
    ConfirmModal .confirm-actions Button {{
        margin: 0 1;
    }}
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._message, classes="confirm-message")
            with Horizontal(classes="confirm-actions"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Confirm", id="confirm-btn", variant="error")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(False)
        elif event.button.id == "confirm-btn":
            self.dismiss(True)


# ── RenameTagModal ────────────────────────────────────────────────────────────


class RenameTagModal(ModalScreen[str | None]):
    """Small dialog for renaming a tag."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = f"""
    RenameTagModal > Vertical {{
        width: 50;
        height: auto;
        background: {HEADER_BG};
        border: solid {ACCENT};
        padding: 1 2;
    }}
    RenameTagModal .rename-label {{
        color: {ACCENT};
        text-style: bold;
        margin-bottom: 1;
    }}
    RenameTagModal Input {{
        width: 1fr;
        margin-bottom: 1;
        border: solid {BORDER};
    }}
    RenameTagModal .rename-actions {{
        height: 3;
        align: right middle;
        margin-top: 1;
    }}
    RenameTagModal .rename-actions Button {{
        margin-left: 1;
    }}
    """

    def __init__(self, tag: str) -> None:
        super().__init__()
        self._tag = tag

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Rename tag: {escape(self._tag)}", markup=True, classes="rename-label")
            yield Input(value=self._tag, id="new-name")
            with Horizontal(classes="rename-actions"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Rename", id="rename-btn", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
        elif event.button.id == "rename-btn":
            new_name = self.query_one("#new-name", Input).value.strip()
            if new_name:
                self.dismiss(new_name)
            else:
                self.app.notify("Tag name cannot be empty", severity="warning")
