from __future__ import annotations

from rich.markup import escape
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, ListItem, ListView

from .. import api
from .lists import PagedListView


class TagItem(ListItem):
    """One row in the tag list."""

    def __init__(self, tag_name: str, count: int) -> None:
        super().__init__()
        self.tag_name = tag_name  # empty string means "all"
        self.count = count

    def compose(self) -> ComposeResult:
        display = "all posts" if not self.tag_name else self.tag_name
        yield Label(
            f"{escape(display):<18}[$relay-muted]({self.count})[/]",
            markup=True,
        )


class TagPanel(Widget):
    """Sidebar that toggles between a Tags view and a folder Tree view."""

    _mode = "tags"  # "tags" | "tree"
    _tags: dict[str, api.Tag] = {}

    class TagSelected(Message):
        def __init__(self, tag: str | None) -> None:
            super().__init__()
            self.tag = tag  # None means all posts

    class FolderSelected(Message):
        def __init__(self, folder: str | None) -> None:
            super().__init__()
            self.folder = folder  # None means all posts

    class ToggleView(Message):
        """Request to switch the sidebar between tags and tree."""

    class ConfigureTag(Message):
        def __init__(self, tag_name: str, current: api.Tag | None) -> None:
            super().__init__()
            self.tag_name = tag_name
            self.current = current  # its config now, to open the modal on

    class RenameTag(Message):
        def __init__(self, tag_name: str) -> None:
            super().__init__()
            self.tag_name = tag_name

    BINDINGS = [
        Binding("t", "toggle_view", "Tags/Tree"),
        Binding("c", "configure_tag", "Config"),
        Binding("R", "rename_tag", "Rename"),
        Binding("slash", "start_filter", "Filter"),
    ]

    DEFAULT_CSS = """
    TagPanel ListView { background: transparent; border: none; height: 1fr; }
    TagPanel ListView:focus { border: none; }
    TagPanel { padding: 0; }
    """

    def compose(self) -> ComposeResult:
        yield PagedListView(id="tag-listview")

    def on_mount(self) -> None:
        self.add_class("panel")
        self.border_title = "Topics"

    def set_tags(self, tags: list[api.Tag], active: str | None = None) -> None:
        self._mode = "tags"
        self._tags = {t.name: t for t in tags}
        self._fill([("", sum(t.count for t in tags))] + [(t.name, t.count) for t in tags], active)

    def set_folders(self, folders: list[tuple[str, int]], active: str | None = None) -> None:
        self._mode = "tree"
        items = [("", sum(c for _, c in folders))] + [(f, c) for f, c in folders]
        self._fill(items, active)

    # Type-to-filter (`/`): narrows the rows already loaded as you type, the
    # border saying `/ que_` while typing and `⌕ query` after. Esc clears it.
    _rows: list[tuple[str, int]] = []
    _active: str | None = None
    _query = ""
    _typing = False

    def _fill(self, rows: list[tuple[str, int]], active: str | None) -> None:
        self._rows, self._active = rows, active
        self._show_rows()

    def _show_rows(self) -> None:
        q = self._query.lower()
        rows = [(n, c) for n, c in self._rows if n and q in n.lower()] if q else self._rows
        lv = self.query_one("#tag-listview", ListView)
        lv.clear()
        for name, count in rows:
            lv.mount(TagItem(name, count))
        want = None if q else (self._active or "")
        index = next((i for i, (n, _) in enumerate(rows) if n == want), 0 if rows else None)
        lv.index = index
        self._show_subtitle()

    def _show_subtitle(self) -> None:
        mode = "tags" if self._mode == "tags" else "folders"
        if self._typing:
            self.border_subtitle = f"/ {self._query}_"
        elif self._query:
            self.border_subtitle = f"⌕ {self._query} · esc clears"
        else:
            self.border_subtitle = f"{mode} · / filters · t"

    @property
    def filtering(self) -> bool:
        return self._typing or bool(self._query)

    def action_start_filter(self) -> None:
        self._typing = True
        self._show_subtitle()

    def clear_filter(self) -> None:
        self._typing, self._query = False, ""
        self._show_rows()

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape" and self.filtering:
            event.stop()
            event.prevent_default()
            self.clear_filter()
            return
        if not self._typing:
            return
        if event.key == "enter":
            self._typing = False   # the list's own Enter then picks the row
            self._show_subtitle()
            return
        if event.key in ("backspace", "ctrl+h"):
            self._query = self._query[:-1]
        elif event.is_printable and event.character:
            self._query += event.character
        else:
            return   # arrows and the rest move the list as usual
        event.stop()
        event.prevent_default()
        self._show_rows()

    def focus(self, scroll_visible: bool = True) -> TagPanel:
        self.query_one("#tag-listview", ListView).focus(scroll_visible)
        return self

    def action_toggle_view(self) -> None:
        self.post_message(self.ToggleView())

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        if not isinstance(event.item, TagItem):
            return
        value = event.item.tag_name or None
        if self._mode == "tree":
            self.post_message(self.FolderSelected(value))
        else:
            self.post_message(self.TagSelected(value))

    def action_configure_tag(self) -> None:
        if self._mode != "tags":
            return
        highlighted = self.query_one("#tag-listview", ListView).highlighted_child
        if isinstance(highlighted, TagItem) and highlighted.tag_name:
            self.post_message(self.ConfigureTag(highlighted.tag_name, self._tags.get(highlighted.tag_name)))

    def action_rename_tag(self) -> None:
        if self._mode != "tags":
            return
        highlighted = self.query_one("#tag-listview", ListView).highlighted_child
        if isinstance(highlighted, TagItem) and highlighted.tag_name:
            self.post_message(self.RenameTag(highlighted.tag_name))
