"""A post's headings as an outline, and one section's text by its heading —
behind ``get_post``'s ``outline`` and ``section``, so a caller can read the
part of a long post it needs instead of the whole body.

A section runs from its heading line to the next heading of the same or a
higher level, so it includes its subsections. Headings inside fenced code are
not headings (``markdown_scan.strip_fences``), and an H1 restating the title
is the title, not a section — paths then start at the H2s, the same shape as
a semantic search ``match``'s ``Heading > Subheading``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import SectionNotFound, SectionNotUnique
from .markdown_scan import strip_fences
from .models import plain_heading

# The closing ``#`` run only counts after whitespace, so ``## C#`` keeps its ``#``.
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", re.MULTILINE)
_NOT_WORD_RE = re.compile(r"\W+")
_LISTED_PATHS = 30


@dataclass(frozen=True)
class Section:
    level: int
    parts: tuple[str, ...]  # plain-text headings from the outermost down to this one
    start: int  # offset of the heading line
    end: int  # offset of the next heading at this level or above, or the end of the post

    @property
    def path(self) -> str:
        return " > ".join(self.parts)


def _key(text: str) -> str:
    return " ".join(text.casefold().split())


def parse(title: str, content: str) -> list[Section]:
    """Every heading of ``content``, in order."""
    title_key = _NOT_WORD_RE.sub("", title).casefold()
    found: list[tuple[int, str, int]] = []
    for m in _HEADING_RE.finditer(strip_fences(content)):
        level, heading = len(m.group(1)), plain_heading(m.group(2))
        if level == 1 and _NOT_WORD_RE.sub("", heading).casefold() == title_key:
            continue
        found.append((level, heading, m.start()))

    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    for i, (level, heading, start) in enumerate(found):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading))
        end = next((s for lvl, _, s in found[i + 1 :] if lvl <= level), len(content))
        sections.append(Section(level, tuple(h for _, h in stack), start, end))
    return sections


def intro(content: str, sections: list[Section]) -> str:
    """The text before the first heading — the whole post when it has none."""
    return content[: sections[0].start].strip() if sections else content


def text(content: str, section: Section) -> str:
    return content[section.start : section.end].strip()


def find(sections: list[Section], query: str) -> Section:
    """The section ``query`` names: a heading, or a ``>``-separated path that
    ends at it (``Setup > Backups``, or just ``Backups``), compared without
    case or Markdown. An exact full path wins over a partial one, which wins
    over the last heading alone; a query that still names several sections is
    refused rather than guessed."""
    want = tuple(_key(plain_heading(p)) for p in query.split(">"))
    keyed = [(s, tuple(_key(p) for p in s.parts)) for s in sections]
    hits = (
        [s for s, parts in keyed if parts == want]
        or [s for s, parts in keyed if parts[-len(want) :] == want]
        # A path whose parents don't match still names a heading that's unique
        # on its own: chunking's paths ignore H1s, so a semantic match can
        # carry the wrong parent (``Local CNAME > Rejected`` for an H3 under
        # ``# Decision log``).
        or [s for s, parts in keyed if len(want) > 1 and parts[-1] == want[-1]]
    )
    if len(hits) == 1:
        return hits[0]
    if not hits:
        listed = "; ".join(s.path for s in sections[:_LISTED_PATHS]) or "none"
        raise SectionNotFound(f"No section {query!r}. Sections: {listed}")
    paths = "; ".join(s.path for s in hits)
    raise SectionNotUnique(f"{query!r} names {len(hits)} sections: {paths} — pass the full path")
