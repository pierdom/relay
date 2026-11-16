"""A small keyboard chooser: type to filter, ↑↓ to move, Enter picks, Esc
cancels. Dismisses with the chosen item's value. Headings, tags and copy
targets go through it."""
from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList
from textual.widgets.option_list import Option


class PickerModal(ModalScreen[Any]):
    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    DEFAULT_CSS = """
    PickerModal { align: center middle; background: $background; }
    PickerModal > Vertical {
        width: 72; max-width: 92%; height: auto; max-height: 80%;
        background: $background; border: round $accent; padding: 1 2;
    }
    PickerModal .pk-title { color: $accent; text-style: bold; }
    PickerModal Input { margin: 1 0; border: round $relay-frame; }
    PickerModal OptionList { height: auto; max-height: 16; background: transparent; border: none; }
    """

    def __init__(self, title: str, items: list[tuple[str, Any]]) -> None:
        super().__init__()
        self._title = title
        self._items = items   # (plain label, value)
        self._shown: list[Any] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(escape(self._title), classes="pk-title")
            yield Input(placeholder="filter…", id="pk-filter")
            yield OptionList(id="pk-list")

    def on_mount(self) -> None:
        self._fill("")
        self.query_one("#pk-filter", Input).focus()

    def _fill(self, text: str) -> None:
        q = text.strip().lower()
        hits = [(label, value) for label, value in self._items if q in label.lower()]
        self._shown = [v for _, v in hits]
        ol = self.query_one("#pk-list", OptionList)
        ol.clear_options()
        ol.add_options([Option(escape(label)) for label, _ in hits])
        ol.highlighted = 0 if hits else None

    def on_input_changed(self, event: Input.Changed) -> None:
        self._fill(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._pick(self.query_one("#pk-list", OptionList).highlighted)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._pick(event.option_index)

    def _pick(self, index: int | None) -> None:
        if index is not None and 0 <= index < len(self._shown):
            self.dismiss(self._shown[index])

    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "down") and self._shown:
            ol = self.query_one("#pk-list", OptionList)
            cur = ol.highlighted or 0
            ol.highlighted = max(0, min(len(self._shown) - 1, cur + (1 if event.key == "down" else -1)))
            event.stop()
