"""Discovering what was deleted.

Every recovery primitive already existed — list a post's revisions, read its
body at one, restore it keeping its id. The missing piece was *discovery*: you
can restore anything if you know its id, and there was no way to learn the id of
something you deleted.

Deletions are found by **diff**, not by parsing commit messages, because the
three ways a post disappears do not all announce themselves: `delete_post`
writes `post <id> delete: <title>`, the TTL sweep writes `ttl expiry: N post(s)`
with **no ids at all**, and a note deleted in Obsidian arrives as an ordinary
external change. `--diff-filter=D` catches all three.
"""
from __future__ import annotations

import os
import shutil

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import database, history
from relay.config import settings
from relay.main import app

HEADERS = {"Authorization": "Bearer test-key"}
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs a git binary")


@pytest.fixture(autouse=True)
def _reset_probe():
    history.reset_state_for_tests()
    yield
    history.reset_state_for_tests()


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings, "history_enabled", True)   # conftest turns it off
    await database.init_db()
    await history.init()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _make(client, title, content="body", tags=None):
    r = await client.post("/posts", json={"title": title, "content": content,
                                          "tags": tags or ["homelab"]}, headers=HEADERS)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_a_deleted_post_becomes_discoverable_and_restorable(client):
    """The whole point: delete, find it without knowing anything, put it back."""
    pid = await _make(client, "Digest mattutino — 16 agosto 2026", "the body that must come back")
    assert (await client.delete(f"/posts/{pid}", headers=HEADERS)).status_code in (200, 204)

    listed = await client.get("/posts/deleted", headers=HEADERS)
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    match = next((d for d in items if d["id"] == pid), None)
    assert match is not None, f"deleted post not discoverable: {[d['id'] for d in items]}"
    assert match["title"] == "Digest mattutino — 16 agosto 2026"
    assert match["reason"] == "deleted"

    # The sha it reports is the one /restore takes — that is the contract.
    back = await client.post(f"/posts/{pid}/restore", json={"sha": match["sha"]}, headers=HEADERS)
    assert back.status_code in (200, 201), back.text
    assert back.json()["id"] == pid, "restore did not keep the original id"
    assert "must come back" in back.json()["content"]


@pytest.mark.asyncio
async def test_a_restored_post_leaves_the_list(client):
    """It is a list of what is *gone*, not a log of deletions."""
    pid = await _make(client, "Briefly Gone")
    await client.delete(f"/posts/{pid}", headers=HEADERS)
    before = {d["id"] for d in (await client.get("/posts/deleted", headers=HEADERS)).json()["items"]}
    assert pid in before

    sha = next(d["sha"] for d in (await client.get("/posts/deleted", headers=HEADERS)).json()["items"]
               if d["id"] == pid)
    await client.post(f"/posts/{pid}/restore", json={"sha": sha}, headers=HEADERS)

    after = {d["id"] for d in (await client.get("/posts/deleted", headers=HEADERS)).json()["items"]}
    assert pid not in after, "a restored post is still listed as deleted"


@pytest.mark.asyncio
async def test_ttl_expiries_are_excluded_by_default(client):
    """This vault sheds fourteen digests a week; left in they bury the accident.

    The TTL sweep's commit carries no ids, so this also proves the listing is
    built from the diff rather than from the message.
    """
    pid = await _make(client, "Expiring Digest", tags=["news"])
    await client.delete(f"/posts/{pid}", headers=HEADERS)

    default = await client.get("/posts/deleted", headers=HEADERS)
    every = await client.get("/posts/deleted?include_expiry=true", headers=HEADERS)
    assert default.status_code == every.status_code == 200
    # An ordinary delete appears in both; the flag only ever *adds*.
    assert {d["id"] for d in default.json()["items"]} <= {d["id"] for d in every.json()["items"]}
    assert all(d["reason"] != "expiry" for d in default.json()["items"])


@pytest.mark.asyncio
async def test_the_literal_route_is_not_shadowed_by_the_id_route(client):
    """`/posts/deleted` must not be parsed as `/posts/{post_id}`.

    FastAPI matches in declaration order and `post_id` is an `int`, so with the
    routes the other way round this answers 422 — a failure that looks like a
    validation bug rather than a routing one.
    """
    r = await client.get("/posts/deleted", headers=HEADERS)
    assert r.status_code == 200, f"literal route shadowed by /{{post_id}}: {r.status_code}"
    assert "items" in r.json()


@pytest.mark.asyncio
async def test_it_needs_auth(client):
    assert (await client.get("/posts/deleted")).status_code in (401, 403)


