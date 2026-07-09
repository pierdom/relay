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
    f = vault_dir / "Inbox" / "My News.md"  # tags a/b are non-domain -> Inbox
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
    assert (vault_dir / "Inbox" / "Old Name.md").exists()
    r = await client.patch(f"/posts/{post['id']}", json={"title": "New Name"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["title"] == "New Name"
    assert (vault_dir / "Inbox" / "New Name.md").exists()
    assert not (vault_dir / "Inbox" / "Old Name.md").exists()


@pytest.mark.asyncio
async def test_delete_unlinks_file(client, vault_dir):
    post = await _create_post(client, title="To Delete")
    f = vault_dir / "Inbox" / "To Delete.md"
    assert f.exists()
    r = await client.delete(f"/posts/{post['id']}", headers=AUTH)
    assert r.status_code == 204
    assert not f.exists()


@pytest.mark.asyncio
async def test_title_collision_gets_suffix(client, vault_dir):
    p1 = await _create_post(client, title="Same Title")
    p2 = await _create_post(client, title="Same Title")
    assert p1["id"] != p2["id"]
    assert (vault_dir / "Inbox" / "Same Title.md").exists()
    assert (vault_dir / "Inbox" / "Same Title 2.md").exists()


@pytest.mark.asyncio
async def test_illegal_chars_sanitized_in_filename(client, vault_dir):
    await _create_post(client, title="AI/ML: news?")
    assert (vault_dir / "Inbox" / "AI ML news.md").exists()


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


# ── folder placement (derive from primary tag; never auto-move) ──────────────


@pytest.mark.asyncio
async def test_create_files_post_by_primary_domain_tag(client, vault_dir):
    await _create_post(client, title="QTH", tags=["radio", "reference"])
    assert (vault_dir / "Radio" / "QTH.md").exists()
    assert not (vault_dir / "QTH.md").exists()


@pytest.mark.asyncio
async def test_first_domain_tag_wins_over_leading_type_tag(client, vault_dir):
    # non-domain tags are skipped; the first *domain* tag decides the folder
    await _create_post(client, title="Corellia Gaming", tags=["reference", "corellia", "gaming"])
    assert (vault_dir / "Gaming" / "Corellia Gaming.md").exists()


@pytest.mark.asyncio
async def test_edit_never_moves_folder_even_when_tags_change(client, vault_dir):
    post = await _create_post(client, title="Note", tags=["radio", "reference"])
    assert (vault_dir / "Radio" / "Note.md").exists()
    r = await client.patch(f"/posts/{post['id']}", json={"tags": ["dev"]}, headers=AUTH)
    assert r.status_code == 200
    # folder is human-owned after creation: stays in Radio, not moved to Dev
    assert (vault_dir / "Radio" / "Note.md").exists()
    assert not (vault_dir / "Dev" / "Note.md").exists()


@pytest.mark.asyncio
async def test_master_document_stays_at_root(client, vault_dir):
    assert (vault_dir / "Master Document.md").exists()


@pytest.mark.asyncio
async def test_rebuild_indexes_nested_note(vault_dir):
    nested = vault_dir / "Radio" / "Nested.md"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text(
        "---\nid: 500\ntags: [radio, reference]\ncreated_at: '2026-01-01T00:00:00Z'\n---\n\nbody\n",
        encoding="utf-8",
    )
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await vault.rebuild_index(db)
        async with db.execute("SELECT path FROM posts WHERE id = 500") as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row["path"] == os.path.join("Radio", "Nested.md")


@pytest.mark.asyncio
async def test_idless_note_in_subfolder_gets_id_in_place(vault_dir):
    hand = vault_dir / "Homelab" / "Hand.md"
    hand.parent.mkdir(parents=True, exist_ok=True)
    hand.write_text("just text, no id\n", encoding="utf-8")
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await vault.rebuild_index(db)
        async with db.execute("SELECT id, path FROM posts WHERE title = 'Hand'") as cur:
            row = await cur.fetchone()
    assert row is not None and row["id"] > 0
    # id stamped in place; file not yanked to root
    assert row["path"] == os.path.join("Homelab", "Hand.md")
    assert hand.exists()
    meta, _ = frontmatter.parse(hand.read_text(encoding="utf-8"))
    assert meta["id"] == row["id"]


@pytest.mark.asyncio
async def test_home_feed_pins_master_on_top(client):
    await _create_post(client, title="Regular Post")
    data = (await client.get("/posts", headers=AUTH)).json()
    assert data["pinned"] is not None and data["pinned"]["id"] == 0
    assert all(i["id"] != 0 for i in data["items"])  # not duplicated in the stream


@pytest.mark.asyncio
async def test_filtered_and_paged_feeds_do_not_pin(client):
    await _create_post(client, title="Tagged", tags=["x"])
    assert (await client.get("/posts?tag=x", headers=AUTH)).json()["pinned"] is None
    assert (await client.get("/posts?search=Tagged", headers=AUTH)).json()["pinned"] is None
    assert (await client.get("/posts?offset=10", headers=AUTH)).json()["pinned"] is None


@pytest.mark.asyncio
async def test_folders_listing_and_filter(client):
    await _create_post(client, title="R1", tags=["radio"])
    await _create_post(client, title="H1", tags=["homelab"])
    fmap = {
        f["folder"]: f["count"]
        for f in (await client.get("/folders", headers=AUTH)).json()["folders"]
    }
    assert fmap.get("Radio") == 1 and fmap.get("Homelab") == 1
    r = (await client.get("/posts?folder=Radio", headers=AUTH)).json()
    assert [i["title"] for i in r["items"]] == ["R1"]
    assert r["pinned"] is None  # folder filter → no master pin


@pytest.mark.asyncio
async def test_set_tag_config_writes_yaml(client):
    r = await client.post("/tags/news/config", json={"ttl_hours": 48}, headers=AUTH)
    assert r.status_code == 200
    data = yaml.safe_load(Path(settings.tags_config_path).read_text(encoding="utf-8"))
    assert data["news"]["ttl_hours"] == 48
