"""Exceptions, row helpers and page bounds shared by every service module."""
from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger(__name__)


class PostNotFound(Exception):
    """Raised when an operation targets a post id that does not exist."""


class ProtectedPost(Exception):
    """Raised when an operation is not allowed on a reserved post (e.g. id=0)."""


class RevisionNotFound(Exception):
    """Raised when a restore names a revision that isn't in the post's history."""


class HistoryUnavailable(Exception):
    """Raised when history is off or git is missing, so there is nothing to read."""


class SemanticSearchUnavailable(Exception):
    """Raised when mode='semantic'/'hybrid' is requested but sqlite-vec isn't
    loaded or settings.embedding_enabled is off. Errors loud rather than
    silently degrading to empty/keyword-only results — a caller who explicitly
    asked for semantic ranking should not get an indistinguishable "no
    matches" for "this relay doesn't have the feature on"."""


class InvalidSearchMode(Exception):
    """Raised when mode isn't one of keyword/semantic/hybrid. REST already
    422s this at the Query(pattern=...) layer before it reaches here, but the
    in-process MCP server calls list_posts directly with no such validation —
    a typo'd mode there would otherwise fall back to keyword silently, the
    same way an unrecognised sort/order value quietly defaults instead of
    erroring. mode is worse to default silently on: it also gates
    SemanticSearchUnavailable, so a typo skips both the caller's intended
    ranking *and* the error that would have flagged the feature is off."""


class AttachmentError(Exception):
    """Raised when an attachment can't be stored (e.g. too large)."""


class InvalidTag(Exception):
    """Raised when a tag name normalises to nothing (``"!!"``) — it would
    otherwise create a nameless ``tag_config`` row that ``list_tags`` shows
    forever (AUDIT.md B-08)."""
class InvalidFolder(Exception):
    """Raised when a caller-supplied ``folder`` is not a plain first-level folder
    name (``..``, a dot-folder, a path) — see ``folders.is_valid_name``."""


class AttachmentSourceError(Exception):
    """Raised when the attachment's byte source fails to resolve — a source_url
    fetch error or a presigned upload slot that's unknown/expired/unfilled. Maps
    to a 400 (loud, actionable), distinct from the 413 size cap."""


async def _fetch(db: aiosqlite.Connection, post_id: int) -> aiosqlite.Row | None:
    async with db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)) as cur:
        return await cur.fetchone()


def _tags_from_sentinel(s: str) -> list[str]:
    return [t for t in s.split(",") if t]



# Page bounds, enforced here so every transport agrees: REST already validates
# these at the Query() layer; the in-process MCP server passed them straight to
# SQL, where `limit=-1` is SQLite's "unbounded" (AUDIT.md S-07).
MAX_PAGE_LIMIT = 100
MAX_HISTORY_LIMIT = 200


def _clamp(value: int, *, low: int, high: int) -> int:
    return max(low, min(int(value), high))

