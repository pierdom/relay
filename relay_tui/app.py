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
from textual.widgets import Footer, Header

from . import api
from .sse import SSESubscriber
from .theme import ACCENT, BORDER, HEADER_BG, build_textual_theme, palette_name
from .widgets.modals import (
    AttachmentsModal,
    ComposeModal,
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
from .widgets.tag_panel import TagPanel

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
    CSS = f"""
    Screen {{ background: {HEADER_BG}; layers: base overlay; }}
    Screen.-transparent,
    Screen.-transparent * {{ background: ansi_default; }}
    Screen.-transparent Widget {{
        scrollbar-background: ansi_default;
        scrollbar-background-hover: ansi_default;
        scrollbar-background-active: ansi_default;
        scrollbar-corner-color: ansi_default;
    }}
    Widget {{
        scrollbar-background: {HEADER_BG};
        scrollbar-background-hover: {HEADER_BG};
        scrollbar-background-active: {HEADER_BG};
        scrollbar-color: {BORDER};
        scrollbar-color-hover: {ACCENT};
        scrollbar-corner-color: {HEADER_BG};
    }}
    Header {{ background: {HEADER_BG}; }}
    #main {{ height: 1fr; layout: horizontal; }}
    TagPanel {{ width: 26; border-right: solid $accent; }}
    PostPanel {{ width: 1fr; }}
    ReaderPane {{ display: none; }}
    #main.-reader PostPanel {{ width: 34%; min-width: 36; max-width: 56; border-right: solid $accent; }}
    #main.-reader ReaderPane {{ display: block; width: 1fr; }}
    Footer {{ background: {HEADER_BG}; }}
    FooterKey .footer-key--key {{ background: {BORDER}; color: {ACCENT}; }}
    FooterKey .footer-key--description {{ color: {ACCENT}; background: {HEADER_BG}; }}
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("n", "compose_post", "New post"),
        Binding("e", "edit_post", "Edit"),
        Binding("d", "delete_post", "Delete"),
        Binding("slash", "search", "Search"),
        Binding("a", "attachments", "Attachments"),
        Binding("v", "open_recovery", "Recovery"),
        Binding("s", "cycle_sort", "Sort field"),
        Binding("o", "cycle_order", "Sort order"),
        Binding("r", "reload", "Refresh"),
        Binding("l", "toggle_layout", "Layout"),
        Binding("j", "step(1)", "Next post", show=False),
        Binding("k", "step(-1)", "Previous post", show=False),
        Binding("f", "follow_link", "Follow link", show=False),
        Binding("h", "history", "History", show=False),
        Binding("left_square_bracket", "pane_back", "Back", show=False),
        Binding("right_square_bracket", "pane_forward", "Forward", show=False),
        Binding("tab", "focus_next_panel", "Switch panel", show=False),
        Binding("shift+tab", "focus_prev_panel", "Switch panel", show=False),
    ]

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
        yield Header()
        with Horizontal(id="main"):
            yield TagPanel(id="tag-panel")
            yield PostPanel(id="post-panel")
            yield ReaderPane(id="reader-pane")
        yield Footer()

    def on_mount(self) -> None:
        theme = build_textual_theme()
        self.register_theme(theme)
        self.theme = theme.name

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
        self._link_index: dict[str, int] = {}   # normalised title -> id
        self._link_titles: dict[int, str] = {}   # id -> title
        self._page_size = 50
        self._offset = 0
        self._total = 0
        self._loading_more = False
        self._pane_reselect = False  # the next load opens its first post (a tag/folder change)
        self._pane_anchor: int | None = None  # the feed post the pane was opened from
        self._sse = SSESubscriber(
            on_post=self._on_sse_post,
            on_connect=self._on_sse_connect,
            on_disconnect=self._on_sse_disconnect,
            on_delete=self._on_sse_delete,
        )
        self._sse.start()
        self._apply_layout()
        self._reload()
        self._load_caller_scope()

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
    def _load_caller_scope(self) -> None:
        """Fetch this key's own effective scope (relay #198, B-8) once at
        startup, so a read-only/tag-restricted key shows up in the header
        instead of only being discovered from a rejected write."""
        try:
            scope = api.get_caller_scope()
            self.call_from_thread(self._set_caller_scope, scope.mode, scope.tags or [])
        except Exception as e:
            self.call_from_thread(self.notify, f"Could not check key scope: {e}", severity="warning")

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
        # Plain text: the Header prints its sub-title literally, markup and all.
        parts = ["● live" if self._connected else "○ offline"]
        badge = self._scope_badge()
        if badge:
            parts.append(badge)
        parts.append(f"[{palette_name()}]")
        self.sub_title = "  ".join(parts)

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
            panel = self.query_one(PostPanel)
            # A 'post' event is either new or an edit streamed in from outside
            # relay (e.g. an Obsidian save). If we already show it, update in
            # place; otherwise prepend it (respecting the active-tag filter).
            if self._reader_active:
                self._pane().refresh_post(post)
            if panel.has_post(post.id):
                panel.update_post(post, flash=True)
            elif (
                # A new post belongs at the top only under the default sort
                # (updated · desc). Under any other order, prepending would
                # misplace it — leave it for the next refresh (`r`) instead.
                self._sort == "updated" and self._order == "desc"
                and (self._active_tag is None or self._active_tag in post.tags)
            ):
                panel.prepend_post(post)
            # A streamed post may carry a brand-new tag or bump a count, so
            # refresh the sidebar regardless of the active-tag filter.
            self._refresh_tags()
        except Exception:
            pass

    @work(thread=True)
    def _reload(self) -> None:
        try:
            posts, total, pinned = api.list_posts(
                tag=self._active_tag, folder=self._active_folder,
                search=self._search, limit=self._page_size,
                sort=self._sort, order=self._order,
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
        self.query_one(PostPanel).set_posts(posts, search=self._search, pinned_id=pinned_id)
        pane = self._pane()
        pane.link_index, pane.link_titles = self._link_index, self._link_titles
        reselect, self._pane_reselect = self._pane_reselect, False
        if self._reader_active:
            self.call_after_refresh(self._sync_pane, reselect)
        if self._topics_mode == "tags":
            self.query_one(TagPanel).set_tags(tags, active=self._active_tag)

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
        self._pane_reselect = True
        self._reload()
        try:
            self.query_one(PostPanel).focus()
        except Exception:
            pass

    def on_tag_panel_folder_selected(self, event: TagPanel.FolderSelected) -> None:
        self._active_folder = event.folder
        self._active_tag = None
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

    def action_toggle_layout(self) -> None:
        if self._pane().editing:
            self.notify("Finish the edit first", severity="warning")
            return
        self._reader_pref = not self._reader_pref
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
        def _on_result(query: str | None) -> None:
            if query is None:
                return
            self._search = query or None
            self._reload()
        self.push_screen(SearchModal(current=self._search or ""), callback=_on_result)

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
        def _on_result(result: dict | None) -> None:
            if result:
                self._do_create_post(result)
        self.push_screen(ComposeModal(), callback=_on_result)

    @work(thread=True)
    def _do_create_post(self, data: dict) -> None:
        try:
            post = api.create_post(
                content=data["content"],
                title=data["title"],
                tags=data.get("tags", []),
                source=data.get("source"),
                expires_at=data.get("expires_at"),
            )
            self.call_from_thread(self._on_post_created, post)
        except Exception as e:
            self.call_from_thread(self.notify, f"Failed: {e}", severity="error")

    def _on_post_created(self, post: api.Post) -> None:
        if self._active_tag is None or self._active_tag in post.tags:
            self.query_one(PostPanel).prepend_post(post)
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
        def _on_result(confirmed: bool) -> None:
            if confirmed:
                self._do_delete_post(post.id)
        self.push_screen(ConfirmModal(f"Delete post #{post.id}?"), callback=_on_result)

    @work(thread=True)
    def _do_delete_post(self, post_id: int) -> None:
        try:
            api.delete_post(post_id)
            self.call_from_thread(self._on_post_deleted, post_id)
        except Exception as e:
            self.call_from_thread(self.notify, f"Delete failed: {e}", severity="error")

    def _on_post_deleted(self, post_id: int) -> None:
        self._drop_post(post_id)
        self.notify("Deleted", severity="information", timeout=3)
        self._refresh_tags()

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
