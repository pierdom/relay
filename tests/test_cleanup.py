"""TTL cleanup — the one path that *deletes* posts. These pin the expiry
precedence (per-post `expires_at` > per-tag config > global), shortest-TTL-wins
on multi-tag posts, the `id=0` master-doc exemption, and that expiry removes the
canonical file **and** its index row."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

os.environ.setdefault("API_KEY", "test-key")

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import cleanup, database, vault
from relay.auth import require_api_key
from relay.config import settings
from relay.main import app

AUTH = {"Authorization": "Bearer test-key"}


def _iso(hours_from_now: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours_from_now)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@pytest_asyncio.fixture
async def vault_dir(tmp_path, monkeypatch):
    vp = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", str(vp))
    monkeypatch.setattr(settings, "default_ttl_hours", 0)  # off unless a test opts in
    await database.init_db()
    return vp


@pytest_asyncio.fixture
async def client(vault_dir):
    async def override_auth():
        return None

    app.dependency_overrides[require_api_key] = override_auth
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(settings.database_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=5000;")
    return db


async def _create(client, *, tags=None) -> dict:
    r = await client.post(
        "/posts", json={"title": f"post {os.urandom(4).hex()}", "content": "body", "tags": tags or []},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _row(db, pid) -> aiosqlite.Row | None:
    async with db.execute("SELECT * FROM posts WHERE id = ?", (pid,)) as cur:
        return await cur.fetchone()


async def _file_exists(db, pid) -> bool:
    row = await _row(db, pid)
    return row is not None and vault.abspath(row["path"]).exists()


# ── explicit per-post expiry ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expired_post_removes_file_and_index_row(client):
    post = await _create(client)
    db = await _db()
    try:
        assert await _file_exists(db, post["id"])
        path = vault.abspath((await _row(db, post["id"]))["path"])
        await db.execute("UPDATE posts SET expires_at = ? WHERE id = ?", (_iso(-1), post["id"]))
        await db.commit()

        assert await cleanup._delete_expired(db) == 1
        assert await _row(db, post["id"]) is None      # index row gone
        assert not path.exists()                        # canonical file gone
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_future_expiry_survives(client):
    post = await _create(client)
    db = await _db()
    try:
        await db.execute("UPDATE posts SET expires_at = ? WHERE id = ?", (_iso(+24), post["id"]))
        await db.commit()
        assert await cleanup._delete_expired(db) == 0
        assert await _row(db, post["id"]) is not None
    finally:
        await db.close()


# ── master-doc exemption ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_master_doc_exempt_even_when_expired(client):
    db = await _db()
    try:
        # Force an expired stamp on id=0; cleanup must still leave it alone.
        await db.execute("UPDATE posts SET expires_at = ? WHERE id = 0", (_iso(-100),))
        await db.commit()
        assert await cleanup._delete_expired(db) == 0
        assert await _row(db, 0) is not None
        assert await _file_exists(db, 0)
    finally:
        await db.close()


# ── per-tag TTL + precedence ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_per_tag_ttl_deletes_after_window(client):
    post = await _create(client, tags=["ephemeral"])
    db = await _db()
    try:
        await db.execute(
            "INSERT INTO tag_config (tag, ttl_hours) VALUES ('ephemeral', 1)"
        )
        await db.execute("UPDATE posts SET created_at = ? WHERE id = ?", (_iso(-2), post["id"]))
        await db.commit()
        assert await cleanup._delete_expired(db) == 1
        assert await _row(db, post["id"]) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_multi_tag_shortest_ttl_wins(client):
    # Two tags on one post — a short (1h) and a long (100h) TTL. Aged 2h, the
    # short window has elapsed, so the post goes; a sibling carrying only the
    # long tag at the same age stays.
    both = await _create(client, tags=["short", "long"])
    long_only = await _create(client, tags=["long"])
    db = await _db()
    try:
        await db.execute("INSERT INTO tag_config (tag, ttl_hours) VALUES ('short', 1)")
        await db.execute("INSERT INTO tag_config (tag, ttl_hours) VALUES ('long', 100)")
        await db.execute("UPDATE posts SET created_at = ? WHERE id IN (?, ?)",
                         (_iso(-2), both["id"], long_only["id"]))
        await db.commit()
        assert await cleanup._delete_expired(db) == 1
        assert await _row(db, both["id"]) is None
        assert await _row(db, long_only["id"]) is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_per_post_future_expiry_overrides_tag_ttl(client):
    # Per-post expiry outranks the tag TTL: an explicit future `expires_at`
    # shields a post the tag rule (aged past its window) would otherwise delete.
    post = await _create(client, tags=["short"])
    db = await _db()
    try:
        await db.execute("INSERT INTO tag_config (tag, ttl_hours) VALUES ('short', 1)")
        await db.execute(
            "UPDATE posts SET created_at = ?, expires_at = ? WHERE id = ?",
            (_iso(-2), _iso(+24), post["id"]),
        )
        await db.commit()
        assert await cleanup._delete_expired(db) == 0
        assert await _row(db, post["id"]) is not None
    finally:
        await db.close()


# ── global TTL ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_global_ttl_deletes_untagged_but_not_configured_tag(client, monkeypatch):
    monkeypatch.setattr(settings, "default_ttl_hours", 1)
    plain = await _create(client, tags=["misc"])       # no tag_config → global applies
    kept = await _create(client, tags=["keep"])        # configured tag → global skips it
    db = await _db()
    try:
        # `keep` has a long TTL config, so global must not touch it.
        await db.execute("INSERT INTO tag_config (tag, ttl_hours) VALUES ('keep', 1000)")
        await db.execute("UPDATE posts SET created_at = ? WHERE id IN (?, ?)",
                         (_iso(-2), plain["id"], kept["id"]))
        await db.commit()
        assert await cleanup._delete_expired(db) == 1
        assert await _row(db, plain["id"]) is None
        assert await _row(db, kept["id"]) is not None
    finally:
        await db.close()


# ── live clients are told about expiries ─────────────────────────────────────


@pytest.mark.asyncio
async def test_expiry_publishes_sse_delete(client):
    """A TTL'd post must stream a `delete` to live subscribers.

    The file unlink is self-delete-suppressed, so the watcher never emits for an
    expiry — without this the post lingers in every connected UI/TUI until reload.
    """
    from relay import events

    post = await _create(client, tags=["news"])
    q = events.subscribe(None)
    db = await _db()
    try:
        await db.execute("UPDATE posts SET expires_at = ? WHERE id = ?", (_iso(-1), post["id"]))
        await db.commit()
        assert await cleanup._delete_expired(db) == 1

        assert not q.empty(), "expiry published no SSE event"
        event = q.get_nowait()
        assert event["type"] == "delete"
        assert event["id"] == post["id"]
        assert event["data"] == {"id": post["id"]}
        assert event["tags"] == ["news"]
    finally:
        events.unsubscribe(q, None)
        await db.close()


@pytest.mark.asyncio
async def test_expiry_delete_reaches_tag_filtered_subscriber(client):
    from relay import events

    post = await _create(client, tags=["news"])
    matching = events.subscribe("news")
    other = events.subscribe("homelab")
    db = await _db()
    try:
        await db.execute("UPDATE posts SET expires_at = ? WHERE id = ?", (_iso(-1), post["id"]))
        await db.commit()
        await cleanup._delete_expired(db)
        assert matching.get_nowait()["id"] == post["id"]
        assert other.empty()
    finally:
        events.unsubscribe(matching, "news")
        events.unsubscribe(other, "homelab")
        await db.close()
