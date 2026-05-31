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
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts (created_at);
CREATE INDEX IF NOT EXISTS idx_posts_tags ON posts (tags);
CREATE TABLE IF NOT EXISTS tag_config (
    tag       TEXT PRIMARY KEY,
    ttl_hours INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT
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
        # Migration: add updated_at to existing databases
        async with db.execute("PRAGMA table_info(posts)") as cur:
            cols = {row[1] async for row in cur}
        if "updated_at" not in cols:
            await db.execute("ALTER TABLE posts ADD COLUMN updated_at TEXT")
            await db.execute("UPDATE posts SET updated_at = created_at")
        if "expires_at" not in cols:
            await db.execute("ALTER TABLE posts ADD COLUMN expires_at TEXT")
        await db.commit()

    async with aiosqlite.connect(settings.database_path) as db:
        async with db.execute("PRAGMA table_info(tag_config)") as cur:
            tag_cols = {row[1] async for row in cur}
        if "expires_at" not in tag_cols:
            await db.execute("ALTER TABLE tag_config ADD COLUMN expires_at TEXT")
            await db.commit()

    # Seed the master document at id=0 (reserved, never auto-assigned, never expires)
    async with aiosqlite.connect(settings.database_path) as db:
        async with db.execute("SELECT id FROM posts WHERE id = 0") as cur:
            if await cur.fetchone() is None:
                await db.execute(
                    "INSERT INTO posts (id, title, content, format, tags) VALUES (0, ?, ?, 'markdown', '')",
                    ("Master Document", "# Master Document\n\nIndex, naming conventions, and instructions for AI agents interacting with this relay."),
                )
                await db.commit()


async def get_db():
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=5000;")
        yield db
