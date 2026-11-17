from __future__ import annotations

import os
import sys
from functools import lru_cache

from rich.color import Color as _RichColor
from rich.color import ColorType as _ColorType
from rich.style import Style as _RichStyle
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.filter import NO_DIM, dim_color
from textual.filter import ANSIToTruecolor as _ANSIToTruecolor
from textual.widgets import Footer, ListView

from . import api
from .sse import SSESubscriber
from .theme import all_themes, initial_theme, load_settings, relay_variable_defaults, save_setting, theme_for
from .widgets.lint import LintModal
from .widgets.menu import ActionMenu, MenuItem
from .widgets.modals import (
    AttachmentsModal,
    ConfirmModal,
    DeletedPostsModal,
    EditModal,
    RenameTagModal,
    SearchModal,
    TagConfigModal,
)
from .widgets.post_panel import PostPanel
from .widgets.post_view import PostDetailModal, PostView
from .widgets.reader import ReaderPane
from .widgets.status import StatusModal
from .widgets.switcher import SwitcherModal
from .widgets.tag_panel import TagPanel
from .widgets.topbar import Breadcrumb, Crumb, StatusMark, TopBar

# Below this many columns the three-column layout falls back to the list and
# the detail modal: the note column would be too narrow to read.
READER_MIN_WIDTH = 110


def _dated_stream_offset(posts: list[api.Post], pinned_id: int | None) -> int:
    """How many of ``posts`` are real dated/ranked-stream entries — i.e. not
    the pinned post (master doc, or a bare-id search hit) the server returns
    separately and the caller prepends. Undercounting this repeats a real post
    on the next page; overcounting it skips one. Pure so it's testable without
    a running app — see ``RelayTuiApp._update_data``, its one caller."""
    return sum(1 for p in posts if p.id != pinned_id)


# ── terminal-transparency filter ──────────────────────────────────────────────
# Textual renders in truecolor, and its built-in ANSIToTruecolor line filter
# rewrites any "terminal default" background into a concrete RGB colour pulled
# from the ANSI terminal theme — which makes the canvas opaque and defeats the
# `ansi_default` backgrounds set in transparent mode.  This subclass leaves a
# default *background* untouched (SGR 49) so the terminal's own background /
# transparency shows through, while still converting ANSI foreground colours to
# truecolor like the stock filter.
class _TransparentANSIToTruecolor(_ANSIToTruecolor):
    @lru_cache(1024)  # noqa: B019 — matches Textual's own cached override
    def truecolor_style(self, style: _RichStyle, background: _RichColor) -> _RichStyle:
        terminal_theme = self._terminal_theme
        changed = False

        color = style.color
        if color is not None and color.triplet is None:
            color = _RichColor.from_triplet(
                color.get_truecolor(terminal_theme, foreground=True)
            )
            changed = True

        bgcolor = style.bgcolor
        keep_default_bg = bgcolor is not None and bgcolor.type == _ColorType.DEFAULT
        if bgcolor is not None and bgcolor.triplet is None and not keep_default_bg:
            bgcolor = _RichColor.from_triplet(
                bgcolor.get_truecolor(terminal_theme, foreground=False)
            )
            changed = True

        if style.dim and color is not None:
            if bgcolor is not None and bgcolor.triplet is not None:
                dim_bg = bgcolor
            elif background.triplet is not None:
                dim_bg = background
            else:
                dim_bg = _RichColor.from_triplet(
                    _RichColor.default().get_truecolor(terminal_theme, foreground=False)
                )
            color = dim_color(dim_bg, color)
            style += NO_DIM
            changed = True

        return style + _RichStyle.from_color(color, bgcolor) if changed else style


class RelayTuiCommands(Provider):
    def _commands(self) -> list[tuple[str, object, str]]:
        app: RelayTuiApp = self.app  # type: ignore[assignment]
        return [
            (
                "Draw theme background",
                app.action_toggle_background,
                "Toggle between theme background and terminal transparency",
            ),
        ]

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for label, callback, help_text in self._commands():
            score = matcher.match(label)
            if score > 0:
                yield Hit(score, matcher.highlight(label), callback, help=help_text)

    async def discover(self) -> Hits:
        for label, callback, help_text in self._commands():
            yield Hit(0, label, callback, help=help_text)


