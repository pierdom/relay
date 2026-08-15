"""In-band recovery — `GET /posts/{id}/history` and `POST /posts/{id}/restore`.

These lean on the *deleted*-post cases on purpose: a post that still exists is the
easy half, and the whole point of the feature is getting back something that is
gone.
"""
from __future__ import annotations

import os
import shutil

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import database, history
from relay.auth import require_api_key
from relay.config import settings
from relay.main import app

AUTH = {"Authorization": "Bearer test-key"}

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs a git binary")


@pytest.fixture(autouse=True)
def _reset_probe():
    history.reset_state_for_tests()
    yield
    history.reset_state_for_tests()


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings, "history_enabled", True)  # conftest turns it off
    await database.init_db()
    await history.init()
    app.dependency_overrides[require_api_key] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _create(client, title, content="v1", tags=("homelab",)) -> dict:
    r = await client.post(
        "/posts", json={"title": title, "content": content, "tags": list(tags)}, headers=AUTH
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _history(client, pid) -> dict:
    r = await client.get(f"/posts/{pid}/history", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


# ── reading history ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_lists_revisions_newest_first(client):
    post = await _create(client, "Tracked", content="v1")
    await client.patch(f"/posts/{post['id']}", json={"content": "v2"}, headers=AUTH)

    data = await _history(client, post["id"])
    assert data["exists"] is True
    messages = [i["message"] for i in data["items"]]
    assert messages[0].startswith(f"post {post['id']} update")
    assert messages[-1].startswith(f"post {post['id']} create")
    assert all(len(i["short_sha"]) == 7 for i in data["items"])


@pytest.mark.asyncio
async def test_history_of_a_deleted_post_is_still_readable(client):
    """The case worth recovering — so this must not 404."""
    post = await _create(client, "Gone", content="precious")
    await client.delete(f"/posts/{post['id']}", headers=AUTH)

    data = await _history(client, post["id"])
    assert data["exists"] is False
    assert data["items"], "no history for a deleted post"
    # Every listed revision is restorable, so the delete commit itself is absent:
    # the file has no blob at that commit. The revision before it is what you want.
    assert all("delete" not in i["message"] for i in data["items"])
    assert "precious" in (
        await client.post(
            f"/posts/{post['id']}/restore", json={"sha": data["items"][0]["sha"]}, headers=AUTH
        )
    ).json()["content"]


@pytest.mark.asyncio
async def test_history_follows_a_rename(client):
    post = await _create(client, "Before Rename", content="v1")
    await client.patch(f"/posts/{post['id']}", json={"title": "After Rename"}, headers=AUTH)
    data = await _history(client, post["id"])
    paths = {i["path"] for i in data["items"]}
    assert any("Before Rename" in p for p in paths), paths
    assert any("After Rename" in p for p in paths), paths


@pytest.mark.asyncio
async def test_history_is_503_when_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "history_enabled", False)
    post_id = 1
    assert (await client.get(f"/posts/{post_id}/history", headers=AUTH)).status_code == 503


# ── restoring ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_undoes_an_overwrite(client):
    post = await _create(client, "Canonical", content="the good version")
    pid = post["id"]
    good = next(i for i in (await _history(client, pid))["items"] if "create" in i["message"])
    await client.patch(f"/posts/{pid}", json={"content": "clobbered"}, headers=AUTH)

    r = await client.post(f"/posts/{pid}/restore", json={"sha": good["sha"]}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert "the good version" in r.json()["content"]
    assert (await client.get(f"/posts/{pid}", headers=AUTH)).json()["content"].strip() == "the good version"


@pytest.mark.asyncio
async def test_restore_recreates_a_deleted_post_with_its_original_id(client):
    post = await _create(client, "Resurrect Me", content="body worth keeping")
    pid = post["id"]
    await client.delete(f"/posts/{pid}", headers=AUTH)
    assert (await client.get(f"/posts/{pid}", headers=AUTH)).status_code == 404

    items = (await _history(client, pid))["items"]
    before_delete = next(i for i in items if "create" in i["message"])
    r = await client.post(f"/posts/{pid}/restore", json={"sha": before_delete["sha"]}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == pid, "restore must keep the id so [[links]] and #id resolve"
    assert "body worth keeping" in r.json()["content"]
    assert (await client.get(f"/posts/{pid}", headers=AUTH)).status_code == 200


@pytest.mark.asyncio
async def test_restore_accepts_a_short_sha_and_is_itself_recorded(client):
    post = await _create(client, "Undoable", content="original")
    pid = post["id"]
    good = (await _history(client, pid))["items"][0]
    await client.patch(f"/posts/{pid}", json={"content": "bad"}, headers=AUTH)

    r = await client.post(f"/posts/{pid}/restore", json={"sha": good["short_sha"]}, headers=AUTH)
    assert r.status_code == 200, r.text
    top = (await _history(client, pid))["items"][0]["message"]
    assert top.startswith(f"post {pid} restore:"), top
    assert good["short_sha"] in top


@pytest.mark.asyncio
async def test_unknown_revision_is_404(client):
    post = await _create(client, "Solid")
    r = await client.post(
        f"/posts/{post['id']}/restore", json={"sha": "deadbeef"}, headers=AUTH
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_restore_is_503_when_disabled(client, monkeypatch):
    post = await _create(client, "Off")
    monkeypatch.setattr(settings, "history_enabled", False)
    r = await client.post(f"/posts/{post['id']}/restore", json={"sha": "abcdef1"}, headers=AUTH)
    assert r.status_code == 503


# ── the filename-reuse hazard ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_reused_filename_does_not_leak_another_posts_history(client):
    """Titles are filenames, so a deleted note's path can be taken over later.
    History is keyed by path, so it must verify each revision's front-matter id."""
    old = await _create(client, "Recycled", content="the first post")
    # A higher-id post keeps MAX(id) up: relay allocates MAX(id)+1, so deleting the
    # newest post would otherwise hand its id straight back and conflate id reuse
    # with the filename reuse this test is about.
    await _create(client, "Keeper", content="holds the id high")
    await client.delete(f"/posts/{old['id']}", headers=AUTH)
    new = await _create(client, "Recycled", content="a different post entirely")
    assert new["id"] != old["id"]

    for item in (await _history(client, old["id"]))["items"]:
        assert "different post" not in item["message"]
    # the new post's history must not include the old post's revisions either
    old_shas = {i["sha"] for i in (await _history(client, old["id"]))["items"]}
    new_shas = {i["sha"] for i in (await _history(client, new["id"]))["items"]}
    assert not (old_shas & new_shas), "histories of two posts sharing a filename overlap"


@pytest.mark.asyncio
async def test_restoring_into_a_taken_filename_does_not_clobber_the_occupant(client):
    """`write_file(exclude=old_path)` treats the old path as free even when a file
    sits there, so a naive restore would overwrite whoever owns the name now."""
    old = await _create(client, "Contested", content="the original occupant")
    old_id = old["id"]
    await _create(client, "Keeper", content="holds the id high")  # see the test above
    sha = (await _history(client, old_id))["items"][0]["sha"]
    await client.delete(f"/posts/{old_id}", headers=AUTH)
    squatter = await _create(client, "Contested", content="moved in afterwards")

    r = await client.post(f"/posts/{old_id}/restore", json={"sha": sha}, headers=AUTH)
    assert r.status_code == 200, r.text

    # both posts must now exist, with their own bodies intact
    assert "the original occupant" in (await client.get(f"/posts/{old_id}", headers=AUTH)).json()["content"]
    kept = await client.get(f"/posts/{squatter['id']}", headers=AUTH)
    assert kept.status_code == 200, "restore deleted the post that had taken the filename"
    assert "moved in afterwards" in kept.json()["content"]
