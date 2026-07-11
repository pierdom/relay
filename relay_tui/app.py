from __future__ import annotations

import os
import sys

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Header
from textual import work

from . import api
from .sse import SSESubscriber
from .theme import ACCENT, BORDER, SCREEN_BG, TRANSPARENT, build_textual_theme, palette_name
from .widgets.modals import (
    AttachmentsModal,
    ComposeModal,
    ConfirmModal,
    EditModal,
    PostDetailModal,
    RenameTagModal,
    SearchModal,
    TagConfigModal,
)
from .widgets.post_panel import PostPanel
from .widgets.tag_panel import TagPanel


# ── terminal-transparency filter ──────────────────────────────────────────────
# Textual renders in truecolor, and its built-in ANSIToTruecolor line filter
# rewrites any "terminal default" background into a concrete RGB colour pulled
# from the ANSI terminal theme — which makes the canvas opaque and defeats the
# `ansi_default` backgrounds set when RELAY_TRANSPARENT is on.  This subclass
# leaves a default *background* untouched (so it is emitted as SGR 49 and the
# terminal's own background / transparency shows through) while still converting
# ANSI foreground colours to truecolor like the stock filter.
if TRANSPARENT:
    from functools import lru_cache

    from rich.color import Color as _RichColor, ColorType as _ColorType
    from rich.style import Style as _RichStyle
    from textual.filter import ANSIToTruecolor as _ANSIToTruecolor, NO_DIM, dim_color

    class _TransparentANSIToTruecolor(_ANSIToTruecolor):
        @lru_cache(1024)
        def truecolor_style(self, style: "_RichStyle", background: "_RichColor") -> "_RichStyle":
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


