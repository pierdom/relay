from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("API_KEY", "test-key")

import aiosqlite
import pytest
import pytest_asyncio
import yaml
from httpx import AsyncClient, ASGITransport

from relay import database, frontmatter, vault
from relay.auth import require_api_key
from relay.config import settings
from relay.main import app

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


async def _create_post(client, **kwargs) -> dict:
    payload = {"content": "original content", "title": "original title", "tags": ["a", "b"], **kwargs}
    r = await client.post("/posts", json=payload, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()


# ── update semantics (unchanged behaviour, file-backed) ──────────────────────


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


# ── vault / filesystem behaviour ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_writes_markdown_file_with_frontmatter(client, vault_dir):
    post = await _create_post(client, title="My News", content="# Hello\n\nbody")
    f = vault_dir / "My News.md"
    assert f.exists()
    meta, body = frontmatter.parse(f.read_text(encoding="utf-8"))
    assert meta["id"] == post["id"]
    assert meta["tags"] == ["a", "b"]
    assert "title" not in meta  # title lives in the filename, never front-matter
    assert "Hello" in body


@pytest.mark.asyncio
async def test_title_is_required(client):
    r = await client.post("/posts", json={"content": "no title here"}, headers=AUTH)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_title_change_renames_file(client, vault_dir):
    post = await _create_post(client, title="Old Name")
    assert (vault_dir / "Old Name.md").exists()
    r = await client.patch(f"/posts/{post['id']}", json={"title": "New Name"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["title"] == "New Name"
    assert (vault_dir / "New Name.md").exists()
    assert not (vault_dir / "Old Name.md").exists()


@pytest.mark.asyncio
async def test_delete_unlinks_file(client, vault_dir):
    post = await _create_post(client, title="To Delete")
    f = vault_dir / "To Delete.md"
    assert f.exists()
    r = await client.delete(f"/posts/{post['id']}", headers=AUTH)
    assert r.status_code == 204
    assert not f.exists()


@pytest.mark.asyncio
async def test_title_collision_gets_suffix(client, vault_dir):
    p1 = await _create_post(client, title="Same Title")
    p2 = await _create_post(client, title="Same Title")
    assert p1["id"] != p2["id"]
    assert (vault_dir / "Same Title.md").exists()
    assert (vault_dir / "Same Title 2.md").exists()


@pytest.mark.asyncio
async def test_illegal_chars_sanitized_in_filename(client, vault_dir):
    await _create_post(client, title="AI/ML: news?")
    assert (vault_dir / "AI ML news.md").exists()


@pytest.mark.asyncio
async def test_master_document_seeded_and_protected(client):
    r = await client.get("/posts/0", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["id"] == 0
    r = await client.delete("/posts/0", headers=AUTH)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_rebuild_adopts_idless_handmade_note(vault_dir):
    (vault_dir / "Hand Made.md").write_text("Just some text\n", encoding="utf-8")
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await vault.rebuild_index(db)
        async with db.execute("SELECT id FROM posts WHERE title = 'Hand Made'") as cur:
            row = await cur.fetchone()
    assert row is not None and row["id"] > 0
    meta, _ = frontmatter.parse((vault_dir / "Hand Made.md").read_text(encoding="utf-8"))
    assert meta["id"] == row["id"]  # id stamped back into the file


@pytest.mark.asyncio
async def test_set_tag_config_writes_yaml(client):
    r = await client.post("/tags/news/config", json={"ttl_hours": 48}, headers=AUTH)
    assert r.status_code == 200
    data = yaml.safe_load(Path(settings.tags_config_path).read_text(encoding="utf-8"))
    assert data["news"]["ttl_hours"] == 48
