"""Ctrl+K: jump to any post by title or ``#id`` — the browser's quick switcher.

Reads the link index the app already holds, so typing costs no request.
"""
from __future__ import annotations

from rich.markup import escape
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList
from textual.widgets.option_list import Option

from ..completion import match_posts
from ..theme import ACCENT, BORDER, HEADER_BG

_LIMIT = 12


class SwitcherModal(ModalScreen["int | None"]):
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    DEFAULT_CSS = f"""
    SwitcherModal {{ align: center top; background: {HEADER_BG} 60%; }}
    SwitcherModal > Vertical {{
        width: 72; max-width: 92%; height: auto; margin-top: 3;
        background: {HEADER_BG}; border: solid {ACCENT}; padding: 1 2;
    }}
    SwitcherModal .sw-title {{ color: {ACCENT}; text-style: bold; }}
    SwitcherModal Input {{ margin: 1 0; border: solid {BORDER}; }}
    SwitcherModal OptionList {{ height: auto; max-height: 14; background: transparent; border: none; }}
    SwitcherModal .sw-empty {{ color: $text-muted; text-style: italic; display: none; }}
    SwitcherModal.-none .sw-empty {{ display: block; }}
    """

    def __init__(self, titles: dict[int, str]) -> None:
        super().__init__()
        self._titles = titles
        self._ids: list[int] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Go to a post", classes="sw-title")
            yield Input(placeholder="title or #id…", id="sw-input")
            yield OptionList(id="sw-list")
            yield Label("No post matches", classes="sw-empty")

    def on_mount(self) -> None:
        self._fill("")
        self.query_one("#sw-input", Input).focus()

    def _fill(self, text: str) -> None:
        hits = match_posts(self._titles, text, _LIMIT)
        self._ids = [pid for pid, _ in hits]
        ol = self.query_one("#sw-list", OptionList)
        ol.clear_options()
        ol.add_options([Option(f"{escape(title)}  [dim]#{pid}[/dim]") for pid, title in hits])
        ol.highlighted = 0 if hits else None
        self.set_class(not hits, "-none")

    def on_input_changed(self, event: Input.Changed) -> None:
        self._fill(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._pick(self.query_one("#sw-list", OptionList).highlighted)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._pick(event.option_index)

    def _pick(self, index: int | None) -> None:
        if index is not None and 0 <= index < len(self._ids):
            self.dismiss(self._ids[index])

    def on_key(self, event: events.Key) -> None:
        # Arrows move the list while the cursor stays in the filter.
        if event.key in ("up", "down") and self._ids:
            ol = self.query_one("#sw-list", OptionList)
            cur = ol.highlighted or 0
            ol.highlighted = max(0, min(len(self._ids) - 1, cur + (1 if event.key == "down" else -1)))
            event.stop()
