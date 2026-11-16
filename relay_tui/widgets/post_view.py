"""One post, rendered: the detail modal and the reader pane both show a post
through `PostView`, so there is one renderer, not two.

Links inside it post `PostView.OpenLink`; whoever hosts the view decides what
following one means (a stacked modal, or moving the reader pane).
"""
from __future__ import annotations

from rich.markup import escape
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Markdown, OptionList, Static
from textual.widgets._markdown import MarkdownHeader  # not re-exported in Textual 8; tested
from textual.widgets.option_list import Option

from .. import api
from ..render import _linkify_markdown, _outbound_link_ids, code_blocks, strip_title_heading
from .post_panel import _time_ago, _time_until

# Backlinks are fetched once the selection settles, not on every j/k step.
_BACKLINK_DELAY = 0.25


def post_meta(post: api.Post) -> str:
    """The detail line under a post's title, as Rich markup."""
    parts = [_time_ago(post.created_at)]
    if post.updated_at:
        parts.append(f"edited {_time_ago(post.updated_at)}")
    if post.updated_by:
        parts.append(f"by {post.updated_by}")
    if post.source:
        parts.append(post.source)
    if post.expires_at:
        parts.append(f"expires {_time_until(post.expires_at)}")
    meta = f"[$relay-muted]{escape('  •  '.join(parts))}[/]"
    if post.tags:
        meta += "  " + "  ".join(f"[$relay-accent-text]#{escape(t)}[/]" for t in post.tags)
    return meta


