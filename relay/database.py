"""The SQLite index — a disposable mirror of the vault.

Files in the vault (``relay.vault``) are canonical; this index exists only to
serve fast list/search/tag/TTL/SSE-replay queries. It is wiped and rebuilt from
the files on every startup, so there are no schema migrations to carry.
"""
from __future__ import annotations

import logging
import os

import aiosqlite

from . import vault
from .config import settings

logger = logging.getLogger(__name__)

# Set by init_db: True once the FTS5 full-text index is live. When False (SQLite
# built without FTS5), service.list_posts falls back to LIKE substring search.
FTS_ENABLED = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id         INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    path       TEXT NOT NULL,
    content    TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '',
    source     TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts (created_at);
CREATE INDEX IF NOT EXISTS idx_posts_tags ON posts (tags);
CREATE TABLE IF NOT EXISTS tag_config (
    tag        TEXT PRIMARY KEY,
    ttl_hours  INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT
);
"""

# External-content FTS5 index over the posts table. `content='posts'` means the
# vtable stores only the inverted index (no copy of the text); the triggers below
# mirror every posts write into it, so it stays in sync through service/MCP edits,
# the watcher's external-edit reindex, and TTL cleanup alike. porter stemming over
# unicode61 gives case/diacritic folding + stem matching ("running" ~ "run").
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE posts_fts USING fts5(
    title, content, source, tags,
    content='posts', content_rowid='id',
    tokenize='porter unicode61'
);
CREATE TRIGGER posts_fts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, title, content, source, tags)
    VALUES (new.id, new.title, new.content, new.source, new.tags);
END;
CREATE TRIGGER posts_fts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, title, content, source, tags)
    VALUES ('delete', old.id, old.title, old.content, old.source, old.tags);
END;
CREATE TRIGGER posts_fts_au AFTER UPDATE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, title, content, source, tags)
    VALUES ('delete', old.id, old.title, old.content, old.source, old.tags);
    INSERT INTO posts_fts(rowid, title, content, source, tags)
    VALUES (new.id, new.title, new.content, new.source, new.tags);
END;
"""

_DROP_FTS = """
DROP TRIGGER IF EXISTS posts_fts_ai;
DROP TRIGGER IF EXISTS posts_fts_ad;
DROP TRIGGER IF EXISTS posts_fts_au;
DROP TABLE IF EXISTS posts_fts;
"""


async def _init_fts(db: aiosqlite.Connection) -> bool:
    """Build the FTS index + sync triggers over the freshly rebuilt posts table.

    Called *after* rebuild_index so no triggers fire during the wipe/repopulate
    (an external-content FTS5 'delete' against rows it never indexed corrupts it).
    We drop any stale objects from a prior run, recreate, then 'rebuild' to
    populate the index from the current posts. Returns False (LIKE fallback) if
    the SQLite build lacks FTS5.
    """
    try:
        await db.executescript(_FTS_SCHEMA)
        await db.execute("INSERT INTO posts_fts(posts_fts) VALUES('rebuild')")
        await db.commit()
        return True
    except aiosqlite.Error as exc:
        logger.warning("FTS5 unavailable — falling back to LIKE search (%s)", exc)
        await db.executescript(_DROP_FTS)
        await db.commit()
        return False


async def init_db() -> None:
    global FTS_ENABLED
    os.makedirs(settings.relay_dir, exist_ok=True)
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(_SCHEMA)
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=5000;")
        # Drop any FTS objects a prior run left so rebuild_index's DELETE/INSERT
        # doesn't fire stale triggers against an index they never populated.
        await db.executescript(_DROP_FTS)
        await db.commit()
        # Files are canonical — repopulate the index from the vault on startup.
        await vault.rebuild_index(db)
        FTS_ENABLED = await _init_fts(db)


async def get_db():
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=5000;")
        yield db
