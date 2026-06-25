from __future__ import annotations

from datetime import datetime, timezone

from rich.markup import escape
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, ListView, ListItem, Static

from .. import api
from ..theme import ACCENT, BORDER, PERF_BAD, PERF_TERRIBLE


def _fmt_span(seconds: int) -> str:
    if seconds < 60:
        return "<1m"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _time_ago(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        s = int((datetime.now(timezone.utc) - dt).total_seconds())
        if s < 60:
            return "just now"
        return f"{_fmt_span(s)} ago"
    except Exception:
        return iso[:10]


def _time_until(iso: str) -> str:
    """Human span for a (usually future) timestamp, e.g. ``in 3d`` / ``2h ago``."""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        s = int((dt - datetime.now(timezone.utc)).total_seconds())
        if s < 0:
            return f"{_fmt_span(-s)} ago"
        if s < 60:
            return "now"
        return f"in {_fmt_span(s)}"
    except Exception:
        return iso[:10]


def _expiry_markup(iso: str) -> str:
    """Colour-graded expiry pill: red <24h (or expired), amber <3d, accent beyond."""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        s = int((dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return ""
    if s < 86400:
        color = PERF_TERRIBLE
    elif s < 3 * 86400:
        color = PERF_BAD
    else:
        color = ACCENT
    return f"[{color} on {BORDER}] expires {_time_until(iso)} [/]"


class PostItem(ListItem):
    DEFAULT_CSS = f"""
    PostItem {{ height: 4; padding: 0 1; border-bottom: solid $surface; }}
    PostItem.master {{ border: solid {ACCENT}; background: $boost; }}
    PostItem.flash {{ background: {ACCENT} 25%; }}
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
                f"[bold {ACCENT} on {BORDER}] {escape(t)} [/]" for t in self.post.tags
            )
            if self.post.tags
            else ""
        )
        meta_parts = [_time_ago(self.post.created_at)]
        if self.post.updated_at:
            meta_parts.append(f"edited {_time_ago(self.post.updated_at)}")
        if self.post.source:
            meta_parts.append(escape(self.post.source[:40]))
        id_badge = f"[on {BORDER}] #{self.post.id} [/on {BORDER}]"
        master_prefix = f"[bold {ACCENT}]✦ MASTER  [/]" if is_master else ""
        yield Label(
            f"{id_badge}  {master_prefix}[bold]{escape(title)}[/]  {tags_markup}",
            markup=True,
        )
        meta_line = f"[dim]{'  •  '.join(meta_parts)}[/dim]"
        if self.post.expires_at:
            pill = _expiry_markup(self.post.expires_at)
            if pill:
                meta_line += f"  {pill}"
        yield Label(meta_line, markup=True)


class PostPanel(Widget):
    class ViewPost(Message):
        def __init__(self, post: api.Post) -> None:
            super().__init__()
            self.post = post

    class LoadMore(Message):
        """Posted when the highlight nears the end of the loaded feed."""

    DEFAULT_CSS = """
    PostPanel ListView { background: transparent; border: none; height: 1fr; }
    PostPanel ListView:focus { border: none; }
    PostPanel { padding: 0; }
    PostPanel > Label { color: $surface; text-style: bold; padding: 0 1; }
    PostPanel:focus-within > Label { color: $accent; }
    PostPanel #post-empty {
        display: none;
        height: 1fr;
        width: 1fr;
        content-align: center middle;
        color: $surface;
        text-style: italic;
    }
    """

    _empty_message = "No posts yet."
    # Tracked explicitly: widget removal/mount is async, so counting live DOM
    # children can't tell whether the feed is empty within a single tick.
    _count = 0

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
        yield Static("", id="post-empty")

    def _apply_visibility(self) -> None:
        """Show the feed when it has posts, the empty-state placeholder otherwise."""
        lv = self.query_one("#post-listview", ListView)
        empty = self.query_one("#post-empty", Static)
        if self._count > 0:
            lv.display = True
            empty.display = False
        else:
            empty.update(self._empty_message)
            lv.display = False
            empty.display = True

    def set_posts(self, posts: list[api.Post], search: str | None = None) -> None:
        header = self.query_one(Label)
        if search:
            header.update(f"[bold]FEED[/]  [dim]search: {escape(search)}[/dim]")
        else:
            header.update("FEED")
        self._empty_message = (
            f'No posts match "{escape(search)}"' if search else "No posts yet."
        )
        lv = self.query_one("#post-listview", ListView)
        lv.clear()
        self._count = len(posts)
        self._apply_visibility()
        for p in posts:
            lv.mount(PostItem(p))

    def prepend_post(self, post: api.Post) -> None:
        lv = self.query_one("#post-listview", ListView)
        # Skip if already present: the server echoes published posts back over
        # SSE, so a post created in this client arrives both optimistically and
        # via the stream (also guards against reconnect replay overlap).
        for child in lv.children:
            if isinstance(child, PostItem) and child.post.id == post.id:
                return
        self._count += 1
        self._apply_visibility()
        item = PostItem(post)
        if lv.children:
            lv.mount(item, before=lv.children[0])
        else:
            lv.mount(item)
        # Briefly highlight live arrivals so the eye catches new posts.
        item.add_class("flash")
        self.set_timer(1.2, lambda: item.remove_class("flash"))

    def append_posts(self, posts: list[api.Post]) -> None:
        lv = self.query_one("#post-listview", ListView)
        existing = {
            c.post.id for c in lv.children if isinstance(c, PostItem)
        }
        for p in posts:
            if p.id not in existing:
                lv.mount(PostItem(p))
                self._count += 1
        self._apply_visibility()

    def remove_post(self, post_id: int) -> None:
        for item in list(self.query_one(ListView).children):
            if isinstance(item, PostItem) and item.post.id == post_id:
                item.remove()
                self._count -= 1
                break
        self._apply_visibility()

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

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        lv = event.list_view
        if lv.index is not None and lv.index >= len(lv.children) - 5:
            self.post_message(self.LoadMore())
