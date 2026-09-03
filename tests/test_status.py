"""GET /status — runtime diagnostics.

The counts matter less than the *effective feature state*: relay degrades
silently in several ways (no git → no history, no FTS5 → substring search,
watcher off → external edits unseen) and this is what makes that visible.
"""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import database, embedding, status, vault, vectors
from relay.auth import require_api_key
from relay.config import settings
from relay.embedding import FakeBackend
from relay.main import app

AUTH = {"Authorization": "Bearer test-key"}


@pytest_asyncio.fixture
async def client():
    await database.init_db()
    status.mark_started()
    app.dependency_overrides[require_api_key] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _status(client) -> dict:
    r = await client.get("/status", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_reports_version_uptime_and_the_vault_it_serves(client):
    from relay import __version__

    data = await _status(client)
    assert data["version"] == __version__
    assert data["uptime_seconds"] >= 0
    assert data["started_at"]
    # answers "which vault am I actually talking to"
    assert data["vault"]["path"] == settings.vault_path


@pytest.mark.asyncio
async def test_counts_track_the_vault(client):
    before = (await _status(client))["vault"]["posts"]
    await client.post(
        "/posts", json={"title": "Counted", "content": "x", "tags": ["homelab", "dev"]}, headers=AUTH
    )
    after = await _status(client)
    assert after["vault"]["posts"] == before + 1
    assert after["vault"]["tags"] >= 2
    assert after["vault"]["folders"] >= 1


@pytest.mark.asyncio
async def test_attachment_counts_are_reported(client):
    r = await client.post("/posts", json={"title": "Holder", "content": "x", "tags": ["homelab"]}, headers=AUTH)
    await client.post(
        "/attachments",
        json={"filename": "a.png", "data": "iVBORw0KGgo=", "post_id": r.json()["id"]},
        headers=AUTH,
    )
    v = (await _status(client))["vault"]
    assert v["attachments"] >= 1
    assert v["attachment_bytes"] > 0


# ── the point of the endpoint: silent degradation ────────────────────────────


@pytest.mark.asyncio
async def test_history_reports_effective_state_not_just_the_flag(client, monkeypatch):
    """`enabled` is intent; `effective` is whether a write would be recorded."""
    monkeypatch.setattr(settings, "history_enabled", True)
    monkeypatch.setattr("relay.history.shutil.which", lambda _n: None)  # git gone
    f = (await _status(client))["features"]["history"]
    assert f["enabled"] is True
    assert f["git"] is None
    assert f["effective"] is False, "a vault with no git must not report history as working"


@pytest.mark.asyncio
async def test_history_effective_when_git_is_present(client, monkeypatch):
    monkeypatch.setattr(settings, "history_enabled", True)
    f = (await _status(client))["features"]["history"]
    if f["git"] is None:
        pytest.skip("no git binary on this host")
    assert f["effective"] is True


@pytest.mark.asyncio
async def test_search_reports_the_like_fallback(client, monkeypatch):
    monkeypatch.setattr(database, "FTS_ENABLED", False)
    assert (await _status(client))["features"]["search"]["fts5"] is False


@pytest.mark.asyncio
async def test_watcher_and_auth_state_are_reported(client, monkeypatch):
    monkeypatch.setattr(settings, "watch_enabled", False)
    f = (await _status(client))["features"]
    assert f["watcher"]["enabled"] is False
    # mcp_oauth is true only when the flag *and* an OIDC client are configured
    monkeypatch.setattr(settings, "mcp_oauth_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", "")
    assert (await _status(client))["features"]["auth"]["mcp_oauth"] is False


# ── consistency + auth ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_and_metrics_report_the_same_counts(client):
    """They share the counting helpers precisely so these can never disagree."""
    await client.post("/posts", json={"title": "Both", "content": "x", "tags": ["homelab"]}, headers=AUTH)
    data = await _status(client)
    text = (await client.get("/metrics", headers=AUTH)).text
    metric = {
        line.split(" ")[0]: float(line.split(" ")[1])
        for line in text.splitlines()
        if line.startswith("relay_posts_total") or line.startswith("relay_tags_total")
    }
    assert metric["relay_posts_total"] == data["vault"]["posts"]
    assert metric["relay_tags_total"] == data["vault"]["tags"]


# ── embeddings section (relay #253's diagnostics) ────────────────────────────


@pytest.mark.asyncio
async def test_embeddings_status_when_disabled(client, monkeypatch):
    # vault._backfill_state is process-global — another test exercising the
    # real app lifespan (test_auth.py) may have already run a (disabled,
    # instant) backfill in this same process. Reset it so "never run yet" is
    # actually what's being asserted, not incidental test order.
    monkeypatch.setattr(
        vault,
        "_backfill_state",
        {"running": False, "checked": 0, "total": 0, "started_at": None, "completed_at": None},
    )
    data = await _status(client)
    e = data["embeddings"]
    assert e["enabled"] is False
    assert e["available"] is False
    assert e["model"] is None
    assert e["dimension"] is None
    assert e["model_size_mb"] is None
    assert e["backend_loaded"] is False
    assert e["posts_missing"] == e["posts_total"]
    # embedding_enabled is off, so nothing has chunks — every post shows up
    # (capped at 50), not just a lone offender.
    assert len(e["posts_missing_ids"]) == min(e["posts_missing"], 50)
    assert e["backfill"]["running"] is False
    assert e["backfill"]["started_at"] is None


@pytest.mark.asyncio
async def test_embeddings_status_when_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "embedding_enabled", True)
    monkeypatch.setattr(embedding, "get_backend", lambda: FakeBackend())
    monkeypatch.setattr(embedding, "_backend", FakeBackend())  # backend_loaded reads this directly

    before = await _status(client)
    posts_before = before["embeddings"]["posts_total"]

    await client.post(
        "/posts", json={"title": "Embed Me", "content": "## Section\nsome real content here", "tags": ["dev"]},
        headers=AUTH,
    )

    e = (await _status(client))["embeddings"]
    assert e["enabled"] is True
    assert e["available"] is True
    assert e["model"] == settings.embedding_model
    assert e["dimension"] == 384
    assert e["model_size_mb"] is not None and e["model_size_mb"] > 0
    assert e["backend_loaded"] is True
    assert e["posts_total"] == posts_before + 1
    assert e["posts_embedded"] >= 1
    assert e["posts_missing"] == e["posts_total"] - e["posts_embedded"]
    assert len(e["posts_missing_ids"]) == min(e["posts_missing"], 50)
    assert e["chunks_total"] >= 1
    assert e["cache_entries"] >= 1


