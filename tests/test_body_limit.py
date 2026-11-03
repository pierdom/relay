"""REST request bodies are capped (`relay.body_limit`): MCP had a 4 MiB cap,
REST read any body whole into memory."""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import base64

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import body_limit, database
from relay.config import settings
from relay.main import app

AUTH = {"Authorization": "Bearer test-key"}
TOO_BIG = body_limit.MAX_BODY_BYTES + 1


@pytest_asyncio.fixture
async def client():
    await database.init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_an_oversized_body_is_refused_before_it_is_read(client):
    r = await client.post("/posts", content=b"x" * TOO_BIG, headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 413
    assert "larger than" in r.json()["detail"]


@pytest.mark.asyncio
async def test_an_oversized_chunked_body_is_refused_as_it_arrives(client):
    async def chunks():
        for _ in range(TOO_BIG // 65536 + 1):
            yield b"x" * 65536

    r = await client.post("/posts", content=chunks(), headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_an_ordinary_body_passes(client):
    r = await client.post("/posts", json={"title": "Fine", "content": "y" * 100_000}, headers=AUTH)
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_an_inline_attachment_may_use_the_attachment_cap(client, monkeypatch):
    monkeypatch.setattr(settings, "attachment_max_mb", 8)
    data = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * (5 * 1024 * 1024)).decode()
    r = await client.post("/attachments", json={"filename": "big.png", "data": data}, headers=AUTH)
    assert r.status_code == 201, r.text


def test_the_mcp_transport_and_the_streamed_upload_keep_their_own_caps():
    assert body_limit.limit_for("POST", "/mcp") is None
    assert body_limit.limit_for("PUT", "/attachments/uploads/abc") is None
    assert body_limit.limit_for("PATCH", "/posts/1") == body_limit.MAX_BODY_BYTES
