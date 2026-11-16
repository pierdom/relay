"""`i`: the server's status, as the browser's status panel shows it — health,
access, the vault, the server, a lint summary, recent activity and recovery.

It dismisses with what to do next: a post id to open (an activity row),
``"lint"`` or ``"recovery"`` — the app owns those screens.
"""
from __future__ import annotations

from rich.markup import escape
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from .. import api
from ..theme import ACCENT, BORDER, HEADER_BG, PERF_BAD, PERF_GREAT, PERF_TERRIBLE
from .post_panel import _time_ago

ACTIONS = {
    "create": "created", "update": "edited", "edit": "edited", "append": "appended to",
    "delete": "deleted", "restore": "restored", "tag_rename": "retagged",
    "external_edit": "edited outside relay", "external_delete": "deleted outside relay", "expiry": "expired",
}


def fmt_uptime(sec: int) -> str:
    if sec < 60:
        return f"{sec}s"
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def fmt_backfill(b: dict) -> str:
    if b.get("running"):
        return f"running — {b.get('checked', 0)}/{b.get('total', 0)} checked"
    if b.get("completed_at"):
        return f"{b.get('checked', 0)}/{b.get('total', 0)} checked, finished {b['completed_at']}"
    return "never run"


def _feature(label: str, state: str, note: str) -> str:
    dot = {"ok": f"[{PERF_GREAT}]●[/]", "warn": f"[{PERF_BAD}]●[/]", "bad": f"[{PERF_TERRIBLE}]●[/]"}.get(
        state, "[dim]○[/]"
    )
    return f"  {dot} {label:<18} [dim]{escape(note)}[/dim]"


def _rows(pairs: list[tuple[str, str]]) -> list[str]:
    return [f"  {k:<14} {escape(v)}" for k, v in pairs]


def _section(title: str) -> str:
    return f"\n[bold {ACCENT}]{title}[/]"


def render_status(d: dict) -> str:
    """The report above the activity list, as Rich markup — pure, so it tests
    without a server."""
    f = d.get("features", {})
    h = f.get("history", {})
    search = f.get("search", {})
    watcher = f.get("watcher", {})
    auth = f.get("auth", {})
    e = d.get("embeddings", {})
    lines = [_section("Health").lstrip("\n")]
    lines.append(_feature(
        "Vault history", "ok" if h.get("effective") else "bad",
        f"git {h.get('git')}" if h.get("effective") else ("disabled" if h.get("git") else "git missing"),
    ))
    lines.append(_feature(
        "Full-text search", "ok" if search.get("fts5") else "warn",
        "FTS5" if search.get("fts5") else "substring fallback",
    ))
    lines.append(_feature(
        "Semantic search",
        "ok" if e.get("available") else "warn" if e.get("enabled") else "off",
        "on — details below" if e.get("available") else "unavailable — sqlite-vec is not loaded" if e.get("enabled")
        else "off — search matches words only  (s turns it on)",
    ))
    lines.append(_feature(
        "External edits", "ok" if watcher.get("running") else "warn",
        "watching" if watcher.get("running") else ("not running" if watcher.get("enabled") else "disabled"),
    ))
    caller = d.get("caller")
    if caller:
        lines.append(_section("Access"))
        mode = caller.get("mode")
        if mode == "full":
            lines += _rows([("This session", "Full access")])
        elif mode == "read":
            lines += _rows([("This session", "Read-only")])
        else:
            tags = ", ".join(caller.get("tags") or []) or "(none — every write will be denied)"
            lines += _rows([("This session", "Write — restricted to tags"), ("Allowed tags", tags)])
    if e.get("enabled"):
        lines.append(_section("Semantic search"))
        lines += _rows([
            ("Model", str(e.get("model") or "—")),
            ("Backend", "loaded (resident)" if e.get("backend_loaded") else "unloaded"),
            ("Coverage", f"{e.get('posts_embedded', 0)} / {e.get('posts_total', 0)} posts"
                         f" ({e.get('posts_missing', 0)} missing)"),
            ("Backfill", fmt_backfill(e.get("backfill") or {})),
        ])
        lines.append("  [dim]s turns it off · B re-runs the backfill[/dim]")
    v = d.get("vault", {})
    lines.append(_section("Vault"))
    lines += _rows([
        ("Path", str(v.get("path", ""))),
        ("Posts", str(v.get("posts", 0))),
        ("Tags", str(v.get("tags", 0))),
        ("Folders", str(v.get("folders", 0))),
        ("Attachments", f"{v.get('attachments', 0)} ({fmt_bytes(v.get('attachment_bytes', 0))})"),
    ])
    lines.append(_section("Server"))
    lines += _rows([
        ("Version", str(d.get("version", ""))),
        ("Uptime", fmt_uptime(int(d.get("uptime_seconds", 0)))),
        ("Live clients", str(d.get("sse_clients", 0))),
        ("OIDC login", "enabled" if auth.get("oidc") else "off"),
        ("MCP OAuth", "enabled" if auth.get("mcp_oauth") else "off"),
    ])
    return "\n".join(lines)


def lint_line(report: dict) -> str:
    n, checked = len(report.get("items", [])), report.get("checked_posts", 0)
    text = (f"{n} issue{'s' if n != 1 else ''} across {checked} posts" if n
            else f"No issues — {checked} post{'s' if checked != 1 else ''} checked")
    skipped = len(report.get("skipped_rules", []))
    if skipped:
        text += f" ({skipped} rule{'s' if skipped != 1 else ''} skipped)"
    return text


