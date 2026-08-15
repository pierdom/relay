"""External edits (Obsidian/nvim) and the last-modified stamp.

An external editor rewrites a note's body but never touches the front-matter, so
`updated_at` alone goes stale the moment a human edits a file. relay derives the
stamp from the canonical file's mtime instead — these pin that, plus the slack
that keeps a freshly-created post from looking edited.
"""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import database, vault, watcher
from relay.auth import require_api_key
from relay.config import settings
from relay.main import app

AUTH = {"Authorization": "Bearer test-key"}


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    await database.init_db()

    async def override_auth():
        return None

    app.dependency_overrides[require_api_key] = override_auth
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(settings.database_path)
    db.row_factory = aiosqlite.Row
    return db


async def _path_of(client, pid: int):
    db = await _db()
    try:
        async with db.execute("SELECT path FROM posts WHERE id = ?", (pid,)) as cur:
            return vault.abspath((await cur.fetchone())["path"])
    finally:
        await db.close()


def _edit_externally(path, new_body: str, *, mtime_offset: int = 60) -> None:
    """Rewrite a note's body the way an external editor would — front-matter
    untouched — and push its mtime clear of the write-slack window."""
    text = path.read_text(encoding="utf-8")
    head, sep, _body = text.partition("---\n\n") if "---\n\n" in text else (text, "", "")
    path.write_text(head + sep + new_body, encoding="utf-8")
    stamp = path.stat().st_mtime + mtime_offset
    os.utime(path, (stamp, stamp))


@pytest.mark.asyncio
async def test_external_edit_bumps_updated_at(client):
    r = await client.post(
        "/posts", json={"title": "Ext", "content": "v1", "tags": ["homelab"]}, headers=AUTH
    )
    pid = r.json()["id"]
    assert r.json()["updated_at"] is None
    path = await _path_of(client, pid)

    _edit_externally(path, "v2 edited by a human\n")
    db = await _db()
    try:
        await watcher._reconcile_file(db, path)
    finally:
        await db.close()

    got = (await client.get(f"/posts/{pid}", headers=AUTH)).json()
    assert "edited by a human" in got["content"]
    assert got["updated_at"] is not None, "external edit left updated_at stale"
    assert got["updated_at"] >= got["created_at"]


@pytest.mark.asyncio
async def test_external_edit_sorts_to_the_top_of_the_updated_feed(client):
    old = (await client.post(
        "/posts", json={"title": "Older", "content": "a", "tags": ["homelab"]}, headers=AUTH
    )).json()
    (await client.post(
        "/posts", json={"title": "Newer", "content": "b", "tags": ["homelab"]}, headers=AUTH
    )).json()

    path = await _path_of(client, old["id"])
    _edit_externally(path, "a, revised\n")
    db = await _db()
    try:
        await watcher._reconcile_file(db, path)
    finally:
        await db.close()

    feed = (await client.get("/posts?sort=updated&order=desc", headers=AUTH)).json()
    assert feed["items"][0]["id"] == old["id"], "externally-edited post did not rise in the feed"


@pytest.mark.asyncio
async def test_fresh_post_is_not_marked_edited(client):
    """The write-slack guard: relay stamps whole seconds before writing, so the
    file mtime can land just after created_at. That must not read as an edit."""
    r = await client.post(
        "/posts", json={"title": "Fresh", "content": "x", "tags": ["homelab"]}, headers=AUTH
    )
    pid = r.json()["id"]
    path = await _path_of(client, pid)
    meta, _ = vault.read_file(path)
    assert vault.effective_updated_at(path, meta) is None


@pytest.mark.asyncio
async def test_index_rebuild_picks_up_external_edit(client):
    """The stamp must survive a restart — the index is rebuilt from files, so it
    has to derive from mtime there too, not only in the live watcher."""
    r = await client.post(
        "/posts", json={"title": "Restarted", "content": "v1", "tags": ["homelab"]}, headers=AUTH
    )
    pid = r.json()["id"]
    path = await _path_of(client, pid)
    _edit_externally(path, "v2\n")

    await database.init_db()  # startup rebuild from the vault
    got = (await client.get(f"/posts/{pid}", headers=AUTH)).json()
    assert got["updated_at"] is not None, "rebuild dropped the external-edit stamp"


@pytest.mark.asyncio
async def test_front_matter_stamp_wins_when_file_is_untouched(client):
    """A post relay itself edited keeps its own stamp — mtime only *fills in*."""
    r = await client.post(
        "/posts", json={"title": "Edited", "content": "v1", "tags": ["homelab"]}, headers=AUTH
    )
    pid = r.json()["id"]
    stamped = (await client.patch(
        f"/posts/{pid}", json={"content": "v2"}, headers=AUTH
    )).json()["updated_at"]
    assert stamped is not None

    path = await _path_of(client, pid)
    meta, _ = vault.read_file(path)
    assert vault.effective_updated_at(path, meta) == stamped


# ── restoring a deleted note (the recovery path) ─────────────────────────────


@pytest.mark.asyncio
async def test_byte_identical_restore_of_a_deleted_note_is_reindexed(client):
    """Recovery must work when the restored bytes are *identical* to what relay
    last wrote — the normal case for `git checkout` out of the history repo.

    Self-write suppression keys on (path, content hash); leaving that entry behind
    after a delete made the restore look like relay's own write, so the note came
    back on disk but never re-entered the index.
    """
    r = await client.post(
        "/posts", json={"title": "Doomed", "content": "irreplaceable", "tags": ["homelab"]}, headers=AUTH
    )
    pid = r.json()["id"]
    path = await _path_of(client, pid)
    saved = path.read_bytes()  # exactly what relay wrote

    assert (await client.delete(f"/posts/{pid}", headers=AUTH)).status_code == 204
    assert (await client.get(f"/posts/{pid}", headers=AUTH)).status_code == 404

    path.write_bytes(saved)  # what `git checkout <sha> -- <path>` does
    db = await _db()
    try:
        await watcher._reconcile_file(db, path)
    finally:
        await db.close()

    got = await client.get(f"/posts/{pid}", headers=AUTH)
    assert got.status_code == 200, "restored note never re-entered the index"
    assert got.json()["id"] == pid, "restore did not keep the original id"
    assert "irreplaceable" in got.json()["content"]


@pytest.mark.asyncio
async def test_relay_own_write_is_still_suppressed(client):
    """The eviction above must not defeat suppression for a live file — relay's
    own writes still have to be ignored, or every write would echo back."""
    r = await client.post(
        "/posts", json={"title": "Live", "content": "x", "tags": ["homelab"]}, headers=AUTH
    )
    path = await _path_of(client, r.json()["id"])
    assert vault.was_self_write(path, path.read_text(encoding="utf-8")) is True
