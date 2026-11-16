"""Matching for the quick switcher and the editor's completion — the same
rules as the browser's (`links.matchPosts`, `editor.js`), kept pure so they
test without a running app."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from textual.suggester import Suggester


def match_posts(titles: dict[int, str], text: str, limit: int = 8) -> list[tuple[int, str]]:
    """Up to ``limit`` (id, title) pairs matching ``text``, best first: an exact
    id, then title prefixes, then titles containing it; ties alphabetical."""
    q = text.strip().lower().removeprefix("#")
    ordered = sorted(titles.items(), key=lambda item: item[1].lower())
    if not q:
        return ordered[:limit]

    def rank(item: tuple[int, str]) -> int:
        pid, title = item
        t = title.strip().lower()
        return 0 if str(pid) == q else 1 if t.startswith(q) else 2 if q in t else 3

    ranked = [(rank(i), i) for i in ordered]
    return [i for r, i in sorted((x for x in ranked if x[0] < 3), key=lambda x: x[0])][:limit]


@dataclass
class Span:
    """What is being typed: replace ``line[start:end]`` with the accepted text."""

    kind: str     # "wiki" | "id"
    start: int
    end: int
    query: str
    pick: bool    # False: nothing preselected, Enter stays a new line


_WIKI_OPEN = re.compile(r"\[\[([^\][|#\n]*)$")
# Not a heading (`# `), and only at a word start: `a#1` is no reference.
_ID_REF = re.compile(r"(?:^|[\s(\[])#([^\s#\[\]()]{1,40})$")


def link_span(before: str, after: str) -> Span | None:
    """``[[Ti`` completes titles; ``#pro`` or ``#4`` completes posts as ``#id``.
    A ``#word`` may just be prose (or an Obsidian tag), so that list preselects
    nothing."""
    m = _WIKI_OPEN.search(before)
    if m:
        end = len(before) + (2 if after.startswith("]]") else 0)
        return Span("wiki", len(before) - len(m.group(1)), end, m.group(1), True)
    m = _ID_REF.search(before)
    if m:
        return Span("id", len(before) - len(m.group(1)) - 1, len(before), m.group(1), False)
    return None


def accepted_text(span: Span, pid: int, title: str) -> str:
    return f"{title}]]" if span.kind == "wiki" else f"#{pid}"


class TagSuggester(Suggester):
    """Completes the tag being typed (after the last comma) from the vault's
    tags; the Input shows the rest dimmed and → accepts it."""

    def __init__(self, tags: list[str]) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self.tags = tags

    async def get_suggestion(self, value: str) -> str | None:
        head, _, current = value.rpartition(",")
        q = current.strip().lower()
        if not q:
            return None
        have = {t.strip().lower() for t in head.split(",") if t.strip()}
        for tag in sorted(self.tags, key=lambda t: (not t.startswith(q), t)):
            if tag.startswith(q) and tag != q and tag not in have:
                return value + tag[len(q):]
        return None


class PathSuggester(Suggester):
    """Completes a local file path as you type it (→ accepts): the first entry
    of the directory typed so far that starts with the last part, with a
    trailing ``/`` on a directory. ``~`` is the home directory."""

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        if not value:
            return None
        head, _, part = value.rpartition("/")
        base = os.path.expanduser(head + "/" if head or value.startswith("/") else ".")
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return None
        show_hidden = part.startswith(".")
        for name in names:
            if name.startswith(part) and name != part and (show_hidden or not name.startswith(".")):
                slash = "/" if os.path.isdir(os.path.join(base, name)) else ""
                return value + name[len(part):] + slash
        return None
