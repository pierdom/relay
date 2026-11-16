"""`i`: the actions for what's highlighted — a post, or a tag in Topics —
then the vault-wide ones, each with its glyph, its own key and a number.
↑ ↓ wrap, Enter or the number picks, Esc or `i` closes. Every single-key
action the footer leaves out is here, so none has to be remembered.
After qobit's context menu."""
from __future__ import annotations

import contextlib
from typing import NamedTuple

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView

MAX_ITEMS = 9   # the number keys


class MenuItem(NamedTuple):
    action: str   # what the caller runs: an app action, or "tags:<action>"
    icon: str     # one single-width glyph — never an emoji
    label: str
    key: str      # the action's own key, shown as a hint ("" for none)


class _WrapList(ListView):
    """↓ on the last row goes to the first, ↑ on the first to the last."""

    def action_cursor_down(self) -> None:
        if self.children and self.index == len(self.children) - 1:
            self.index = 0
        else:
            super().action_cursor_down()

    def action_cursor_up(self) -> None:
        if self.children and self.index in (0, None):
            self.index = len(self.children) - 1
        else:
            super().action_cursor_up()


class _Row(ListItem):
    def __init__(self, item: MenuItem, number: int | None, width: int) -> None:
        super().__init__()
        self.item = item
        self._number = number
        self._width = width

    def _content(self) -> Content:
        item = self.item
        number = str(self._number) if self._number is not None else " "
        if self.highlighted:   # the selection's own text colour reads; spans' wouldn't
            return Content(f"{item.icon}  {item.label:<{self._width}}  {item.key:>6}  {number}")
        return Content.assemble(
            (f"{item.icon}  ", "$relay-accent-text"),
            f"{item.label:<{self._width}}  ",
            (f"{item.key:>6}  ", "$relay-muted"),
            (number, "$relay-muted"),
        )

    def compose(self) -> ComposeResult:
        yield Label(self._content())

    def watch_highlighted(self, value: bool) -> None:
        super().watch_highlighted(value)
        with contextlib.suppress(NoMatches):
            self.query_one(Label).update(self._content())


class ActionMenu(ModalScreen["str | None"]):
    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("i", "close", "Close", show=False),
        *[Binding(str(n), f"pick({n})", show=False) for n in range(1, MAX_ITEMS + 1)],
    ]

    DEFAULT_CSS = """
    ActionMenu { align: center middle; }
    ActionMenu > Vertical {
        width: auto; height: auto; max-height: 90%;
        background: $background; border: round $accent; padding: 0 1;
        border-title-color: $relay-accent-text; border-title-style: bold;
    }
    ActionMenu ListView { width: 100%; height: auto; background: transparent; }
    ActionMenu .menu-rule { color: $relay-frame; width: 100%; }
    """

    def __init__(self, title: str, sections: list[list[MenuItem]]) -> None:
        super().__init__()
        self._title = title
        self._sections = [s for s in sections if s]
        items = [i for s in self._sections for i in s]
        self._width = max((len(i.label) for i in items), default=10)
        self._numbered = items[:MAX_ITEMS]

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = self._title
            # icon, label, key hint, number, the frame and its padding
            box.styles.width = max(self._width + 19, len(self._title) + 8)
            rows: list[ListItem] = []
            n = 0
            for i, section in enumerate(self._sections):
                if i:   # a rule between sections; the cursor passes over it
                    rows.append(ListItem(Label("─" * (self._width + 15), classes="menu-rule"), disabled=True))
                for item in section:
                    n += 1
                    rows.append(_Row(item, n if n <= MAX_ITEMS else None, self._width))
            yield _WrapList(*rows, initial_index=0)

    def on_mount(self) -> None:
        self.query_one(_WrapList).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, _Row):
            self.dismiss(event.item.item.action)

    def action_pick(self, n: int) -> None:
        if 1 <= n <= len(self._numbered):
            self.dismiss(self._numbered[n - 1].action)

    def action_close(self) -> None:
        self.dismiss(None)
