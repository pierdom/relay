from __future__ import annotations

import difflib

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
    OptionList,
    Select,
    Static,
)
from textual.widgets.option_list import Option

from .. import api
from ..theme import ACCENT, BORDER, HEADER_BG
from .edit_form import EditForm
from .post_panel import _time_ago


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


# ── EditModal ─────────────────────────────────────────────────────────────────


class EditModal(ModalScreen["api.Post | None"]):
    """The editor over the feed — a new post (``post=None``) or an edit;
    dismisses with the saved post, or None."""

    DEFAULT_CSS = f"""
    EditModal > EditForm {{
        width: 84;
        max-height: 90%;
        background: {HEADER_BG};
        border: solid {ACCENT};
        padding: 1 2;
    }}
    """

    def __init__(self, post: api.Post | None = None) -> None:
        super().__init__()
        self._post = post

    def compose(self) -> ComposeResult:
        yield EditForm(self._post)

    def on_edit_form_saved(self, event: EditForm.Saved) -> None:
        self.dismiss(event.post)

    def on_edit_form_cancelled(self, event: EditForm.Cancelled) -> None:
        self.dismiss(None)


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
    """Dialog for a tag's TTL, expiry date and keep-last retention.

    Opens on the tag's current config: saving replaces all of it, so a field
    the form did not show would be cleared. Clearing every field removes it."""

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

    def __init__(self, tag: str, current: api.Tag | None = None) -> None:
        super().__init__()
        self._tag = tag
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                f"Configure tag: {escape(self._tag)}",
                markup=True,
                classes="config-label",
            )
            cur = self._current
            yield Input(str(cur.ttl_hours) if cur and cur.ttl_hours else "",
                        placeholder="TTL hours (integer, optional)", id="ttl-input")
            yield Input(cur.expires_at if cur and cur.expires_at else "",
                        placeholder="Expires at (ISO, optional)", id="expires-input")
            yield Input(str(cur.keep_last) if cur and cur.keep_last else "",
                        placeholder="Keep only the newest N posts (optional)", id="keep-input")
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
            keep_raw = self.query_one("#keep-input", Input).value.strip()
            cur = self._current
            had_config = bool(cur and (cur.ttl_hours or cur.expires_at or cur.keep_last))
            if not ttl_raw and not expires_raw and not keep_raw and not had_config:
                self.app.notify("Enter a TTL, an expiry datetime or a keep-last count", severity="warning")
                return
            numbers: dict[str, int | None] = {}
            for key, raw, label in (("ttl_hours", ttl_raw, "TTL"), ("keep_last", keep_raw, "Keep-last")):
                try:
                    numbers[key] = value = int(raw) if raw else None
                    if value is not None and value <= 0:
                        raise ValueError
                except ValueError:
                    self.app.notify(f"{label} must be a positive integer", severity="warning")
                    return
            # All empty on a tag that had a config removes it.
            self.dismiss({**numbers, "expires_at": expires_raw or None})


# ── SearchModal ───────────────────────────────────────────────────────────────


class SearchModal(ModalScreen["tuple[str, str] | None"]):
    """Prompt for a search; returns (query, mode) or None on cancel. The mode
    picker shows only when the server has semantic search on."""

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
    SearchModal Input, SearchModal Select {{
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

    MODES = [
        ("Hybrid — meaning and words", "hybrid"),
        ("Semantic — meaning", "semantic"),
        ("Keyword — exact words", "keyword"),
    ]

    def __init__(self, current: str = "", mode: str = "keyword", semantic: bool = False) -> None:
        super().__init__()
        self._current = current
        self._mode = mode
        self._semantic = semantic

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Search posts", classes="search-label")
            yield Input(value=self._current, placeholder="title, content or source…", id="search-input")
            if self._semantic:
                yield Select(self.MODES, value=self._mode, allow_blank=False, id="search-mode")
            with Horizontal(classes="search-actions"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Search", id="search-btn", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        mode = self.query_one("#search-mode", Select).value if self._semantic else "keyword"
        self.dismiss((self.query_one("#search-input", Input).value.strip(), str(mode)))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
        elif event.button.id == "search-btn":
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()


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
