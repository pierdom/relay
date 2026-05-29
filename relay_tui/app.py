from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Header
from textual import work

from . import api
from .sse import SSESubscriber
from .theme import ACCENT, BORDER, HEADER_BG, build_textual_theme, palette_name
from .widgets.modals import ComposeModal, ConfirmModal, EditModal, PostDetailModal
from .widgets.post_panel import PostPanel
from .widgets.tag_panel import TagPanel


class RelayTuiApp(App):
    TITLE = "relay"
    CSS = f"""
    Screen {{ background: $background; layers: base overlay; }}
    Widget {{
        scrollbar-background: {HEADER_BG};
        scrollbar-color: {BORDER};
        scrollbar-color-hover: {ACCENT};
        scrollbar-corner-color: {HEADER_BG};
    }}
    #main {{ height: 1fr; layout: horizontal; }}
    TagPanel {{ width: 26; border-right: solid $accent; }}
    PostPanel {{ width: 1fr; }}
    Footer {{ background: {HEADER_BG}; }}
    FooterKey .footer-key--key {{ background: {BORDER}; color: {ACCENT}; }}
    FooterKey .footer-key--description {{ color: {ACCENT}; background: {HEADER_BG}; }}
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("n", "compose_post", "New post"),
        Binding("e", "edit_post", "Edit"),
        Binding("d", "delete_post", "Delete"),
        Binding("r", "reload", "Refresh"),
        Binding("tab", "focus_next_panel", "Switch panel", show=False),
        Binding("shift+tab", "focus_prev_panel", "Switch panel", show=False),
    ]

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
        self._sse = SSESubscriber(
            on_post=self._on_sse_post,
            on_connect=self._on_sse_connect,
            on_disconnect=self._on_sse_disconnect,
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

    def _prepend_post(self, post: api.Post) -> None:
        try:
            if self._active_tag is None or self._active_tag in post.tags:
                self.query_one(PostPanel).prepend_post(post)
        except Exception:
            pass

    @work(thread=True)
    def _reload(self) -> None:
        try:
            posts, _ = api.list_posts(tag=self._active_tag)
            tags = api.list_tags()
            if posts:
                self._sse.set_last_id(posts[0].id)
            self.call_from_thread(self._update_data, posts, tags)
        except Exception as e:
            self.call_from_thread(self.notify, f"Reload failed: {e}", severity="error")

    def _update_data(self, posts: list[api.Post], tags: list[api.Tag]) -> None:
        self.query_one(PostPanel).set_posts(posts)
        self.query_one(TagPanel).set_tags(tags, active=self._active_tag)

    @work(thread=True)
    def _refresh_tags(self) -> None:
        try:
            tags = api.list_tags()
            self.call_from_thread(
                self.query_one(TagPanel).set_tags, tags, self._active_tag
            )
        except Exception:
            pass

    def on_tag_panel_tag_selected(self, event: TagPanel.TagSelected) -> None:
        self._active_tag = event.tag
        self._reload()
        try:
            self.query_one(PostPanel).focus()
        except Exception:
            pass

    def on_post_panel_view_post(self, event: PostPanel.ViewPost) -> None:
        self.push_screen(PostDetailModal(event.post))

    async def action_reload(self) -> None:
        self._reload()
        self.notify("Refreshing…", severity="information", timeout=2)

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
                title=data.get("title") or None,
                tags=data.get("tags", []),
                fmt=data.get("format", "markdown"),
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
                fmt=data.get("format"),
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
