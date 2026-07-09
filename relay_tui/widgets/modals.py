from __future__ import annotations

import re

from rich.markup import escape
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    Markdown,
    Static,
    TextArea,
)
from textual.containers import Horizontal, Vertical, VerticalScroll

from .. import api
from ..theme import ACCENT, BORDER, HEADER_BG, SCREEN_BG
from .post_panel import _time_ago, _time_until


# ── Wikilink preprocessing ────────────────────────────────────────────────────

_WIKI_RE = re.compile(r"\[\[([^\]|#]+?)(#[^\]|]+)?(?:\|([^\]]+))?\]\]")
_IDREF_RE = re.compile(r"(?<![\w#])#(\d{1,5})\b")
_CODE_SPLIT = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)


def _linkify_markdown(content: str, index: dict[str, int]) -> str:
    """Turn ``[[Title]]`` / ``#NNN`` into ``[label](relay:ID)`` links.

    Resolution is by title (case-insensitive). Broken wikilinks degrade to plain
    text; unknown ``#NNN`` are left untouched. Code spans/blocks are skipped.
    """
    ids = set(index.values())

    def convert(text: str) -> str:
        def wiki(m: re.Match) -> str:
            target = m.group(1).strip()
            alias = (m.group(3) or target).strip()
            pid = index.get(target.lower())
            return f"[{alias}](relay:{pid})" if pid is not None else alias

        def idref(m: re.Match) -> str:
            n = m.group(1)
            return f"[#{n}](relay:{n})" if int(n) in ids else m.group(0)

        return _IDREF_RE.sub(idref, _WIKI_RE.sub(wiki, text))

    return "".join(
        part if i % 2 else convert(part)
        for i, part in enumerate(_CODE_SPLIT.split(content))
    )


# ── PostDetailModal ───────────────────────────────────────────────────────────


