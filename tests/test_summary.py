from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay.auth import require_api_key
from relay.config import settings
from relay.main import app
from relay.models import make_excerpt

AUTH = {"Authorization": "Bearer test-key"}


@pytest_asyncio.fixture
async def vault_dir(tmp_path, monkeypatch):
    from relay import database

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


async def _create_post(client, **kwargs) -> dict:
    payload = {"content": "original content", "title": "original title", "tags": ["a", "b"], **kwargs}
    r = await client.post("/posts", json=payload, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()


# ── make_excerpt (unit) ──────────────────────────────────────────────────────


def test_excerpt_strips_markdown_and_wikilinks():
    body = "# Heading\n\n**Bold** and *italic* with a [[Target Note|alias]] and [text](http://x)."
    out = make_excerpt(body)
    assert "#" not in out
    assert "*" not in out
    assert "[[" not in out and "]]" not in out
    assert "alias" in out and "Target Note" not in out
    assert "text" in out and "http://x" not in out


def test_excerpt_strips_frontmatter_and_code_and_embeds():
    body = "---\nid: 5\n---\n\n```py\nsecret_code()\n```\n\n![[diagram.png]] real prose here"
    out = make_excerpt(body)
    assert "secret_code" not in out
    assert "diagram.png" not in out
    assert "real prose here" in out
    assert out.startswith("real prose")


def test_excerpt_preserves_snake_case_identifiers():
    out = make_excerpt("Set `RELAY_VAULT_PATH` and the snake_case_var in the config.")
    assert "RELAY_VAULT_PATH" in out
    assert "snake_case_var" in out


def test_excerpt_handles_empty_and_none():
    assert make_excerpt("") == ""
    assert make_excerpt(None) == ""


def test_excerpt_truncates_on_word_boundary():
    body = "word " * 200
    out = make_excerpt(body, limit=240)
    assert len(out) <= 241  # 240 + ellipsis
    assert out.endswith("…")
    assert "  " not in out  # whitespace collapsed


# ── summary listing (REST) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rest_defaults_to_full_content(client):
    await _create_post(client, title="Full One", content="the whole body")
    data = (await client.get("/posts", headers=AUTH)).json()
    item = next(i for i in data["items"] if i["title"] == "Full One")
    assert item["content"] == "the whole body"
    assert "excerpt" not in item


@pytest.mark.asyncio
async def test_rest_summary_returns_excerpt_no_content(client):
    await _create_post(client, title="Sum One", content="# Title\n\nsome body text here", tags=["radio"])
    data = (await client.get("/posts?summary=true", headers=AUTH)).json()
    item = next(i for i in data["items"] if i["title"] == "Sum One")
    assert "content" not in item
    assert item["excerpt"] == "Title some body text here"
    assert item["folder"] == "Radio"
    assert item["tags"] == ["radio"]
    assert item["id"] and item["created_at"]


@pytest.mark.asyncio
async def test_summary_pins_master_as_summary(client):
    await _create_post(client, title="Regular")
    data = (await client.get("/posts?summary=true", headers=AUTH)).json()
    assert data["pinned"] is not None
    assert data["pinned"]["id"] == 0
    assert "content" not in data["pinned"]
    assert "excerpt" in data["pinned"]


# ── summary listing (in-process MCP) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_list_posts_defaults_to_summary(client):
    # write via REST, read back through the shared service the MCP tool calls
    await _create_post(client, title="Mcp One", content="body of the note", tags=["dev"])
    import aiosqlite

    from relay import service

    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        result = await service.list_posts(db, summary=True)
    item = next(i for i in result.items if i.title == "Mcp One")
    assert not hasattr(item, "content")
    assert item.excerpt == "body of the note"
    assert item.folder == "Dev"
