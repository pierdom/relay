from __future__ import annotations

import pytest
import pytest_asyncio
import aiosqlite
from httpx import AsyncClient, ASGITransport

from relay.main import app
from relay.database import get_db
from relay.auth import require_api_key

AUTH = {"Authorization": "Bearer test-key"}

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
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS tag_config (
        tag       TEXT PRIMARY KEY,
        ttl_hours INTEGER NOT NULL
    );
"""


@pytest_asyncio.fixture
async def client():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(_SCHEMA)
    await db.commit()

    async def override_get_db():
        yield db

    async def override_auth():
        return None

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_api_key] = override_auth
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    await db.close()


async def _create_post(client, **kwargs) -> dict:
    payload = {"content": "original content", "title": "original title", "tags": ["a", "b"], **kwargs}
    r = await client.post("/posts", json=payload, headers=AUTH)
    assert r.status_code == 201
    return r.json()


@pytest.mark.asyncio
async def test_partial_update_only_changes_provided_fields(client):
    post = await _create_post(client)
    r = await client.patch(f"/posts/{post['id']}", json={"content": "new content"}, headers=AUTH)
    assert r.status_code == 200
    updated = r.json()
    assert updated["content"] == "new content"
    assert updated["title"] == "original title"
    assert updated["tags"] == ["a", "b"]
    assert updated["updated_at"] is not None


@pytest.mark.asyncio
async def test_tag_replacement(client):
    post = await _create_post(client)
    r = await client.patch(f"/posts/{post['id']}", json={"tags": ["x", "y", "z"]}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["tags"] == ["x", "y", "z"]


@pytest.mark.asyncio
async def test_empty_array_clears_tags(client):
    post = await _create_post(client)
    r = await client.patch(f"/posts/{post['id']}", json={"tags": []}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["tags"] == []


@pytest.mark.asyncio
async def test_update_nonexistent_id_returns_404(client):
    r = await client.patch("/posts/99999", json={"content": "x"}, headers=AUTH)
    assert r.status_code == 404
