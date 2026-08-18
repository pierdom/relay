from __future__ import annotations

import difflib
import re
from urllib.parse import quote

from rich.markup import escape
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    Markdown,
    OptionList,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from .. import api
from ..theme import ACCENT, BORDER, HEADER_BG
from .post_panel import _time_ago, _time_until

# ── Wikilink preprocessing ────────────────────────────────────────────────────

_EMBED_RE = re.compile(r"!\[\[([^\]|#]+?)(?:\|([^\]]+))?\]\]")
_WIKI_RE = re.compile(r"\[\[([^\]|#]+?)(#[^\]|]+)?(?:\|([^\]]+))?\]\]")
_IDREF_RE = re.compile(r"(?<![\w#])#(\d{1,5})\b")
# Any extension — used only on the ![[…]] embed path (always a file in Obsidian).
_ANY_EXT_RE = re.compile(r"\.[a-z0-9]{1,12}$", re.IGNORECASE)
# Curated attachment types for the plain [[…]] link path — not a generic ``\.xxx$``
# — so dotted note titles (e.g. ``[[Section 2.1]]``) aren't mistaken for files.
_FILE_EXT_RE = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|avif|bmp|pdf|canvas|docx?|xlsx?|pptx?|csv|txt"
    r"|rtf|odt|ods|zip|epub|mp3|m4a|wav|flac|ogg|aac|opus|mp4|mov|webm|mkv|avi)$",
    re.IGNORECASE,
)
_CODE_SPLIT = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)


def _attachment_link(name: str, label: str) -> str:
    """A markdown link to the attachment endpoint — terminals can't inline images,
    so both ``![[img]]`` embeds and ``[[file.pdf]]`` render as an external link."""
    return f"[\U0001F4CE {label}]({api._base()}/attachments/{quote(name)})"


def _linkify_markdown(content: str, index: dict[str, int]) -> str:
    """Turn ``[[Title]]`` / ``#NNN`` into ``[label](relay:ID)`` links and Obsidian
    attachment embeds into external links.

    Resolution is by title (case-insensitive). Broken wikilinks degrade to plain
    text; unknown ``#NNN`` are left untouched. Code spans/blocks are skipped.
    """
    ids = set(index.values())

    def convert(text: str) -> str:
        def embed(m: re.Match) -> str:
            name = m.group(1).strip()
            opt = (m.group(2) or "").strip()
            if _ANY_EXT_RE.search(name):  # embed of any file → external link
                label = name if (not opt or re.fullmatch(r"\d+(x\d+)?", opt)) else opt
                return _attachment_link(name, label)
            # No extension → note transclusion; link to the note or degrade to text.
            pid = index.get(name.lower())
            return f"[{opt or name}](relay:{pid})" if pid is not None else (opt or name)

        def wiki(m: re.Match) -> str:
            target = m.group(1).strip()
            alias = (m.group(3) or target).strip()
            pid = index.get(target.lower())
            if pid is not None:
                return f"[{alias}](relay:{pid})"
            # Unresolved but file-like (e.g. [[doc.pdf]]) → attachment link.
            return _attachment_link(target, alias) if _FILE_EXT_RE.search(target) else alias

        def idref(m: re.Match) -> str:
            n = m.group(1)
            return f"[#{n}](relay:{n})" if int(n) in ids else m.group(0)

        return _IDREF_RE.sub(idref, _WIKI_RE.sub(wiki, _EMBED_RE.sub(embed, text)))

    return "".join(
        part if i % 2 else convert(part)
        for i, part in enumerate(_CODE_SPLIT.split(content))
    )


def _outbound_link_ids(content: str, index: dict[str, int]) -> list[int]:
    """Resolved link targets in ``content``, in document order, de-duplicated.

    Covers ``[[Title]]`` and ``#NNN``; skips code and unresolved links.
    """
    ids = set(index.values())
    ordered: list[int] = []
    for i, part in enumerate(_CODE_SPLIT.split(content)):
        if i % 2:
            continue
        hits: list[tuple[int, int]] = []
        for m in _WIKI_RE.finditer(part):
            pid = index.get(m.group(1).strip().lower())
            if pid is not None:
                hits.append((m.start(), pid))
        for m in _IDREF_RE.finditer(part):
            n = int(m.group(1))
            if n in ids:
                hits.append((m.start(), n))
        hits.sort()
        ordered.extend(pid for _, pid in hits)
    seen: set[int] = set()
    return [p for p in ordered if not (p in seen or seen.add(p))]