class PostView(Vertical):
    class OpenLink(Message):
        def __init__(self, post_id: int) -> None:
            super().__init__()
            self.post_id = post_id

    class Restored(Message):
        """A revision was restored from the history modal opened here."""

        def __init__(self, post: api.Post) -> None:
            super().__init__()
            self.post = post

    DEFAULT_CSS = """
    PostView { height: 1fr; width: 1fr; }
    PostView .pv-title { text-style: bold; color: $accent; width: 1fr; height: auto; }
    PostView .pv-meta { width: 1fr; height: auto; }
    PostView .pv-rule { border-bottom: solid $relay-badge; height: 1; width: 1fr; }
    PostView VerticalScroll { height: 1fr; width: 1fr; border: none; background: transparent; }
    PostView Markdown { width: 1fr; height: auto; }
    """

    post: api.Post | None = None

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._link_index: dict[str, int] = {}
        self._link_titles: dict[int, str] = {}
        self._backlinks: list[tuple[int, str]] = []
        self._related: list[tuple[int, str]] = []
        self._backlink_timer = None
        self._generation = 0

    def compose(self) -> ComposeResult:
        yield Label("", classes="pv-title", markup=True)
        yield Label("", classes="pv-meta", markup=True)
        yield Static("", classes="pv-rule")
        with VerticalScroll(classes="pv-scroll"):
            # open_links=False: clicks are routed here (relay: links inside,
            # real URLs to the browser) so the widget never opens relay:N.
            yield Markdown("", classes="pv-body", open_links=False)
            yield Markdown("", classes="pv-backlinks", open_links=False)

    def show(
        self,
        post: api.Post,
        link_index: dict[str, int] | None = None,
        link_titles: dict[int, str] | None = None,
    ) -> None:
        same = self.post is not None and self.post.id == post.id
        self.post = post
        if link_index is not None:
            self._link_index = link_index
        if link_titles is not None:
            self._link_titles = link_titles
        badge = f"[on $relay-badge] #{post.id} [/on $relay-badge]"
        master = "  [$relay-accent-text]✦ Master document[/]" if post.id == 0 else ""
        title = post.title or post.content.split("\n")[0][:80]
        self.query_one(".pv-title", Label).update(f"{badge}{master}  [bold]{escape(title)}[/]")
        self.query_one(".pv-meta", Label).update(post_meta(post))
        body = strip_title_heading(post.content, post.title)
        self.query_one(".pv-body", Markdown).update(_linkify_markdown(body, self._link_index))
        if not same:
            self.query_one(".pv-scroll", VerticalScroll).scroll_home(animate=False)
            self._backlinks = []
            self._related = []
            self.query_one(".pv-backlinks", Markdown).update("")
        self._generation += 1
        if self._backlink_timer is not None:
            self._backlink_timer.stop()
        generation = self._generation
        self._backlink_timer = self.set_timer(
            _BACKLINK_DELAY, lambda: self._load_backlinks(post.id, generation)
        )

    @work(thread=True)
    def _load_backlinks(self, post_id: int, generation: int) -> None:
        try:
            items = api.get_backlinks(post_id)
        except Exception:
            items = []
        # Linked mentions show at once; a similarity query may take longer.
        self.app.call_from_thread(self._set_backlinks, items, generation)
        # "Related, not linked" needs semantic search; the server 503s without it.
        if not getattr(self.app, "_embeddings_on", False):
            return
        try:
            related = api.get_related(post_id)
        except Exception:
            return
        if related:
            self.app.call_from_thread(self._set_backlinks, items, generation, related)

    def _set_backlinks(
        self, items: list[tuple[int, str]], generation: int, related: list[tuple[int, str]] | None = None
    ) -> None:
        if generation != self._generation:
            return  # the view moved on to another post meanwhile
        self._backlinks = items
        self._related = related or []
        parts = []
        if items:
            parts.append("**Linked mentions**  ([b]f[/b] to follow)\n\n" + "\n".join(
                f"- [#{i} {t}](relay:{i})" for i, t in items
            ))
        if self._related:
            parts.append("**Related, not linked**\n\n" + "\n".join(
                f"- [#{i} {t}](relay:{i})" for i, t in self._related
            ))
        md = "\n---\n\n" + "\n\n".join(parts) if parts else ""
        self.query_one(".pv-backlinks", Markdown).update(md)

    def follow_link(self) -> None:
        """A keyboard picker of every link in the post, outbound and back."""
        if self.post is None:
            return
        items: list[tuple[int, str, str]] = []
        seen: set[int] = set()
        for pid in _outbound_link_ids(self.post.content, self._link_index):
            seen.add(pid)
            items.append((pid, self._link_titles.get(pid, ""), "→"))
        for pid, title in self._backlinks:
            if pid not in seen:
                seen.add(pid)
                items.append((pid, title, "←"))
        for pid, title in self._related:
            if pid not in seen:
                seen.add(pid)
                items.append((pid, title, "≈"))
        if not items:
            self.app.notify("No links in this post", severity="warning")
            return

        def _on_pick(pid: int | None) -> None:
            if pid is not None:
                self.post_message(self.OpenLink(pid))

        self.app.push_screen(LinkPickerModal(items), _on_pick)

    def headings(self) -> list[tuple[int, str, Widget]]:
        """The body's H2/H3s as (level, text, heading widget) — the outline.
        Read off the rendered blocks: the Markdown widget's cached table of
        contents can name ids from a previous render."""
        md = self.query_one(".pv-body", Markdown)
        heads = []
        for h in md.query(MarkdownHeader):
            if h.LEVEL in (2, 3):
                content = getattr(h, "_content", None)
                heads.append((h.LEVEL, getattr(content, "plain", str(content or "")), h))
        return heads

    def goto_heading(self) -> None:
        """Pick a heading and scroll to it — the browser's outline beside a
        long post. A post with fewer than two has none, as there."""
        heads = self.headings()
        if len(heads) < 2:
            self.app.notify("No outline: this post has fewer than two headings", severity="warning", timeout=2)
            return
        from .picker import PickerModal

        def _go(block: Widget | None) -> None:
            if block is not None and block.is_attached:
                self.query_one(".pv-scroll", VerticalScroll).scroll_to_widget(block, top=True, animate=False)

        items = [(("    " if lvl == 3 else "") + text, h) for lvl, text, h in heads]
        self.app.push_screen(PickerModal("Outline", items), _go)

    def copy(self) -> None:
        """Copy the post's Markdown, or one of its code blocks, through the
        terminal's clipboard (OSC 52 — most terminals, and over SSH)."""
        if self.post is None:
            return
        post = self.post
        blocks = code_blocks(post.content)

        def _copy(text: str | None) -> None:
            if text is None:
                return
            self.app.copy_to_clipboard(text)
            self.app.notify(f"Copied {len(text.splitlines()) or 1} line(s)", timeout=2)

        if not blocks:
            _copy(post.content)
            return
        from .picker import PickerModal

        items = [("The whole post (Markdown)", post.content)] + [
            (f"Code {n}{f' · {lang}' if lang else ''}: {(code.strip().splitlines() or [''])[0][:60]}", code)
            for n, (lang, code) in enumerate(blocks, 1)
        ]
        self.app.push_screen(PickerModal("Copy", items), _copy)

    def history(self) -> None:
        if self.post is None:
            return
        from .modals import PostHistoryModal  # modals hosts the edit form, which this module doesn't need

        def _on_done(restored: api.Post | None) -> None:
            if restored is not None:
                self.show(restored)
                self.post_message(self.Restored(restored))

        self.app.push_screen(PostHistoryModal(self.post), _on_done)

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        event.stop()
        href = event.href
        if href.startswith("relay:"):
            try:
                self.post_message(self.OpenLink(int(href.split(":", 1)[1])))
            except ValueError:
                pass
        else:
            self.app.open_url(href)


