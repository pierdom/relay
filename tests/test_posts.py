from __future__ import annotations

import asyncio
import os
from pathlib import Path

os.environ.setdefault("API_KEY", "test-key")

import aiosqlite
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from relay import database, service, vault
from relay.auth import require_api_key
from relay.config import settings
from relay.main import app
from relay.models import PostCreate

AUTH = {"Authorization": "Bearer test-key"}


@pytest_asyncio.fixture
async def vault_dir(tmp_path, monkeypatch):
    vp = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", str(vp))
    await database.init_db()  # creates .relay/index.db + Master Document.md
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


# ── CRUD round-trip ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_get_list_update_delete_roundtrip(client):
    r = await client.post(
        "/posts", json={"title": "roundtrip", "content": "hello", "tags": ["x"]}, headers=AUTH
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    r = await client.get(f"/posts/{pid}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["content"] == "hello"

    r = await client.get("/posts", headers=AUTH)
    assert pid in [p["id"] for p in r.json()["items"]]

    r = await client.patch(f"/posts/{pid}", json={"content": "bye"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["content"] == "bye"

    r = await client.delete(f"/posts/{pid}", headers=AUTH)
    assert r.status_code in (200, 204)
    r = await client.get(f"/posts/{pid}", headers=AUTH)
    assert r.status_code == 404


# ── task C: concurrent-create id race ────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_creates_yield_distinct_ids(client):
    """Fire many creates at once: every one must get its own id and its own file —
    no two share an id, and none clobbers another (the old upsert-based path
    would let a lost MAX(id)+1 race overwrite an already-written post)."""
    n = 8
    results = await asyncio.gather(
        *(
            client.post(
                "/posts", json={"title": f"race {i}", "content": f"body {i}", "tags": ["r"]}, headers=AUTH
            )
            for i in range(n)
        )
    )
    assert all(r.status_code == 201 for r in results), [r.status_code for r in results]

    ids = [r.json()["id"] for r in results]
    assert len(set(ids)) == n, f"duplicate ids allocated: {ids}"

    # Every post is independently retrievable with its own distinct body.
    for i, pid in enumerate(ids):
        got = await client.get(f"/posts/{pid}", headers=AUTH)
        assert got.status_code == 200
    bodies = set()
    for pid in ids:
        bodies.add((await client.get(f"/posts/{pid}", headers=AUTH)).json()["content"])
    assert len(bodies) == n, "a create clobbered another post's content"

    # One .md file on disk per created post.
    files = [p for p in Path(settings.vault_path).rglob("*.md") if ".relay" not in p.parts]
    assert len([p for p in files if p.stem.startswith("race ")]) == n


class _NoLock:
    """Neutralises the process-global write_lock so concurrent create_post calls
    on separate connections actually interleave — exercising the DB-level
    BEGIN IMMEDIATE + retry, not just the in-process asyncio lock."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_concurrent_creates_separate_connections_no_clobber(vault_dir, monkeypatch):
    # Without write_lock, N create_post calls on N connections run with real
    # thread-level concurrency (aiosqlite runs each connection in its own thread).
    # The old upsert path would let two that read the same MAX(id)+1 clobber each
    # other; the immediate-txn + plain-insert + retry must keep ids distinct.
    monkeypatch.setattr(vault, "write_lock", _NoLock())
    n = 10
    dbs = [await _db() for _ in range(n)]
    try:
        posts = await asyncio.gather(
            *(
                service.create_post(dbs[i], PostCreate(title=f"conc {i}", content=f"b{i}", tags=["c"]))
                for i in range(n)
            )
        )
        ids = [p.id for p in posts]
        assert len(set(ids)) == n, f"collision under concurrency: {sorted(ids)}"
        assert len({p.content for p in posts}) == n, "a create clobbered another's content"

        # Index agrees: n distinct rows, each pointing at its own on-disk file.
        vdb = await _db()
        try:
            async with vdb.execute(
                f"SELECT id, path FROM posts WHERE id IN ({','.join('?' * n)})", ids
            ) as cur:
                rows = await cur.fetchall()
            assert len(rows) == n
            assert len({r["path"] for r in rows}) == n
            for r in rows:
                assert vault.abspath(r["path"]).exists()
        finally:
            await vdb.close()
    finally:
        for db in dbs:
            await db.close()


@pytest.mark.asyncio
async def test_index_insert_rejects_duplicate_id(vault_dir):
    """The create path uses a plain INSERT, so a colliding id raises instead of
    silently overwriting via ON CONFLICT DO UPDATE."""
    db = await _db()
    try:
        kw = dict(
            content="c", tags=["t"], source=None,
            created_at=vault.utcnow_iso(), updated_at=None, expires_at=None,
        )
        vp = Path(settings.vault_path)
        await vault.index_insert(db, id=555, title="first", path=vp / "Inbox/first.md", **kw)
        await db.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await vault.index_insert(db, id=555, title="second", path=vp / "Inbox/second.md", **kw)
        await db.rollback()
    finally:
        await db.close()


# ── Ordering (sort/order params) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_sort_and_order(client, monkeypatch):
    # Drive an increasing clock so timestamps are distinct (utcnow_iso is
    # second-resolution, and the three creates otherwise land in the same second).
    clock = {"n": 0}

    def fake_now() -> str:
        clock["n"] += 1
        return f"2026-01-01T00:00:{clock['n']:02d}Z"

    monkeypatch.setattr(vault, "utcnow_iso", fake_now)

    # three posts, created A → B → C
    ids = []
    for t in ("A", "B", "C"):
        r = await client.post("/posts", json={"title": t, "content": t, "tags": ["z"]}, headers=AUTH)
        ids.append(r.json()["id"])
    a, b, c = ids

    # edit A last so its updated_at is newest
    r = await client.patch(f"/posts/{a}", json={"content": "A2"}, headers=AUTH)
    assert r.status_code == 200, r.text

    async def titles(**params):
        r = await client.get("/posts", params={"tag": "z", **params}, headers=AUTH)
        assert r.status_code == 200, r.text
        return [p["title"] for p in r.json()["items"]]

    # default: updated desc → A (just edited) first, then C, B
    assert await titles() == ["A", "C", "B"]
    # updated asc → reverse
    assert await titles(order="asc") == ["B", "C", "A"]
    # created desc → newest-created first, edits ignored
    assert await titles(sort="created") == ["C", "B", "A"]
    # created asc → creation order
    assert await titles(sort="created", order="asc") == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_list_rejects_bad_sort_params(client):
    assert (await client.get("/posts", params={"sort": "bogus"}, headers=AUTH)).status_code == 422
    assert (await client.get("/posts", params={"order": "sideways"}, headers=AUTH)).status_code == 422
