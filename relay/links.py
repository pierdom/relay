"""Wikilink resolution: ``[[Title]]`` / ``[[Title|alias]]`` and ``#NNN`` refs.

Files store links Obsidian-native (``[[Title]]``); relay resolves them to post
ids at *display* time and never rewrites the stored form — except on rename,
where inbound ``[[OldTitle]]`` links are rewritten to the new title (see
``service.update_post``). Resolution is by title (the filename), case-insensitive
and exact, matching Obsidian.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import markdown_scan

# (!)?[[target]] | [[target#heading]] | [[target|alias]] | [[target#heading|alias]]
# A leading "!" is Obsidian's embed marker (``extract_links`` below decides,
# from the target's own shape, whether that makes it a file embed or a note
# transclusion — see the ``kind`` comment there). Every character class here
# excludes "\n": relay #198 N-5 follow-up L-4 found a finding whose ``match``
# ran ~900 characters, opening at a "[[" and closing on a "]]" several
# paragraphs later — a stray, never-closed "[[" earlier in the same post (in
# prose describing the syntax) let the old pattern's unbounded reach cross
# paragraph after paragraph looking for the next "]]" anywhere in the file.
# Confining every group to one line makes that physically impossible: a
# same-line "]]" is the only thing that can ever end a match.
WIKILINK_RE = re.compile(r"(!)?\[\[([^\]|#\n]+?)(#[^\]|\n]+)?(?:\|([^\]\n]+))?\]\]")
# #123 id-reference: '#' + digits, not preceded by a word char or another '#'
# (so markdown headings like "# Title" and "## x" never match).
IDREF_RE = re.compile(r"(?<![\w#])#(\d{1,5})\b")
# Obsidian embed syntax with no extension ("![[Some Note]]") is a note
# transclusion, not a file — relay doesn't transclude, but (matching
# main.js's own HAS_EXT_RE/linkifySegment) still treats it as a link to that
# post. Only an extensioned target ("![[photo.png]]", "![[doc.pdf]]") names
# an actual file and belongs to the attachment table instead of the posts
# table — same split main.js's renderer already makes.
_HAS_EXT_RE = re.compile(r"\.[a-z0-9]{1,12}$", re.IGNORECASE)


@dataclass(frozen=True)
class Link:
    kind: str                 # "wiki" | "id" | "embed"
    raw: str                  # the exact matched substring
    target: str               # title (wiki/embed) or numeric string (id)
    alias: str | None         # display alias, if any
    resolved_id: int | None   # "wiki"/"id" only — None => broken link. Always
                               # None for "embed": that target resolves against
                               # attachments, not posts, and has no post id to
                               # carry here — the caller checks it separately.


def norm_title(title: str) -> str:
    return title.strip().lower()


def extract_links(content: str, title_to_id: dict[str, int], ids: set[int]) -> list[Link]:
    """All wiki + id + embed links in ``content``, each resolved against the
    given maps (embeds are returned unresolved — see ``Link.resolved_id``).

    ``title_to_id`` must be keyed by :func:`norm_title`. ``ids`` is the set of
    existing post ids (to mark ``#NNN`` refs broken when the id is gone).

    Scans ``markdown_scan.strip_code(content)``, not ``content`` itself: a
    vault post *documenting* relay's own link syntax — a real, previously
    unhandled case (relay #198 N-5 follow-up, L-2/L-3) — used to have every
    example in it treated as a live link, including fenced code (a Telegraf
    TOML config's `` [[inputs.cpu]] `` sections, a bash `` [[ -n $X ]]` test)
    and backticked spans (`` `[[Title]]` ``) never meant to resolve at all.
    """
    stripped = markdown_scan.strip_code(content)
    out: list[Link] = []
    for m in WIKILINK_RE.finditer(stripped):
        bang, target, _heading, alias_group = m.group(1), m.group(2).strip(), m.group(3), m.group(4)
        alias = (alias_group or "").strip() or None
        if bang and _HAS_EXT_RE.search(target):
            # Attachment embed — resolved against the attachment table by the
            # caller (lint.py has vault access; this module doesn't).
            out.append(Link("embed", m.group(0), target, alias, None))
        else:
            out.append(Link("wiki", m.group(0), target, alias, title_to_id.get(norm_title(target))))
    for m in IDREF_RE.finditer(stripped):
        pid = int(m.group(1))
        out.append(Link("id", m.group(0), m.group(1), None, pid if pid in ids else None))
    return out


def target_ids(content: str, title_to_id: dict[str, int], ids: set[int]) -> set[int]:
    """Set of post ids ``content`` links to (resolved links only)."""
    return {link.resolved_id for link in extract_links(content, title_to_id, ids) if link.resolved_id is not None}


def rewrite_wikilink_targets(content: str, old_title: str, new_title: str) -> tuple[str, bool]:
    """Rewrite ``[[old_title]]`` (any alias/heading) to point at ``new_title``.

    Case-insensitive match on the target; alias and ``#heading`` are preserved.
    Returns ``(new_content, changed)``.
    """
    old_norm = norm_title(old_title)

    # Deliberately still scans raw content, unlike extract_links above: a
    # rename that skipped rewriting a link sitting inside a code span would
    # be a second, narrower defect (an inbound link staying literal text
    # forever looks the same as one that was never a link), not the one this
    # fix was asked for. Left as-is rather than folded in un-requested.
    def repl(m: re.Match) -> str:
        if norm_title(m.group(2)) != old_norm:
            return m.group(0)
        bang = m.group(1) or ""
        heading = m.group(3) or ""
        alias = m.group(4)
        inner = new_title + heading + (f"|{alias}" if alias else "")
        return f"{bang}[[{inner}]]"

    new = WIKILINK_RE.sub(repl, content)
    return new, new != content
