"""Scope checks, row helpers and page bounds shared by every service module."""
from __future__ import annotations

import logging
from collections.abc import Iterable

import aiosqlite

from ..errors import ScopeDenied
from ..identity import Actor

logger = logging.getLogger(__name__)


def _require_write_scope(actor: Actor | None, tags: Iterable[str]) -> None:
    """Enforce a write-restricted-to-tags key's scope against the tags of
    the post/attachment being written (relay #198, B-8; ALL-of semantics —
    see ``Actor.can_write_tags``). ``actor=None`` means an internal/test
    caller that bypasses auth entirely — never itself a reason to deny,
    mirroring ``actor``'s existing provenance-only contract elsewhere in
    this module (both REST's ``require_api_key`` and MCP's
    ``_current_actor()`` always hand back a real ``Actor`` on an
    authenticated call)."""
    if actor is not None and not actor.can_write_tags(tags):
        raise ScopeDenied


def _require_full_access(actor: Actor | None) -> None:
    """Vault-wide operations (rename_tag, set_tag_config, the embeddings
    toggle/backfill) have no single owning tag to check — only a full-access
    key (or an untracked internal caller, ``actor=None``) may call them; a
    merely-``write`` tag-restricted key is denied outright, same as
    ``read``."""
    if actor is not None and actor.scope.mode != "full":
        raise ScopeDenied


async def _fetch(db: aiosqlite.Connection, post_id: int) -> aiosqlite.Row | None:
    async with db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)) as cur:
        return await cur.fetchone()


# Page bounds, enforced here so every transport agrees: REST already validates
# these at the Query() layer; the in-process MCP server passed them straight to
# SQL, where `limit=-1` is SQLite's "unbounded" (AUDIT.md S-07).
MAX_PAGE_LIMIT = 100
MAX_HISTORY_LIMIT = 200


def _clamp(value: int, *, low: int, high: int) -> int:
    return max(low, min(int(value), high))