def _line_diff(current: str, revision: str) -> str:
    """Unified diff current→revision with Rich markup. - = loses, + = gains on restore."""
    a = current.splitlines(keepends=True)
    b = revision.splitlines(keepends=True)
    lines = list(difflib.unified_diff(a, b, fromfile="current", tofile="revision", lineterm=""))
    if not lines:
        return "[dim](identical to current version)[/dim]"
    parts = []
    for line in lines[:500]:
        s = line.rstrip("\n")
        if s.startswith(("+++", "---", "@@")):
            parts.append(f"[dim]{escape(s)}[/dim]")
        elif s.startswith("+"):
            parts.append(f"[green]{escape(s)}[/green]")
        elif s.startswith("-"):
            parts.append(f"[red]{escape(s)}[/red]")
        else:
            parts.append(escape(s))
    if len(lines) > 500:
        parts.append("[dim]… diff truncated[/dim]")
    return "\n".join(parts)


# ── PostDetailModal ───────────────────────────────────────────────────────────


class PostDetailModal(ModalScreen[None]):
    """Full-screen modal showing the complete content of a post."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("f", "follow_link", "Follow link"),
        Binding("h", "history", "History"),
    ]

    DEFAULT_CSS = f"""
    PostDetailModal {{
        align: center middle;
        background: {HEADER_BG};
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

    def __init__(
        self,
        post: api.Post,
        link_index: dict[str, int] | None = None,
        link_titles: dict[int, str] | None = None,
    ) -> None:
        super().__init__()
        self._post = post
        self._link_index = link_index or {}
        self._link_titles = link_titles or {}
        self._backlinks: list[tuple[int, str]] = []

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
        self._backlinks = items
        if not items:
            return
        md = "\n---\n\n**Linked mentions**  ([b]f[/b] to follow)\n\n" + "\n".join(
            f"- [#{i} {t}](relay:{i})" for i, t in items
        )
        self.app.call_from_thread(self.query_one("#backlinks", Markdown).update, md)

    def action_follow_link(self) -> None:
        """Open a keyboard-driven picker of every link in the post (out + back)."""
        items: list[tuple[int, str, str]] = []
        seen: set[int] = set()
        for pid in _outbound_link_ids(self._post.content, self._link_index):
            seen.add(pid)
            items.append((pid, self._link_titles.get(pid, ""), "→"))
        for pid, title in self._backlinks:
            if pid in seen:
                continue
            seen.add(pid)
            items.append((pid, title, "←"))
        if not items:
            self.app.notify("No links in this post", severity="warning")
            return

        def _on_pick(pid: int | None) -> None:
            if pid is not None:
                self.app.open_post(pid)

        self.app.push_screen(LinkPickerModal(items), _on_pick)

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

    def action_history(self) -> None:
        def _on_done(restored: api.Post | None) -> None:
            if restored is not None:
                self._post = restored
                self.query_one(".detail-content", Markdown).update(
                    _linkify_markdown(restored.content, self._link_index)
                )
                try:
                    self.app._reload()
                except Exception:
                    pass
        self.app.push_screen(PostHistoryModal(self._post), _on_done)


# ── LinkPickerModal ───────────────────────────────────────────────────────────


class LinkPickerModal(ModalScreen[int | None]):
    """Keyboard link chooser: type to filter, ↑↓ to move, Enter to follow."""

    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    DEFAULT_CSS = f"""
    LinkPickerModal {{ align: center middle; background: {HEADER_BG}; }}
    LinkPickerModal > Vertical {{
        width: 64; max-width: 90%; height: auto; max-height: 80%;
        background: {HEADER_BG}; border: solid {ACCENT}; padding: 1 2;
    }}
    LinkPickerModal .picker-title {{ text-style: bold; color: {ACCENT}; width: 1fr; }}
    LinkPickerModal Input {{ margin: 1 0; }}
    LinkPickerModal OptionList {{
        height: auto; max-height: 16; background: transparent; border: none;
    }}
    """

    def __init__(self, items: list[tuple[int, str, str]]) -> None:
        super().__init__()
        self._items = items  # (id, title, arrow)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                f"Follow link  ·  {len(self._items)} in this post",
                classes="picker-title",
            )
            yield Input(placeholder="filter by title or #id…", id="pick-filter")
            yield OptionList(id="pick-list")

    def on_mount(self) -> None:
        self._populate("")
        self.query_one("#pick-filter", Input).focus()

    def _populate(self, query: str) -> None:
        q = query.strip().lower()
        ol = self.query_one("#pick-list", OptionList)
        ol.clear_options()
        for pid, title, arrow in self._items:
            label = f"{arrow} #{pid}  {title}".rstrip()
            if not q or q in label.lower() or q in str(pid):
                ol.add_option(Option(label, id=str(pid)))
        if ol.option_count:
            ol.highlighted = 0

    def _follow(self, index: int | None) -> None:
        ol = self.query_one("#pick-list", OptionList)
        if index is None or not ol.option_count:
            return
        opt = ol.get_option_at_index(index)
        if opt.id is not None:
            self.dismiss(int(opt.id))

    def on_input_changed(self, event: Input.Changed) -> None:
        self._populate(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._follow(self.query_one("#pick-list", OptionList).highlighted)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.dismiss(int(event.option.id))

    def on_key(self, event) -> None:
        if event.key in ("up", "down"):
            ol = self.query_one("#pick-list", OptionList)
            if ol.option_count:
                cur = ol.highlighted or 0
                ol.highlighted = max(
                    0, min(ol.option_count - 1, cur + (1 if event.key == "down" else -1))
                )
            event.stop()


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


# ── PostHistoryModal ──────────────────────────────────────────────────────────


class PostHistoryModal(ModalScreen[api.Post | None]):
    """Two-pane revision browser: list on the left, preview + diff on the right."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("r", "restore", "Restore"),
        Binding("tab", "toggle_diff", "Body/Diff"),
    ]

    DEFAULT_CSS = f"""
    PostHistoryModal {{
        align: center middle;
    }}
    PostHistoryModal > Vertical {{
        width: 95%;
        height: 90%;
        background: {HEADER_BG};
        border: solid {ACCENT};
    }}
    PostHistoryModal .hist-title {{
        color: {ACCENT};
        text-style: bold;
        height: 1;
        padding: 0 1;
        background: {BORDER};
    }}
    PostHistoryModal .hist-left {{
        width: 40;
        border-right: solid {BORDER};
        height: 1fr;
    }}
    PostHistoryModal #rev-list {{
        height: 1fr;
        background: {HEADER_BG};
        border: none;
    }}
    PostHistoryModal #rev-preview {{
        width: 1fr;
        height: 1fr;
        padding: 0 1;
        background: {HEADER_BG};
    }}
    PostHistoryModal #rev-body {{
        width: 1fr;
    }}
    PostHistoryModal .hist-footer {{
        height: 3;
        border-top: solid {BORDER};
        align: left middle;
        padding: 0 2;
    }}
    PostHistoryModal .hist-hint {{
        color: #888888;
        width: 1fr;
    }}
    PostHistoryModal .hist-footer Button {{
        margin-left: 1;
    }}
    """

    def __init__(self, post: api.Post) -> None:
        super().__init__()
        self._post = post
        self._revisions: list[api.Revision] = []
        self._current_rev: api.RevisionContent | None = None
        self._show_diff = False

    def compose(self) -> ComposeResult:
        title = self._post.title or f"#{self._post.id}"
        with Vertical():
            yield Label(
                f" History — [bold]{escape(title)}[/bold]",
                markup=True,
                classes="hist-title",
            )
            with Horizontal():
                with Vertical(classes="hist-left"):
                    yield OptionList(id="rev-list")
                with VerticalScroll(id="rev-preview"):
                    yield Static("Loading…", id="rev-body", markup=True)
            with Horizontal(classes="hist-footer"):
                yield Label(
                    "[dim]↑↓ select · tab body/diff · r restore · esc close[/dim]",
                    markup=True,
                    classes="hist-hint",
                )
                yield Button("Restore", id="restore-btn", variant="primary", disabled=True)

    def on_mount(self) -> None:
        self._load_history()

    @work(thread=True)
    def _load_history(self) -> None:
        try:
            revisions, _exists = api.get_post_history(self._post.id)
        except Exception as exc:
            self.app.call_from_thread(
                self.query_one("#rev-body", Static).update,
                f"[red]Failed to load history: {escape(str(exc))}[/red]",
            )
            return
        self._revisions = revisions
        self.app.call_from_thread(self._populate_list, revisions)

    def _populate_list(self, revisions: list[api.Revision]) -> None:
        ol = self.query_one("#rev-list", OptionList)
        ol.clear_options()
        if not revisions:
            self.query_one("#rev-body", Static).update("[dim]No history available.[/dim]")
            return
        for i, rev in enumerate(revisions):
            msg = rev.message if len(rev.message) <= 36 else rev.message[:33] + "…"
            badge = f"  [bold {ACCENT}]current[/]" if i == 0 else ""
            label = (
                f"[bold]{escape(msg)}[/bold]{badge}\n"
                f"[dim]{rev.short_sha} · {_time_ago(rev.when)}[/dim]"
            )
            ol.add_option(Option(label, id=rev.sha))
        ol.highlighted = 0
        ol.focus()
        self._load_revision(revisions[0].sha)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id:
            self._load_revision(event.option.id)

    @work(thread=True)
    def _load_revision(self, sha: str) -> None:
        try:
            rev = api.get_post_revision(self._post.id, sha)
        except Exception as exc:
            self.app.call_from_thread(
                self.query_one("#rev-body", Static).update,
                f"[red]Failed: {escape(str(exc))}[/red]",
            )
            return
        self._current_rev = rev
        self.app.call_from_thread(self._refresh_preview)
        self.app.call_from_thread(self._enable_restore)

    def _enable_restore(self) -> None:
        self.query_one("#restore-btn", Button).disabled = False

    def _refresh_preview(self) -> None:
        rev = self._current_rev
        if rev is None:
            return
        body = self.query_one("#rev-body", Static)
        if self._show_diff:
            body.update(_line_diff(self._post.content, rev.content))
        else:
            tags_str = "  ".join(f"[{escape(t)}]" for t in rev.tags) if rev.tags else ""
            header = (
                f"[bold {ACCENT}]{escape(rev.title)}[/bold {ACCENT}]  "
                f"[dim]{rev.short_sha} · {_time_ago(rev.when)}[/dim]\n"
            )
            if tags_str:
                header += tags_str + "\n"
            header += "\n"
            body.update(header + escape(rev.content))

    def action_toggle_diff(self) -> None:
        self._show_diff = not self._show_diff
        self._refresh_preview()

    def action_restore(self) -> None:
        rev = self._current_rev
        if rev is None:
            return

        def _cb(ok: bool | None) -> None:
            if ok:
                self._do_restore(rev)

        self.app.push_screen(ConfirmModal(f"Restore to {rev.short_sha}?"), _cb)

    @work(thread=True)
    def _do_restore(self, rev: api.RevisionContent) -> None:
        try:
            restored = api.restore_post(self._post.id, rev.sha)
        except Exception as exc:
            self.app.call_from_thread(
                self.app.notify, f"Restore failed: {exc}", severity="error"
            )
            return
        self.app.call_from_thread(self.dismiss, restored)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "restore-btn":
            self.action_restore()


# ── DeletedPostsModal ─────────────────────────────────────────────────────────


class DeletedPostsModal(ModalScreen[api.Post | None]):
    """Browse deleted posts and restore them by their last restorable revision."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("r", "restore", "Restore"),
    ]

    DEFAULT_CSS = f"""
    DeletedPostsModal {{
        align: center middle;
    }}
    DeletedPostsModal > Vertical {{
        width: 95%;
        height: 90%;
        background: {HEADER_BG};
        border: solid {ACCENT};
    }}
    DeletedPostsModal .del-title {{
        color: {ACCENT};
        text-style: bold;
        height: 1;
        padding: 0 1;
        background: {BORDER};
    }}
    DeletedPostsModal .del-left {{
        width: 40;
        border-right: solid {BORDER};
        height: 1fr;
    }}
    DeletedPostsModal #del-list {{
        height: 1fr;
        background: {HEADER_BG};
        border: none;
    }}
    DeletedPostsModal #del-preview {{
        width: 1fr;
        height: 1fr;
        padding: 0 1;
        background: {HEADER_BG};
    }}
    DeletedPostsModal #del-body {{
        width: 1fr;
    }}
    DeletedPostsModal .del-footer {{
        height: 3;
        border-top: solid {BORDER};
        align: left middle;
        padding: 0 2;
    }}
    DeletedPostsModal .del-hint {{
        color: #888888;
        width: 1fr;
    }}
    DeletedPostsModal .del-footer Button {{
        margin-left: 1;
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._deleted: list[api.DeletedPost] = []
        self._current: api.DeletedPost | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(" Recovery — Deleted Posts", classes="del-title")
            with Horizontal():
                with Vertical(classes="del-left"):
                    yield OptionList(id="del-list")
                with VerticalScroll(id="del-preview"):
                    yield Static("Select a post to preview.", id="del-body", markup=True)
            with Horizontal(classes="del-footer"):
                yield Label(
                    "[dim]↑↓ select · r restore · esc close[/dim]",
                    markup=True,
                    classes="del-hint",
                )
                yield Button("Restore", id="restore-btn", variant="primary", disabled=True)

    def on_mount(self) -> None:
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        try:
            items = api.list_deleted_posts()
        except Exception as exc:
            self.app.call_from_thread(
                self.query_one("#del-body", Static).update,
                f"[red]Failed to load: {escape(str(exc))}[/red]",
            )
            return
        self._deleted = items
        self.app.call_from_thread(self._populate, items)

    def _populate(self, items: list[api.DeletedPost]) -> None:
        ol = self.query_one("#del-list", OptionList)
        ol.clear_options()
        if not items:
            self.query_one("#del-body", Static).update("[dim]Nothing to recover.[/dim]")
            return
        reason_colors = {"deleted": ACCENT, "external": "yellow", "expiry": "dim"}
        for d in items:
            title = d.title if len(d.title) <= 28 else d.title[:25] + "…"
            rc = reason_colors.get(d.reason, "dim")
            label = (
                f"[bold {ACCENT}]#{d.id}[/]  {escape(title)}\n"
                f"[{rc}]{d.reason}[/]  [dim]{d.short_sha} · {_time_ago(d.when)}[/dim]"
            )
            ol.add_option(Option(label, id=str(d.id)))
        ol.highlighted = 0
        ol.focus()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if not event.option.id:
            return
        try:
            post_id = int(event.option.id)
        except ValueError:
            return
        d = next((x for x in self._deleted if x.id == post_id), None)
        if d:
            self._current = d
            self._load_preview(d)

    @work(thread=True)
    def _load_preview(self, d: api.DeletedPost) -> None:
        try:
            rev = api.get_post_revision(d.id, d.sha)
        except Exception as exc:
            self.app.call_from_thread(
                self.query_one("#del-body", Static).update,
                f"[red]Failed to load preview: {escape(str(exc))}[/red]",
            )
            return
        tags_str = "  ".join(f"[{escape(t)}]" for t in rev.tags) if rev.tags else ""
        header = (
            f"[bold {ACCENT}]{escape(rev.title)}[/bold {ACCENT}]  "
            f"[dim]#{d.id} · {d.short_sha} · {_time_ago(d.when)}[/dim]\n"
        )
        if tags_str:
            header += tags_str + "\n"
        header += "\n"
        self.app.call_from_thread(
            self.query_one("#del-body", Static).update, header + escape(rev.content)
        )
        self.app.call_from_thread(self._enable_restore)

    def _enable_restore(self) -> None:
        self.query_one("#restore-btn", Button).disabled = False

    def action_restore(self) -> None:
        d = self._current
        if d is None:
            return

        def _cb(ok: bool | None) -> None:
            if ok:
                self._do_restore(d)

        self.app.push_screen(ConfirmModal(f"Restore post #{d.id}?"), _cb)

    @work(thread=True)
    def _do_restore(self, d: api.DeletedPost) -> None:
        try:
            restored = api.restore_post(d.id, d.sha)
        except Exception as exc:
            self.app.call_from_thread(
                self.app.notify, f"Restore failed: {exc}", severity="error"
            )
            return
        self.app.call_from_thread(self.dismiss, restored)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "restore-btn":
            self.action_restore()


# ── AttachmentsModal ──────────────────────────────────────────────────────────


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


class AttachmentsModal(ModalScreen[None]):
    """Browse vault attachments: open externally (images can't render in a terminal)
    or delete. Enter/o opens the file in the browser; d deletes the selected one."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("o", "open_external", "Open"),
        Binding("d", "delete", "Delete"),
        Binding("r", "reload", "Refresh"),
    ]

    DEFAULT_CSS = f"""
    AttachmentsModal {{
        align: center middle;
    }}
    AttachmentsModal > Vertical {{
        width: 80%;
        height: 80%;
        background: {HEADER_BG};
        border: solid {ACCENT};
        padding: 1 2;
    }}
    AttachmentsModal .att-title {{
        text-style: bold;
        color: {ACCENT};
        margin-bottom: 1;
    }}
    AttachmentsModal OptionList {{
        height: 1fr;
        background: {HEADER_BG};
    }}
    AttachmentsModal .att-hint {{
        color: #888888;
        margin-top: 1;
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._items: list[api.Attachment] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Attachments", classes="att-title")
            yield OptionList(id="att-list")
            yield Label("enter/o open · d delete · r refresh · esc close", classes="att-hint")

    def on_mount(self) -> None:
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        try:
            items = api.list_attachments()
        except Exception:
            items = []
        self._items = items
        self.app.call_from_thread(self._populate)

    def _populate(self) -> None:
        ol = self.query_one("#att-list", OptionList)
        ol.clear_options()
        if not self._items:
            ol.add_option(Option("(no attachments)"))
            return
        for a in self._items:
            ol.add_option(Option(f"{a.folder}/assets/{a.filename}  ({_fmt_bytes(a.bytes)})"))
        ol.highlighted = 0
        ol.focus()

    def _selected(self) -> api.Attachment | None:
        ol = self.query_one("#att-list", OptionList)
        idx = ol.highlighted
        if idx is None or not self._items or idx >= len(self._items):
            return None
        return self._items[idx]

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.action_open_external()

    def action_open_external(self) -> None:
        a = self._selected()
        if a is not None:
            self.app.open_url(api.attachment_url(a.folder, a.filename))

    def action_reload(self) -> None:
        self._load()

    def action_delete(self) -> None:
        a = self._selected()
        if a is None:
            return

        def _cb(ok: bool | None) -> None:
            if ok:
                self._do_delete(a)

        self.app.push_screen(ConfirmModal(f"Delete {a.filename} from the vault?"), _cb)

    @work(thread=True)
    def _do_delete(self, a: api.Attachment) -> None:
        try:
            res = api.delete_attachment(a.filename)
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, f"Delete failed: {exc}", severity="error")
            return
        refs = res.get("referenced_by") or []
        msg = f"Deleted {a.filename}"
        if refs:
            msg += " — still ref'd by " + ", ".join(f"#{i}" for i in refs)
        self.app.call_from_thread(self.app.notify, msg)
        self.app.call_from_thread(self._load)
