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
    class TagSelected(Message):
        def __init__(self, tag: str | None) -> None:
            super().__init__()
            self.tag = tag  # None means all posts

    class ConfigureTag(Message):
        def __init__(self, tag_name: str) -> None:
            super().__init__()
            self.tag_name = tag_name

    class RenameTag(Message):
        def __init__(self, tag_name: str) -> None:
            super().__init__()
            self.tag_name = tag_name

    BINDINGS = [
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
        lv = self.query_one("#tag-listview", ListView)
        lv.clear()
        lv.mount(TagItem("", sum(t.count for t in tags)))
        for t in tags:
            lv.mount(TagItem(t.name, t.count))
        # highlight active
        for item in lv.children:
            if isinstance(item, TagItem):
                if item.tag_name == (active or ""):
                    lv.index = list(lv.children).index(item)
                    break

    def focus(self, scroll_visible: bool = True) -> "TagPanel":
        self.query_one("#tag-listview", ListView).focus(scroll_visible)
        return self

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        if isinstance(event.item, TagItem):
            tag = event.item.tag_name or None
            self.post_message(self.TagSelected(tag))

    def action_configure_tag(self) -> None:
        lv = self.query_one("#tag-listview", ListView)
        highlighted = lv.highlighted_child
        if isinstance(highlighted, TagItem) and highlighted.tag_name:
            self.post_message(self.ConfigureTag(highlighted.tag_name))

    def action_rename_tag(self) -> None:
        lv = self.query_one("#tag-listview", ListView)
        highlighted = lv.highlighted_child
        if isinstance(highlighted, TagItem) and highlighted.tag_name:
            self.post_message(self.RenameTag(highlighted.tag_name))
