"""Folder placement policy: which sub-directory a post's file lives in.

Folders are a *projection of the primary tag at creation time* — a human browse
tree layered on top of tags, which stay the query layer. A post is filed once,
on create, by its first domain tag; thereafter its folder is human-owned and
relay never auto-moves it on retag (see ``vault.write_file``, which preserves the
existing directory whenever it edits a file in place). The one exception is
``Inbox`` — the "unfiled" bucket for tag-less notes: when a note there gains its
first domain tag, ``service.update_post`` moves it (and its own attachments) into
that domain folder. Moves only ever go *out of* Inbox, never between real folders.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from .config import settings

logger = logging.getLogger(__name__)

INBOX = "Inbox"


@dataclass(frozen=True)
class Taxonomy:
    """What decides a post's folder, from configuration (``RELAY_DOMAINS``,
    ``RELAY_FOLDER_FALLBACKS``; defaults in ``config.py``).

    ``domains`` maps each domain tag to its folder, in the configured order.
    ``fallbacks`` routes a post with no domain tag by a series/type tag
    instead (briefings, digests, scratch notes) — avoids a mass retag.
    """

    domains: dict[str, str]
    fallbacks: dict[str, str]


@lru_cache(maxsize=8)
def _parse(domains_raw: str, fallbacks_raw: str) -> Taxonomy:
    """Parse both settings, once per value. ``tag`` or ``tag:Folder`` for a
    domain (the folder defaults to the tag capitalised); ``tag:target`` for a
    fallback, the target a domain tag or a folder name. A malformed entry is
    skipped with one warning, never a startup failure (as RELAY_API_KEYS)."""
    from .models import clean_tag  # models imports this module

    domains: dict[str, str] = {}
    for entry in filter(None, (e.strip() for e in domains_raw.split(","))):
        raw_tag, _, raw_folder = entry.partition(":")
        tag = clean_tag(raw_tag)
        folder = raw_folder.strip() or tag.capitalize()
        if not tag or tag != raw_tag.strip().lower() or not is_valid_name(folder):
            logger.warning("RELAY_DOMAINS: skipping malformed entry %r", entry)
        elif tag in domains:
            logger.warning("RELAY_DOMAINS: duplicate domain %r — keeping the first", tag)
        else:
            domains[tag] = folder
    fallbacks: dict[str, str] = {}
    for entry in filter(None, (e.strip() for e in fallbacks_raw.split(","))):
        raw_tag, sep, target = (part.strip() for part in entry.partition(":"))
        tag = clean_tag(raw_tag)
        folder = domains.get(target.lower(), INBOX if target.lower() == "inbox" else target)
        if not sep or not tag or tag != raw_tag.lower() or not is_valid_name(folder):
            logger.warning("RELAY_FOLDER_FALLBACKS: skipping malformed entry %r", entry)
        elif tag in domains:
            logger.warning("RELAY_FOLDER_FALLBACKS: %r is a domain already — skipping", tag)
        else:
            fallbacks.setdefault(tag, folder)
    return Taxonomy(domains, fallbacks)


def taxonomy() -> Taxonomy:
    return _parse(settings.relay_domains, settings.relay_folder_fallbacks)


def domains() -> list[str]:
    """The configured domain tags, in order."""
    return list(taxonomy().domains)

# Dated, disposable snapshot tags (relay #198 O-4/N-5): briefings and digests
# that pile up near-identical week over week. Nothing is expected to link back
# to a specific day's issue (lint.py's zero_backlinks/link_to_deleted_post
# exemption) and their volume would otherwise dominate embedding similarity
# (vectors.py excludes them from embedding entirely) — one constant, one
# judgment call, reused by both.
DISPOSABLE_TAGS = {"digest", "news", "daily-digest", "news-digest", "briefing", "financial-analyst"}


def is_disposable(tags: Iterable[str]) -> bool:
    """Whether a post carrying ``tags`` is a disposable snapshot (any one of
    ``DISPOSABLE_TAGS``): never embedded, never offered as ``similar``."""
    return not DISPOSABLE_TAGS.isdisjoint(tags)


def folder_of(relpath: str, *, default: str = "") -> str:
    """First-level folder of a vault-relative path (``"Homelab/x.md"`` → ``"Homelab"``);
    ``default`` for a root file such as the master document."""
    return relpath.split("/", 1)[0] if "/" in relpath else default

def is_valid_name(folder: str) -> bool:
    """Whether ``folder`` names a first-level vault folder a caller may address.

    One path segment, not ``.``/``..``, not dot-prefixed (``.relay``, ``.obsidian``,
    ``.trash`` are relay/Obsidian bookkeeping, never a post folder). Stripping the
    name with ``Path(folder).name`` alone is not enough: ``Path("..").name`` is
    ``".."``, which let ``folder=".."`` write beside the vault (AUDIT.md S-02).
    """
    if not folder or folder in (".", "..") or folder.startswith("."):
        return False
    return "/" not in folder and "\\" not in folder


def folder_for(post_id: int, tags: list[str]) -> str:
    """Sub-directory (relative to the vault) for a post's file.

    Returns ``""`` for the vault root. The master document (id=0) stays at root
    as the entry point; everything else files under its primary-domain folder,
    falling back to a series tag, then to ``Inbox``. A post files by the first
    of *its own* tags that is a domain, not by the configured order.
    """
    if post_id == 0:
        return ""
    tax = taxonomy()
    for t in tags:
        if t in tax.domains:
            return tax.domains[t]
    for t in tags:
        if t in tax.fallbacks:
            return tax.fallbacks[t]
    return INBOX
