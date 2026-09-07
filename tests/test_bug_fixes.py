"""Regression tests for the bug-fix audit PR (AUDIT.md, Phase 2).

Each test pins one fixed behaviour and was written to fail before its fix:
garbage `expires_at` is rejected instead of swept (B-01), an embedding
failure never fails a write (B-02), a duplicated external note gets a fresh id
(B-03), dot-directories are not indexed (B-04), `""` clears a field over MCP
(B-05), orphan chunks are pruned at startup (B-06), embedding sync no longer
commits mid-transaction (B-07), empty tags are rejected (B-08), empty filters
mean "no filter" (B-14), a blank sha cannot restore (B-15), SSE queues are
bounded (S-06), and a ranked search degrades to keyword on backend failure.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("API_KEY", "test-key")

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import cleanup, database, embedding, events, vault, vectors, watcher
from relay.auth import require_api_key
from relay.config import settings
from relay.main import app
from relay.mcp_server import restore_post as mcp_restore_post
from relay.mcp_server import update_post as mcp_update_post

AUTH = {"Authorization": "Bearer test-key"}


@pytest_asyncio.fixture
async def vault_dir(tmp_path, monkeypatch):
    vp = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", str(vp))
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


@pytest_asyncio.fixture
async def fake_embeddings(monkeypatch):
    assert database.VEC_ENABLED, "sqlite-vec should load in this dev/CI environment"
    monkeypatch.setattr(settings, "embedding_enabled", True)
    monkeypatch.setattr(embedding, "get_backend", lambda: embedding.FakeBackend())


async def _db():
    db = await aiosqlite.connect(settings.database_path)
    db.row_factory = aiosqlite.Row
    if database.VEC_ENABLED:
        await vectors.load_extension(db)
    return db


async def _create(client, **fields) -> dict:
    r = await client.post("/posts", json={"title": "t", "content": "body", **fields}, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()


# ── B-01: expires_at is validated and normalised, never compared as garbage ──


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["1 week", "12h", "tomorrow", "1725000000"])
async def test_non_iso_expires_at_is_rejected(client, bad):
    r = await client.post("/posts", json={"title": "ttl", "content": "b", "expires_at": bad}, headers=AUTH)
    assert r.status_code == 422, r.text
    r = await client.post("/tags/x/config", json={"expires_at": bad}, headers=AUTH)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_iso_expires_at_is_normalised_to_utc_z(client):
    post = await _create(client, expires_at="2999-06-30T02:00:00+02:00")
    assert post["expires_at"] == "2999-06-30T00:00:00Z"
    post = await _create(client, expires_at="2999-06-30")
    assert post["expires_at"] == "2999-06-30T00:00:00Z"
    r = await client.patch(f"/posts/{post['id']}", json={"expires_at": "2999-07-01T00:00:00Z"}, headers=AUTH)
    assert r.json()["expires_at"] == "2999-07-01T00:00:00Z"


@pytest.mark.asyncio
async def test_cleanup_ignores_a_hand_edited_non_iso_expiry(client, vault_dir):
    """Front-matter can still carry anything (Obsidian edits); the sweep must
    skip it with a warning rather than compare it lexically."""
    post = await _create(client)
    path = vault_dir / "Inbox" / "t.md"
    path.write_text(path.read_text(encoding="utf-8").replace("---\n\n", "expires_at: 1 week\n---\n\n", 1))
    db = await _db()
    try:
        await vault.rebuild_index(db)
        assert await cleanup._delete_expired(db) == 0
    finally:
        await db.close()
    assert (await client.get(f"/posts/{post['id']}", headers=AUTH)).status_code == 200


# ── B-02: embedding failure never fails a write ─────────────────────────────


@pytest.mark.asyncio
async def test_embedding_backend_failure_does_not_fail_the_write(client, monkeypatch, caplog):
    assert database.VEC_ENABLED
    monkeypatch.setattr(settings, "embedding_enabled", True)

    def boom():
        raise RuntimeError("model download failed")

    monkeypatch.setattr(embedding, "get_backend", boom)
    post = await _create(client)
    r = await client.patch(f"/posts/{post['id']}", json={"content": "edited"}, headers=AUTH)
    assert r.status_code == 200
    assert (await client.get(f"/posts/{post['id']}", headers=AUTH)).json()["content"] == "edited"
    assert "Embedding sync skipped" in caplog.text


@pytest.mark.asyncio
async def test_backfill_skips_a_failing_post_and_continues(client, monkeypatch):
    await _create(client, title="a")
    await _create(client, title="b")
    monkeypatch.setattr(settings, "embedding_enabled", True)
    calls: list[int] = []

    async def flaky(db, *, post_id, title, content):
        calls.append(post_id)
        if post_id == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(vectors, "sync_post_chunks", flaky)
    db = await _db()
    try:
        assert await vault.backfill_embeddings(db) == 3  # master + 2
    finally:
        await db.close()
    assert sorted(calls) == [0, 1, 2]
    assert vault.backfill_status()["running"] is False


# ── B-03: an external duplicate does not steal the original's id ─────────────


@pytest.mark.asyncio
async def test_external_copy_with_a_taken_id_gets_a_fresh_id(client, vault_dir):
    post = await _create(client, title="Orig", content="original body", tags=["homelab"])
    src = vault_dir / "Homelab" / "Orig.md"
    dup = vault_dir / "Homelab" / "Orig copy.md"
    dup.write_text(src.read_text(encoding="utf-8").replace("original body", "copied body"), encoding="utf-8")
    await watcher._reconcile([str(dup)])
    orig = (await client.get(f"/posts/{post['id']}", headers=AUTH)).json()
    assert (orig["title"], orig["content"]) == ("Orig", "original body")
    copies = (await client.get("/posts", params={"search": "copied"}, headers=AUTH)).json()["items"]
    assert len(copies) == 1 and copies[0]["id"] != post["id"]
    assert f"id: {copies[0]['id']}" in dup.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_external_rename_is_not_treated_as_a_collision(client, vault_dir):
    post = await _create(client, title="Before", tags=["homelab"])
    src = vault_dir / "Homelab" / "Before.md"
    dst = vault_dir / "Homelab" / "After.md"
    src.rename(dst)
    await watcher._reconcile([str(src), str(dst)])
    got = (await client.get(f"/posts/{post['id']}", headers=AUTH)).json()
    assert got["title"] == "After"


# ── B-04: dot-directories are bookkeeping, not vault content ────────────────


@pytest.mark.asyncio
async def test_notes_under_dot_directories_are_not_indexed(tmp_path, monkeypatch):
    vp = tmp_path / "vault2"
    for hidden in (".trash", ".obsidian/plugins/x"):
        (vp / hidden).mkdir(parents=True)
        (vp / hidden / "Old.md").write_text(
            "---\nid: 7\ntags: []\ncreated_at: 2026-01-01T00:00:00Z\n---\n\ntrashed\n", encoding="utf-8"
        )
    (vp / "Homelab").mkdir()
    (vp / "Homelab" / "Live.md").write_text(
        "---\nid: 7\ntags: [homelab]\ncreated_at: 2026-01-02T00:00:00Z\n---\n\nlive\n", encoding="utf-8"
    )
    monkeypatch.setattr(settings, "vault_path", str(vp))
    await database.init_db()
    db = await _db()
    try:
        async with db.execute("SELECT id, path FROM posts WHERE id != 0") as cur:
            rows = [tuple(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    assert rows == [(7, "Homelab/Live.md")]
    assert not watcher._Handler(asyncio.get_running_loop())._relevant(str(vp / ".trash" / "Old.md"))


# ── B-05: "" clears expires_at / source over the in-process MCP server ───────


@pytest.mark.asyncio
async def test_mcp_update_clears_expiry_and_source_with_empty_string(client):
    post = await _create(client, expires_at="2999-01-01T00:00:00Z", source="agent")
    out = await mcp_update_post(id=post["id"], expires_at="", source="")
    assert out["expires_at"] is None and out["source"] is None
    out = await mcp_update_post(id=post["id"], content="still here")
    assert out["expires_at"] is None and out["content"] == "still here"


# ── B-06: chunks for posts deleted while relay was down are pruned ──────────


@pytest.mark.asyncio
async def test_rebuild_prunes_chunks_of_vanished_posts(client, fake_embeddings, vault_dir):
    post = await _create(client, content="some body text here")
    (vault_dir / "Inbox" / "t.md").unlink()  # deleted while relay is "down"
    await database.init_db()  # restart
    db = await _db()
    try:
        async with db.execute("SELECT COUNT(*) FROM chunks WHERE post_id = ?", (post["id"],)) as cur:
            assert (await cur.fetchone())[0] == 0
        posts_with_chunks, _chunks, _cache = await vectors.coverage(db)
        async with db.execute("SELECT COUNT(*) FROM posts") as cur:
            assert posts_with_chunks <= (await cur.fetchone())[0]
    finally:
        await db.close()


# ── B-07: embedding sync does not commit inside the caller's transaction ─────


@pytest.mark.asyncio
async def test_embedding_sync_does_not_commit_on_its_own(client, fake_embeddings, monkeypatch):
    commits: list[int] = []
    orig = aiosqlite.Connection.commit

    async def spy(self):
        commits.append(1)
        return await orig(self)

    monkeypatch.setattr(aiosqlite.Connection, "commit", spy)
    await _create(client)
    assert len(commits) == 1


# ── B-08: an empty tag name is rejected, never stored ───────────────────────


@pytest.mark.asyncio
async def test_empty_tag_config_is_rejected(client):
    r = await client.post("/tags/%21%21/config", json={"ttl_hours": 1}, headers=AUTH)
    assert r.status_code == 422
    assert all(t["tag"] for t in (await client.get("/tags", headers=AUTH)).json()["tags"])
    r = await client.patch("/tags/%21%21", json={"new_name": "x"}, headers=AUTH)
    assert r.status_code == 422


# ── B-14: empty filter values mean "no filter" ──────────────────────────────


@pytest.mark.asyncio
async def test_empty_search_and_tag_still_pin_the_master_doc(client):
    await _create(client)
    for params in ({"search": ""}, {"tag": ""}, {"folder": ""}):
        body = (await client.get("/posts", params=params, headers=AUTH)).json()
        assert body["pinned"] is not None and body["pinned"]["id"] == 0, params
        assert body["total"] == 1


# ── B-15: a blank sha cannot restore anything ───────────────────────────────


@pytest.mark.asyncio
async def test_blank_sha_does_not_match_the_newest_revision(client, monkeypatch):
    monkeypatch.setattr(settings, "history_enabled", True)
    from relay import history

    history.reset_state_for_tests()
    post = await _create(client)
    out = await mcp_restore_post(id=post["id"], sha="")
    assert "error" in out


# ── S-06: SSE subscriber queues are bounded ─────────────────────────────────


@pytest.mark.asyncio
async def test_slow_sse_subscriber_gets_an_overflow_marker_not_unbounded_growth():
    events._subscribers.clear()
    q = events.subscribe(None)
    try:
        for i in range(events.QUEUE_MAXSIZE + 50):
            await events.publish({"id": i, "tags": []})
        assert q.qsize() == events.QUEUE_MAXSIZE
        items = [q.get_nowait() for _ in range(q.qsize())]
        assert items[-1] is events.OVERFLOW
    finally:
        events.unsubscribe(q, None)


# ── semantic/hybrid degrade to keyword when the backend fails at query time ──


@pytest.mark.asyncio
async def test_ranked_search_degrades_to_keyword_when_the_backend_fails(client, fake_embeddings, monkeypatch):
    await _create(client, title="Docker services", content="containers on the homelab")

    async def broken(*args, **kwargs):
        raise RuntimeError("model download failed")

    monkeypatch.setattr(vectors, "semantic_search", broken)
    for mode in ("hybrid", "semantic"):
        r = await client.get("/posts", params={"search": "docker", "mode": mode}, headers=AUTH)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["search_timing"]["degraded"] is True
        assert [p["title"] for p in body["items"]] == ["Docker services"]
    # Configured off stays a loud 503 — that is a config state, not a failure.
    monkeypatch.setattr(settings, "embedding_enabled", False)
    r = await client.get("/posts", params={"search": "docker", "mode": "hybrid"}, headers=AUTH)
    assert r.status_code == 503
