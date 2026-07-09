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

# [[target]] | [[target#heading]] | [[target|alias]] | [[target#heading|alias]]
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(#[^\]|]+)?(?:\|([^\]]+))?\]\]")
# #123 id-reference: '#' + digits, not preceded by a word char or another '#'
# (so markdown headings like "# Title" and "## x" never match).
IDREF_RE = re.compile(r"(?<![\w#])#(\d{1,5})\b")


@dataclass(frozen=True)
class Link:
    kind: str                 # "wiki" | "id"
    raw: str                  # the exact matched substring
    target: str               # title (wiki) or numeric string (id)
    alias: str | None         # display alias, if any
    resolved_id: int | None   # None => broken link


def norm_title(title: str) -> str:
    return title.strip().lower()


def extract_links(content: str, title_to_id: dict[str, int], ids: set[int]) -> list[Link]:
    """All wiki + id links in ``content``, each resolved against the given maps.

    ``title_to_id`` must be keyed by :func:`norm_title`. ``ids`` is the set of
    existing post ids (to mark ``#NNN`` refs broken when the id is gone).
    """
    out: list[Link] = []
    for m in WIKILINK_RE.finditer(content):
        target = m.group(1).strip()
        alias = (m.group(3) or "").strip() or None
        out.append(Link("wiki", m.group(0), target, alias, title_to_id.get(norm_title(target))))
    for m in IDREF_RE.finditer(content):
        pid = int(m.group(1))
        out.append(Link("id", m.group(0), m.group(1), None, pid if pid in ids else None))
    return out


def target_ids(content: str, title_to_id: dict[str, int], ids: set[int]) -> set[int]:
    """Set of post ids ``content`` links to (resolved links only)."""
    return {l.resolved_id for l in extract_links(content, title_to_id, ids) if l.resolved_id is not None}


def rewrite_wikilink_targets(content: str, old_title: str, new_title: str) -> tuple[str, bool]:
    """Rewrite ``[[old_title]]`` (any alias/heading) to point at ``new_title``.

    Case-insensitive match on the target; alias and ``#heading`` are preserved.
    Returns ``(new_content, changed)``.
    """
    old_norm = norm_title(old_title)

    def repl(m: re.Match) -> str:
        if norm_title(m.group(1)) != old_norm:
            return m.group(0)
        heading = m.group(2) or ""
        alias = m.group(3)
        inner = new_title + heading + (f"|{alias}" if alias else "")
        return f"[[{inner}]]"

    new = WIKILINK_RE.sub(repl, content)
    return new, new != content
