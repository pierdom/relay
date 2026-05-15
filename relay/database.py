from __future__ import annotations

import os

import aiosqlite

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT,
    content    TEXT NOT NULL,
    format     TEXT NOT NULL DEFAULT 'markdown'
                   CHECK (format IN ('markdown', 'text', 'html', 'json')),
    tags       TEXT NOT NULL DEFAULT '',
    source     TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts (created_at);
CREATE INDEX IF NOT EXISTS idx_posts_tags ON posts (tags);
CREATE TABLE IF NOT EXISTS tag_config (
    tag       TEXT PRIMARY KEY,
    ttl_hours INTEGER NOT NULL
);
"""


async def init_db() -> None:
    db_dir = os.path.dirname(settings.database_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    async with aiosqlite.connect(settings.database_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=5000;")
        await db.commit()


async def get_db():
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=5000;")
        yield db
