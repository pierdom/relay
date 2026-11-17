"""The top row: where you are on the left, the connection on the right.

The trail reads ``All posts › #homelab › Docker Services › Home Network Map``:
the feed, its filter, then the post open in the reader and any links followed
from it. Click a crumb to go back there; the last one is where you are. On the
right, ``● live · write:notes · ◢◤ relay`` — the dot pulses as live changes
arrive. After qobit's breadcrumb and wordmark.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from textual import events
from textual.app import ComposeResult
from textual.content import Content
from textual.widget import Widget

_SEP = " › "
_CRUMB_MAX = 28
_PULSE_SECS = 0.8


class Crumb(NamedTuple):
    label: str
    go: Callable[[], None] | None   # None: where you are, or not a place to go back to


def _short(text: str) -> str:
    return text if len(text) <= _CRUMB_MAX else text[: _CRUMB_MAX - 1] + "…"


class Breadcrumb(Widget):
    DEFAULT_CSS = """
    Breadcrumb { height: 1; width: 1fr; color: $relay-muted; text-wrap: nowrap; text-overflow: ellipsis; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._crumbs: list[Crumb] = []
        self._spans: list[tuple[int, int, Crumb]] = []

    def set_crumbs(self, crumbs: list[Crumb]) -> None:
        self._crumbs = crumbs
        self.refresh()

    @property
    def text(self) -> str:
        return _SEP.join(_short(c.label) for c in self._crumbs)

    def render(self) -> Content:
        parts: list[tuple[str, str] | str] = []
        self._spans = []
        x = 0
        for i, crumb in enumerate(self._crumbs):
            if i:
                parts.append((_SEP, "$relay-muted"))
                x += len(_SEP)
            label = _short(crumb.label)
            last = i == len(self._crumbs) - 1
            parts.append((label, "bold $relay-accent-text" if last else "$relay-muted"))
            self._spans.append((x, x + len(label), crumb))
            x += len(label)
        return Content.assemble(*parts)

    def on_click(self, event: events.Click) -> None:
        for start, end, crumb in self._spans:
            if start <= event.x < end and crumb.go is not None:
                event.stop()
                crumb.go()
                return


class StatusMark(Widget):
    DEFAULT_CSS = """
    StatusMark { height: 1; width: auto; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._live = False
        self._badge = ""
        self._pulsing = False
        self._timer = None

    def set_state(self, live: bool, badge: str = "") -> None:
        self._live, self._badge = live, badge
        self.refresh(layout=True)

    def pulse(self) -> None:
        """A live change arrived: light the dot for a moment."""
        self._pulsing = True
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(_PULSE_SECS, self._settle)
        self.refresh()

    def _settle(self) -> None:
        self._pulsing = False
        self.refresh()

    @property
    def text(self) -> str:
        parts = ["● live" if self._live else "○ offline"]
        if self._badge:
            parts.append(self._badge)
        return " · ".join(parts) + "   ◢◤ relay"

    def render(self) -> Content:
        dot_style = ("bold $relay-great" if self._pulsing else "$relay-accent-text") if self._live else "$relay-muted"
        parts: list[tuple[str, str] | str] = [
            ("●" if self._live else "○", dot_style),
            (" live" if self._live else " offline", "$relay-muted"),
        ]
        if self._badge:
            parts += [(" · ", "$relay-muted"), (self._badge, "$relay-bad")]
        parts += [("   ◢◤", "$relay-accent-text"), (" relay", "bold $relay-muted")]
        return Content.assemble(*parts)


class TopBar(Widget):
    DEFAULT_CSS = """
    TopBar { dock: top; height: 1; layout: horizontal; padding: 0 2; }
    """

    def compose(self) -> ComposeResult:
        yield Breadcrumb(id="tb-trail")
        yield StatusMark(id="tb-status")
