from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, ListView, ListItem

from .. import api
from ..theme import ACCENT, BORDER


class TagItem(ListItem):
    """One row in the tag list."""

    def __init__(self, tag_name: str, count: int) -> None:
        super().__init__()
        self.tag_name = tag_name  # empty string means "all"
        self.count = count

    def compose(self) -> ComposeResult:
        display = "all posts" if not self.tag_name else self.tag_name
        yield Label(
            f"{escape(display):<18}[dim]({self.count})[/dim]",
            markup=True,
        )


class TagPanel(Widget):
    """Sidebar that toggles between a Tags view and a folder Tree view."""

    _mode = "tags"  # "tags" | "tree"

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
        def __init__(self, tag_name: str) -> None:
            super().__init__()
            self.tag_name = tag_name

    class RenameTag(Message):
        def __init__(self, tag_name: str) -> None:
            super().__init__()
            self.tag_name = tag_name

    BINDINGS = [
        Binding("t", "toggle_view", "Tags/Tree"),
        Binding("c", "configure_tag", "Config"),
        Binding("R", "rename_tag", "Rename"),
    ]

    DEFAULT_CSS = """
    TagPanel ListView { background: transparent; border: none; height: 1fr; }
    TagPanel ListView:focus { border: none; }
    TagPanel { padding: 0; }
    TagPanel > Label { color: $surface; text-style: bold; padding: 0 1; }
    TagPanel:focus-within > Label { color: $accent; }
    """

    def compose(self) -> ComposeResult:
        yield Label("TOPICS")
        yield ListView(id="tag-listview")

    def set_tags(self, tags: list[api.Tag], active: str | None = None) -> None:
        self._mode = "tags"
        self.query_one(Label).update("TOPICS · [dim]tags[/]")
        self._fill([("", sum(t.count for t in tags))] + [(t.name, t.count) for t in tags], active)

    def set_folders(self, folders: list[tuple[str, int]], active: str | None = None) -> None:
        self._mode = "tree"
        self.query_one(Label).update("TOPICS · [dim]tree[/]")
        items = [("", sum(c for _, c in folders))] + [(f, c) for f, c in folders]
        self._fill(items, active)

    def _fill(self, rows: list[tuple[str, int]], active: str | None) -> None:
        lv = self.query_one("#tag-listview", ListView)
        lv.clear()
        for name, count in rows:
            lv.mount(TagItem(name, count))
        for i, item in enumerate(lv.children):
            if isinstance(item, TagItem) and item.tag_name == (active or ""):
                lv.index = i
                break

    def focus(self, scroll_visible: bool = True) -> "TagPanel":
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
            self.post_message(self.ConfigureTag(highlighted.tag_name))

    def action_rename_tag(self) -> None:
        if self._mode != "tags":
            return
        highlighted = self.query_one("#tag-listview", ListView).highlighted_child
        if isinstance(highlighted, TagItem) and highlighted.tag_name:
            self.post_message(self.RenameTag(highlighted.tag_name))
