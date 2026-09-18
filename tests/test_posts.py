from __future__ import annotations

import asyncio
import os
from pathlib import Path

os.environ.setdefault("API_KEY", "test-key")

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import database, service, vault
from relay.auth import require_api_key
from relay.config import settings
from relay.main import app
from relay.models import PostCreate, PostUpdate

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


# ── updated_by (relay #198, B-7) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_updated_by_is_set_on_create_and_overwritten_on_update(vault_dir):
    from relay.identity import Actor

    db = await _db()
    try:
        agent = Actor(name="news-agent", email="news-agent@relay.local")
        human = Actor(name="me@example.com", email="me@example.com")

        created = await service.create_post(
            db, PostCreate(title="Attributed", content="one", tags=[]), actor=agent
        )
        assert created.updated_by == "news-agent"
        # The file itself carries it — a 7th reserved front-matter key, not
        # just an index-only field.
        row = await _fetch_row(db, created.id)
        text = vault.abspath(row["path"]).read_text(encoding="utf-8")
        assert "updated_by: news-agent" in text

        updated = await service.update_post(
            db, created.id, PostUpdate(content="two"), actor=human
        )
        assert updated.updated_by == "me@example.com"

        # A caller that doesn't thread identity leaves attribution alone
        # rather than clearing it — the safe default for call sites relay
        # hasn't wired yet.
        unattributed = await service.update_post(db, created.id, PostUpdate(content="three"))
        assert unattributed.updated_by == "me@example.com"
    finally:
        await db.close()


async def _fetch_row(db, post_id: int):
    async with db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)) as cur:
        return await cur.fetchone()


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
    for pid in ids:
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
        kw = {
            "content": "c", "tags": ["t"], "source": None,
            "created_at": vault.utcnow_iso(), "updated_at": None, "expires_at": None,
        }
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


# ── input validation ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_post_missing_title_is_422(client):
    r = await client.post("/posts", json={"content": "body"}, headers=AUTH)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_post_blank_title_is_422(client):
    r = await client.post("/posts", json={"title": "   ", "content": "body"}, headers=AUTH)
    assert r.status_code == 422


# ── /posts/deleted when history is disabled ───────────────────────────────────


@pytest.mark.asyncio
async def test_list_deleted_posts_is_503_when_history_disabled(client):
    # conftest sets history_enabled=False; the route must surface that as 503
    r = await client.get("/posts/deleted", headers=AUTH)
    assert r.status_code == 503


# ── advisory duplicate-guard + related posts (relay #198, N-7) ─────────────


@pytest_asyncio.fixture
async def fake_embeddings(monkeypatch):
    from relay import embedding
    from relay.config import settings as _settings

    assert database.VEC_ENABLED, "sqlite-vec should load in this dev/CI environment"
    monkeypatch.setattr(_settings, "embedding_enabled", True)
    monkeypatch.setattr(embedding, "get_backend", lambda: embedding.FakeBackend())


@pytest.mark.asyncio
async def test_create_post_similar_field_is_empty_without_embeddings(client):
    r = await client.post("/posts", json={"title": "Alpha", "content": "hello", "tags": ["dev"]}, headers=AUTH)
    assert r.status_code == 201, r.text
    assert r.json()["similar"] == []


@pytest.mark.asyncio
async def test_create_post_surfaces_a_similar_existing_post(client, fake_embeddings, monkeypatch):
    from relay import vectors

    first = (await client.post("/posts", json={"title": "Alpha", "content": "x", "tags": ["dev"]}, headers=AUTH)).json()

    # The similarity lookup itself is stubbed (already covered end to end in
    # tests/test_vectors.py, including the self-exclusion FakeBackend can't
    # exercise across differently-titled posts) — this test is only about
    # create_post wiring the result into its response as `similar`.
    async def fake_similar(db, *, exclude_post_id, title, content, **kwargs):
        assert exclude_post_id != first["id"]  # the new (not-yet-known) post's own id
        return [(first["id"], 0.2)]

    monkeypatch.setattr(vectors, "find_similar_posts", fake_similar)

    r = await client.post("/posts", json={"title": "Alpha Two", "content": "x", "tags": ["dev"]}, headers=AUTH)
    assert r.status_code == 201, r.text
    similar = r.json()["similar"]
    assert similar == [{"id": first["id"], "title": "Alpha", "score": vectors.similarity_score(0.2)}]


@pytest.mark.asyncio
async def test_related_404s_for_a_missing_post(client, fake_embeddings):
    r = await client.get("/posts/999999/related", headers=AUTH)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_related_is_503_without_embeddings(client):
    r = await client.post("/posts", json={"title": "Alpha", "content": "hello", "tags": ["dev"]}, headers=AUTH)
    pid = r.json()["id"]
    r = await client.get(f"/posts/{pid}/related", headers=AUTH)
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_related_excludes_an_already_linked_post(client, fake_embeddings, monkeypatch):
    a = (await client.post("/posts", json={"title": "Alpha", "content": "x", "tags": ["dev"]}, headers=AUTH)).json()
    # B cross-links A explicitly — already made the relationship, so it must
    # not also show up as an unlinked "related" suggestion. The similarity
    # lookup itself is stubbed (already covered end to end in
    # tests/test_vectors.py) so this test is only about the link-exclusion
    # logic in service.get_related, not FakeBackend's hash behavior.
    b = (await client.post(
        "/posts", json={"title": "Alpha Linked", "content": "[[Alpha]]\n\nx", "tags": ["dev"]}, headers=AUTH
    )).json()

    from relay import vectors

    async def fake_similar(db, *, exclude_post_id, title, content, **kwargs):
        return [(a["id"], 0.1)]

    monkeypatch.setattr(vectors, "find_similar_posts", fake_similar)

    r = await client.get(f"/posts/{b['id']}/related", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_related_surfaces_an_unlinked_similar_post(client, fake_embeddings, monkeypatch):
    a = (await client.post("/posts", json={"title": "Alpha", "content": "x", "tags": ["dev"]}, headers=AUTH)).json()
    b = (await client.post("/posts", json={"title": "Beta", "content": "y", "tags": ["dev"]}, headers=AUTH)).json()

    from relay import vectors

    async def fake_similar(db, *, exclude_post_id, title, content, **kwargs):
        return [(a["id"], 0.1)]

    monkeypatch.setattr(vectors, "find_similar_posts", fake_similar)

    r = await client.get(f"/posts/{b['id']}/related", headers=AUTH)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [item["id"] for item in items] == [a["id"]]
    assert items[0]["score"] == vectors.similarity_score(0.1)