class PostDetailModal(ModalScreen[None]):
    """Full-screen modal showing the complete content of a post."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    DEFAULT_CSS = f"""
    PostDetailModal {{
        align: center middle;
        background: {SCREEN_BG};
    }}
    PostDetailModal > Vertical {{
        width: 92%;
        height: 92%;
        background: {SCREEN_BG};
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

    def __init__(self, post: api.Post, link_index: dict[str, int] | None = None) -> None:
        super().__init__()
        self._post = post
        self._link_index = link_index or {}

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
        if post.expires_at:
            meta_parts.append(f"expires {_time_until(post.expires_at)}")
        meta_text = "  •  ".join(meta_parts)

        is_master = post.id == 0
        id_badge = f"[on {BORDER}] #{post.id} [/on {BORDER}]"
        master_label = f"  [bold {ACCENT}]✦ MASTER DOCUMENT[/]" if is_master else ""
        with Vertical():
            yield Label(
                f"{id_badge}{master_label}  [bold]{escape(title_text)}[/]",
                markup=True,
                classes="detail-title",
            )
            yield Label(
                f"[dim]{escape(meta_text)}[/dim]  {tags_str}",
                markup=True,
                classes="detail-meta",
            )
            yield Static("", classes="detail-rule")
            with VerticalScroll():
                # open_links=False: we route clicks ourselves (relay: internally,
                # real URLs to the browser) so the widget never auto-opens relay:N.
                yield Markdown(
                    _linkify_markdown(post.content, self._link_index),
                    classes="detail-content",
                    open_links=False,
                )
                yield Markdown("", id="backlinks", classes="detail-content", open_links=False)
            with Horizontal(classes="detail-actions"):
                yield Button("Close", id="close-btn", variant="default")

    def on_mount(self) -> None:
        self._load_backlinks()

    @work(thread=True)
    def _load_backlinks(self) -> None:
        try:
            items = api.get_backlinks(self._post.id)
        except Exception:
            return
        if not items:
            return
        md = "\n---\n\n**Linked mentions**\n\n" + "\n".join(
            f"- [#{i} {t}](relay:{i})" for i, t in items
        )
        self.app.call_from_thread(self.query_one("#backlinks", Markdown).update, md)

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        event.stop()
        href = event.href
        if href.startswith("relay:"):
            try:
                self.app.open_post(int(href.split(":", 1)[1]))
            except (ValueError, AttributeError):
                pass
        else:
            # real external URL — hand off to the OS browser
            self.app.open_url(href)

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
            yield Input(placeholder="Title (required — becomes the filename)", id="title-input")
            yield Input(placeholder="Tags (comma-separated)", id="tags-input")
            yield Input(placeholder="Source (optional)", id="source-input")
            yield TextArea(id="content-input", language="markdown")
            yield Input(placeholder="Expires at (ISO, optional)", id="expires-input")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Publish", id="submit-btn", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        title = self.query_one("#title-input", Input).value.strip()
        if not title:
            self.app.notify("Title is required", severity="warning")
            return
        tags_raw = self.query_one("#tags-input", Input).value.strip()
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
        content = self.query_one("#content-input", TextArea).text
        if not content.strip():
            self.app.notify("Content cannot be empty", severity="warning")
            return
        expires_val = self.query_one("#expires-input", Input).value.strip() or None
        source = self.query_one("#source-input", Input).value.strip() or None
        self.dismiss(
            {
                "title": title,
                "tags": tags,
                "content": content,
                "expires_at": expires_val,
                "source": source,
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
                placeholder="Title (required — becomes the filename)",
                id="title-input",
            )
            yield Input(
                value=tags_str,
                placeholder="Tags (comma-separated)",
                id="tags-input",
            )
            yield Input(
                value=post.source or "",
                placeholder="Source (optional)",
                id="source-input",
            )
            yield TextArea(post.content, id="content-input", language="markdown")
            yield Input(
                value=post.expires_at or "",
                placeholder="Expires at (ISO, optional)",
                id="expires-input",
            )
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Save", id="submit-btn", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        title = self.query_one("#title-input", Input).value.strip()
        if not title:
            self.app.notify("Title is required", severity="warning")
            return
        tags_raw = self.query_one("#tags-input", Input).value.strip()
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
        content = self.query_one("#content-input", TextArea).text
        if not content.strip():
            self.app.notify("Content cannot be empty", severity="warning")
            return
        expires_val = self.query_one("#expires-input", Input).value.strip() or None
        source = self.query_one("#source-input", Input).value.strip() or None
        self.dismiss(
            {
                "title": title,
                "tags": tags,
                "content": content,
                "expires_at": expires_val,
                "source": source,
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

    def on_mount(self) -> None:
        self.query_one("#new-name", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        new_name = self.query_one("#new-name", Input).value.strip()
        if new_name:
            self.dismiss(new_name)
        else:
            self.app.notify("Tag name cannot be empty", severity="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
        elif event.button.id == "rename-btn":
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()


# ── TagConfigModal ────────────────────────────────────────────────────────────


class TagConfigModal(ModalScreen[dict | None]):
    """Dialog for setting per-tag TTL or expiry."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = f"""
    TagConfigModal > Vertical {{
        width: 60;
        height: auto;
        background: {HEADER_BG};
        border: solid {ACCENT};
        padding: 1 2;
    }}
    TagConfigModal .config-label {{
        color: {ACCENT};
        text-style: bold;
        margin-bottom: 1;
    }}
    TagConfigModal Input {{
        width: 1fr;
        margin-bottom: 1;
        border: solid {BORDER};
    }}
    TagConfigModal .config-actions {{
        height: 3;
        align: right middle;
        margin-top: 1;
    }}
    TagConfigModal .config-actions Button {{
        margin-left: 1;
    }}
    """

    def __init__(self, tag: str) -> None:
        super().__init__()
        self._tag = tag

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                f"Configure tag: {escape(self._tag)}",
                markup=True,
                classes="config-label",
            )
            yield Input(placeholder="TTL hours (integer, optional)", id="ttl-input")
            yield Input(placeholder="Expires at (ISO, optional)", id="expires-input")
            with Horizontal(classes="config-actions"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Save", id="save-btn", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
        elif event.button.id == "save-btn":
            ttl_raw = self.query_one("#ttl-input", Input).value.strip()
            expires_raw = self.query_one("#expires-input", Input).value.strip()
            if not ttl_raw and not expires_raw:
                self.app.notify("Enter TTL hours or an expiry datetime", severity="warning")
                return
            ttl_hours: int | None = None
            if ttl_raw:
                try:
                    ttl_hours = int(ttl_raw)
                    if ttl_hours <= 0:
                        raise ValueError
                except ValueError:
                    self.app.notify("TTL must be a positive integer", severity="warning")
                    return
            self.dismiss(
                {
                    "ttl_hours": ttl_hours,
                    "expires_at": expires_raw or None,
                }
            )


# ── SearchModal ───────────────────────────────────────────────────────────────


class SearchModal(ModalScreen[str | None]):
    """Prompt for a search query; returns the string or None on cancel."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = f"""
    SearchModal > Vertical {{
        width: 60;
        height: auto;
        background: {HEADER_BG};
        border: solid {ACCENT};
        padding: 1 2;
    }}
    SearchModal .search-label {{
        color: {ACCENT};
        text-style: bold;
        margin-bottom: 1;
    }}
    SearchModal Input {{
        width: 1fr;
        margin-bottom: 1;
        border: solid {BORDER};
    }}
    SearchModal .search-actions {{
        height: 3;
        align: right middle;
    }}
    SearchModal .search-actions Button {{
        margin-left: 1;
    }}
    """

    def __init__(self, current: str = "") -> None:
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Search posts", classes="search-label")
            yield Input(value=self._current, placeholder="title, content or source…", id="search-input")
            with Horizontal(classes="search-actions"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Search", id="search-btn", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
        elif event.button.id == "search-btn":
            self.dismiss(self.query_one("#search-input", Input).value.strip() or "")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or "")
