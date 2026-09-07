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
from httpx import ASGITransport, AsyncClient

from relay import database
from relay.auth import require_api_key
from relay.config import settings
from relay.main import app
from relay.mcp_server import add_attachment as mcp_add_attachment
from relay.mcp_server import list_folders as mcp_list_folders

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
