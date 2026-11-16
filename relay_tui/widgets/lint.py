"""`L` (or from the status panel): GET /lint as a two-pane browser — the
findings on the left, the finding and its post on the right, as in the
browser's lint modal. ``e`` fixes the post in the editor and runs the check
again; ``o`` leaves to read the post (dismisses with its id)."""
from __future__ import annotations

from rich.markup import escape
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from .. import api
from ..theme import ACCENT, BORDER, HEADER_BG, PERF_BAD, PERF_TERRIBLE
from .post_view import PostView

# Fixed, readable order — not by count, so the filter doesn't reshuffle as the
# vault changes. The browser's lint.js RULE_LABELS.
RULE_LABELS = {
    "zero_tags": "Zero tags",
    "missing_domain_tag": "Missing domain tag",
    "missing_type_tag": "Missing type tag",
    "stale_inbox": "Stuck in Inbox",
    "h1_missing": "H1 missing",
    "h1_title_mismatch": "H1 / title mismatch",
    "broken_link": "Broken link",
    "link_to_deleted_post": "Links to a deleted post",
    "broken_attachment_embed": "Broken attachment embed",
    "wikilink_to_filename": "Wikilink to a filename",
    "master_doc_post_count": "#0 post count is stale",
    "stale_last_updated": "Stale hub/plan",
    "zero_backlinks": "Zero backlinks",
    "zero_chunks": "Zero embedding chunks",
    "empty_tag_config": "Unused tag config",
}


def rule_label(rule: str) -> str:
    return RULE_LABELS.get(rule, rule.replace("_", " ").capitalize())


def rule_options(items: list[dict]) -> list[tuple[str, str]]:
    """The filter's choices: every rule with findings, in the fixed order, then
    any rule this client doesn't know yet."""
    counts: dict[str, int] = {}
    for f in items:
        counts[f["rule"]] = counts.get(f["rule"], 0) + 1
    order = [r for r in RULE_LABELS if r in counts] + sorted(r for r in counts if r not in RULE_LABELS)
    return [(f"All rules ({len(items)})", "")] + [(f"{rule_label(r)} ({counts[r]})", r) for r in order]


