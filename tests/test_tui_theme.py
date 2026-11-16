"""The TUI's colours are theme variables: every palette is a Textual theme,
relay's own variables are computed per theme and checked for contrast, and the
theme can change while the TUI runs — and is remembered."""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from textual.color import Color

from relay_tui import app as tui_app
from relay_tui import theme
from relay_tui.widgets.post_panel import PostPanel
from relay_tui.widgets.tag_panel import TagPanel

from .test_tui_reader import WIDE, FakeApi, _settle

THEMES = theme.all_themes()


def _c(value: str) -> Color:
    return Color.parse(value)


@pytest.mark.parametrize("t", THEMES, ids=lambda t: t.name)
def test_every_palettes_text_reads_where_it_sits(t):
    """The browser's themes have tested floors; the TUI's now do too: muted and
    accent text on the page and on an unfocused highlight, and a focused
    selection's text on the selection, all at least WCAG AA."""
    if t.ansi:
        pytest.skip("ANSI themes take the terminal's own colours")
    v = theme.relay_variable_defaults(t)
    bg = _c(t.to_color_system().generate()["background"])
    for name in ("relay-muted", "relay-accent-text", "relay-great", "relay-bad", "relay-terrible"):
        for under in (bg, _c(v["relay-select-dim"])):
            assert theme.contrast(_c(v[name]), under) >= 4.5, (t.name, name)
    ratio = theme.contrast(_c(v["relay-select-text"]), _c(v["relay-select"]))
    assert ratio >= 4.5, (t.name, ratio)


def test_light_palettes_are_light_themes():
    by_name = {t.name: t for t in THEMES}
    assert not by_name["relay-light"].dark
    assert not by_name["relay-github-light"].dark
    assert by_name["relay-default"].dark


def test_the_starting_theme_is_explicit_then_remembered_then_default():
    available = {t.name for t in THEMES}
    assert theme.initial_theme("default", available) == "relay-default"
    theme.save_setting("theme", "relay-nord")
    assert theme.initial_theme("default", available) == "relay-nord"         # picked last time
    assert theme.initial_theme("dracula", available) == "relay-dracula"      # RELAY_PALETTE wins
    theme.save_setting("theme", "no-such-theme")
    assert theme.initial_theme("default", available) == "relay-default"


@pytest.fixture
def fake(monkeypatch) -> FakeApi:
    return FakeApi(monkeypatch)


@pytest.mark.asyncio
async def test_the_theme_changes_while_running_and_is_remembered(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        assert app.theme == "relay-default"
        before = app.theme_variables["relay-accent-text"]
        app.theme = "relay-light"
        await _settle(pilot)
        assert app.theme_variables["relay-accent-text"] != before
        assert "[light]" in app.sub_title
        assert theme.load_settings()["theme"] == "relay-light"
        app.theme = "textual-dark"   # a built-in theme gets relay's variables too
        await _settle(pilot)
        assert "relay-muted" in app.theme_variables


@pytest.mark.asyncio
async def test_panels_are_titled_with_their_state_in_the_frame(fake):
    app = tui_app.RelayTuiApp()
    async with app.run_test(size=WIDE) as pilot:
        await _settle(pilot)
        feed = app.query_one(PostPanel)
        assert str(feed.border_title) == "Feed"
        assert str(feed.border_subtitle) == "↓ updated · 3"
        assert str(app.query_one(TagPanel).border_title) == "Topics"
        app._active_tag = "notes"
        app._reload()
        await _settle(pilot)
        assert str(feed.border_title) == "Feed · #notes"


def test_no_colour_is_hard_coded_outside_the_theme():
    """Colour lives in theme variables only, as the browser's lives in tokens
    (tests/test_css_tokens.py): a literal can't follow a theme switch."""
    import re
    from pathlib import Path

    root = Path(theme.__file__).parent
    named = r"yellow|red|green|cyan|magenta|blue|white|grey\w*|bright_\w+"
    literal = re.compile(rf"#[0-9a-fA-F]{{6}}\b|\[(?:on )?(?:{named})\b")
    offenders = [
        f"{p.relative_to(root)}:{n}"
        for p in root.rglob("*.py") if p.name != "theme.py"
        for n, line in enumerate(p.read_text().splitlines(), 1) if literal.search(line)
    ]
    assert not offenders, offenders
