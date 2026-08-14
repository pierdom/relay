from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import vault
from relay.auth import require_api_key
from relay.config import settings
from relay.database import init_db
from relay.main import app

AUTH = {"Authorization": "Bearer test-key"}


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", str(vault_root))
    await init_db()

    # Seed an attachment under a domain folder's assets/ dir.
    assets = vault_root / "Homelab" / "assets"
    assets.mkdir(parents=True)
    (assets / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (assets / "notes.pdf").write_bytes(b"%PDF-1.4 fake")

    async def override_auth():
        return None

    app.dependency_overrides[require_api_key] = override_auth
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ── pure resolver ─────────────────────────────────────────────────────────────


def test_resolve_bare_filename_under_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    assets = tmp_path / "Radio" / "assets"
    assets.mkdir(parents=True)
    f = assets / "antenna.jpg"
    f.write_bytes(b"x")
    assert vault.resolve_attachment("antenna.jpg") == f.resolve()


def test_resolve_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    assert vault.resolve_attachment("../secret.txt") is None


def test_resolve_rejects_relay_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    relay = tmp_path / "vault" / ".relay"
    relay.mkdir(parents=True)
    (relay / "index.db").write_text("db")
    assert vault.resolve_attachment(".relay/index.db") is None


def test_resolve_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    assert vault.resolve_attachment("ghost.png") is None


def test_resolve_glob_metachar_is_literal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    assets = tmp_path / "Homelab" / "assets"
    assets.mkdir(parents=True)
    (assets / "a.png").write_bytes(b"x")
    (assets / "b.png").write_bytes(b"x")
    # "*.png" must be a literal filename, never a glob pattern → no match.
    assert vault.resolve_attachment("*.png") is None


def test_resolve_absolute_path_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    assert vault.resolve_attachment("/etc/hostname") is None


# ── endpoint ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_attachment_image(client):
    r = await client.get("/attachments/diagram.png", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.content.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_get_attachment_pdf(client):
    r = await client.get("/attachments/notes.pdf", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"


@pytest.mark.asyncio
async def test_get_attachment_missing_404(client):
    r = await client.get("/attachments/nope.png", headers=AUTH)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_attachment_traversal_404(client):
    r = await client.get("/attachments/../../secret.txt", headers=AUTH)
    assert r.status_code in (400, 404)


# ── upload (POST /attachments) ────────────────────────────────────────────────

import base64  # noqa: E402

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nhello").decode()


async def _create(client, title, content="body", tags=None):
    r = await client.post(
        "/posts", json={"title": title, "content": content, "tags": tags or ["homelab"]}, headers=AUTH
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_add_attachment_standalone_goes_to_inbox(client):
    r = await client.post("/attachments", json={"filename": "chart.png", "data": PNG}, headers=AUTH)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ref"] == "![[chart.png]]"
    assert body["folder"] == "Inbox"
    assert body["post_id"] is None
    # served back
    assert (await client.get("/attachments/chart.png", headers=AUTH)).status_code == 200


@pytest.mark.asyncio
async def test_add_attachment_to_post_appends_embed_and_uses_post_folder(client):
    post = await _create(client, "Rack Notes", content="Initial.", tags=["homelab"])
    r = await client.post(
        "/attachments", json={"filename": "rack.png", "data": PNG, "post_id": post["id"]}, headers=AUTH
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["folder"] == "Homelab"
    assert body["post_id"] == post["id"]
    # embed appended to the post body
    updated = (await client.get(f"/posts/{post['id']}", headers=AUTH)).json()
    assert "![[rack.png]]" in updated["content"]
    assert updated["content"].startswith("Initial.")
    # filed under the post's folder assets
    assert (await client.get("/attachments/Homelab/assets/rack.png", headers=AUTH)).status_code == 200


@pytest.mark.asyncio
async def test_add_attachment_embed_false_files_in_post_folder_without_appending(client):
    post = await _create(client, "Switch Notes", content="Body only.", tags=["homelab"])
    r = await client.post(
        "/attachments",
        json={"filename": "sw.png", "data": PNG, "post_id": post["id"], "embed": False},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["folder"] == "Homelab"      # filed under the post's folder
    assert body["post_id"] is None          # nothing appended
    # post body is untouched — the UI inserts the ref itself
    updated = (await client.get(f"/posts/{post['id']}", headers=AUTH)).json()
    assert updated["content"] == "Body only."
    assert "![[sw.png]]" not in updated["content"]
    # but the file is there, under the post's folder
    assert (await client.get("/attachments/Homelab/assets/sw.png", headers=AUTH)).status_code == 200


@pytest.mark.asyncio
async def test_add_attachment_tags_derive_folder(client):
    # compose flow: no post yet, but tags decide the folder the note will use
    r = await client.post("/attachments", json={"filename": "wave.png", "data": PNG, "tags": ["audio"]}, headers=AUTH)
    assert r.status_code == 201, r.text
    assert r.json()["folder"] == "Audio"
    assert (await client.get("/attachments/Audio/assets/wave.png", headers=AUTH)).status_code == 200
    # no domain tag → Inbox
    r2 = await client.post("/attachments", json={"filename": "misc.png", "data": PNG, "tags": ["random"]}, headers=AUTH)
    assert r2.json()["folder"] == "Inbox"


@pytest.mark.asyncio
async def test_add_attachment_collision_suffix(client):
    a = await client.post("/attachments", json={"filename": "dup.png", "data": PNG}, headers=AUTH)
    b = await client.post("/attachments", json={"filename": "dup.png", "data": PNG}, headers=AUTH)
    assert a.json()["filename"] == "dup.png"
    assert b.json()["filename"] == "dup 1.png"  # extension preserved, suffix before it


@pytest.mark.asyncio
async def test_add_attachment_names_are_vault_global_unique(client):
    # same filename to two different folders → the second is suffixed, so a bare
    # ![[chart.png]] can never resolve ambiguously across folders.
    a = await client.post("/attachments", json={"filename": "chart.png", "data": PNG, "folder": "Audio"}, headers=AUTH)
    b = await client.post(
        "/attachments", json={"filename": "chart.png", "data": PNG, "folder": "Homelab"}, headers=AUTH
    )
    assert a.json()["filename"] == "chart.png"
    assert b.json()["filename"] == "chart 1.png"
    assert a.json()["folder"] == "Audio" and b.json()["folder"] == "Homelab"
    # both resolvable and distinct
    assert (await client.get("/attachments/Audio/assets/chart.png", headers=AUTH)).status_code == 200
    assert (await client.get("/attachments/Homelab/assets/chart 1.png", headers=AUTH)).status_code == 200


@pytest.mark.asyncio
async def test_add_attachment_invalid_base64(client):
    r = await client.post("/attachments", json={"filename": "x.png", "data": "!!!not64!!!"}, headers=AUTH)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_add_attachment_post_not_found(client):
    r = await client.post(
        "/attachments", json={"filename": "x.png", "data": PNG, "post_id": 9999}, headers=AUTH
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_add_attachment_too_large(client, monkeypatch):
    monkeypatch.setattr(settings, "attachment_max_mb", 0)  # 0 bytes → always too big
    r = await client.post("/attachments", json={"filename": "big.png", "data": PNG}, headers=AUTH)
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_add_attachment_traversal_filename_is_sanitized(client):
    r = await client.post(
        "/attachments", json={"filename": "../../evil.png", "data": PNG}, headers=AUTH
    )
    assert r.status_code == 201
    assert "/" not in r.json()["filename"] and ".." not in r.json()["filename"]


@pytest.mark.asyncio
async def test_list_attachments_scopes_by_folder_and_post(client):
    post = await _create(client, "Rack", tags=["homelab"])
    await client.post("/attachments", json={"filename": "a.png", "data": PNG, "post_id": post["id"]}, headers=AUTH)
    await client.post("/attachments", json={"filename": "loose.png", "data": PNG}, headers=AUTH)  # Inbox

    # whole vault: the seeded fixture files + both uploads
    allr = (await client.get("/attachments", headers=AUTH)).json()["items"]
    names = {i["filename"] for i in allr}
    assert {"a.png", "loose.png", "diagram.png", "notes.pdf"} <= names
    assert all(i["ref"] == f"![[{i['filename']}]]" for i in allr)

    # scoped to the post's folder (Homelab)
    scoped = (await client.get(f"/attachments?post_id={post['id']}", headers=AUTH)).json()["items"]
    folders = {i["folder"] for i in scoped}
    assert folders == {"Homelab"}
    assert "loose.png" not in {i["filename"] for i in scoped}

    # scoped by folder name
    inbox = (await client.get("/attachments?folder=Inbox", headers=AUTH)).json()["items"]
    assert {i["filename"] for i in inbox} == {"loose.png"}


@pytest.mark.asyncio
async def test_list_attachments_post_not_found(client):
    r = await client.get("/attachments?post_id=9999", headers=AUTH)
    assert r.status_code == 404


# ── Inbox → domain auto-move on first tag ─────────────────────────────────────


async def _ids_in_folder(client, folder):
    r = await client.get(f"/posts?folder={folder}", headers=AUTH)
    return {p["id"] for p in r.json()["items"]}


@pytest.mark.asyncio
async def test_untagged_note_moves_out_of_inbox_on_first_domain_tag(client):
    r = await client.post("/posts", json={"title": "Quick Note", "content": "jot", "tags": []}, headers=AUTH)
    pid = r.json()["id"]
    assert pid in await _ids_in_folder(client, "Inbox")
    await client.patch(f"/posts/{pid}", json={"tags": ["audio"]}, headers=AUTH)
    assert pid in await _ids_in_folder(client, "Audio")
    assert pid not in await _ids_in_folder(client, "Inbox")


@pytest.mark.asyncio
async def test_move_carries_exclusive_attachments(client):
    r = await client.post("/posts", json={"title": "Shot", "content": "x", "tags": []}, headers=AUTH)
    pid = r.json()["id"]
    await client.post("/attachments", json={"filename": "shot.png", "data": PNG, "post_id": pid}, headers=AUTH)
    assert (await client.get("/attachments/Inbox/assets/shot.png", headers=AUTH)).status_code == 200
    await client.patch(f"/posts/{pid}", json={"tags": ["audio"]}, headers=AUTH)
    assert (await client.get("/attachments/Audio/assets/shot.png", headers=AUTH)).status_code == 200
    assert (await client.get("/attachments/Inbox/assets/shot.png", headers=AUTH)).status_code == 404


@pytest.mark.asyncio
async def test_move_keeps_shared_attachments_in_place(client):
    r = await client.post("/posts", json={"title": "Owner", "content": "x", "tags": []}, headers=AUTH)
    pid = r.json()["id"]
    await client.post("/attachments", json={"filename": "common.png", "data": PNG, "post_id": pid}, headers=AUTH)
    await client.post("/posts", json={"title": "Sharer", "content": "also ![[common.png]]", "tags": []}, headers=AUTH)
    await client.patch(f"/posts/{pid}", json={"tags": ["audio"]}, headers=AUTH)
    # shared with another Inbox note → not moved
    assert (await client.get("/attachments/Inbox/assets/common.png", headers=AUTH)).status_code == 200
    assert (await client.get("/attachments/Audio/assets/common.png", headers=AUTH)).status_code == 404


@pytest.mark.asyncio
async def test_note_already_in_domain_folder_not_moved_on_retag(client):
    r = await client.post("/posts", json={"title": "HL", "content": "x", "tags": ["homelab"]}, headers=AUTH)
    pid = r.json()["id"]
    await client.patch(f"/posts/{pid}", json={"tags": ["homelab", "audio"]}, headers=AUTH)
    assert pid in await _ids_in_folder(client, "Homelab")  # human-owned, stays put


@pytest.mark.asyncio
async def test_inbox_note_stays_on_nondomain_tag(client):
    r = await client.post("/posts", json={"title": "N", "content": "x", "tags": []}, headers=AUTH)
    pid = r.json()["id"]
    await client.patch(f"/posts/{pid}", json={"tags": ["scratch"]}, headers=AUTH)  # no domain
    assert pid in await _ids_in_folder(client, "Inbox")


# ── delete + orphan cleanup ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_attachment(client):
    await client.post("/attachments", json={"filename": "del.png", "data": PNG}, headers=AUTH)
    r = await client.delete("/attachments/del.png", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {"filename": "del.png", "referenced_by": []}
    assert (await client.get("/attachments/del.png", headers=AUTH)).status_code == 404


@pytest.mark.asyncio
async def test_delete_attachment_missing_404(client):
    r = await client.delete("/attachments/ghost.png", headers=AUTH)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_attachment_reports_dangling_references(client):
    post = await _create(client, "Refd", content="see ![[keep.png]]", tags=["homelab"])
    await client.post("/attachments", json={"filename": "keep.png", "data": PNG, "folder": "Homelab"}, headers=AUTH)
    r = await client.delete("/attachments/keep.png", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["referenced_by"] == [post["id"]]


@pytest.mark.asyncio
async def test_delete_post_removes_orphan_assets(client):
    post = await _create(client, "Has Image", content="body", tags=["homelab"])
    await client.post("/attachments", json={"filename": "orph.png", "data": PNG, "post_id": post["id"]}, headers=AUTH)
    assert (await client.get("/attachments/orph.png", headers=AUTH)).status_code == 200
    assert (await client.delete(f"/posts/{post['id']}", headers=AUTH)).status_code == 204
    # orphan asset cleaned up with its only referencing post
    assert (await client.get("/attachments/orph.png", headers=AUTH)).status_code == 404


@pytest.mark.asyncio
async def test_delete_post_keeps_shared_assets(client):
    p1 = await _create(client, "Owner", content="x", tags=["homelab"])
    await client.post("/attachments", json={"filename": "shared.png", "data": PNG, "post_id": p1["id"]}, headers=AUTH)
    await _create(client, "Sharer", content="also ![[shared.png]]", tags=["homelab"])
    await client.delete(f"/posts/{p1['id']}", headers=AUTH)
    # still referenced by the other post → kept
    assert (await client.get("/attachments/shared.png", headers=AUTH)).status_code == 200


@pytest.mark.asyncio
async def test_add_attachment_accepts_data_uri_and_whitespace(client):
    from relay import service

    wrapped = "data:image/png;base64," + "\n".join([PNG[i:i + 8] for i in range(0, len(PNG), 8)])
    r = await client.post("/attachments", json={"filename": "u.png", "data": wrapped}, headers=AUTH)
    assert r.status_code == 201, r.text
    # served bytes match the original
    got = await client.get("/attachments/u.png", headers=AUTH)
    assert got.content == base64.b64decode(PNG)
    # unit: same decoder used
    assert service.decode_attachment_b64(wrapped) == base64.b64decode(PNG)


# ── decode helper + mime + retrieval guard (unit) ─────────────────────────────


def test_decode_attachment_b64_rejects_garbage():
    from relay import service

    with pytest.raises(ValueError):
        service.decode_attachment_b64("!!!not base64!!!")


def test_attachment_mime_fallback_for_avif(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    assert vault.attachment_mime(tmp_path / "x.avif") == "image/avif"
    assert vault.attachment_mime(tmp_path / "x.svg") == "image/svg+xml"


def test_read_attachment_size_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    assets = tmp_path / "Inbox" / "assets"
    assets.mkdir(parents=True)
    (assets / "big.png").write_bytes(b"x" * 2048)
    with pytest.raises(ValueError):
        vault.read_attachment("big.png", max_bytes=1024)
    # under the limit reads fine
    ok = vault.read_attachment("big.png", max_bytes=4096)
    assert ok is not None and len(ok[1]) == 2048
