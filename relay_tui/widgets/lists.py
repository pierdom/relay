"""A ListView whose Page Up/Down, Home and End move the selection — and the
view follows it — instead of scrolling the view past a cursor that stays put
(plain ListView binds only ↑ ↓ Enter, so page keys fell through to scrolling).
After qobit's PagedListView."""
from __future__ import annotations

from textual.binding import Binding
from textual.widgets import ListView


class PagedListView(ListView):
    BINDINGS = [
        Binding("pageup", "page_up", "Page up", show=False),
        Binding("pagedown", "page_down", "Page down", show=False),
        Binding("home", "cursor_first", "First", show=False),
        Binding("end", "cursor_last", "Last", show=False),
    ]

    def _page(self) -> int:
        """Rows that fit in the view, measured on the first row."""
        first = self.children[0] if self.children else None
        row_height = max(1, first.region.height) if first is not None else 1
        return max(1, self.size.height // row_height)

    def _move(self, index: int) -> None:
        n = len(self.children)
        if n:
            self.index = max(0, min(n - 1, index))

    def action_page_down(self) -> None:
        self._move((self.index if self.index is not None else -1) + self._page())

    def action_page_up(self) -> None:
        self._move((self.index if self.index is not None else 0) - self._page())

    def action_cursor_first(self) -> None:
        self._move(0)

    def action_cursor_last(self) -> None:
        self._move(len(self.children) - 1)
