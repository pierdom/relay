from __future__ import annotations

from datetime import UTC, datetime

from rich.markup import escape
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, ListItem, ListView, Static

from .. import api
from ..render import preview_line
from .lists import PagedListView


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
        color = "$relay-terrible"
    elif s < 3 * 86400:
        color = "$relay-bad"
    else:
        color = "$relay-accent-text"
    return f"[{color} on $relay-badge] expires {_time_until(iso)} [/]"


def date_group(iso: str | None, now: datetime | None = None) -> str:
    """The feed's date group for a timestamp, in the reader's local days:
    Today, Yesterday, Earlier this week, Earlier this month, then the month
    (``March``) within this year and the year before that."""
    if not iso:
        return "Undated"
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "Undated"
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    local, today = when.astimezone(), now.astimezone().date()
    days = (today - local.date()).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return "Earlier this week"
    if days < 31:
        return "Earlier this month"
    if local.year == today.year:
        return local.strftime("%B")
    return str(local.year)


class PostItem(ListItem):
    DEFAULT_CSS = """
    PostItem { height: 4; padding: 0 1; border-bottom: solid $relay-badge; }
    PostItem.-grouped { height: 5; }
    PostItem .pi-group { display: none; color: $relay-accent-text; text-style: bold; text-overflow: clip; }
    PostItem.-grouped .pi-group { display: block; }
    PostItem.master { height: 5; border: round $relay-frame; }
    PostItem.flash { background: $accent 20%; }
    PostItem Label { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
    PostItem .pi-preview { color: $relay-muted; }
    """

    def __init__(self, post: api.Post) -> None:
        super().__init__()
        self.post = post
        if post.id == 0:
            self.add_class("master")

    def compose(self) -> ComposeResult:
        post = self.post
        title = post.title or post.content.split("\n")[0]
        id_badge = f"[on $relay-badge] #{post.id} [/on $relay-badge]"
        mark = "[$relay-accent-text]✦[/] " if post.id == 0 else ""
        yield Label("", classes="pi-group")
        yield Label(f"{id_badge} {mark}[bold]{escape(title)}[/]", markup=True)
        yield Label(escape(preview_line(post.content, post.title)), markup=True, classes="pi-preview")
        meta_parts = [_time_ago(post.created_at)]
        if post.updated_at:
            meta_parts.append(f"edited {_time_ago(post.updated_at)}")
        if post.updated_by:
            meta_parts.append(f"by {escape(post.updated_by)}")
        if post.source:
            meta_parts.append(escape(post.source[:40]))
        meta_line = f"[$relay-muted]{'  •  '.join(meta_parts)}[/]"
        if post.expires_at:
            pill = _expiry_markup(post.expires_at)
            if pill:
                meta_line += f"  {pill}"
        if post.tags:
            meta_line += "  " + " ".join(f"[$relay-accent-text]#{escape(t)}[/]" for t in post.tags)
        yield Label(meta_line, markup=True)

    group: str | None = None

    def set_group(self, label: str | None) -> None:
        """Open a date group with a divider line above the card, or not."""
        if label == self.group:
            return
        self.group = label
        self.set_class(label is not None, "-grouped")
        self._show_group()

    def on_mount(self) -> None:
        self._show_group()

    def _show_group(self) -> None:
        # Not there yet while the card mounts (on_mount then draws it).
        for line in self.query(".pi-group").results(Label):
            line.update(f"── {escape(self.group)} " + "─" * 60 if self.group else "")


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
    PostPanel #post-empty {
        display: none;
        height: 1fr;
        width: 1fr;
        content-align: center middle;
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
        yield PagedListView(id="post-listview")
        yield Static("", id="post-empty", classes="status-line")

    def on_mount(self) -> None:
        self.add_class("panel")
        self.border_title = "Feed"

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
        summary: str = "",
        group_by: str | None = None,
    ) -> None:
        """``label`` names the filter (the title: ``Feed · #homelab``);
        ``summary`` is the order and count (the bottom border: ``↓ updated · 42``)."""
        label = label if label is not None else (f"⌕ {search}" if search else None)
        self.border_title = f"Feed · {escape(label)}" if label else "Feed"
        self._summary = summary
        self.set_pending(0)
        self._empty_message = escape(
            empty if empty is not None else (f'No posts match "{search}"' if search else "No posts yet.")
        )
        lv = self.query_one("#post-listview", ListView)
        lv.clear()
        self._count = len(posts)
        self._pinned_id = pinned_id
        self._group_by = group_by
        self._apply_visibility()
        for p in posts:
            lv.mount(PostItem(p))
        self.call_after_refresh(self._regroup)

    # The feed's date groups: by the date it is sorted on (none for a search,
    # ranked by relevance, not by date). A divider sits in the first card of a
    # group rather than as a row of its own, so every index stays a post's.
    _group_by: str | None = None

    def _regroup(self) -> None:
        previous = None
        for item in self._items():
            if self._group_by is None or item.post.id == self._pinned_id:
                item.set_group(None)
                continue
            post = item.post
            stamp = post.created_at if self._group_by == "created" else (post.updated_at or post.created_at)
            label = date_group(stamp)
            item.set_group(label if label != previous else None)
            previous = label

    _summary = ""

    def set_pending(self, n: int) -> None:
        """Say how many live posts are waiting for a reload (`r`), in place of
        the order and count while there are any."""
        self.border_subtitle = f"● {n} new · r shows" if n else self._summary

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
        self.call_after_refresh(self._regroup)

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
        self.call_after_refresh(self._regroup)

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
        self._regroup()
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
        self._regroup()
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