class LintModal(ModalScreen["int | None"]):
    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("e", "edit", "Edit post"),
        Binding("o", "open", "Open post"),
        Binding("r", "rerun", "Check again"),
    ]

    DEFAULT_CSS = f"""
    LintModal {{ align: center middle; background: {HEADER_BG}; }}
    LintModal > Vertical {{
        width: 96%; height: 94%; background: {HEADER_BG}; border: solid {ACCENT}; padding: 1 2;
    }}
    LintModal .lm-title {{ color: {ACCENT}; text-style: bold; }}
    LintModal .lm-panes {{ height: 1fr; }}
    LintModal .lm-left {{ width: 42%; min-width: 34; border-right: solid {BORDER}; padding-right: 1; }}
    LintModal .lm-right {{ width: 1fr; padding-left: 2; }}
    LintModal Select {{ margin-bottom: 1; }}
    LintModal #lm-list {{ height: 1fr; background: transparent; border: none; }}
    LintModal #lm-detail {{ height: auto; margin-bottom: 1; }}
    LintModal .lm-empty {{ color: $text-muted; text-style: italic; }}
    LintModal .lm-hint {{ color: $text-muted; border-top: solid {BORDER}; }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._items: list[dict] = []
        self._shown: list[dict] = []
        self._rule = ""
        self._post: api.Post | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Vault lint", classes="lm-title", id="lm-title")
            with Horizontal(classes="lm-panes"):
                with Vertical(classes="lm-left"):
                    yield Select([("All rules", "")], value="", allow_blank=False, id="lm-rule")
                    yield OptionList(id="lm-list")
                with Vertical(classes="lm-right"):
                    yield Static("Checking the vault…", id="lm-detail", markup=True)
                    yield PostView(id="lm-post")
            yield Label("e edit the post  ·  o open it  ·  r check again  ·  Esc closes", classes="lm-hint")

    def on_mount(self) -> None:
        self.query_one("#lm-post").display = False
        self._run()

    @work(thread=True, exclusive=True, group="lint")
    def _run(self) -> None:
        try:
            report = api.get_lint()
        except Exception as exc:
            msg = f"[{PERF_TERRIBLE}]Could not run the vault lint: {escape(str(exc))}[/]"
            self.app.call_from_thread(self._detail, msg)
            return
        self.app.call_from_thread(self._show_report, report)

    def _detail(self, markup: str) -> None:
        self.query_one("#lm-detail", Static).update(markup)

    def _show_report(self, report: dict) -> None:
        self._items = report.get("items", [])
        n, checked = len(self._items), report.get("checked_posts", 0)
        title = f"Vault lint  [dim]{n} issue{'s' if n != 1 else ''} across {checked} posts"
        if report.get("skipped_rules"):
            title += f" · skipped: {escape(', '.join(report['skipped_rules']))}"
        self.query_one("#lm-title", Label).update(title + "[/dim]")
        select = self.query_one("#lm-rule", Select)
        options = rule_options(self._items)
        if self._rule not in {v for _, v in options}:
            self._rule = ""
        select.set_options(options)
        select.value = self._rule
        self._fill()

    def on_select_changed(self, event: Select.Changed) -> None:
        self._rule = str(event.value or "")
        self._fill()

    def _fill(self) -> None:
        self._shown = [f for f in self._items if not self._rule or f["rule"] == self._rule]
        ol = self.query_one("#lm-list", OptionList)
        ol.clear_options()
        for f in self._shown:
            colour = PERF_TERRIBLE if f.get("severity") == "error" else PERF_BAD
            where = escape(f.get("title") or ("vault" if f.get("post_id") is None else f"#{f['post_id']}"))
            ol.add_option(Option(f"[{colour}]●[/] {where}\n  [dim]{escape(rule_label(f['rule']))}[/dim]"))
        if self._shown:
            ol.highlighted = 0
            ol.focus()
        else:
            self._post = None
            self.query_one("#lm-post").display = False
            self._detail("[dim]No issues — nothing to fix.[/dim]")

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        idx = event.option_index
        if not 0 <= idx < len(self._shown):
            return
        f = self._shown[idx]
        lines = [f"[bold]{escape(rule_label(f['rule']))}[/]  [dim]{escape(f.get('severity', ''))}[/dim]",
                 escape(f.get("detail", ""))]
        if f.get("match"):
            lines.append(f"[{ACCENT}]{escape(f['match'])}[/]")
        self._detail("\n".join(lines))
        self._post = None
        self.query_one("#lm-post").display = False
        if f.get("post_id") is not None:
            self._load_post(f["post_id"])

    @work(thread=True, exclusive=True, group="lint-post")
    def _load_post(self, post_id: int) -> None:
        try:
            post = api.get_post(post_id)
        except Exception:
            return
        self.app.call_from_thread(self._show_post, post)

    def _show_post(self, post: api.Post) -> None:
        self._post = post
        view = self.query_one("#lm-post", PostView)
        view.display = True
        view.show(post, getattr(self.app, "_link_index", {}), getattr(self.app, "_link_titles", {}))

    def on_post_view_open_link(self, event: PostView.OpenLink) -> None:
        event.stop()
        self.dismiss(event.post_id)

    def action_open(self) -> None:
        if self._post is not None:
            self.dismiss(self._post.id)

    def action_rerun(self) -> None:
        self._detail("Checking the vault…")
        self._run()

    def action_edit(self) -> None:
        if self._post is None:
            return
        from .modals import EditModal

        def _saved(post: api.Post | None) -> None:
            if post is not None:
                updated = getattr(self.app, "_on_post_updated", None)
                if updated is not None:
                    updated(post)   # the feed and pane behind this modal
                self.action_rerun()

        self.app.push_screen(EditModal(self._post), _saved)
