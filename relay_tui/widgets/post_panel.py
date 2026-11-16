from __future__ import annotations

from datetime import UTC, datetime

from rich.markup import escape
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, ListItem, ListView, Static

from .. import api
from ..render import preview_line
from ..theme import ACCENT, BORDER, PERF_BAD, PERF_TERRIBLE


def _skip_pinned(first_child_id: int | None, pinned_id: int | None) -> int:
    """How many leading children a new SSE arrival must be inserted after, so
    it lands below whatever's pinned on top (master doc, or a bare-id search
    hit) instead of displacing it. Takes just the current first child's id
    (``None`` if the feed is empty), not the whole list — `prepend_post`
    indexes the very list this is computed from, so building a second,
    ``PostItem``-filtered list to compute it from would risk the two silently
    drifting apart if a non-``PostItem`` were ever mounted into the feed.
    Pure so it's testable without mounting a Textual app — see
    ``prepend_post``, its one caller."""
    return 1 if pinned_id is not None and first_child_id == pinned_id else 0


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
            dt = dt.replace(tzinfo=UTC)
        s = int((datetime.now(UTC) - dt).total_seconds())
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
            dt = dt.replace(tzinfo=UTC)
        s = int((dt - datetime.now(UTC)).total_seconds())
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
            dt = dt.replace(tzinfo=UTC)
        s = int((dt - datetime.now(UTC)).total_seconds())
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
    PostItem.master {{ height: 5; border: solid {ACCENT}; background: $boost; }}
    PostItem.flash {{ background: {ACCENT} 25%; }}
    PostItem Label {{ width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }}
    PostItem .pi-preview {{ color: $text-muted; }}
    """

    def __init__(self, post: api.Post) -> None:
        super().__init__()
        self.post = post
        if post.id == 0:
            self.add_class("master")

    def compose(self) -> ComposeResult:
        post = self.post
        title = post.title or post.content.split("\n")[0]
        id_badge = f"[on {BORDER}] #{post.id} [/on {BORDER}]"
        mark = f"[{ACCENT}]✦[/] " if post.id == 0 else ""
        yield Label(f"{id_badge} {mark}[bold]{escape(title)}[/]", markup=True)
        yield Label(escape(preview_line(post.content, post.title)), markup=True, classes="pi-preview")
        meta_parts = [_time_ago(post.created_at)]
        if post.updated_at:
            meta_parts.append(f"edited {_time_ago(post.updated_at)}")
        if post.updated_by:
            meta_parts.append(f"by {escape(post.updated_by)}")
        if post.source:
            meta_parts.append(escape(post.source[:40]))
        meta_line = f"[dim]{'  •  '.join(meta_parts)}[/dim]"
        if post.expires_at:
            pill = _expiry_markup(post.expires_at)
            if pill:
                meta_line += f"  {pill}"
        if post.tags:
            meta_line += "  " + " ".join(f"[{ACCENT}]#{escape(t)}[/]" for t in post.tags)
        yield Label(meta_line, markup=True)


class PostPanel(Widget):
    class ViewPost(Message):
        def __init__(self, post: api.Post) -> None:
            super().__init__()
            self.post = post

    class LoadMore(Message):
        """Posted when the highlight nears the end of the loaded feed."""

    class Highlighted(Message):
        """The highlight moved to a post (the reader pane follows it)."""

        def __init__(self, post: api.Post) -> None:
            super().__init__()
            self.post = post

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
    # id of whichever post `set_posts` pinned on top (master doc, or a bare-id
    # search hit), if any — `prepend_post` needs this to know which slot an
    # incoming SSE post must not be inserted above.
    _pinned_id: int | None = None

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

    def set_posts(
        self,
        posts: list[api.Post],
        search: str | None = None,
        pinned_id: int | None = None,
        label: str | None = None,
        empty: str | None = None,
    ) -> None:
        header = self.query_one(Label)
        label = label if label is not None else (f"search: {search}" if search else None)
        header.update(f"[bold]FEED[/]  [dim]{escape(label)}[/dim]" if label else "FEED")
        self._empty_message = escape(
            empty if empty is not None else (f'No posts match "{search}"' if search else "No posts yet.")
        )
        lv = self.query_one("#post-listview", ListView)
        lv.clear()
        self._count = len(posts)
        self._pinned_id = pinned_id
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
        children = list(lv.children)
        first = children[0] if children else None
        first_id = first.post.id if isinstance(first, PostItem) else None
        skip = _skip_pinned(first_id, self._pinned_id)
        before = children[skip] if skip < len(children) else None
        if before is not None:
            lv.mount(item, before=before)
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
        lv = self.query_one(ListView)
        items = self._items()
        ids = [i.post.id for i in items]
        if post_id not in ids:
            return
        i = ids.index(post_id)
        # Where the highlight belongs once the card is gone: the same post when
        # one above it went, the neighbour when it was the highlighted one.
        target: int | None = None
        if lv.index is not None and i <= lv.index < len(items):
            if i < lv.index:
                target = ids[lv.index]
            elif (after := self.neighbour(post_id)) is not None:
                target = after.id
        self._count -= 1
        self._apply_visibility()
        self.call_later(self._remove_item, items[i], target)

    async def _remove_item(self, item: PostItem, target: int | None) -> None:
        await item.remove()
        if target is not None:
            self._reselect(target)

    def _reselect(self, post_id: int) -> None:
        """Highlight ``post_id`` afresh: a card mounted or removed under the
        index leaves the ListView pointing at a card it hasn't drawn as such."""
        self.query_one(ListView).index = None
        self.select(post_id)

    def has_post(self, post_id: int) -> bool:
        return any(
            isinstance(c, PostItem) and c.post.id == post_id
            for c in self.query_one(ListView).children
        )

    def update_post(self, post: api.Post, *, flash: bool = False) -> None:
        lv = self.query_one(ListView)
        for i, item in enumerate(self._items()):
            if item.post.id == post.id:
                self.call_later(self._replace_item, item, post, flash, lv.index == i)
                break

    async def _replace_item(self, old: PostItem, post: api.Post, flash: bool, highlighted: bool) -> None:
        new_item = PostItem(post)
        await self.query_one(ListView).mount(new_item, before=old)
        await old.remove()
        if highlighted:
            self._reselect(post.id)
        if flash:
            new_item.add_class("flash")
            self.set_timer(1.2, lambda: new_item.remove_class("flash"))

    def focus(self, scroll_visible: bool = True) -> PostPanel:
        self.query_one("#post-listview", ListView).focus(scroll_visible)
        return self

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        if isinstance(event.item, PostItem):
            self.post_message(self.ViewPost(event.item.post))

    def _items(self) -> list[PostItem]:
        return [c for c in self.query_one(ListView).children if isinstance(c, PostItem)]

    def select(self, post_id: int | None) -> bool:
        """Highlight a post by id; False when it isn't in the feed."""
        for i, item in enumerate(self._items()):
            if item.post.id == post_id:
                self.query_one(ListView).index = i
                return True
        return False

    def first_post(self) -> api.Post | None:
        """The first post a reader would open: the master document is skipped,
        it is pinned for reference, not for reading first."""
        items = self._items()
        for item in items:
            if item.post.id != 0:
                return item.post
        return items[0].post if items else None

    def neighbour(self, post_id: int) -> api.Post | None:
        """The post after ``post_id`` (or before it, at the end) — where the
        selection lands when that post goes away. Never the master document:
        it is pinned for reference, and an emptied list says so instead."""
        items = [i for i in self._items() if i.post.id == post_id or i.post.id != 0]
        ids = [i.post.id for i in items]
        if post_id not in ids:
            return None
        i = ids.index(post_id)
        rest = items[i + 1:] or items[:i][-1:]
        return rest[0].post if rest else None

    def step(self, delta: int) -> None:
        lv = self.query_one(ListView)
        n = len(lv.children)
        if not n:
            return
        lv.index = max(0, min(n - 1, (lv.index if lv.index is not None else -1) + delta))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, PostItem):
            self.post_message(self.Highlighted(event.item.post))
        lv = event.list_view
        if lv.index is not None and lv.index >= len(lv.children) - 5:
            self.post_message(self.LoadMore())
