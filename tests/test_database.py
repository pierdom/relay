"""`init_db`'s in-place schema migrations (`relay/database.py`).

Every test elsewhere in this suite starts from an empty `index.db`, so
`CREATE TABLE IF NOT EXISTS posts (...)` always includes every column and no
migration path ever runs. That's exactly the gap that let a real bug reach
production (relay #198, B-7): `CREATE INDEX ... (updated_by)` lived in the
same `executescript` as the `CREATE TABLE`, which is a no-op against an
*existing* table — so on an upgrade, the index statement ran before the
`ALTER TABLE ADD COLUMN updated_by` migration below it had a chance to add
the column, and startup 500'd with `no such column: updated_by`. These tests
build a pre-migration schema by hand and upgrade it in place, the only way
to actually exercise that path.
"""
from __future__ import annotations

import os
import sqlite3

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio

from relay import database
from relay.config import settings

# The schema as it existed immediately before the `updated_by` column/index
# (relay #198, B-7) — i.e. what a real, already-deployed vault's index.db
# looks like when this migration runs against it for the first time.
_PRE_B7_SCHEMA = """
CREATE TABLE posts (
    id         INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    path       TEXT NOT NULL,
    content    TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '',
    source     TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    expires_at TEXT,
    properties TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_posts_created_at ON posts (created_at);
CREATE INDEX idx_posts_tags ON posts (tags);
CREATE TABLE tag_config (
    tag        TEXT PRIMARY KEY,
    ttl_hours  INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT
);
CREATE TABLE changes (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    title   TEXT NOT NULL,
    tags    TEXT NOT NULL DEFAULT '',
    action  TEXT NOT NULL,
    at      TEXT NOT NULL,
    sha     TEXT NOT NULL,
    author  TEXT
);
CREATE INDEX idx_changes_post_id ON changes (post_id);
"""


@pytest_asyncio.fixture
async def pre_b7_vault(tmp_path, monkeypatch):
    """A vault whose `.relay/index.db` predates the `updated_by` migration.
    `isolated_vault` (conftest.py, autouse) already repoints `vault_path`
    under `tmp_path`; this fixture points it at its own subdirectory instead
    (seeding the *old* schema there before `init_db` ever runs) so the
    monkeypatch here — applied after the autouse one — wins."""
    relay_dir = tmp_path / "vault" / ".relay"
    relay_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(relay_dir / "index.db"))
    try:
        conn.executescript(_PRE_B7_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    return relay_dir / "index.db"


@pytest.mark.asyncio
async def test_init_db_upgrades_a_pre_updated_by_database_in_place(pre_b7_vault):
    await database.init_db()  # must not raise

    conn = sqlite3.connect(str(pre_b7_vault))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='posts'"
            )
        }
    finally:
        conn.close()
    assert "updated_by" in columns
    assert "idx_posts_updated_by" in indexes


@pytest.mark.asyncio
async def test_init_db_is_idempotent_across_repeated_startups(pre_b7_vault):
    """The exact sequence a container restart performs — must not raise the
    second time either (a duplicate ALTER TABLE, a duplicate CREATE INDEX)."""
    await database.init_db()
    await database.init_db()