# ── PostDetailModal ───────────────────────────────────────────────────────────


class PostDetailModal(ModalScreen[None]):
    """A post over the feed: the list-only layout, or a narrow terminal."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("f", "follow_link", "Follow link"),
        Binding("h", "history", "History"),
        Binding("g", "outline", "Outline"),
        Binding("y", "copy", "Copy"),
        Binding("number_sign", "filter_tag", "Filter by tag"),
    ]

    DEFAULT_CSS = """
    PostDetailModal { align: center middle; background: $background; }
    PostDetailModal > Vertical {
        width: 92%;
        height: 92%;
        background: $background;
        border: round $accent;
        padding: 1 2;
    }
    PostDetailModal .detail-actions { height: 3; align: center middle; margin-top: 1; }
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

    def compose(self) -> ComposeResult:
        with Vertical():
            yield PostView()
            with Horizontal(classes="detail-actions"):
                yield Button("Close", id="close-btn", variant="default")

    def on_mount(self) -> None:
        self.query_one(PostView).show(self._post, self._link_index, self._link_titles)

    def action_follow_link(self) -> None:
        self.query_one(PostView).follow_link()

    def action_history(self) -> None:
        self.query_one(PostView).history()

    def action_outline(self) -> None:
        self.query_one(PostView).goto_heading()

    def action_copy(self) -> None:
        self.query_one(PostView).copy()

    def action_filter_tag(self) -> None:
        post = self.query_one(PostView).post
        if post is None:
            return
        self.dismiss()
        self.app.filter_by_tag_of(post)

    def on_post_view_open_link(self, event: PostView.OpenLink) -> None:
        event.stop()  # the app's own handler moves the reader pane
        self.app.open_post(event.post_id)

    def on_post_view_restored(self, event: PostView.Restored) -> None:
        event.stop()
        try:
            self.app._reload()
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-btn":
            self.dismiss()



class LinkPickerModal(ModalScreen[int | None]):
    """Keyboard link chooser: type to filter, ↑↓ to move, Enter to follow."""

    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    DEFAULT_CSS = """
    LinkPickerModal { align: center middle; background: $background; }
    LinkPickerModal > Vertical {
        width: 64; max-width: 90%; height: auto; max-height: 80%;
        background: $background; border: round $accent; padding: 1 2;
    }
    LinkPickerModal .picker-title { text-style: bold; color: $accent; width: 1fr; }
    LinkPickerModal Input { margin: 1 0; }
    LinkPickerModal OptionList {
        height: auto; max-height: 16; background: transparent; border: none;
    }
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

