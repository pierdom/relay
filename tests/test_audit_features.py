"""Feature gaps closed by the audit (AUDIT.md G-01, G-02, G-03).

Each was an asymmetry a daily user hits: REST accepted something MCP did not,
or a thing could be created but never removed.
"""
from __future__ import annotations

import base64
import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
import yaml
from httpx import ASGITransport, AsyncClient

from relay import database
from relay.auth import require_api_key
from relay.config import settings
from relay.main import app
from relay.mcp_server import add_attachment as mcp_add_attachment
from relay.mcp_server import list_folders as mcp_list_folders
from relay.mcp_server import list_tags as mcp_list_tags
from relay.mcp_server import set_tag_config as mcp_set_tag_config

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


# ── G-01: MCP add_attachment accepts tags and embed like REST ───────────────


@pytest.mark.asyncio
async def test_mcp_add_attachment_can_file_by_tags_and_skip_the_embed(client, vault_dir):
    data = base64.b64encode(b"\x89PNG").decode()
    out = await mcp_add_attachment(filename="chart.png", data=data, tags=["finance"])
    assert out["folder"] == "Finance"
    assert (vault_dir / "Finance" / "assets" / "chart.png").exists()

    r = await client.post("/posts", json={"title": "p", "content": "body", "tags": ["homelab"]}, headers=AUTH)
    pid = r.json()["id"]
    out = await mcp_add_attachment(filename="side.png", data=data, post_id=pid, embed=False)
    assert out["folder"] == "Homelab" and out["post_id"] is None
    assert (await client.get(f"/posts/{pid}", headers=AUTH)).json()["content"] == "body"


# ── G-02: MCP can discover folder names, as REST GET /folders can ───────────


@pytest.mark.asyncio
async def test_mcp_list_folders_matches_rest(client):
    for tags in (["homelab"], ["homelab"], ["finance"]):
        await client.post("/posts", json={"title": "p", "content": "b", "tags": tags}, headers=AUTH)
    out = await mcp_list_folders()
    assert out == (await client.get("/folders", headers=AUTH)).json()
    assert out["folders"] == [{"folder": "Finance", "count": 1}, {"folder": "Homelab", "count": 2}]


# ── G-03: a tag's expiry configuration can be removed, not just set ──────────


@pytest.mark.asyncio
async def test_tag_config_with_neither_field_removes_it(client, vault_dir):
    r = await client.post("/tags/digest/config", json={"ttl_hours": 24}, headers=AUTH)
    assert r.status_code == 200
    assert {"tag": "digest", "count": 0, "ttl_hours": 24, "expires_at": None} in (
        await client.get("/tags", headers=AUTH)
    ).json()["tags"]
    assert "digest" in yaml.safe_load((vault_dir / ".relay" / "tags.yml").read_text(encoding="utf-8"))

    r = await client.post("/tags/digest/config", json={}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"tag": "digest", "ttl_hours": None, "expires_at": None}
    assert all(t["tag"] != "digest" for t in (await client.get("/tags", headers=AUTH)).json()["tags"])
    assert not yaml.safe_load((vault_dir / ".relay" / "tags.yml").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_tag_list_carries_each_tags_expiry_config(client):
    """The browser's expiry form opened empty because nothing reported what a
    tag was already set to (relay #198, B-12): each tag carries its config, and
    a tag without one says so with nulls rather than zeroes."""
    await client.post("/posts", json={"title": "Plain", "content": "x", "tags": ["plain"]}, headers=AUTH)
    await client.post("/tags/news/config", json={"expires_at": "2030-01-01T00:00:00Z"}, headers=AUTH)
    await client.post("/tags/digest/config", json={"ttl_hours": 48}, headers=AUTH)
    tags = {t["tag"]: t for t in (await client.get("/tags", headers=AUTH)).json()["tags"]}
    assert (tags["plain"]["ttl_hours"], tags["plain"]["expires_at"]) == (None, None)
    assert (tags["news"]["ttl_hours"], tags["news"]["expires_at"]) == (None, "2030-01-01T00:00:00Z")
    assert (tags["digest"]["ttl_hours"], tags["digest"]["expires_at"]) == (48, None)


@pytest.mark.asyncio
async def test_mcp_set_tag_config_clears_too(client):
    await mcp_set_tag_config(tag="news", ttl_hours=1)
    assert {"tag": "news", "count": 0, "ttl_hours": 1, "expires_at": None} in (await mcp_list_tags())["tags"]
    await mcp_set_tag_config(tag="news")
    assert all(t["tag"] != "news" for t in (await mcp_list_tags())["tags"])