class StatusModal(ModalScreen["int | str | None"]):
    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("L", "lint", "Lint"),
        Binding("v", "recovery", "Recovery"),
        Binding("s", "toggle_semantic", "Semantic search on/off"),
        Binding("B", "backfill", "Re-run backfill"),
    ]

    DEFAULT_CSS = f"""
    StatusModal {{ align: center middle; background: {HEADER_BG}; }}
    StatusModal > Vertical {{
        width: 84; max-width: 96%; height: 92%;
        background: {HEADER_BG}; border: solid {ACCENT}; padding: 1 2;
    }}
    StatusModal .st-title {{ color: {ACCENT}; text-style: bold; }}
    StatusModal VerticalScroll {{ height: 1fr; }}
    StatusModal #st-body {{ height: auto; }}
    StatusModal .st-section {{ color: {ACCENT}; text-style: bold; margin-top: 1; }}
    StatusModal #st-activity {{ height: auto; max-height: 14; background: transparent; border: none; }}
    StatusModal .st-hint {{ color: $text-muted; border-top: solid {BORDER}; }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._activity_ids: list[int | None] = []
        self._embeddings: dict = {}
        self._busy = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Status", classes="st-title")
            with VerticalScroll():
                yield Static("Loading…", id="st-body", markup=True)
                yield Label("Vault lint", classes="st-section")
                yield Static("Checking…", id="st-lint")
                yield Label("Recent activity", classes="st-section", id="st-activity-head")
                yield OptionList(id="st-activity")
                yield Label("Recovery", classes="st-section")
                yield Static("Checking…", id="st-recovery")
            yield Label("Enter opens a post  ·  L lint  ·  v recovery  ·  Esc closes", classes="st-hint")

    def on_mount(self) -> None:
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        try:
            report = api.get_status_report()
        except Exception as exc:
            msg = f"[{PERF_TERRIBLE}]Could not read status: {escape(str(exc))}[/]"
            self.app.call_from_thread(self._show_body, msg)
            return
        self._embeddings = report.get("embeddings") or {}
        self.app.call_from_thread(self._show_body, render_status(report))
        history = bool(report.get("features", {}).get("history", {}).get("effective"))
        try:
            lint = lint_line(api.get_lint())
        except Exception:
            lint = "Could not run the vault lint."
        self.app.call_from_thread(self._set, "#st-lint", lint + "  [dim](L to browse)[/dim]")
        if not history:
            self.app.call_from_thread(self._no_history)
            return
        try:
            changes = api.list_changes(12)
        except Exception:
            changes = None
        self.app.call_from_thread(self._show_activity, changes)
        try:
            deleted = [d for d in api.list_deleted_posts(limit=100, include_expiry=True) if d.reason != "expiry"]
            n = len(deleted)
            line = (f"{n} deleted post{'s' if n != 1 else ''} can be restored  [dim](v to browse)[/dim]" if n
                    else "Nothing deleted — or nothing left to recover.")
        except Exception:
            line = "Could not read deleted posts."
        self.app.call_from_thread(self._set, "#st-recovery", line)

    def _show_body(self, markup: str) -> None:
        self.query_one("#st-body", Static).update(markup)

    def _set(self, selector: str, markup: str) -> None:
        self.query_one(selector, Static).update(markup)

    def _no_history(self) -> None:
        self.query_one("#st-activity-head").display = False
        self.query_one("#st-activity").display = False
        self._set("#st-recovery", "Vault history is off — deleted posts cannot be recovered.")

    def _show_activity(self, changes: list[dict] | None) -> None:
        ol = self.query_one("#st-activity", OptionList)
        ol.clear_options()
        if changes is None:
            ol.add_option(Option("Could not read recent changes.", disabled=True))
            return
        if not changes:
            ol.add_option(Option("Nothing changed yet.", disabled=True))
            return
        titles = getattr(self.app, "_link_titles", {}) or {}
        self._activity_ids = []
        for c in changes:
            verb = ACTIONS.get(c.get("action", ""), c.get("action", ""))
            who = f" by {c['author']}" if c.get("author") else ""
            exists = c.get("id") in titles
            title = escape(c.get("title") or f"#{c.get('id')}")
            meta = f"{escape(verb + who)} · {_time_ago(c.get('when', ''))}"
            label = f"{title if exists else f'[dim]{title}[/dim]'}  [dim]{meta}[/dim]"
            ol.add_option(Option(label))
            self._activity_ids.append(c.get("id") if exists else None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        idx = event.option_index
        if 0 <= idx < len(self._activity_ids) and self._activity_ids[idx] is not None:
            self.dismiss(self._activity_ids[idx])

    def action_toggle_semantic(self) -> None:
        """Semantic search on or off at runtime (no restart), as the browser's
        Turn on/off. Needs full access; the server says so otherwise."""
        if not self._busy:
            self._busy = True
            self._embedding_action(lambda: api.set_embeddings_enabled(not self._embeddings.get("enabled")))

    def action_backfill(self) -> None:
        e = self._embeddings
        if not (e.get("enabled") and e.get("available")):
            self.app.notify("Turn semantic search on first", severity="warning", timeout=2)
            return
        if not self._busy:
            self._busy = True
            self._embedding_action(api.run_embedding_backfill)

    @work(thread=True)
    def _embedding_action(self, fn) -> None:
        try:
            fn()
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, f"Failed: {exc}", severity="error")
        else:
            # The health row, the coverage and the app's search modes all move.
            reload_status = getattr(self.app, "_load_status", None)
            if reload_status is not None:
                self.app.call_from_thread(reload_status)
        self._busy = False
        self.app.call_from_thread(self._load)

    def action_lint(self) -> None:
        self.dismiss("lint")

    def action_recovery(self) -> None:
        self.dismiss("recovery")
