"""Folder placement policy: which sub-directory a post's file lives in.

Folders are a *projection of the primary tag at creation time* — a human browse
tree layered on top of tags, which stay the query layer. A post is filed once,
on create, by its first domain tag; thereafter its folder is human-owned and
relay never auto-moves it on retag (see ``vault.write_file``, which preserves the
existing directory whenever it edits a file in place).
"""
from __future__ import annotations

# Domain tags, in priority order: the *first* of these found in a post's tag
# list decides its folder. Keep in sync with the taxonomy in master doc #0.
DOMAINS = [
    "finance", "career", "homelab", "dev", "audio", "photography",
    "music", "auto", "gaming", "radio", "watches", "reading",
]

# Posts that carry no domain tag (briefings, digests, the master doc/to-do)
# route via a series/type tag instead — avoids a mass retag. Revisit if tag
# hygiene improves and every post gains a real domain tag.
FALLBACK = {
    "financial-analyst": "finance",
    "briefing": "finance",
    "daily-digest": "digests",
    "news-digest": "digests",
    "digest": "digests",
    "news": "digests",
    # `meta`/`index` (short-lived notes: to-do lists, scratch) live in Inbox.
    "meta": "inbox",
    "index": "inbox",
}

INBOX = "Inbox"

# tag/pseudo-domain -> on-disk folder name
_FOLDER = {d: d.capitalize() for d in DOMAINS}
_FOLDER["digests"] = "Digests"
_FOLDER["inbox"] = INBOX

_DOMSET = set(DOMAINS)


def folder_for(post_id: int, tags: list[str]) -> str:
    """Sub-directory (relative to the vault) for a post's file.

    Returns ``""`` for the vault root. The master document (id=0) stays at root
    as the entry point; everything else files under its primary-domain folder,
    falling back to a series tag, then to ``Inbox``.
    """
    if post_id == 0:
        return ""
    for t in tags:
        if t in _DOMSET:
            return _FOLDER[t]
    for t in tags:
        if t in FALLBACK:
            return _FOLDER[FALLBACK[t]]
    return INBOX
