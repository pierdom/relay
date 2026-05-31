from __future__ import annotations

from datetime import datetime, timezone

from rich.markup import escape
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, ListView, ListItem

from .. import api
from ..theme import ACCENT, BORDER


def _time_ago(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        s = int((datetime.now(timezone.utc) - dt).total_seconds())
        if s < 60:
            return "just now"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except Exception:
        return iso[:10]


class PostItem(ListItem):
    DEFAULT_CSS = f"""
    PostItem {{ height: 4; padding: 0 1; border-bottom: solid $surface; }}
    PostItem.master {{ border: solid {ACCENT}; background: $boost; }}
    PostItem Label {{ width: 1fr; }}
    """

    def __init__(self, post: api.Post) -> None:
        super().__init__()
        self.post = post
        if post.id == 0:
            self.add_class("master")

    def compose(self) -> ComposeResult:
        is_master = self.post.id == 0
        title = self.post.title or self.post.content.split("\n")[0]
        if len(title) > 60:
            title = title[:57] + "…"
        tags_markup = (
            "  ".join(
                f"[bold {ACCENT}][{escape(t)}][/]" for t in self.post.tags
            )
            if self.post.tags
            else ""
        )
        meta_parts = [_time_ago(self.post.created_at)]
        if self.post.updated_at:
            meta_parts.append(f"edited {_time_ago(self.post.updated_at)}")
        if self.post.source:
            meta_parts.append(self.post.source[:40])
        id_badge = f"[on {BORDER}] #{self.post.id} [/on {BORDER}]"
        master_prefix = f"[bold {ACCENT}]✦ MASTER  [/]" if is_master else ""
        yield Label(
            f"{id_badge}  {master_prefix}[bold]{escape(title)}[/]  {tags_markup}",
            markup=True,
        )
        yield Label(
            f"[dim]{'  •  '.join(meta_parts)}[/dim]",
            markup=True,
        )


class PostPanel(Widget):
    class ViewPost(Message):
        def __init__(self, post: api.Post) -> None:
            super().__init__()
            self.post = post

    DEFAULT_CSS = """
    PostPanel ListView { background: transparent; border: none; height: 1fr; }
    PostPanel ListView:focus { border: none; }
    PostPanel { padding: 0; }
    PostPanel > Label { color: $accent; text-style: bold; padding: 0 1; }
    """

    @property
    def selected_post(self) -> api.Post | None:
        lv = self.query_one(ListView)
        item = lv.highlighted_child
        if isinstance(item, PostItem):
            return item.post
        return None

    def compose(self) -> ComposeResult:
        yield Label("FEED")
        yield ListView(id="post-listview")

    def set_posts(self, posts: list[api.Post]) -> None:
        lv = self.query_one("#post-listview", ListView)
        lv.clear()
        for p in posts:
            lv.mount(PostItem(p))

    def prepend_post(self, post: api.Post) -> None:
        lv = self.query_one("#post-listview", ListView)
        item = PostItem(post)
        if lv.children:
            lv.mount(item, before=lv.children[0])
        else:
            lv.mount(item)

    def remove_post(self, post_id: int) -> None:
        for item in list(self.query_one(ListView).children):
            if isinstance(item, PostItem) and item.post.id == post_id:
                item.remove()
                break

    def update_post(self, post: api.Post) -> None:
        lv = self.query_one(ListView)
        children = list(lv.children)
        for i, item in enumerate(children):
            if isinstance(item, PostItem) and item.post.id == post.id:
                new_item = PostItem(post)
                item.remove()
                current_children = list(lv.children)
                if i < len(current_children):
                    lv.mount(new_item, before=current_children[i])
                else:
                    lv.mount(new_item)
                break

    def focus(self, scroll_visible: bool = True) -> "PostPanel":
        self.query_one("#post-listview", ListView).focus(scroll_visible)
        return self

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        if isinstance(event.item, PostItem):
            self.post_message(self.ViewPost(event.item.post))