class RelayTuiApp(App):
    TITLE = "relay"
    # Class-level: a Resize can arrive before on_mount has run.
    _reader_pref = True     # the three-column layout, when the terminal is wide enough
    _reader_active = False
    COMMANDS = App.COMMANDS | {RelayTuiCommands}
    CSS = """
    Screen { background: $background; layers: base overlay; }
    Screen.-transparent,
    Screen.-transparent * { background: ansi_default; }
    Screen.-transparent Widget {
        scrollbar-background: ansi_default;
        scrollbar-background-hover: ansi_default;
        scrollbar-background-active: ansi_default;
        scrollbar-corner-color: ansi_default;
    }
    /* One scrollbar everywhere: a thin thumb on no track. */
    * {
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
        scrollbar-background: $background 0%;
        scrollbar-background-hover: $background 0%;
        scrollbar-background-active: $background 0%;
        scrollbar-corner-color: $background 0%;
        scrollbar-color: $relay-frame;
        scrollbar-color-hover: $accent;
        scrollbar-color-active: $accent;
    }
    #main { height: 1fr; layout: horizontal; padding: 0 1; }
    /* Panels are framed, titled in the top border, with their state (sort,
       count, pending posts) in the bottom one. The focused one is lit. */
    .panel {
        border: round $relay-frame;
        border-title-color: $relay-muted;
        border-subtitle-color: $relay-muted;
        border-title-align: left;
        border-subtitle-align: right;
    }
    .panel:focus-within {
        border: round $accent;
        border-title-color: $relay-accent-text;
        border-title-style: bold;
        border-subtitle-color: $relay-readout;
    }
    TagPanel { width: 28; }
    PostPanel { width: 1fr; }
    ReaderPane { display: none; }
    #main.-reader PostPanel { width: 34%; min-width: 38; max-width: 58; }
    #main.-reader ReaderPane { display: block; width: 1fr; }
    /* Fills mean state only — the selection, a flashed arrival, a floating
       menu — so the page reads the same with the background on or off. */
    ListView { background: transparent; }
    /* A focused list's selection is a solid pair measured to read; an
       unfocused list's is a faint tint, so two lists don't both look chosen.
       Here, not in a widget's DEFAULT_CSS, which ListView's own rule outranks. */
    ListView > ListItem.-highlight { background: $relay-select-dim; text-style: none; }
    ListView:focus > ListItem.-highlight { background: $relay-select; text-style: none; }
    ListView:focus > ListItem.-highlight Label { color: $relay-select-text; }
    ListItem.-highlight > Label { background: transparent; }
    /* Feed cards keep their own colours (accent tags, the id badge): a tint
       measured for them, and a bar that is accent while the feed has focus. */
    #post-listview > PostItem.-highlight,
    #post-listview:focus > PostItem.-highlight {
        background: $relay-select-dim;
        border-left: outer $relay-frame;
        padding-left: 0;
    }
    #post-listview:focus > PostItem.-highlight { border-left: outer $accent; }
    #post-listview:focus > PostItem.-highlight Label { color: $foreground; }
    #post-listview:focus > PostItem.-highlight .pi-preview { color: $relay-muted; }
    /* Fields: a frame like the panels', lit while typing in it. */
    Input:focus, TextArea:focus { border: round $accent; }
    Input, TextArea { background: transparent; }
    Input > .input--placeholder, Input > .input--suggestion { color: $relay-muted; }
    /* One look for an empty or failed list. */
    .status-line { color: $relay-muted; text-style: italic; }
    Footer, Footer > * { background: transparent; }
    FooterKey .footer-key--key { color: $relay-readout; background: transparent; text-style: bold; }
    FooterKey .footer-key--description { color: $foreground; background: transparent; }
    """
    # The footer shows the few keys that apply to what's in front of you
    # (check_action hides the rest); `i` lists every action for it.
    BINDINGS = [
        Binding("n", "compose_post", "New"),
        Binding("e", "edit_post", "Edit"),
        Binding("d", "delete_post", "Delete"),
        Binding("f", "follow_link", "Links"),
        Binding("left_square_bracket", "pane_back", "Back"),
        Binding("u", "undo_delete", "Undo"),
        Binding("slash", "search", "Search"),
        Binding("ctrl+k", "switcher", "Go to"),
        Binding("i", "menu", "Menu"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "back", "Back", show=False),
        Binding("a", "attachments", "Attachments", show=False),
        Binding("v", "open_recovery", "Recovery", show=False),
        Binding("s", "cycle_sort", "Sort field", show=False),
        Binding("o", "cycle_order", "Sort order", show=False),
        Binding("r", "reload", "Refresh", show=False),
        Binding("l", "toggle_layout", "Layout", show=False),
        Binding("I", "status", "Status", show=False),
        Binding("L", "lint", "Lint", show=False),
        Binding("b", "filter_author", "By writer", show=False),
        Binding("j", "step(1)", "Next post", show=False),
        Binding("k", "step(-1)", "Previous post", show=False),
        Binding("h", "history", "History", show=False),
        Binding("g", "outline", "Outline", show=False),
        Binding("y", "copy", "Copy", show=False),
        Binding("number_sign", "filter_tag", "Filter by tag", show=False),
        Binding("right_square_bracket", "pane_forward", "Forward", show=False),
        Binding("tab", "focus_next_panel", "Switch panel", show=False),
        Binding("shift+tab", "focus_prev_panel", "Switch panel", show=False),
    ]

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """relay's own theme variables (theme.py), computed for whichever theme
        is current — a Textual built-in included — and checked for contrast."""
        return relay_variable_defaults(self.current_theme)

    def watch_theme(self, theme: str) -> None:
        # Remember a theme picked from Ctrl+P → Theme (not the one started with).
        if getattr(self, "_theme_ready", False):
            save_setting("theme", theme)
            self._render_subtitle()

    def _refresh_truecolor_filter(self, theme) -> None:
        if not getattr(self, "_transparent", False) or self.native_ansi_color:
            return super()._refresh_truecolor_filter(theme)
        for index, flt in enumerate(self._filters):
            if isinstance(flt, _ANSIToTruecolor):
                self._filters[index] = _TransparentANSIToTruecolor(theme, enabled=True)
                return

    async def _shutdown(self) -> None:
        # ansi_default canvas in transparent mode can leave stray SGR state.
        if getattr(self, "_transparent", False):
            if self._driver is not None:
                try:
                    self._driver.close()
                except Exception:
                    pass
            try:
                sys.stdout.write("\033[?25h\033[?1049l\033[0m")
                sys.stdout.flush()
            except Exception:
                pass
            os._exit(0)
        await super()._shutdown()

    def action_toggle_background(self) -> None:
        self._transparent = not getattr(self, "_transparent", False)
        for screen in self.screen_stack:
            screen.set_class(self._transparent, "-transparent")
        for index, flt in enumerate(self._filters):
            if isinstance(flt, (_ANSIToTruecolor, _TransparentANSIToTruecolor)):
                theme = flt._terminal_theme
                if self._transparent:
                    self._filters[index] = _TransparentANSIToTruecolor(theme, enabled=True)
                else:
                    self._filters[index] = _ANSIToTruecolor(theme, enabled=True)
                break
        self.refresh(layout=True)

    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        with Horizontal(id="main"):
            yield TagPanel(id="tag-panel")
            yield PostPanel(id="post-panel")
            yield ReaderPane(id="reader-pane")
        yield Footer()

    def on_mount(self) -> None:
        from relay.config import settings

        for theme in all_themes():
            self.register_theme(theme)
        if os.path.isabs(settings.relay_palette) and os.path.isfile(settings.relay_palette):
            self.register_theme(theme_for(settings.relay_palette))   # a palette file of your own
        self.theme = initial_theme(settings.relay_palette, set(self.available_themes))
        self._theme_ready = True

        self._connected = False
        self._scope_mode: str | None = None
        self._scope_tags: list[str] = []
        self._render_subtitle()

        self._transparent = False
        from relay.config import settings
        if settings.relay_transparent:
            self.action_toggle_background()

        self._active_tag: str | None = None
        self._active_folder: str | None = None
        self._topics_mode = "tags"   # "tags" | "tree"
        self._sort = "updated"       # "updated" | "created"
        self._order = "desc"         # "desc" | "asc"
        self._search: str | None = None
        self._search_mode = "keyword"   # hybrid once /status says semantic search is on
        self._author: str | None = None  # the third feed filter, beside tag and folder
        self._history_on = False         # deletes are undoable
        self._embeddings_on = False
        self._last_deleted: api.Post | None = None
        self._pending_ids: set[int] = set()   # live posts held back from a filtered feed
        self._link_index: dict[str, int] = {}   # normalised title -> id
        self._link_titles: dict[int, str] = {}   # id -> title
        self._page_size = 50
        self._offset = 0
        self._total = 0
        self._loading_more = False
        self._pane_reselect = False  # the next load opens its first post (a tag/folder change)
        self._pane_anchor: int | None = None  # the feed post the pane was opened from
        self._restore_post_id: int | None = None
        self._save_timer = None
        self._restore_session()
        self._sse = SSESubscriber(
            on_post=self._on_sse_post,
            on_connect=self._on_sse_connect,
            on_disconnect=self._on_sse_disconnect,
            on_delete=self._on_sse_delete,
        )
        self._sse.start()
        self._apply_layout()
        self._reload()
        if self._topics_mode == "tree":
            self._load_topics()
        # Start in the feed: it's what j/k, Enter and the menu act on.
        self.call_after_refresh(self.query_one(PostPanel).focus)
        self._load_status()

    def on_unmount(self) -> None:
        self._sse.stop()

    def _on_sse_connect(self) -> None:
        self.call_from_thread(self._set_live_status, True)

    def _on_sse_disconnect(self) -> None:
        self.call_from_thread(self._set_live_status, False)

    def _set_live_status(self, connected: bool) -> None:
        self._connected = connected
        self._render_subtitle()

    @work(thread=True)
    def _load_status(self) -> None:
        """Once at startup: this key's own scope (so a read-only or
        tag-restricted key shows in the header instead of surfacing as a
        rejected write), whether deletes are undoable, and whether semantic
        search is on."""
        try:
            status = api.get_status()
        except Exception as e:
            self.call_from_thread(self.notify, f"Could not read server status: {e}", severity="warning")
            return
        self.call_from_thread(self._set_status, status)

    def _set_status(self, status: api.ServerStatus) -> None:
        self._history_on = status.history
        self._embeddings_on = status.embeddings
        # Hybrid beats keyword alone on this relay's eval, as in the browser.
        self._search_mode = "hybrid" if status.embeddings else "keyword"
        self._set_caller_scope(status.caller.mode, status.caller.tags or [])

    def _set_caller_scope(self, mode: str, tags: list[str]) -> None:
        self._scope_mode = mode
        self._scope_tags = tags
        self._render_subtitle()

    def _scope_badge(self) -> str:
        """No badge for full access — the header has less room than the
        status panel's own "Access: Full access" row, and unrestricted is
        the common case, not something worth calling out every session."""
        if self._scope_mode is None or self._scope_mode == "full":
            return ""
        if self._scope_mode == "read":
            return "read-only"
        tags = ",".join(sorted(self._scope_tags)) or "(none)"
        return f"write:{tags}"

    def _render_subtitle(self) -> None:
        """The top bar's right end: live or offline, and the key's scope."""
        try:
            self.query_one(StatusMark).set_state(self._connected, self._scope_badge())
        except NoMatches:
            pass

    def _reject_if_read_only(self) -> bool:
        """True (and toasts) when the local key is read-only (relay #198,
        B-8) — short-circuits n/e/d before a write the server would just
        403 anyway. A tag-restricted ("write" + tags) key is deliberately
        NOT intercepted here — only the coarse read/write gate is checked
        client-side in the TUI; a write outside its allowed tags still
        reaches the server and fails there, surfaced by the existing
        `except Exception` toast each action's own worker already has."""
        if self._scope_mode == "read":
            self.notify("This key is read-only", severity="warning")
            return True
        return False

    def _on_sse_post(self, post_data: dict) -> None:
        try:
            post = api.Post.from_dict(post_data)
            # K-13: SSESubscriber._connect already sets _last_id from the
            # frame's own `id:` (a changes.seq) right after this callback
            # returns — calling set_last_id(post.id) here mixed a post's
            # database id into a cursor that's supposed to be a seq, and
            # since it's overwritten by the correct value moments later, it
            # never did anything but look like a real cursor update.
            self.call_from_thread(self._prepend_post, post)
        except Exception:
            pass

    def _on_sse_delete(self, post_id: int) -> None:
        # External delete (e.g. a note removed in Obsidian) — drop it live.
        self.call_from_thread(self._remove_post_live, post_id)

    def _remove_post_live(self, post_id: int) -> None:
        try:
            self.query_one(StatusMark).pulse()
            self._drop_post(post_id)
            self._refresh_tags()
        except Exception:
            pass

    def _drop_post(self, post_id: int) -> None:
        """Take a deleted post out of the feed; an open pane moves to its neighbour."""
        panel = self.query_one(PostPanel)
        neighbour = panel.neighbour(post_id)
        panel.remove_post(post_id)
        pane = self._pane()
        if not self._reader_active or pane.post is None or pane.post.id != post_id:
            return
        if neighbour is None:
            self._pane_anchor = None
            pane.set_empty()
            return
        self._pane_anchor = neighbour.id
        pane.show(neighbour)
        self.call_after_refresh(panel.select, neighbour.id)

    def _prepend_post(self, post: api.Post) -> None:
        try:
            self.query_one(StatusMark).pulse()
            panel = self.query_one(PostPanel)
            # A 'post' event is either new or an edit streamed in from outside
            # relay (e.g. an Obsidian save). If we already show it, update in
            # place; otherwise prepend it (respecting the active-tag filter).
            if self._reader_active:
                self._pane().refresh_post(post)
            if panel.has_post(post.id):
                panel.update_post(post, flash=True)
            elif self._active_tag is not None and self._active_tag not in post.tags:
                pass  # another tag's post: never this feed's
            elif self._plain_feed():
                panel.prepend_post(post)
            else:
                self._note_pending(post.id)
            # A streamed post may carry a brand-new tag or bump a count, so
            # refresh the sidebar regardless of the active-tag filter.
            self._refresh_tags()
        except Exception:
            pass

    def _plain_feed(self) -> bool:
        """Whether a new post belongs at the top of the feed as it stands: the
        default order, and no search, folder or writer filter it might not
        match. The stream can't tell which of those a post meets (the browser
        filters its stream by tag alone, too)."""
        return (self._sort == "updated" and self._order == "desc"
                and not (self._search or self._active_folder or self._author))

    def _note_pending(self, post_id: int) -> None:
        """A post arrived that this feed may or may not show: count it in the
        header (`r` reloads), as the browser's "new posts" pill does, rather
        than drop it into a list it might not belong to. Counted once per
        post: a create here is followed by its own echo on the stream, and an
        agent may save the same post several times."""
        self._pending_ids.add(post_id)
        self.query_one(PostPanel).set_pending(len(self._pending_ids))

    @work(thread=True)
    def _reload(self) -> None:
        try:
            posts, total, pinned = api.list_posts(
                tag=self._active_tag, folder=self._active_folder,
                search=self._search, limit=self._page_size,
                sort=self._sort, order=self._order,
                author=self._author, mode=self._search_mode,
            )
            tags = api.list_tags()
            try:
                targets = api.link_targets()
                self._link_index = {t.strip().lower(): i for i, t in targets}
                self._link_titles = dict(targets)
            except Exception:
                pass
            # K-13: this used to call self._sse.set_last_id(posts[0].id) here —
            # a post's database id is not a changes.seq, and stamping the SSE
            # cursor with one caused an over-wide (not lossy) reconnect replay
            # if a reconnect happened before the next real SSE frame's own
            # `id:` overwrote it. Leaving the cursor alone (whatever the last
            # real seq seen was, or None) is both simpler and type-correct;
            # this reload already has a fresh, complete snapshot of its own.
            pinned_id = pinned.id if pinned is not None else None
            if pinned is not None:
                posts = [pinned, *posts]  # master doc or a bare-id search hit, pinned on top
            self.call_from_thread(self._update_data, posts, total, tags, pinned_id)
        except Exception as e:
            self.call_from_thread(self.notify, f"Reload failed: {e}", severity="error")

    def _update_data(
        self, posts: list[api.Post], total: int, tags: list[api.Tag], pinned_id: int | None = None
    ) -> None:
        self._total = total
        self._offset = _dated_stream_offset(posts, pinned_id)
        self._loading_more = False
        arrow = "↓" if self._order == "desc" else "↑"
        self.query_one(PostPanel).set_posts(
            posts, search=self._search, pinned_id=pinned_id,
            label=self._feed_label(), empty=self._empty_message(),
            summary=f"{arrow} {self._sort} · {total}",
            group_by=None if self._search else self._sort,
        )
        self._save_feed()
        pane = self._pane()
        pane.link_index, pane.link_titles = self._link_index, self._link_titles
        self._pending_ids.clear()
        reselect, self._pane_reselect = self._pane_reselect, False
        if self._reader_active:
            self.call_after_refresh(self._sync_pane, reselect)
        if self._topics_mode == "tags":
            self.query_one(TagPanel).set_tags(tags, active=self._active_tag)
        self._update_trail()

    def on_post_panel_load_more(self, event: PostPanel.LoadMore) -> None:
        if self._loading_more or self._offset >= self._total:
            return
        self._loading_more = True
        self._load_more(self._offset)

    @work(thread=True)
    def _load_more(self, offset: int) -> None:
        try:
            posts, total, _ = api.list_posts(
                tag=self._active_tag,
                folder=self._active_folder,
                search=self._search,
                limit=self._page_size,
                offset=offset,
                sort=self._sort,
                order=self._order,
                author=self._author, mode=self._search_mode,
            )
            self.call_from_thread(self._append_page, posts, total)
        except Exception as e:
            self.call_from_thread(self.notify, f"Load failed: {e}", severity="error")
            self.call_from_thread(self._reset_loading_more)

    def _append_page(self, posts: list[api.Post], total: int) -> None:
        self._total = total
        self._offset += len(posts)
        self._loading_more = False
        self.query_one(PostPanel).append_posts(posts)

    def _reset_loading_more(self) -> None:
        self._loading_more = False

    @work(thread=True)
    def _refresh_tags(self) -> None:
        # Refresh whichever TOPICS view is active. A streamed edit can retag a post
        # (tag counts) or move it Inbox→domain (folder counts), so the Tree view
        # needs re-fetching too — not just Tags.
        try:
            panel = self.query_one(TagPanel)
            if self._topics_mode == "tree":
                folders = api.list_folders()
                self.call_from_thread(panel.set_folders, folders, self._active_folder)
            else:
                tags = api.list_tags()
                self.call_from_thread(panel.set_tags, tags, self._active_tag)
        except Exception:
            pass

    def on_tag_panel_tag_selected(self, event: TagPanel.TagSelected) -> None:
        self._active_tag = event.tag
        self._active_folder = None
        self._author = None
        self._pane_reselect = True
        self._reload()
        try:
            self.query_one(PostPanel).focus()
        except Exception:
            pass

    def on_tag_panel_folder_selected(self, event: TagPanel.FolderSelected) -> None:
        self._active_folder = event.folder
        self._active_tag = None
        self._author = None
        self._pane_reselect = True
        self._reload()
        try:
            self.query_one(PostPanel).focus()
        except Exception:
            pass

    def on_tag_panel_toggle_view(self, event: TagPanel.ToggleView) -> None:
        self._topics_mode = "tree" if self._topics_mode == "tags" else "tags"
        self._load_topics()

    @work(thread=True)
    def _load_topics(self) -> None:
        panel = self.query_one(TagPanel)
        try:
            if self._topics_mode == "tree":
                folders = api.list_folders()
                self.call_from_thread(panel.set_folders, folders, self._active_folder)
            else:
                tags = api.list_tags()
                self.call_from_thread(panel.set_tags, tags, self._active_tag)
        except Exception as e:
            self.call_from_thread(self.notify, f"Topics failed: {e}", severity="error")
            return
        self.call_from_thread(panel.focus)

    def on_post_panel_view_post(self, event: PostPanel.ViewPost) -> None:
        if self._reader_active:
            # The post is already in the pane: Enter moves there to read it.
            self._pane().view.query_one(VerticalScroll).focus()
            return
        self.push_screen(PostDetailModal(event.post, self._link_index, self._link_titles))

    # ── three-column layout ──────────────────────────────────────────────────

    def _pane(self) -> ReaderPane:
        return self.query_one(ReaderPane)

    def on_resize(self, event) -> None:
        self._apply_layout()

    def _apply_layout(self) -> None:
        active = self._reader_pref and self.size.width >= READER_MIN_WIDTH
        if active == self._reader_active:
            return
        try:
            main = self.query_one("#main")
        except NoMatches:
            return  # a Resize before the layout is composed; on_mount applies it
        self._reader_active = active
        main.set_class(active, "-reader")
        if active:
            self.call_after_refresh(self._sync_pane, False)

    def _sync_pane(self, reselect: bool) -> None:
        """After a load: a tag/folder change (or an empty pane) opens the first
        post; any other reload keeps the open one and re-selects it."""
        panel, pane = self.query_one(PostPanel), self._pane()
        if pane.editing:
            return
        restore, self._restore_post_id = self._restore_post_id, None
        if restore is not None:
            # The post open when the TUI last closed: selected in the feed if
            # it's there (which shows it), else fetched and shown on its own.
            if not panel.select(restore):
                self._restore_post(restore)
            return
        if not reselect and pane.post is not None:
            if not panel.select(self._pane_anchor):
                panel.select(pane.post.id)
            return
        post = panel.first_post() if reselect else (panel.selected_post or panel.first_post())
        if post is None:
            self._pane_anchor = None
            pane.set_empty()
            return
        self._pane_anchor = post.id
        pane.show(post)
        panel.select(post.id)

    def on_post_panel_highlighted(self, event: PostPanel.Highlighted) -> None:
        self.refresh_bindings()
        if not self._reader_active:
            return
        pane = self._pane()
        if pane.editing:
            return
        if event.post.id == self._pane_anchor and pane.post is not None:
            # The same feed post highlighted again (a reload, a live edit):
            # keep any link trail the reader followed from it.
            pane.refresh_post(event.post)
            return
        self._pane_anchor = event.post.id
        pane.show(event.post)

    def on_post_view_open_link(self, event: PostView.OpenLink) -> None:
        self._navigate_pane(event.post_id)

    @work(thread=True)
    def _navigate_pane(self, post_id: int) -> None:
        try:
            post = api.get_post(post_id)
        except Exception as e:
            self.call_from_thread(self.notify, f"Open failed: {e}", severity="error")
            return
        self.call_from_thread(self._pane().navigate, post)

    def on_post_view_restored(self, event: PostView.Restored) -> None:
        panel = self.query_one(PostPanel)
        if panel.has_post(event.post.id):
            panel.update_post(event.post)
        self._refresh_tags()

    def on_reader_pane_saved(self, event: ReaderPane.Saved) -> None:
        self._on_post_updated(event.post)

    def on_reader_pane_editor_closed(self, event: ReaderPane.EditorClosed) -> None:
        self.query_one(PostPanel).focus()

    # ── where you are: the trail, Escape, the footer, the menu ──────────────

    def _update_trail(self) -> None:
        """``All posts › #homelab › Docker Services › Home Network Map``: the
        feed, its filter, the post open in the reader and the links followed
        from it. Each crumb but the last goes back to where it names."""
        try:
            bar = self.query_one(Breadcrumb)
        except NoMatches:
            return
        pane = self._pane()
        filtered = self._feed_label()
        open_posts = (pane.trail + [pane.post]) if self._reader_active and pane.post is not None else []
        crumbs = [Crumb("All posts", self._clear_filters if filtered else None)]
        if filtered:
            crumbs.append(Crumb(filtered, (lambda: pane.back_to(0)) if len(open_posts) > 1 else None))
        for depth, post in enumerate(open_posts):
            last = depth == len(open_posts) - 1
            go = None if last else (lambda d=depth: pane.back_to(d))
            crumbs.append(Crumb(post.title or f"#{post.id}", go))
        bar.set_crumbs(crumbs)

    def on_reader_pane_moved(self, event: ReaderPane.Moved) -> None:
        self._update_trail()
        self.refresh_bindings()
        # Remembered for next time — once the reader settles, not per j/k step.
        if self._save_timer is not None:
            self._save_timer.stop()
        post = self._pane().post
        self._save_timer = self.set_timer(1.0, lambda: save_setting("open_post", post.id if post else None))

    # ── remembered between runs ──────────────────────────────────────────────

    def _restore_session(self) -> None:
        """Start where the last run ended: the layout, the feed's filter and
        order, and the open post. A search is not kept — it's a moment's
        question, not a place."""
        saved = load_settings()
        if saved.get("layout") in ("reader", "list"):
            self._reader_pref = saved["layout"] == "reader"
        feed = saved.get("feed") if isinstance(saved.get("feed"), dict) else {}
        self._active_tag = feed.get("tag") or None
        self._active_folder = feed.get("folder") or None
        self._author = feed.get("author") or None
        if feed.get("sort") in ("updated", "created"):
            self._sort = feed["sort"]
        if feed.get("order") in ("desc", "asc"):
            self._order = feed["order"]
        if self._active_folder:
            self._topics_mode = "tree"
        if isinstance(saved.get("open_post"), int):
            self._restore_post_id = saved["open_post"]

    def _save_feed(self) -> None:
        save_setting("feed", {
            "tag": self._active_tag, "folder": self._active_folder, "author": self._author,
            "sort": self._sort, "order": self._order,
        })

    @work(thread=True)
    def _restore_post(self, post_id: int) -> None:
        try:
            post = api.get_post(post_id)
        except Exception:
            self.call_from_thread(self._sync_pane, True)   # gone since: open the first post instead
            return
        self.call_from_thread(self._pane().show, post)

    def _clear_filters(self) -> None:
        self._active_tag = self._active_folder = self._author = self._search = None
        self._pane_reselect = True
        self._reload()

    def action_back(self) -> None:
        """Escape: back one level, always the same way — along the links
        followed in the reader, then out of the reader to the feed, then off
        the search, then off the filter. A dialog or the editor takes Escape
        for itself first."""
        pane = self._pane()
        focus = self.focused
        if self._reader_active and pane.trail and not pane.editing:
            pane.go_back()
        elif focus is not None and pane in focus.ancestors:
            self.query_one(PostPanel).focus()
        elif self._search:
            self._search = None
            self._reload()
        elif self._active_tag or self._active_folder or self._author:
            self._clear_filters()
        else:
            self.query_one(PostPanel).focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide (and disable) a key that does nothing here, so the footer lists
        only what applies — qobit's footer."""
        try:
            pane = self._pane()
        except NoMatches:
            return True
        if action in ("follow_link", "history", "outline", "copy"):
            return self._pane_ready()
        if action == "pane_back":
            return self._pane_ready() and bool(pane.trail)
        if action == "pane_forward":
            return self._pane_ready() and pane.can_go_forward
        if action in ("edit_post", "delete_post", "filter_tag", "filter_author", "open_browser"):
            return self._target_post() is not None
        if action == "undo_delete":
            return self._last_deleted is not None
        return True

    def action_open_browser(self) -> None:
        post = self._target_post()
        if post is not None:
            self.open_url(f"{api._base()}/id/{post.id}")

    def action_menu(self) -> None:
        if self._pane().editing:
            return
        title, sections = self._menu()

        def _run(action: str | None) -> None:
            if not action:
                return
            if action.startswith("tags:"):
                tags = self.query_one(TagPanel)
                name = action.split(":", 1)[1]
                if name == "select":
                    tags.query_one("#tag-listview", ListView).action_select_cursor()
                else:
                    self.call_later(tags.run_action, name)   # run_action is a coroutine
            else:
                self.call_later(self.run_action, action)

        self.push_screen(ActionMenu(title, sections), _run)

    def _menu(self) -> tuple[str, list[list[MenuItem]]]:
        """What the menu offers: actions for what's highlighted, then the
        vault-wide ones."""
        vault = [
            MenuItem("status", "●", "Status", "I"),
            MenuItem("lint", "⚑", "Vault lint", "L"),
            MenuItem("open_recovery", "↶", "Recover deleted posts", "v"),
            MenuItem("attachments", "▤", "Attachments", "a"),
            MenuItem("cycle_sort", "⇅", "Sort by created / updated", "s"),
            MenuItem("cycle_order", "↕", "Oldest / newest first", "o"),
            MenuItem("toggle_layout", "▥", "Three columns / list", "l"),
            MenuItem("reload", "↻", "Refresh", "r"),
        ]
        tag_list = self.query_one("#tag-listview", ListView)
        if self.focused is tag_list:
            item = tag_list.highlighted_child
            name = getattr(item, "tag_name", "")
            if self._topics_mode == "tags" and name:
                return f"#{name}", [[
                    MenuItem("tags:select", "▸", "Show its posts", "enter"),
                    MenuItem("tags:configure_tag", "◷", "Expiry and retention", "c"),
                    MenuItem("tags:rename_tag", "▸", "Rename", "R"),
                ], vault]
            return "Topics", [[MenuItem("tags:toggle_view", "⇄", "Tags / folders", "t")], vault]
        post = self._target_post()
        if post is None:
            return "relay", [vault]
        actions = [
            MenuItem("edit_post", "▸", "Edit", "e"),
            MenuItem("delete_post", "✕", "Delete", "d"),
        ]
        if self._pane_ready():
            actions += [
                MenuItem("follow_link", "→", "Follow a link", "f"),
                MenuItem("history", "↺", "History", "h"),
                MenuItem("outline", "≡", "Outline", "g"),
                MenuItem("copy", "⧉", "Copy", "y"),
            ]
        if post.tags:
            actions.append(MenuItem("filter_tag", "#", "Posts with its tag", "#"))
        if post.updated_by:
            actions.append(MenuItem("filter_author", "@", f"Posts by {post.updated_by}", "b"))
        actions.append(MenuItem("open_browser", "↗", "Open in the browser", ""))
        return post.title or f"#{post.id}", [actions, vault]

    def action_toggle_layout(self) -> None:
        if self._pane().editing:
            self.notify("Finish the edit first", severity="warning")
            return
        self._reader_pref = not self._reader_pref
        save_setting("layout", "reader" if self._reader_pref else "list")
        self._apply_layout()
        if self._reader_pref and not self._reader_active:
            self.notify(f"Three columns need a terminal {READER_MIN_WIDTH} columns wide", severity="warning")
        else:
            self.notify("Three columns" if self._reader_pref else "List", timeout=2)

    def action_step(self, delta: int) -> None:
        self.query_one(PostPanel).step(delta)

    def _pane_ready(self) -> bool:
        pane = self._pane()
        return self._reader_active and pane.post is not None and not pane.editing

    def action_follow_link(self) -> None:
        if self._pane_ready():
            self._pane().view.follow_link()

    def action_history(self) -> None:
        if self._pane_ready():
            self._pane().view.history()

    def action_outline(self) -> None:
        if self._pane_ready():
            self._pane().view.goto_heading()

    def action_copy(self) -> None:
        if self._pane_ready():
            self._pane().view.copy()

    def action_filter_tag(self) -> None:
        post = self._target_post()
        if post is not None:
            self.filter_by_tag_of(post)

    def filter_by_tag_of(self, post: api.Post) -> None:
        """Filter the feed by one of ``post``'s tags — the browser's tag pills
        in a post's header. One tag applies at once; several ask which."""
        if not post.tags:
            self.notify("This post has no tags", severity="warning", timeout=2)
            return
        if len(post.tags) == 1:
            self._filter_by_tag(post.tags[0])
            return
        from .widgets.picker import PickerModal

        self.push_screen(
            PickerModal("Filter by tag", [(f"#{t}", t) for t in post.tags]),
            lambda tag: self._filter_by_tag(tag) if tag else None,
        )

    def _filter_by_tag(self, tag: str) -> None:
        self._active_tag, self._active_folder, self._author = tag, None, None
        self._pane_reselect = True
        if self._topics_mode != "tags":
            self._topics_mode = "tags"   # so the sidebar shows the tag that is now active
        self._reload()

    def action_pane_back(self) -> None:
        if self._pane_ready():
            self._pane().go_back()

    def action_pane_forward(self) -> None:
        if self._pane_ready():
            self._pane().go_forward()

    def open_post(self, post_id: int) -> None:
        """Open a post by id (from a clicked wikilink) in a stacked detail modal."""
        self._open_post_worker(post_id)

    @work(thread=True)
    def _open_post_worker(self, post_id: int) -> None:
        try:
            post = api.get_post(post_id)
        except Exception as e:
            self.call_from_thread(self.notify, f"Open failed: {e}", severity="error")
            return
        self.call_from_thread(
            self.push_screen, PostDetailModal(post, self._link_index, self._link_titles)
        )

    async def action_reload(self) -> None:
        self._reload()
        self.notify("Refreshing…", severity="information", timeout=2)

    def action_cycle_sort(self) -> None:
        self._sort = "created" if self._sort == "updated" else "updated"
        self._reload()
        label = "update time" if self._sort == "updated" else "create time"
        self.notify(f"Sorting by {label}", severity="information", timeout=2)

    def action_cycle_order(self) -> None:
        self._order = "asc" if self._order == "desc" else "desc"
        self._reload()
        label = "oldest first" if self._order == "asc" else "newest first"
        self.notify(f"Order: {label}", severity="information", timeout=2)

    def action_search(self) -> None:
        def _on_result(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            query, mode = result
            self._search = query or None
            self._search_mode = mode
            self._reload()

        modal = SearchModal(current=self._search or "", mode=self._search_mode, semantic=self._embeddings_on)
        self.push_screen(modal, callback=_on_result)

    def _feed_label(self) -> str | None:
        """What the feed is showing, for its title: ``#homelab``, ``Homelab/``,
        ``by agent``, ``⌕ wireguard · hybrid``."""
        parts = []
        if self._active_tag:
            parts.append(f"#{self._active_tag}")
        if self._active_folder:
            parts.append(f"{self._active_folder}/")
        if self._author:
            parts.append(f"by {self._author}")
        if self._search:
            mode = f" · {self._search_mode}" if self._embeddings_on else ""
            parts.append(f"⌕ {self._search}{mode}")
        return "  ".join(parts) or None

    def _empty_message(self) -> str:
        if self._search:
            return f'No posts match "{self._search}"'
        if self._author:
            return f"No posts by {self._author}"
        return "No posts yet."

    def action_filter_author(self) -> None:
        """Show only the posts the open (or selected) post's writer last wrote;
        again on the same writer clears it — the browser's author chip."""
        post = self._target_post()
        writer = post.updated_by if post is not None else None
        if self._author is not None and (writer is None or writer == self._author):
            self._author = None
        elif writer is None:
            self.notify("This post has no recorded writer", severity="warning")
            return
        else:
            self._author = writer
            self._active_tag = self._active_folder = None
        self._pane_reselect = True
        self._reload()

    def action_switcher(self) -> None:
        if self._pane().editing or isinstance(self.screen, (EditModal, SwitcherModal)):
            return  # never over an editor, as in the browser

        def _on_pick(post_id: int | None) -> None:
            if post_id is not None:
                self._open_anywhere(post_id)

        self.push_screen(SwitcherModal(self._link_titles), _on_pick)

    def _open_anywhere(self, post_id: int) -> None:
        """Open a post picked from a panel: in the pane when it is showing and
        nothing covers it, else in a detail modal."""
        if self._reader_active and len(self.screen_stack) == 1:
            self._navigate_pane(post_id)
        else:
            self.open_post(post_id)

    def action_status(self) -> None:
        def _next(choice: int | str | None) -> None:
            if choice == "lint":
                self.action_lint()
            elif choice == "recovery":
                self.action_open_recovery()
            elif isinstance(choice, int):
                self._open_anywhere(choice)

        self.push_screen(StatusModal(), _next)

    def action_lint(self) -> None:
        def _next(post_id: int | None) -> None:
            if post_id is not None:
                self._open_anywhere(post_id)

        self.push_screen(LintModal(), _next)

    def action_attachments(self) -> None:
        self.push_screen(AttachmentsModal())

    def action_open_recovery(self) -> None:
        def _on_done(restored: api.Post | None) -> None:
            if restored is not None:
                self._on_post_restored(restored)
        self.push_screen(DeletedPostsModal(), _on_done)

    def _on_post_restored(self, post: api.Post) -> None:
        try:
            panel = self.query_one(PostPanel)
            if panel.has_post(post.id):
                panel.update_post(post)
            else:
                panel.prepend_post(post)
            self._refresh_tags()
            self.notify(f"Restored: {post.title}", severity="information", timeout=3)
        except Exception:
            pass

    def action_compose_post(self) -> None:
        if self._reject_if_read_only():
            return
        def _on_result(post: api.Post | None) -> None:
            if post is not None:
                self._on_post_created(post)

        self.push_screen(EditModal(None), callback=_on_result)

    def _on_post_created(self, post: api.Post) -> None:
        panel = self.query_one(PostPanel)
        if (self._active_tag is None or self._active_tag in post.tags) and self._plain_feed():
            panel.prepend_post(post)
            if self._reader_active:
                self._pane_anchor = post.id
                self._pane().show(post)
                self.call_after_refresh(panel.select, post.id)
        else:
            self._note_pending(post.id)
            if self._reader_active and not self._pane().editing:
                self._pane().navigate(post)   # still shown, without claiming a place in this feed
        # K-13: no self._sse.set_last_id(post.id) here — a post's database id
        # is not a changes.seq (see _reload's comment). The server broadcasts
        # this create over SSE like any other write, so the real seq arrives
        # via the live stream moments later and updates the cursor correctly.
        self.notify("Published", severity="information", timeout=3)
        self._refresh_tags()

    def action_edit_post(self) -> None:
        if self._reject_if_read_only():
            return
        post = self._target_post()
        if post is None:
            self.notify("No post selected", severity="warning")
            return
        if self._reader_active:
            self._pane().edit()
            return

        def _on_result(saved: api.Post | None) -> None:
            if saved is not None:
                self._on_post_updated(saved)

        self.push_screen(EditModal(post), callback=_on_result)

    def _target_post(self) -> api.Post | None:
        """The post e/d act on: the open one in the pane, else the selected one."""
        if self._reader_active:
            return self._pane().post
        return self.query_one(PostPanel).selected_post

    def _on_post_updated(self, post: api.Post) -> None:
        self.query_one(PostPanel).update_post(post)
        self.notify("Updated", severity="information", timeout=3)

    def action_delete_post(self) -> None:
        if self._reject_if_read_only():
            return
        post = self._target_post()
        if post is None:
            self.notify("No post selected", severity="warning")
            return
        if self._history_on:
            # Recoverable, so no question up front: the toast offers Undo.
            self._do_delete_post(post)
            return

        def _on_result(confirmed: bool) -> None:
            if confirmed:
                self._do_delete_post(post)

        name = post.title or f"#{post.id}"
        self.push_screen(
            ConfirmModal(f"Delete “{name}”? History is off, so this can’t be undone."), callback=_on_result
        )

    @work(thread=True)
    def _do_delete_post(self, post: api.Post) -> None:
        try:
            api.delete_post(post.id)
            self.call_from_thread(self._on_post_deleted, post.id, post)
        except Exception as e:
            self.call_from_thread(self.notify, f"Delete failed: {e}", severity="error")

    def _on_post_deleted(self, post_id: int, post: api.Post | None = None) -> None:
        self._drop_post(post_id)
        name = f"“{post.title}”" if post is not None and post.title else f"#{post_id}"
        if self._history_on and post is not None:
            self._last_deleted = post
            self.refresh_bindings()
            self.notify(f"Deleted {name} — u to undo", timeout=8)
        else:
            self.notify(f"Deleted {name}", timeout=3)
        self._refresh_tags()

    def action_undo_delete(self) -> None:
        post, self._last_deleted = self._last_deleted, None
        self.refresh_bindings()
        if post is None:
            self.notify("Nothing to undo", severity="warning", timeout=2)
            return
        self._undo_delete(post)

    @work(thread=True)
    def _undo_delete(self, post: api.Post) -> None:
        # The restorable revision is the one before the delete commit; the
        # recovery list already resolves it, so ask it rather than walk history.
        try:
            gone = next((d for d in api.list_deleted_posts(limit=100) if d.id == post.id), None)
            if gone is None:
                raise RuntimeError("it is no longer in the recovery list")
            restored = api.restore_post(post.id, gone.sha)
        except Exception as e:
            self.call_from_thread(self.notify, f"Couldn’t undo: {e}", severity="error")
            return
        self.call_from_thread(self._after_undo, restored)

    def _after_undo(self, restored: api.Post) -> None:
        self._reload()
        self.notify(f"Restored “{restored.title}”", timeout=3)

    def on_tag_panel_configure_tag(self, event: TagPanel.ConfigureTag) -> None:
        tag_name = event.tag_name
        def _on_result(result: dict | None) -> None:
            if result:
                self._do_set_tag_config(tag_name, result)
        self.push_screen(TagConfigModal(tag_name, event.current), callback=_on_result)

    def on_tag_panel_rename_tag(self, event: TagPanel.RenameTag) -> None:
        old_name = event.tag_name
        def _on_result(new_name: str | None) -> None:
            if new_name and new_name != old_name:
                self._do_rename_tag(old_name, new_name)
        self.push_screen(RenameTagModal(old_name), callback=_on_result)

    @work(thread=True)
    def _do_rename_tag(self, old: str, new: str) -> None:
        try:
            tags = api.rename_tag(old, new)
            if self._active_tag == old:
                self._active_tag = new
            self.call_from_thread(
                self.query_one(TagPanel).set_tags, tags, self._active_tag
            )
            self.call_from_thread(self._reload)
            self.call_from_thread(
                self.notify, f"Renamed [{old}] → [{new}]", severity="information", timeout=3
            )
        except Exception as e:
            self.call_from_thread(self.notify, f"Rename failed: {e}", severity="error")

    @work(thread=True)
    def _do_set_tag_config(self, tag: str, result: dict) -> None:
        try:
            api.set_tag_config(
                tag,
                ttl_hours=result.get("ttl_hours"),
                expires_at=result.get("expires_at"),
                keep_last=result.get("keep_last"),
            )
            self.call_from_thread(self.notify, f"Config saved for [{tag}]", severity="information", timeout=3)
        except Exception as e:
            self.call_from_thread(self.notify, f"Config failed: {e}", severity="error")

    def _panels(self) -> list:
        panels = [self.query_one("#tag-listview"), self.query_one("#post-listview")]
        if self._reader_active and self._pane().post is not None:
            panels.append(self._pane().view.query_one(VerticalScroll))
        return panels

    def _cycle_panels(self, delta: int) -> None:
        pane = self._pane()
        if pane.editing and self.focused is not None and pane in self.focused.ancestors:
            # Inside the pane's editor Tab moves between its fields.
            (self.screen.focus_next if delta > 0 else self.screen.focus_previous)()
            return
        panels = self._panels()
        current = next((i for i, w in enumerate(panels) if w is self.focused), 0)
        panels[(current + delta) % len(panels)].focus()

    def action_focus_next_panel(self) -> None:
        self._cycle_panels(1)

    def action_focus_prev_panel(self) -> None:
        self._cycle_panels(-1)


def main() -> None:
    RelayTuiApp().run()


if __name__ == "__main__":
    main()