class RelayTuiApp(App):
    TITLE = "relay"
    CSS = f"""
    Screen {{ background: {SCREEN_BG}; layers: base overlay; }}
    Widget {{
        scrollbar-background: {SCREEN_BG};
        scrollbar-background-hover: {SCREEN_BG};
        scrollbar-background-active: {SCREEN_BG};
        scrollbar-color: {BORDER};
        scrollbar-color-hover: {ACCENT};
        scrollbar-corner-color: {SCREEN_BG};
    }}
    Header {{ background: {SCREEN_BG}; }}
    #main {{ height: 1fr; layout: horizontal; }}
    TagPanel {{ width: 26; border-right: solid $accent; }}
    PostPanel {{ width: 1fr; }}
    Footer {{ background: {SCREEN_BG}; }}
    FooterKey .footer-key--key {{ background: {BORDER}; color: {ACCENT}; }}
    FooterKey .footer-key--description {{ color: {ACCENT}; background: {SCREEN_BG}; }}
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("n", "compose_post", "New post"),
        Binding("e", "edit_post", "Edit"),
        Binding("d", "delete_post", "Delete"),
        Binding("slash", "search", "Search"),
        Binding("a", "attachments", "Attachments"),
        Binding("r", "reload", "Refresh"),
        Binding("tab", "focus_next_panel", "Switch panel", show=False),
        Binding("shift+tab", "focus_prev_panel", "Switch panel", show=False),
    ]

    def _refresh_truecolor_filter(self, theme) -> None:
        # Textual reinstalls a stock ANSIToTruecolor filter whenever the theme
        # changes.  In transparent mode, swap in our subclass that preserves the
        # terminal-default background instead.
        if not TRANSPARENT or self.native_ansi_color:
            return super()._refresh_truecolor_filter(theme)
        for index, flt in enumerate(self._filters):
            if isinstance(flt, _ANSIToTruecolor):
                self._filters[index] = _TransparentANSIToTruecolor(theme, enabled=True)
                return

    async def _shutdown(self) -> None:
        # Custom background colours (and the ansi_default canvas in transparent
        # mode) can leave stray SGR state on the terminal.  Close the driver so
        # its queued escape sequences flush, then write an explicit reset +
        # show-cursor + alt-screen-exit so the terminal is clean before exit.
        if TRANSPARENT:
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

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            yield TagPanel(id="tag-panel")
            yield PostPanel(id="post-panel")
        yield Footer()

    def on_mount(self) -> None:
        theme = build_textual_theme()
        self.register_theme(theme)
        self.theme = theme.name
        self.sub_title = palette_name()

        self._active_tag: str | None = None
        self._active_folder: str | None = None
        self._topics_mode = "tags"   # "tags" | "tree"
        self._search: str | None = None
        self._link_index: dict[str, int] = {}   # normalised title -> id
        self._link_titles: dict[int, str] = {}   # id -> title
        self._page_size = 50
        self._offset = 0
        self._total = 0
        self._loading_more = False
        self._sse = SSESubscriber(
            on_post=self._on_sse_post,
            on_connect=self._on_sse_connect,
            on_disconnect=self._on_sse_disconnect,
            on_delete=self._on_sse_delete,
        )
        self._sse.start()
        self._reload()

    def on_unmount(self) -> None:
        self._sse.stop()

    def _on_sse_connect(self) -> None:
        self.call_from_thread(self._set_live_status, True)

    def _on_sse_disconnect(self) -> None:
        self.call_from_thread(self._set_live_status, False)

    def _set_live_status(self, connected: bool) -> None:
        dot = f"[{ACCENT}]●[/]" if connected else "[dim]○[/]"
        status = "live" if connected else "offline"
        self.sub_title = f"{dot} {status}  [{palette_name()}]"

    def _on_sse_post(self, post_data: dict) -> None:
        try:
            post = api.Post.from_dict(post_data)
            self._sse.set_last_id(post.id)
            self.call_from_thread(self._prepend_post, post)
        except Exception:
            pass

    def _on_sse_delete(self, post_id: int) -> None:
        # External delete (e.g. a note removed in Obsidian) — drop it live.
        self.call_from_thread(self._remove_post_live, post_id)

    def _remove_post_live(self, post_id: int) -> None:
        try:
            self.query_one(PostPanel).remove_post(post_id)
            self._refresh_tags()
        except Exception:
            pass

    def _prepend_post(self, post: api.Post) -> None:
        try:
            panel = self.query_one(PostPanel)
            # A 'post' event is either new or an edit streamed in from outside
            # relay (e.g. an Obsidian save). If we already show it, update in
            # place; otherwise prepend it (respecting the active-tag filter).
            if panel.has_post(post.id):
                panel.update_post(post, flash=True)
            elif self._active_tag is None or self._active_tag in post.tags:
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
            )
            tags = api.list_tags()
            try:
                targets = api.link_targets()
                self._link_index = {t.strip().lower(): i for i, t in targets}
                self._link_titles = {i: t for i, t in targets}
            except Exception:
                pass
            if posts:
                self._sse.set_last_id(posts[0].id)  # newest real post (before pin)
            if pinned is not None:
                posts = [pinned, *posts]  # master document pinned on top
            self.call_from_thread(self._update_data, posts, total, tags)
        except Exception as e:
            self.call_from_thread(self.notify, f"Reload failed: {e}", severity="error")

    def _update_data(
        self, posts: list[api.Post], total: int, tags: list[api.Tag]
    ) -> None:
        self._total = total
        # the pinned master (#0) isn't part of the dated stream — don't count it
        # toward the offset or the next page skips a real post
        self._offset = sum(1 for p in posts if p.id != 0)
        self._loading_more = False
        self.query_one(PostPanel).set_posts(posts, search=self._search)
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
        self._reload()
        try:
            self.query_one(PostPanel).focus()
        except Exception:
            pass

    def on_tag_panel_folder_selected(self, event: TagPanel.FolderSelected) -> None:
        self._active_folder = event.folder
        self._active_tag = None
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
        self.push_screen(PostDetailModal(event.post, self._link_index, self._link_titles))

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

    def action_search(self) -> None:
        def _on_result(query: str | None) -> None:
            if query is None:
                return
            self._search = query or None
            self._reload()
        self.push_screen(SearchModal(current=self._search or ""), callback=_on_result)

    def action_attachments(self) -> None:
        self.push_screen(AttachmentsModal())

    def action_compose_post(self) -> None:
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
        self._sse.set_last_id(post.id)
        self.notify("Published", severity="information", timeout=3)
        self._refresh_tags()

    def action_edit_post(self) -> None:
        post = self.query_one(PostPanel).selected_post
        if post is None:
            self.notify("No post selected", severity="warning")
            return
        def _on_result(result: dict | None) -> None:
            if result:
                self._do_update_post(post.id, result)
        self.push_screen(EditModal(post), callback=_on_result)

    @work(thread=True)
    def _do_update_post(self, post_id: int, data: dict) -> None:
        try:
            post = api.update_post(
                post_id,
                content=data.get("content"),
                title=data.get("title"),
                tags=data.get("tags"),
                source=data.get("source"),
                expires_at=data["expires_at"],
            )
            self.call_from_thread(self._on_post_updated, post)
        except Exception as e:
            self.call_from_thread(self.notify, f"Update failed: {e}", severity="error")

    def _on_post_updated(self, post: api.Post) -> None:
        self.query_one(PostPanel).update_post(post)
        self.notify("Updated", severity="information", timeout=3)

    def action_delete_post(self) -> None:
        post = self.query_one(PostPanel).selected_post
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
        self.query_one(PostPanel).remove_post(post_id)
        self.notify("Deleted", severity="information", timeout=3)
        self._refresh_tags()

    def on_tag_panel_configure_tag(self, event: TagPanel.ConfigureTag) -> None:
        tag_name = event.tag_name
        def _on_result(result: dict | None) -> None:
            if result:
                self._do_set_tag_config(tag_name, result)
        self.push_screen(TagConfigModal(tag_name), callback=_on_result)

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
            )
            self.call_from_thread(self.notify, f"Config saved for [{tag}]", severity="information", timeout=3)
        except Exception as e:
            self.call_from_thread(self.notify, f"Config failed: {e}", severity="error")

    def action_focus_next_panel(self) -> None:
        focused = self.focused
        tag_lv = self.query_one("#tag-listview")
        post_lv = self.query_one("#post-listview")
        if focused is post_lv:
            tag_lv.focus()
        else:
            post_lv.focus()

    def action_focus_prev_panel(self) -> None:
        self.action_focus_next_panel()  # only 2 panels, same behavior


def main() -> None:
    RelayTuiApp().run()


if __name__ == "__main__":
    main()
