"""The TUI's third catch-up: the status panel, the lint browser, palettes for
every browser theme (with light ones drawn light), and feed-card previews."""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

os.environ.setdefault("API_KEY", "test-key")

import pytest
from textual.widgets import OptionList

from relay_tui import api
from relay_tui import app as tui_app
from relay_tui.render import preview_line
from relay_tui.theme import is_dark
from relay_tui.widgets.lint import LintModal, rule_options
from relay_tui.widgets.status import StatusModal, fmt_uptime, lint_line, render_status

from .test_tui_reader import WIDE, FakeApi, _pane_title, _settle

ROOT = Path(__file__).resolve().parent.parent
PALETTES = ROOT / "relay_tui" / "palettes"
# Browser theme ids whose TUI palette goes by another name.
SAME_THEME = {"dark": "default", "solarized-dark": "solarized"}


# ── palettes ──────────────────────────────────────────────────────────────────


def test_every_browser_theme_has_a_tui_palette():
    web = set(re.findall(r"id: '([a-z0-9-]+)'", (ROOT / "relay/static/ui/js/theme.js").read_text()))
    tui = {p.stem for p in PALETTES.glob("*.toml")}
    missing = sorted(t for t in web if SAME_THEME.get(t, t) not in tui)
    assert not missing, f"no TUI palette for: {missing}"


@pytest.mark.parametrize("path", sorted(PALETTES.glob("*.toml")), ids=lambda p: p.stem)
def test_each_palette_is_complete(path):
    data = tomllib.loads(path.read_text())
    for key in ("primary", "border", "bg", "perf_great", "perf_bad", "perf_terrible"):
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", data.get(key, "")), f"{path.stem}: {key}"


def test_light_palettes_are_drawn_light():
    # A light palette under `dark=True` got Textual's light text on its light canvas.
    assert not is_dark("#edeae1")   # Relay Light
    assert not is_dark("#ffffff")
    assert not is_dark("#e6e9ef")   # Catppuccin Latte
    assert is_dark("#06080c")
    assert is_dark("#232a2e")       # Everforest Dark
    assert is_dark("ansi_default")


# ── cards ─────────────────────────────────────────────────────────────────────


def test_a_cards_preview_is_its_first_line_of_prose():
    assert preview_line("# Title\n\nFirst **real** line.\nSecond", "Title") == "First real line."
    assert preview_line("```\ncode\n```\n- [ ] a [[Note|task]] to do", "") == "a task to do"
    assert preview_line("| a | b |\n|---|---|\n\n> quoted [link](http://x) here", "") == "quoted link here"
    assert preview_line("# Only a title", "Only a title") == ""


# ── status ────────────────────────────────────────────────────────────────────

STATUS = {
    "version": "1.28.0", "uptime_seconds": 90061, "started_at": "", "sse_clients": 2,
    "vault": {"path": "/vault", "posts": 12, "tags": 5, "folders": 3, "attachments": 4, "attachment_bytes": 2048},
    "features": {"history": {"enabled": True, "git": "2.45", "effective": True},
                 "search": {"fts5": True, "embeddings": False},
                 "watcher": {"enabled": True, "running": False},
                 "auth": {"oidc": True, "mcp_oauth": False}},
    "embeddings": {"enabled": False, "available": False},
    "caller": {"mode": "write", "tags": ["notes"]},
}
LINT = {
    "items": [
        {"rule": "broken_link", "severity": "error", "post_id": 2, "title": "Beta",
         "detail": "links to [[Nowhere]]", "match": "[[Nowhere]]"},
        {"rule": "zero_tags", "severity": "warning", "post_id": 1, "title": "Alpha", "detail": "no tags"},
        {"rule": "something_new", "severity": "warning", "post_id": None, "title": None, "detail": "vault-wide"},
    ],
    "checked_posts": 3, "skipped_rules": ["zero_chunks"],
}


def test_the_status_report_reads_like_the_browsers():
    text = render_status(STATUS)
    assert "git 2.45" in text and "substring fallback" not in text
    assert "off — search matches words only" in text
    assert "not running" in text
    assert "Write — restricted to tags" in text and "notes" in text
    assert "2.0 KB" in text and "1d 1h" in text
    assert fmt_uptime(59) == "59s" and fmt_uptime(3700) == "1h 1m"


def test_the_lint_summary_and_filter():
    assert lint_line(LINT) == "3 issues across 3 posts (1 rule skipped)"
    assert lint_line({"items": [], "checked_posts": 1, "skipped_rules": []}) == "No issues — 1 post checked"
    assert [v for _, v in rule_options(LINT["items"])] == ["", "zero_tags", "broken_link", "something_new"]


@pytest.fixture
def fake(monkeypatch) -> FakeApi:
    f = FakeApi(monkeypatch)
    f.history = True
    monkeypatch.setattr(api, "get_status_report", lambda: STATUS)
    monkeypatch.setattr(api, "get_lint", lambda: LINT)
    monkeypatch.setattr(api, "list_changes", lambda limit=12: [
        {"seq": 9, "id": 3, "title": "Gamma", "action": "update", "when": "2026-11-16T10:00:00Z", "author": "agent"},
        {"seq": 8, "id": 77, "title": "Gone", "action": "delete", "when": "2026-11-16T09:00:00Z", "author": None},
    ])
    return f


@pytest.mark.asyncio
async def test_i_opens_the_status_panel_and_an_activity_row_opens_its_post(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("I")
        await _settle(pilot)
        assert isinstance(app.screen, StatusModal)
        activity = app.screen.query_one("#st-activity", OptionList)
        assert activity.option_count == 2
        activity.focus()
        activity.highlighted = 0
        await pilot.press("enter")
        await _settle(pilot)
        assert not isinstance(app.screen, StatusModal)
        assert _pane_title(app) == "Gamma"


@pytest.mark.asyncio
async def test_lint_browses_findings_and_opens_the_post(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        await pilot.press("I")
        await _settle(pilot)
        await pilot.press("L")
        await _settle(pilot)
        modal = app.screen
        assert isinstance(modal, LintModal)
        assert modal.query_one("#lm-list", OptionList).option_count == 3
        assert "Broken link" in str(modal.query_one("#lm-detail").render())
        await pilot.press("o")
        await _settle(pilot)
        assert not isinstance(app.screen, LintModal)
        assert _pane_title(app) == "Beta"