@pytest.mark.asyncio
async def test_embeddings_status_names_a_zero_chunk_post(client, monkeypatch):
    # relay #253 usage report, Issue 1: posts_missing had no way to learn
    # *which* post. A body that's entirely a fenced code block chunks to
    # zero rows (chunking._strip_code_fences), so it's a deterministic,
    # reproducible way to land in posts_missing_ids.
    monkeypatch.setattr(settings, "embedding_enabled", True)
    monkeypatch.setattr(embedding, "get_backend", lambda: FakeBackend())

    r = await client.post(
        "/posts",
        json={"title": "Code Only", "content": "```\nrclone sync a b\n```\n", "tags": ["dev"]},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    post_id = r.json()["id"]

    e = (await _status(client))["embeddings"]
    assert post_id in e["posts_missing_ids"]


@pytest.mark.asyncio
async def test_embeddings_status_reflects_a_completed_backfill(client, monkeypatch):
    monkeypatch.setattr(settings, "embedding_enabled", True)
    monkeypatch.setattr(embedding, "get_backend", lambda: FakeBackend())

    conn = await aiosqlite.connect(settings.database_path)
    conn.row_factory = aiosqlite.Row
    await vectors.load_extension(conn)
    try:
        checked = await vault.backfill_embeddings(conn)
    finally:
        await conn.close()

    e = (await _status(client))["embeddings"]
    b = e["backfill"]
    assert b["running"] is False
    assert b["checked"] == checked
    assert b["total"] == checked
    assert b["started_at"] is not None
    assert b["completed_at"] is not None


@pytest.mark.asyncio
async def test_status_requires_auth():
    """It exposes the vault path and size — it must not be public like /health."""
    await database.init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/status")).status_code == 401
        assert (await c.get("/health")).status_code == 200  # unchanged, still public
