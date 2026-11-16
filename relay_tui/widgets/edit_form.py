"""The post editor, for a new post (Compose) and an existing one — in the edit
modal and in the reader pane alike.

An edit saves with the etag it opened on as ``if_match``: a post that changed
meanwhile (an agent's append, an Obsidian save) is not overwritten. The form
says so and keeps the edit, and a second save overwrites on purpose.

Writing aids, as in the browser: ``[[`` and ``#`` complete posts from the link
index, the Tags field suggests the vault's tags (→ accepts), ``Ctrl+T`` swaps
the text for a preview, ``Ctrl+O`` uploads a local file and inserts its
``![[embed]]``, and a new post's draft is kept on disk until it is published or
discarded.
"""
from __future__ import annotations

import os

from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Markdown, OptionList, TextArea
from textual.widgets.option_list import Option

from .. import api, drafts
from ..completion import PathSuggester, Span, TagSuggester, accepted_text, link_span, match_posts
from ..render import _linkify_markdown
from ..theme import ACCENT, BORDER

_DRAFT_DELAY = 0.5


def _tags(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


class _ContentArea(TextArea):
    """The body field; while the completion list is open it owns ↑ ↓ Tab
    Enter and Esc."""

    async def _on_key(self, event: events.Key) -> None:
        form = next((a for a in self.ancestors if isinstance(a, EditForm)), None)
        if form is not None and form.completion_key(event.key):
            event.prevent_default()
            event.stop()
            return
        await super()._on_key(event)


class EditForm(Vertical):
    class Saved(Message):
        def __init__(self, post: api.Post, created: bool) -> None:
            super().__init__()
            self.post = post
            self.created = created

    class Cancelled(Message):
        """The user left the editor (after confirming, when that loses work)."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+t", "toggle_preview", "Preview"),
        Binding("ctrl+o", "attach", "Attach file"),
    ]

    DEFAULT_CSS = f"""
    EditForm {{ height: auto; }}
    EditForm .modal-title {{ color: {ACCENT}; text-style: bold; margin-bottom: 1; }}
    EditForm Input {{ margin-bottom: 1; width: 1fr; border: solid {BORDER}; }}
    EditForm TextArea {{ height: 12; width: 1fr; margin-bottom: 1; border: solid {BORDER}; }}
    EditForm #ef-preview {{ height: 12; width: 1fr; margin-bottom: 1; border: solid {BORDER}; display: none; }}
    EditForm.-preview #ef-preview {{ display: block; }}
    EditForm.-preview TextArea {{ display: none; }}
    EditForm #ef-complete {{
        height: auto; max-height: 8; width: 1fr; margin-bottom: 1;
        border: solid {ACCENT}; display: none;
    }}
    EditForm.-completing #ef-complete {{ display: block; }}
    EditForm .ef-conflict {{ color: $warning; height: auto; display: none; }}
    EditForm.-conflict .ef-conflict {{ display: block; }}
    EditForm .modal-actions {{ height: 3; align: right middle; margin-top: 1; }}
    EditForm .modal-actions Button {{ margin-left: 1; }}
    """

    def __init__(self, post: api.Post | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.post = post
        self._etag = post.etag if post else ""
        self._saving = False
        self._span: Span | None = None
        self._choices: list[tuple[int, str]] = []
        self._accepting = False
        self._draft_timer = None
        self._suggester = TagSuggester([])
        self._restored = drafts.load() if post is None else None

    @property
    def creating(self) -> bool:
        return self.post is None

    def _initial(self) -> dict:
        if self.post is not None:
            p = self.post
            return {"title": p.title or "", "tags": ", ".join(p.tags), "source": p.source or "",
                    "content": p.content, "expires_at": p.expires_at or ""}
        d = self._restored or {}
        return {k: str(d.get(k) or "") for k in ("title", "tags", "source", "content", "expires_at")}

    def compose(self) -> ComposeResult:
        v = self._initial()
        yield Label("New Post" if self.creating else f"Edit Post #{self.post.id}", classes="modal-title")
        yield Input(value=v["title"], placeholder="Title (required — becomes the filename)", id="title-input")
        yield Input(value=v["tags"], placeholder="Tags (comma-separated, → accepts a suggestion)",
                    id="tags-input", suggester=self._suggester)
        yield Input(value=v["source"], placeholder="Source (optional)", id="source-input")
        yield _ContentArea(v["content"], id="content-input", language="markdown")
        yield OptionList(id="ef-complete")
        with VerticalScroll(id="ef-preview"):
            yield Markdown("", open_links=False)
        yield Input(value=v["expires_at"], placeholder="Expires at (ISO, optional)", id="expires-input")
        yield Label("", classes="ef-conflict", id="ef-conflict")
        with Horizontal(classes="modal-actions"):
            if self.creating:
                yield Button("Discard", id="discard-btn", variant="error")
            yield Button("Attach", id="attach-btn", variant="default")
            yield Button("Preview", id="preview-btn", variant="default")
            yield Button("Cancel", id="cancel-btn", variant="default")
            yield Button("Publish" if self.creating else "Save", id="submit-btn", variant="primary")

    def on_mount(self) -> None:
        if self.creating and not self._restored:
            self.query_one("#title-input", Input).focus()
        else:
            self.query_one("#content-input", TextArea).focus()
        if self._restored:
            self.app.notify("Draft restored", timeout=3)
        self._load_tags()

    @work(thread=True)
    def _load_tags(self) -> None:
        # Fetched when the editor opens, never earlier, so the list is current.
        try:
            names = [t.name for t in api.list_tags()]
        except Exception:
            return
        self._suggester.tags = names

    def values(self) -> dict:
        return {
            "title": self.query_one("#title-input", Input).value.strip(),
            "tags": _tags(self.query_one("#tags-input", Input).value),
            "source": self.query_one("#source-input", Input).value.strip() or None,
            "content": self.query_one("#content-input", TextArea).text,
            "expires_at": self.query_one("#expires-input", Input).value.strip() or None,
        }

    def _raw(self) -> dict:
        return {
            "title": self.query_one("#title-input", Input).value,
            "tags": self.query_one("#tags-input", Input).value,
            "source": self.query_one("#source-input", Input).value,
            "content": self.query_one("#content-input", TextArea).text,
            "expires_at": self.query_one("#expires-input", Input).value,
        }

    @property
    def is_dirty(self) -> bool:
        if self.post is None:
            return any(v.strip() for v in self._raw().values())
        v, p = self.values(), self.post
        return (v["title"], v["tags"], v["source"], v["content"], v["expires_at"]) != (
            p.title or "", p.tags, p.source, p.content, p.expires_at
        )

    # ── draft ────────────────────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        self._draft_soon()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._draft_soon()
        self._update_completion()

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:
        self._update_completion()

    def _draft_soon(self) -> None:
        if not self.creating:
            return
        if self._draft_timer is not None:
            self._draft_timer.stop()
        self._draft_timer = self.set_timer(_DRAFT_DELAY, lambda: drafts.save(self._raw()))

    # ── completion ───────────────────────────────────────────────────────────

    def _update_completion(self) -> None:
        if self._accepting:  # the text we just inserted is not a new query
            self._accepting = False
            self._close_completion()
            return
        ta = self.query_one("#content-input", TextArea)
        if not ta.selection.is_empty:
            self._close_completion()
            return
        row, col = ta.cursor_location
        line = ta.document.get_line(row)
        span = link_span(line[:col], line[col:])
        titles = getattr(self.app, "_link_titles", {}) or {}
        choices = match_posts(titles, span.query) if span else []
        if not span or not choices:
            self._close_completion()
            return
        self._span, self._choices = span, choices
        ol = self.query_one("#ef-complete", OptionList)
        ol.clear_options()
        ol.add_options([Option(f"{title}  [dim]#{pid}[/dim]") for pid, title in choices])
        ol.highlighted = 0 if span.pick else None
        self.add_class("-completing")

    def _close_completion(self) -> None:
        self._span, self._choices = None, []
        self.remove_class("-completing")

    def completion_key(self, key: str) -> bool:
        """Handle a key while the list is open; False lets the editor have it."""
        if not self.has_class("-completing"):
            return False
        ol = self.query_one("#ef-complete", OptionList)
        n = len(self._choices)
        if key in ("down", "up"):
            step = 1 if key == "down" else -1
            cur = ol.highlighted
            ol.highlighted = (0 if step > 0 else n - 1) if cur is None else (cur + step) % n
            return True
        if key == "tab" or (key == "enter" and ol.highlighted is not None):
            self._accept(ol.highlighted or 0)
            return True
        if key in ("enter", "escape"):
            self._close_completion()  # Enter with nothing chosen is still a new line
            return key == "escape"
        return False

    def _accept(self, index: int) -> None:
        span = self._span
        if span is None or not 0 <= index < len(self._choices):
            return
        pid, title = self._choices[index]
        ta = self.query_one("#content-input", TextArea)
        row = ta.cursor_location[0]
        self._accepting = True
        ta.replace(accepted_text(span, pid, title), (row, span.start), (row, span.end))
        self._close_completion()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._accept(event.option_index)
        self.query_one("#content-input", TextArea).focus()

    # ── preview ──────────────────────────────────────────────────────────────

    def action_toggle_preview(self) -> None:
        showing = not self.has_class("-preview")
        if showing:
            index = getattr(self.app, "_link_index", {}) or {}
            text = self.query_one("#content-input", TextArea).text
            self.query_one("#ef-preview Markdown", Markdown).update(_linkify_markdown(text, index))
            self._close_completion()
        self.set_class(showing, "-preview")
        self.query_one("#preview-btn", Button).label = "Write" if showing else "Preview"
        if not showing:
            self.query_one("#content-input", TextArea).focus()

    # ── attachments ──────────────────────────────────────────────────────────

    def action_attach(self) -> None:
        """Upload a local file and insert its embed at the cursor — the
        browser's drop, paste or file picker. An edit files it beside the post;
        a new post where the post will file (by its tags)."""
        def _chosen(path: str | None) -> None:
            if path:
                tags = None if self.post is not None else self.values()["tags"]
                self._upload(path, self.post.id if self.post is not None else None, tags)

        self.app.push_screen(AttachFileModal(), _chosen)

    @work(thread=True, group="attach")
    def _upload(self, path: str, post_id: int | None, tags: list[str] | None) -> None:
        name = os.path.basename(path)
        self.app.call_from_thread(self.app.notify, f"Uploading {name}…", timeout=2)
        try:
            result = api.upload_attachment(path, post_id=post_id, tags=tags)
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, f"Couldn’t attach {name}: {exc}", severity="error")
            return
        self.app.call_from_thread(self._insert_ref, result.get("ref") or f"![[{result.get('filename', name)}]]")

    def _insert_ref(self, ref: str) -> None:
        if self.has_class("-preview"):
            self.action_toggle_preview()
        ta = self.query_one("#content-input", TextArea)
        row, col = ta.cursor_location
        line = ta.document.get_line(row)
        # On a line of its own, so the embed renders as a block.
        before = "" if not line[:col].strip() else "\n"
        after = "" if not line[col:].strip() else "\n"
        ta.insert(f"{before}{ref}{after}")
        ta.focus()
        self.app.notify(f"Attached {ref}", timeout=3)

    # ── leaving and saving ───────────────────────────────────────────────────

    def action_cancel(self) -> None:
        if self.has_class("-completing"):
            self._close_completion()
            return
        if self.creating:
            # A new post's text is kept as a draft, so leaving loses nothing.
            if self._draft_timer is not None:
                self._draft_timer.stop()
            drafts.save(self._raw())
            if self.is_dirty:
                self.app.notify("Draft kept — n opens it again", timeout=3)
            self.post_message(self.Cancelled())
            return
        if not self.is_dirty:
            self.post_message(self.Cancelled())
            return
        self._confirm("Discard your changes?", lambda: self.post_message(self.Cancelled()))

    def _discard(self) -> None:
        def _go() -> None:
            if self._draft_timer is not None:
                self._draft_timer.stop()
            drafts.clear()
            self.post_message(self.Cancelled())

        if self.is_dirty:
            self._confirm("Discard this draft?", _go)
        else:
            _go()

    def _confirm(self, message: str, then) -> None:
        from .modals import ConfirmModal  # modals imports this module

        self.app.push_screen(ConfirmModal(message), lambda ok: then() if ok else None)

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
            if self.post is None:
                post = api.create_post(**v)
            else:
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
        verb = "Publish" if self.creating else "Update"
        self.app.notify(f"{verb} failed: {exc}", severity="error")

    def _saved(self, post: api.Post) -> None:
        self._saving = False
        created = self.creating
        if created:
            if self._draft_timer is not None:
                self._draft_timer.stop()
            drafts.clear()
        self.post_message(self.Saved(post, created))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        actions = {
            "cancel-btn": self.action_cancel,
            "submit-btn": self.action_save,
            "preview-btn": self.action_toggle_preview,
            "attach-btn": self.action_attach,
            "discard-btn": self._discard,
        }
        if (action := actions.get(event.button.id or "")) is not None:
            action()


class AttachFileModal(ModalScreen["str | None"]):
    """Ask for a local file to attach; → accepts a path suggestion."""

    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    DEFAULT_CSS = f"""
    AttachFileModal {{ align: center middle; }}
    AttachFileModal > Vertical {{
        width: 76; max-width: 92%; height: auto; background: $background;
        border: solid {ACCENT}; padding: 1 2;
    }}
    AttachFileModal .af-title {{ color: {ACCENT}; text-style: bold; }}
    AttachFileModal Input {{ margin-top: 1; border: solid {BORDER}; }}
    AttachFileModal .af-hint {{ color: $text-muted; }}
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Attach a file", classes="af-title")
            yield Input(placeholder="~/Pictures/diagram.png", id="af-path", suggester=PathSuggester())
            yield Label("→ completes the path · Enter uploads · Esc cancels", classes="af-hint")

    def on_mount(self) -> None:
        self.query_one("#af-path", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        path = os.path.expanduser(event.value.strip())
        if not path:
            return
        if not os.path.isfile(path):
            self.app.notify(f"No such file: {event.value.strip()}", severity="warning", timeout=3)
            return
        self.dismiss(path)
