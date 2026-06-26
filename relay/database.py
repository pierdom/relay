"""The SQLite index — a disposable mirror of the vault.

Files in the vault (``relay.vault``) are canonical; this index exists only to
serve fast list/search/tag/TTL/SSE-replay queries. It is wiped and rebuilt from
the files on every startup, so there are no schema migrations to carry.
"""
from __future__ import annotations

import os

import aiosqlite

from . import vault
from .config import settings

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


async def init_db() -> None:
    os.makedirs(settings.relay_dir, exist_ok=True)
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(_SCHEMA)
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=5000;")
        await db.commit()
        # Files are canonical — repopulate the index from the vault on startup.
        await vault.rebuild_index(db)


async def get_db():
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=5000;")
        yield db
