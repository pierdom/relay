"""Runtime diagnostics: what this relay is, and what it can actually do.

`/metrics` already exposes the version and a couple of counts, but in Prometheus
text — awkward for a human eyeballing a deployment and worse for an agent. The
reason this module exists is not the counts, though; it is **effective feature
state**.

Relay degrades silently in several ways, each visible only in a startup log line:
the `git` binary can be missing (vault history disables itself, so every write
becomes unrecoverable — this actually shipped in one image), SQLite can lack FTS5
(search falls back to `LIKE` substring matching), `MCP_OAUTH_ENABLED` can be set
without an OIDC client to broker to (static-bearer only), and the watcher can be
switched off (external Obsidian edits never reindex). Reporting the *effective*
state rather than the configured intent turns each of those from an archaeology
exercise into one request.

It also answers "which vault am I actually talking to", which is not obvious when
a local checkout and a remote deployment are both in play.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

import aiosqlite

from . import __version__, database, events, history, vault, watcher
from .config import settings
from .models import (
    AuthStatus,
    FeatureStatus,
    HistoryStatus,
    SearchStatus,
    StatusResponse,
    VaultStatus,
    WatcherStatus,
)

# Wall-clock and monotonic start, set once from the app lifespan. Monotonic for
# the duration (immune to clock changes), wall-clock for the timestamp.
_started_monotonic: float | None = None
_started_at: datetime | None = None


def mark_started() -> None:
    global _started_monotonic, _started_at
    _started_monotonic = time.monotonic()
    _started_at = datetime.now(UTC)


def uptime_seconds() -> int:
    if _started_monotonic is None:
        return 0
    return int(time.monotonic() - _started_monotonic)


def started_at_iso() -> str | None:
    return _started_at.strftime("%Y-%m-%dT%H:%M:%SZ") if _started_at is not None else None


# ── counts (shared with /metrics so the two surfaces cannot disagree) ─────────


async def post_count(db: aiosqlite.Connection) -> int:
    async with db.execute("SELECT COUNT(*) FROM posts") as cur:
        return (await cur.fetchone())[0]


async def tag_count(db: aiosqlite.Connection) -> int:
    """Distinct tags across all posts, split the way ``service.list_tags`` does."""
    async with db.execute("SELECT tags FROM posts WHERE tags != ''") as cur:
        distinct: set[str] = set()
        for row in await cur.fetchall():
            distinct.update(t for t in row[0].split(",") if t)
    return len(distinct)


async def folder_count(db: aiosqlite.Connection) -> int:
    async with db.execute("SELECT path FROM posts") as cur:
        rows = await cur.fetchall()
    # Root files (the master doc) live in no folder.
    return len({r[0].split("/", 1)[0] for r in rows if "/" in r[0]})


async def build(db: aiosqlite.Connection) -> StatusResponse:
    """Assemble the full status. Reports what is *working*, not what is configured."""
    attachments = vault.list_attachments()
    git = await history.git_version()
    return StatusResponse(
        version=__version__,
        uptime_seconds=uptime_seconds(),
        started_at=started_at_iso(),
        sse_clients=events.subscriber_count(),
        vault=VaultStatus(
            path=settings.vault_path,
            posts=await post_count(db),
            tags=await tag_count(db),
            folders=await folder_count(db),
            attachments=len(attachments),
            attachment_bytes=sum(size for (_n, _f, size) in attachments),
        ),
        features=FeatureStatus(
            history=HistoryStatus(
                enabled=settings.history_enabled,
                git=git,
                # The distinction that matters: history_enabled is intent, this is
                # whether a write would actually be recorded.
                effective=settings.history_enabled and git is not None,
            ),
            search=SearchStatus(fts5=database.FTS_ENABLED),
            watcher=WatcherStatus(enabled=settings.watch_enabled, running=watcher.is_running()),
            auth=AuthStatus(oidc=settings.oidc_enabled, mcp_oauth=settings.mcp_oauth_active),
        ),
    )
