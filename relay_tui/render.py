"""Markdown preprocessing for the TUI's post view: wikilinks, ``#N`` refs and
attachment embeds become links the Markdown widget can route."""
from __future__ import annotations

import re
from urllib.parse import quote

from relay import links

from . import api

_EMBED_RE = re.compile(r"!\[\[([^\]|#]+?)(?:\|([^\]]+))?\]\]")
# Same link grammar the server resolves with — one definition, not a copy.
_WIKI_RE = links.WIKILINK_RE
_IDREF_RE = links.IDREF_RE
# Any extension — used only on the ![[…]] embed path (always a file in Obsidian).
_ANY_EXT_RE = re.compile(r"\.[a-z0-9]{1,12}$", re.IGNORECASE)
# Curated attachment types for the plain [[…]] link path — not a generic ``\.xxx$``
# — so dotted note titles (e.g. ``[[Section 2.1]]``) aren't mistaken for files.
_FILE_EXT_RE = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|avif|bmp|pdf|canvas|docx?|xlsx?|pptx?|csv|txt"
    r"|rtf|odt|ods|zip|epub|mp3|m4a|wav|flac|ogg|aac|opus|mp4|mov|webm|mkv|avi)$",
    re.IGNORECASE,
)
_CODE_SPLIT = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)


def _attachment_link(name: str, label: str) -> str:
    """A markdown link to the attachment endpoint — terminals can't inline images,
    so both ``![[img]]`` embeds and ``[[file.pdf]]`` render as an external link."""
    return f"[\U0001F4CE {label}]({api._base()}/attachments/{quote(name)})"


def _linkify_markdown(content: str, index: dict[str, int]) -> str:
    """Turn ``[[Title]]`` / ``#NNN`` into ``[label](relay:ID)`` links and Obsidian
    attachment embeds into external links.

    Resolution is by title (case-insensitive). Broken wikilinks degrade to plain
    text; unknown ``#NNN`` are left untouched. Code spans/blocks are skipped.
    """
    ids = set(index.values())

    def convert(text: str) -> str:
        def embed(m: re.Match) -> str:
            name = m.group(1).strip()
            opt = (m.group(2) or "").strip()
            if _ANY_EXT_RE.search(name):  # embed of any file → external link
                label = name if (not opt or re.fullmatch(r"\d+(x\d+)?", opt)) else opt
                return _attachment_link(name, label)
            # No extension → note transclusion; link to the note or degrade to text.
            pid = index.get(name.lower())
            return f"[{opt or name}](relay:{pid})" if pid is not None else (opt or name)

        def wiki(m: re.Match) -> str:
            # group(1) is links.WIKILINK_RE's own embed-marker capture — always
            # None here, since _EMBED_RE.sub above already consumed every
            # "![[...]]" before this pattern ever runs.
            target = m.group(2).strip()
            alias = (m.group(4) or target).strip()
            pid = index.get(target.lower())
            if pid is not None:
                return f"[{alias}](relay:{pid})"
            # Unresolved but file-like (e.g. [[doc.pdf]]) → attachment link.
            return _attachment_link(target, alias) if _FILE_EXT_RE.search(target) else alias

        def idref(m: re.Match) -> str:
            n = m.group(1)
            return f"[#{n}](relay:{n})" if int(n) in ids else m.group(0)

        return _IDREF_RE.sub(idref, _WIKI_RE.sub(wiki, _EMBED_RE.sub(embed, text)))

    return "".join(
        part if i % 2 else convert(part)
        for i, part in enumerate(_CODE_SPLIT.split(content))
    )


def _outbound_link_ids(content: str, index: dict[str, int]) -> list[int]:
    """Resolved link targets in ``content``, in document order, de-duplicated.

    Covers ``[[Title]]`` and ``#NNN``; skips code and unresolved links.
    """
    ids = set(index.values())
    ordered: list[int] = []
    for i, part in enumerate(_CODE_SPLIT.split(content)):
        if i % 2:
            continue
        hits: list[tuple[int, int]] = []
        for m in _WIKI_RE.finditer(part):
            pid = index.get(m.group(2).strip().lower())
            if pid is not None:
                hits.append((m.start(), pid))
        for m in _IDREF_RE.finditer(part):
            n = int(m.group(1))
            if n in ids:
                hits.append((m.start(), n))
        hits.sort()
        ordered.extend(pid for _, pid in hits)
    seen: set[int] = set()
    return [p for p in ordered if not (p in seen or seen.add(p))]



# A titled post's body usually opens with that title as a heading; the view
# shows the title already, so it drops it — only when it *is* the title (a note
# opening on "## Part 1" keeps its first section). Compared on letters and
# digits alone, since the title is a filename and lost the heading's ":" or "/".
# The browser's render.stripTitleHeading does the same.
_LEADING_HEADING_RE = re.compile(r"^\s*#{1,6}\s+([^\n]*)\n*")


def _bare(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def strip_title_heading(content: str, title: str) -> str:
    m = _LEADING_HEADING_RE.match(content)
    return content[m.end():] if m and title and _bare(m.group(1)) == _bare(title) else content
