"""POST /embeddings/backfill and PATCH /embeddings — runtime control over
semantic search without a restart (relay #253, v1.3.0).
"""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import database, embedding, service, status, vault, vectors
from relay.auth import require_api_key
from relay.config import settings
from relay.embedding import FakeBackend
from relay.main import app
from relay.models import PostCreate

AUTH = {"Authorization": "Bearer test-key"}

LONG_SECTION = "content word " * 60  # safely over chunking's 50-word runt floor


@pytest_asyncio.fixture
async def client(monkeypatch):
    await database.init_db()
    status.mark_started()
    # A clean, non-running snapshot per test — vault._backfill_state is
    # process-global and other tests (real app lifespan, direct backfill
    # calls) can leave it pointing at a prior run otherwise.
    monkeypatch.setattr(
        vault,
        "_backfill_state",
        {"running": False, "checked": 0, "total": 0, "started_at": None, "completed_at": None},
    )
    app.dependency_overrides[require_api_key] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def enabled(client, monkeypatch):
    """embedding_enabled=True with a fake backend already resolving to the
    real default model's dim (384) — the schema init_vec would have built at
    startup with embedding_enabled=False anyway, so this matches what a real
    process's `embedding_state`/`vec_chunks` looks like pre-enable."""
    monkeypatch.setattr(settings, "embedding_enabled", True)
    monkeypatch.setattr(embedding, "get_backend", lambda: FakeBackend())
    return client


async def _db():
    conn = await aiosqlite.connect(settings.database_path)
    conn.row_factory = aiosqlite.Row
    await vectors.load_extension(conn)
    return conn


# ── PATCH /embeddings ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_enables_and_triggers_a_backfill(enabled):
    r = await enabled.patch("/embeddings", json={"enabled": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["backfill"]["running"] is True


@pytest.mark.asyncio
async def test_patch_disables_and_force_unloads(client, monkeypatch):
    monkeypatch.setattr(settings, "embedding_enabled", True)
    monkeypatch.setattr(embedding, "_backend", object())
    calls: list[bool] = []
    monkeypatch.setattr(embedding, "force_unload", lambda: calls.append(True) or True)

    r = await client.patch("/embeddings", json={"enabled": False}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False
    assert settings.embedding_enabled is False
    assert calls == [True]


@pytest.mark.asyncio
async def test_patch_enable_rejects_when_vec_unavailable(client, monkeypatch):
    monkeypatch.setattr(database, "VEC_ENABLED", False)
    r = await client.patch("/embeddings", json={"enabled": True}, headers=AUTH)
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_patch_enable_rejects_an_unresolvable_model(client, monkeypatch):
    monkeypatch.setattr(settings, "embedding_model", "not-a-real-model")
    r = await client.patch("/embeddings", json={"enabled": True}, headers=AUTH)
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_patch_enable_rejects_a_dimension_mismatch(client, monkeypatch):
    monkeypatch.setattr(embedding, "resolve_dim", lambda model_id: 999)
    r = await client.patch("/embeddings", json={"enabled": True}, headers=AUTH)
    assert r.status_code == 409
    # Rejected before flipping the flag — a failed enable must not leave the
    # process claiming embeddings are on when the schema can't back it.
    assert settings.embedding_enabled is False


# ── POST /embeddings/backfill ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_trigger_requires_embeddings_enabled(client):
    r = await client.post("/embeddings/backfill", headers=AUTH)
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_backfill_trigger_marks_running_immediately(enabled):
    r = await enabled.post("/embeddings/backfill", headers=AUTH)
    assert r.status_code == 202, r.text
    assert r.json()["backfill"]["running"] is True


@pytest.mark.asyncio
async def test_backfill_trigger_conflicts_while_already_running(enabled, monkeypatch):
    monkeypatch.setitem(vault._backfill_state, "running", True)
    r = await enabled.post("/embeddings/backfill", headers=AUTH)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_backfill_force_wipes_before_retriggering(enabled):
    conn = await _db()
    try:
        post = await service.create_post(
            conn, PostCreate(title="Post A", content=f"## Section\n{LONG_SECTION}", tags=["dev"])
        )
        posts_with_chunks, chunks_total, cache_entries = await vectors.coverage(conn)
        assert (posts_with_chunks, chunks_total, cache_entries) == (1, 1, 1)

        r = await enabled.post("/embeddings/backfill?force=true", headers=AUTH)
        assert r.status_code == 202, r.text
        body = r.json()
        # vectors.reset() runs synchronously before the response is built —
        # the re-embed itself is what's backgrounded, not the wipe.
        assert body["chunks_total"] == 0
        assert body["cache_entries"] == 0
        assert body["posts_embedded"] == 0

        posts_with_chunks, chunks_total, cache_entries = await vectors.coverage(conn)
        assert (posts_with_chunks, chunks_total, cache_entries) == (0, 0, 0)
        assert post.id  # keeps the reference alive/used
    finally:
        await conn.close()
