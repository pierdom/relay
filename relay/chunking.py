"""Split a post's body into embeddable chunks. Pure functions, no I/O — the
post's own reasoning (relay #253) is that whole-post embedding averages a
multi-topic post into a vector for nothing, so retrieval quality lives here.

``body`` (the raw chunk text) is the cache-hash input in ``relay.vectors`` —
title-independent, so a rename doesn't invalidate the cache. ``embed_text`` is
what actually gets sent to the embedding model, and *does* carry the title for
context. Those two only need to agree at embed time, never at hash time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.MULTILINE)

_RUNT_WORDS = 50
_GIANT_WORDS = 400
_OVERLAP_RATIO = 0.15


@dataclass(frozen=True)
class Chunk:
    heading_path: str
    body: str
    embed_text: str


def _strip_code_fences(content: str) -> str:
    """Drop fenced code blocks before chunking — noise in embedding space
    (relay #253: keep them in the post and in FTS5, drop from the vector)."""
    return _CODE_FENCE_RE.sub("", content)


def _split_on_headings(content: str) -> list[tuple[str, str]]:
    """(heading_path, section_text) pairs. Text before the first heading gets
    ``""``. An H3 nests under the nearest preceding H2 as ``"H2 > H3"``; an H3
    with no preceding H2 stands alone."""
    matches = list(_HEADING_RE.finditer(content))
    if not matches:
        return [("", content.strip())] if content.strip() else []

    sections: list[tuple[str, str]] = []
    intro = content[: matches[0].start()].strip()
    if intro:
        sections.append(("", intro))

    current_h2: str | None = None
    for i, m in enumerate(matches):
        level, title = m.group(1), m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()

        if level == "##":
            current_h2 = title
            heading_path = title
        else:  # ###
            heading_path = f"{current_h2} > {title}" if current_h2 else title

        if body:
            sections.append((heading_path, body))

    return sections


def _word_count(text: str) -> int:
    return len(text.split())


def _merge_runts(sections: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge sections under ~50 words into an adjacent one. A runt adopts the
    *other* section's heading, not its own — the larger section is what the
    merged chunk is actually about."""
    if not sections:
        return sections

    merged: list[tuple[str, str]] = []
    pending_heading, pending_body = sections[0]
    for heading, body in sections[1:]:
        if _word_count(pending_body) < _RUNT_WORDS:
            # Runt folds forward, adopting the next (larger) section's heading.
            pending_heading, pending_body = heading, f"{pending_body}\n\n{body}"
        else:
            merged.append((pending_heading, pending_body))
            pending_heading, pending_body = heading, body
    merged.append((pending_heading, pending_body))

    # A trailing runt has nothing left to merge forward into — fold it back,
    # adopting the previous (larger) section's heading instead of its own.
    if len(merged) > 1 and _word_count(merged[-1][1]) < _RUNT_WORDS:
        _, last_body = merged.pop()
        prev_heading, prev_body = merged.pop()
        merged.append((prev_heading, f"{prev_body}\n\n{last_body}"))

    return merged


def _split_giant(heading: str, body: str) -> list[tuple[str, str]]:
    """Split a >~400-word section into ~400-word pieces with ~15% overlap."""
    words = body.split()
    if len(words) <= _GIANT_WORDS:
        return [(heading, body)]

    step = int(_GIANT_WORDS * (1 - _OVERLAP_RATIO))
    pieces: list[tuple[str, str]] = []
    start = 0
    while start < len(words):
        piece = words[start : start + _GIANT_WORDS]
        pieces.append((heading, " ".join(piece)))
        if start + _GIANT_WORDS >= len(words):
            break
        start += step
    return pieces


def chunk_post(title: str, content: str) -> list[Chunk]:
    stripped = _strip_code_fences(content)
    sections = _merge_runts(_split_on_headings(stripped))

    chunks: list[Chunk] = []
    for heading, body in sections:
        for h, b in _split_giant(heading, body):
            prefix = f"{title} > {h}" if h else title
            chunks.append(Chunk(heading_path=h, body=b, embed_text=f"{prefix}\n\n{b}"))
    return chunks
