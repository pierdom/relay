"""Post ids are never reused.

`allocate_id` used to be `MAX(id)+1` over the live table, so deleting the newest
post handed its id straight to the next one created. That silently repointed every
`#id` cross-link at unrelated content, and made a post's history ambiguous — a
restore could resurrect a previous holder's body under the same id.
"""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import database, vault
from relay.auth import require_api_key
from relay.main import app

AUTH = {"Authorization": "Bearer test-key"}


@pytest_asyncio.fixture
async def client():
    await database.init_db()
    app.dependency_overrides[require_api_key] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _create(client, title, content="body") -> dict:
    r = await client.post(
        "/posts", json={"title": title, "content": content, "tags": ["homelab"]}, headers=AUTH
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_deleting_the_newest_post_does_not_free_its_id(client):
    first = await _create(client, "First")
    assert (await client.delete(f"/posts/{first['id']}", headers=AUTH)).status_code == 204
    second = await _create(client, "Second")
    assert second["id"] > first["id"], "id was reused after deleting the newest post"


@pytest.mark.asyncio
async def test_ids_keep_climbing_across_repeated_delete_and_create(client):
    seen = []
    for i in range(4):
        post = await _create(client, f"Churn {i}")
        seen.append(post["id"])
        await client.delete(f"/posts/{post['id']}", headers=AUTH)
    assert seen == sorted(set(seen)), f"ids repeated across delete/create churn: {seen}"


@pytest.mark.asyncio
async def test_the_high_water_mark_survives_a_restart(client):
    post = await _create(client, "Before Restart")
    await client.delete(f"/posts/{post['id']}", headers=AUTH)

    await database.init_db()  # the index is wiped and rebuilt from files

    after = await _create(client, "After Restart")
    assert after["id"] > post["id"], "restart reset the counter and reused an id"


@pytest.mark.asyncio
async def test_counter_is_seeded_from_existing_posts_when_absent(client):
    """Upgrade path: a vault that predates the counter must not restart from 1."""
    post = await _create(client, "Existing")
    vault.id_counter_path().unlink()
    assert vault.read_id_counter() == 0

    await database.init_db()  # rebuild seeds the mark from the files it finds
    assert vault.read_id_counter() >= post["id"]
    nxt = await _create(client, "Next")
    assert nxt["id"] > post["id"]


@pytest.mark.asyncio
async def test_an_externally_added_note_cannot_take_a_retired_id(client):
    """The rebuild stamps ids into hand-created notes — that path must respect the
    mark too, or a note dropped into the vault could claim a deleted post's id."""
    post = await _create(client, "Retired")
    retired_id = post["id"]
    await client.delete(f"/posts/{retired_id}", headers=AUTH)

    hand_made = vault.vault_dir() / "Homelab" / "Dropped In.md"
    hand_made.parent.mkdir(parents=True, exist_ok=True)
    hand_made.write_text("no front-matter here\n", encoding="utf-8")
    await database.init_db()

    r = await client.get("/posts?limit=50", headers=AUTH)
    ids = {p["id"]: p["title"] for p in r.json()["items"]}
    assert retired_id not in ids, f"a stamped note took the retired id {retired_id}: {ids}"


@pytest.mark.asyncio
async def test_the_counter_file_is_not_mistaken_for_a_post(client):
    await _create(client, "Real")
    r = await client.get("/posts?limit=50", headers=AUTH)
    titles = {p["title"] for p in r.json()["items"]}
    assert "last_id" not in titles
    assert vault.id_counter_path().parent.name == ".relay"