@pytest.mark.asyncio
async def test_a_post_deleted_long_after_it_was_created_still_restores(client):
    """The regression that shipped in the first cut of this feature.

    `deletions()` reported the delete commit's **parent**, reasoning that it is
    the last state the file had. It is — but `restore_post` resolves a sha
    against `revisions()`, which lists only commits that *touched* the file.
    Unless a post is deleted in the very next commit after its last edit, that
    parent is some unrelated write, and the restore 404s with "No revision …"
    — which reads as a lookup bug rather than an off-by-one commit.

    ⚠️ The obvious test (create, delete, restore) **cannot catch this**: it makes
    the parent *be* the create commit, so it passes against the broken code. The
    unrelated writes below are the whole test.
    """
    pid = await _make(client, "Digest mattutino — 15 agosto 2026", "corpo da salvare",
                      tags=["digest"])
    for i in range(3):
        await _make(client, f"Unrelated {i}", "x")

    assert (await client.delete(f"/posts/{pid}", headers=HEADERS)).status_code in (200, 204)

    listed = (await client.get("/posts/deleted", headers=HEADERS)).json()["items"]
    entry = next(d for d in listed if d["id"] == pid)

    out = await client.post(f"/posts/{pid}/restore", json={"sha": entry["sha"]}, headers=HEADERS)
    assert out.status_code == 200, f"restore failed: {out.status_code} {out.text}"
    assert out.json()["content"].strip() == "corpo da salvare"

    # The preview the UI shows before restoring reads the same sha, so it has to
    # resolve too — they share `_resolve_revision` precisely so they cannot
    # disagree, and this pins that they don't.
    prev = await client.get(f"/posts/{pid}/history/{entry['sha']}", headers=HEADERS)
    assert prev.status_code == 200, prev.text


@pytest.mark.asyncio
async def test_deletions_after_a_cached_scan_are_still_found(client):
    """The scan is cached per HEAD and extended with only the new commits — a
    deletion after the first listing must appear, newest first, and a post
    deleted, restored and deleted again must list once, at its latest delete."""
    first = await _make(client, "First Gone")
    second = await _make(client, "Second Gone")
    await client.delete(f"/posts/{first}", headers=HEADERS)
    assert [d.post_id for d in await history.deletions()] == [first]

    await client.delete(f"/posts/{second}", headers=HEADERS)
    assert [d.post_id for d in await history.deletions()] == [second, first]

    sha = next(d.sha for d in await history.deletions() if d.post_id == first)
    await client.post(f"/posts/{first}/restore", json={"sha": sha}, headers=HEADERS)
    await client.delete(f"/posts/{first}", headers=HEADERS)
    found = await history.deletions()
    assert [d.post_id for d in found] == [first, second]
    history.reset_state_for_tests()
    assert await history.deletions() == found, "incremental scan disagrees with a full one"


# ── a restored post is not swept straight back out ───────────────────────────


async def _sweep() -> int:
    from relay import cleanup

    async with database.connect() as db:
        return await cleanup._delete_expired(db)


async def _restore_expired(client, pid) -> dict:
    listed = (await client.get("/posts/deleted?include_expiry=true", headers=HEADERS)).json()["items"]
    sha = next(d["sha"] for d in listed if d["id"] == pid)
    r = await client.post(f"/posts/{pid}/restore", json={"sha": sha}, headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _hours_ahead(iso: str) -> float:
    from datetime import UTC, datetime

    return (datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds() / 3600


@pytest.mark.asyncio
async def test_a_post_restored_past_its_keep_last_survives_the_next_sweep(client, monkeypatch):
    """Retention counts from created_at, which a restore keeps: without its own
    expires_at the restored post was the oldest again and went at once."""
    monkeypatch.setattr(settings, "default_ttl_hours", 0)
    older = await _make(client, "Older Digest", tags=["roundup"])
    await _make(client, "Newer Digest", tags=["roundup"])
    await client.post("/tags/roundup/config", json={"keep_last": 1}, headers=HEADERS)
    assert await _sweep() == 1

    back = await _restore_expired(client, older)
    assert 167 < _hours_ahead(back["expires_at"]) <= 168   # a week: keep_last has no TTL of its own
    assert await _sweep() == 0
    assert (await client.get(f"/posts/{older}", headers=HEADERS)).status_code == 200


@pytest.mark.asyncio
async def test_a_post_restored_past_its_tag_date_gets_one_tag_ttl(client):
    pid = await _make(client, "Dated", tags=["shortlived"])
    await client.post("/tags/shortlived/config",
                      json={"ttl_hours": 2, "expires_at": "2020-01-01T00:00:00Z"}, headers=HEADERS)
    assert await _sweep() == 1

    back = await _restore_expired(client, pid)
    assert 1.9 < _hours_ahead(back["expires_at"]) <= 2
    assert await _sweep() == 0


@pytest.mark.asyncio
async def test_a_revision_whose_own_date_passed_is_restored_with_a_new_one(client, monkeypatch):
    monkeypatch.setattr(settings, "default_ttl_hours", 0)
    r = await client.post("/posts", json={"title": "Own Date", "content": "v1", "tags": ["homelab"],
                                          "expires_at": "2020-01-01T00:00:00Z"}, headers=HEADERS)
    pid = r.json()["id"]
    first = (await client.get(f"/posts/{pid}/history", headers=HEADERS)).json()["items"][-1]["sha"]
    await client.patch(f"/posts/{pid}", json={"content": "v2", "expires_at": ""}, headers=HEADERS)

    live = await client.post(f"/posts/{pid}/restore", json={"sha": first}, headers=HEADERS)
    assert live.status_code == 200, live.text
    assert _hours_ahead(live.json()["expires_at"]) > 167
    assert await _sweep() == 0


@pytest.mark.asyncio
async def test_a_restore_no_rule_would_sweep_keeps_no_expiry(client, monkeypatch):
    monkeypatch.setattr(settings, "default_ttl_hours", 0)
    pid = await _make(client, "Plain")
    await client.delete(f"/posts/{pid}", headers=HEADERS)
    assert (await _restore_expired(client, pid))["expires_at"] is None
