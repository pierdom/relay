"""Folder placement — a post is filed once on create by its first domain tag,
and a tag-less note in Inbox migrates to its domain folder when it gains one.
Real folders stay human-owned; moves only ever go *out of* Inbox."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import database, folders, vault
from relay.auth import require_api_key
from relay.config import settings
from relay.main import app

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


def _folder_of(vp: Path, pid: int) -> str:
    """First-level vault folder holding the post whose file carries ``id: pid``."""
    for p in vp.rglob("*.md"):
        if ".relay" in p.parts:
            continue
        meta, _ = vault.read_file(p)
        if meta.get("id") == pid:
            rel = p.relative_to(vp)
            return rel.parts[0] if len(rel.parts) > 1 else ""
    raise AssertionError(f"no file for id {pid}")


# ── pure policy ──────────────────────────────────────────────────────────────


def test_folder_for_policy():
    assert folders.folder_for(0, ["homelab"]) == ""              # master stays at root
    assert folders.folder_for(5, ["homelab"]) == "Homelab"       # first domain tag
    assert folders.folder_for(5, ["dev", "homelab"]) == "Dev"    # priority = first match
    assert folders.folder_for(5, ["news"]) == "Digests"          # series-tag fallback
    assert folders.folder_for(5, []) == folders.INBOX            # unfiled
    assert folders.folder_for(5, ["nope"]) == folders.INBOX      # unknown tag → Inbox


# ── create-time placement ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_files_by_first_domain_tag(client, vault_dir):
    post = (await client.post(
        "/posts", json={"title": "hl note", "content": "x", "tags": ["homelab"]}, headers=AUTH
    )).json()
    assert _folder_of(vault_dir, post["id"]) == "Homelab"


@pytest.mark.asyncio
async def test_create_untagged_lands_in_inbox(client, vault_dir):
    post = (await client.post(
        "/posts", json={"title": "loose note", "content": "x", "tags": []}, headers=AUTH
    )).json()
    assert _folder_of(vault_dir, post["id"]) == folders.INBOX


# ── Inbox → domain move on first tag ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbox_note_moves_to_domain_on_first_tag(client, vault_dir):
    post = (await client.post(
        "/posts", json={"title": "drifting", "content": "x", "tags": []}, headers=AUTH
    )).json()
    assert _folder_of(vault_dir, post["id"]) == folders.INBOX

    r = await client.patch(f"/posts/{post['id']}", json={"tags": ["dev"]}, headers=AUTH)
    assert r.status_code == 200
    assert _folder_of(vault_dir, post["id"]) == "Dev"


@pytest.mark.asyncio
async def test_domain_folder_not_moved_on_retag(client, vault_dir):
    # A post already filed in a real folder is human-owned: retagging it must not
    # relocate the file (only Inbox notes migrate).
    post = (await client.post(
        "/posts", json={"title": "stays put", "content": "x", "tags": ["homelab"]}, headers=AUTH
    )).json()
    assert _folder_of(vault_dir, post["id"]) == "Homelab"

    r = await client.patch(f"/posts/{post['id']}", json={"tags": ["dev"]}, headers=AUTH)
    assert r.status_code == 200
    assert _folder_of(vault_dir, post["id"]) == "Homelab"  # unchanged
