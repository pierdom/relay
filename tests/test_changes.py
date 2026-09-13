"""The vault changelog (`relay/changes.py`, relay #198 N-4) and the SSE
reconnect fix it enables (`relay/routes/events.py`, audit B-10/G-07).

`changes` is a materialized index over git history, not a second source of
truth — `sync()` backfills it from `history.commits()` and `record_latest()`
appends to it after every write, exactly the call sites `history.commit()`
already has.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

os.environ.setdefault("API_KEY", "test-key")

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import changes, database, events, history, mcp_server, vault
from relay.auth import require_api_key
from relay.config import settings
from relay.main import app
from relay.routes.events import catchup_frames

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
    await _db_sync()

    app.dependency_overrides[require_api_key] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _db_sync() -> None:
    """`main.py`'s lifespan calls `changes.sync` after `history.init()` —
    the test fixture above mirrors those two calls directly, so tests see
    the same startup behavior without booting the whole app."""
    async with database.connect() as db:
        await changes.sync(db)


async def _db():
    conn = await aiosqlite.connect(settings.database_path)
    conn.row_factory = aiosqlite.Row
    return conn


def git(*args: str) -> str:
    r = subprocess.run(
        ["git", f"--git-dir={settings.history_dir}", f"--work-tree={settings.vault_path}", *args],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


async def _create(client, title, content="body", tags=("homelab",)) -> dict:
    r = await client.post(
        "/posts", json={"title": title, "content": content, "tags": list(tags)}, headers=AUTH
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _all_rows(db) -> list[aiosqlite.Row]:
    async with db.execute("SELECT * FROM changes ORDER BY seq ASC") as cur:
        return list(await cur.fetchall())


# ── action classification, one write type at a time ──────────────────────────


@pytest.mark.asyncio
async def test_create_is_recorded(client):
    post = await _create(client, "Created Post")
    db = await _db()
    rows = await _all_rows(db)
    await db.close()
    mine = [r for r in rows if r["post_id"] == post["id"]]
    assert len(mine) == 1
    assert mine[0]["action"] == "create"
    assert mine[0]["title"] == "Created Post"
    assert mine[0]["tags"] == ",homelab,"


@pytest.mark.asyncio
async def test_record_latest_is_idempotent_against_an_unrecorded_noop_commit(client):
    """The bug this guards: every `history.commit(...)` call site discards
    its bool return and calls `record_latest` unconditionally — most
    consequentially `watcher._reconcile`, which calls both even when a
    debounced batch turned out to be entirely self-write no-ops, so HEAD is
    just whatever the previous write already made. Caught by hand while
    manually verifying the SSE reconnect fix end-to-end: an already
    fully-caught-up vault kept growing a duplicate row of the same commit
    every time record_latest ran again with nothing new to record."""
    post = await _create(client, "Idempotency Check")
    db = await _db()
    before = await _all_rows(db)
    assert len([r for r in before if r["post_id"] == post["id"]]) == 1

    result = await changes.record_latest(db)
    assert result == {}   # nothing new — HEAD is already the last recorded sha

    after = await _all_rows(db)
    await db.close()
    assert after == before


@pytest.mark.asyncio
async def test_watcher_noop_reconcile_does_not_duplicate_the_changes_row(client):
    """The actual bug scenario, not just the abstraction above: a watcher
    reconcile over a file that turns out to be a pure self-write must not
    grow the changes table at all, even though `_reconcile` calls
    `history.commit()` (a no-op here) and `record_latest` unconditionally."""
    from relay import watcher

    post = await _create(client, "Self Write Only")
    path = Path(settings.vault_path) / git("ls-files", "*Self Write Only.md")
    await watcher._reconcile([str(path)])   # file is untouched — was_self_write is True

    db = await _db()
    rows = await _all_rows(db)
    await db.close()
    assert len([r for r in rows if r["post_id"] == post["id"]]) == 1


async def _write_post_without_recording(db, title: str, content: str = "body") -> int:
    """Create a post via the same primitives `create_post` uses, but skip
    the `changes.record_latest` call it normally makes right after —
    simulates "another writer's commit landed, but nobody has caught the
    changes table up on it yet" without needing real concurrency."""
    post_id = await vault.allocate_id(db)
    path = vault.write_file(
        id=post_id, title=title, content=content, tags=[], source=None,
        created_at=vault.utcnow_iso(), updated_at=None, expires_at=None,
    )
    await vault.index_insert(
        db, id=post_id, title=path.stem, path=path, content=content,
        tags=[], source=None, created_at=vault.utcnow_iso(), updated_at=None, expires_at=None,
    )
    await db.commit()
    await history.commit(f"post {post_id} create: {title}")
    return post_id


@pytest.mark.asyncio
async def test_concurrent_writers_racing_record_latest_lose_no_rows(client):
    """The more serious sibling of the no-op-duplicate bug: `history._lock`
    only wraps the git commit itself, not the read-and-insert afterward, so
    two concurrent writers' `history.commit()` calls can land in either
    order relative to each other's `record_latest` call. A `-1`-based fetch
    (this feature's first version) only ever looked at the single latest
    commit — if *both* commits landed before *either* caller's turn to
    catch up, the earlier one's row was silently lost until the next full
    restart's backfill. Simulated deterministically (no real threads/asyncio
    race needed): two commits land with no catch-up in between, then the
    *second* writer's record_latest call runs first — proving both are
    still recorded, and each writer still gets its own correct seq back.
    """
    async with database.connect() as db:
        pid_a = await _write_post_without_recording(db, "Writer A")
        pid_b = await _write_post_without_recording(db, "Writer B")

        # B's request happens to call record_latest first (it "won" the race).
        seqs_b = await changes.record_latest(db, post_ids=(pid_b,))
        assert pid_b in seqs_b, "the racing-ahead caller must still get its own seq"

        # A's request calls record_latest after — A's commit already landed
        # before B's call, so _catch_up's range already swept it up; the
        # fallback lookup must still find A's seq for A's own return value.
        seqs_a = await changes.record_latest(db, post_ids=(pid_a,))
        assert pid_a in seqs_a, "the earlier commit must not be lost, and its owner must find it"

        rows = await _all_rows(db)
    a_rows = [r for r in rows if r["post_id"] == pid_a]
    b_rows = [r for r in rows if r["post_id"] == pid_b]
    assert len(a_rows) == 1, f"post A's row must exist exactly once, got {a_rows}"
    assert len(b_rows) == 1, f"post B's row must exist exactly once, got {b_rows}"
    assert a_rows[0]["seq"] < b_rows[0]["seq"]   # commit order preserved


@pytest.mark.asyncio
async def test_concurrent_catch_up_calls_do_not_duplicate_rows(client, monkeypatch):
    """A third, distinct bug from the same missing-lock family as the two
    tests above: `_catch_up`'s own read-then-insert has no lock *within a
    single call*, so two concurrent callers can both read the same "last
    recorded sha" before either has inserted anything, both compute the
    identical `sha..HEAD` range, and both redundantly re-ingest every commit
    in it — a duplicate row per overlapping caller, not a lost one. The
    range fix (see the test above) closes the data-loss failure mode; it
    does nothing about this one, which needs `changes._lock` instead.

    Found live: 5 truly concurrent `POST /posts` requests against a running
    server produced 13 changelog rows, several `(post_id, sha)` pairs
    duplicated 2-3x — never reproduced by any single-caller test.
    Reproduced here deterministically with real `asyncio` concurrency
    (`asyncio.gather` over independent connections, per `database.connect`'s
    one-connection-per-request design) and a small artificial delay in
    `_resolve` to widen the race window that `_lock` must close.
    """
    async with database.connect() as db:
        post_ids = [await _write_post_without_recording(db, f"Racer {i}") for i in range(6)]

    real_resolve = changes._resolve

    async def _slow_resolve(status, path, sha):
        await asyncio.sleep(0.01)
        return await real_resolve(status, path, sha)

    monkeypatch.setattr(changes, "_resolve", _slow_resolve)

    async def _record(post_id: int) -> dict[int, int]:
        async with database.connect() as conn:
            return await changes.record_latest(conn, post_ids=(post_id,))

    results = await asyncio.gather(*(_record(pid) for pid in post_ids))
    for pid, result in zip(post_ids, results, strict=True):
        assert pid in result, f"racer for post {pid} must still get its own seq back"

    db = await _db()
    rows = await _all_rows(db)
    await db.close()
    for pid in post_ids:
        mine = [r for r in rows if r["post_id"] == pid]
        assert len(mine) == 1, f"post {pid} must have exactly one row, got {mine}"


@pytest.mark.asyncio
async def test_update_edit_append_delete_are_recorded(client):
    post = await _create(client, "Lifecycle Post", content="one two three")
    pid = post["id"]
    await client.patch(f"/posts/{pid}", json={"content": "updated content"}, headers=AUTH)
    await client.post(f"/posts/{pid}/edit", json={"old_str": "updated", "new_str": "edited"}, headers=AUTH)
    await client.post(f"/posts/{pid}/append", json={"content": "more"}, headers=AUTH)
    await client.delete(f"/posts/{pid}", headers=AUTH)

    db = await _db()
    rows = await _all_rows(db)
    await db.close()
    actions = [r["action"] for r in rows if r["post_id"] == pid]
    assert actions == ["create", "update", "edit", "append", "delete"]


@pytest.mark.asyncio
async def test_restore_is_recorded(client):
    post = await _create(client, "Restorable")
    pid = post["id"]
    hist = (await client.get(f"/posts/{pid}/history", headers=AUTH)).json()
    sha = hist["items"][-1]["sha"]
    await client.delete(f"/posts/{pid}", headers=AUTH)
    r = await client.post(f"/posts/{pid}/restore", json={"sha": sha}, headers=AUTH)
    assert r.status_code == 200

    db = await _db()
    rows = await _all_rows(db)
    await db.close()
    actions = [r["action"] for r in rows if r["post_id"] == pid]
    assert actions == ["create", "delete", "restore"]


@pytest.mark.asyncio
async def test_tag_rename_records_one_row_per_affected_post(client):
    a = await _create(client, "Tag A", tags=("oldtag",))
    b = await _create(client, "Tag B", tags=("oldtag", "other"))
    r = await client.patch("/tags/oldtag", json={"new_name": "newtag"}, headers=AUTH)
    assert r.status_code == 200

    db = await _db()
    rows = await _all_rows(db)
    await db.close()
    renamed = {r["post_id"]: r for r in rows if r["action"] == "tag_rename"}
    assert set(renamed) == {a["id"], b["id"]}
    assert renamed[a["id"]]["tags"] == ",newtag,"


@pytest.mark.asyncio
async def test_external_edit_and_delete_are_recorded(client):
    from relay import watcher

    post = await _create(client, "External Subject", tags=("homelab",))
    path = Path(settings.vault_path) / git("ls-files", "*External Subject.md")
    path.write_text(path.read_text(encoding="utf-8") + "\nhand-edited\n", encoding="utf-8")
    await watcher._reconcile([str(path)])
    path.unlink()
    await watcher._reconcile([str(path)])

    db = await _db()
    rows = await _all_rows(db)
    await db.close()
    actions = [r["action"] for r in rows if r["post_id"] == post["id"]]
    assert actions == ["create", "external_edit", "external_delete"]


@pytest.mark.asyncio
async def test_vault_initial_import_is_not_recorded(client):
    """The startup baseline commit isn't a change anyone published."""
    db = await _db()
    rows = await _all_rows(db)
    await db.close()
    assert not any(r["sha"] == git("rev-list", "--max-parents=0", "HEAD") for r in rows)


@pytest.mark.asyncio
async def test_attachment_only_commit_yields_no_rows_and_does_not_break_the_next_one(client):
    """Attachments aren't posts (relay/changes.py filters to `.md` paths in
    Python, not via a git pathspec — see history.py's `_commits_sync`
    docstring for why a pathspec + `-1` on a plain ref is the wrong tool)."""
    import base64

    post = await _create(client, "Has An Attachment")
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    r = await client.post(
        "/attachments",
        json={"filename": "pic.png", "data": png, "post_id": post["id"], "embed": False},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    # A genuine post write right after must still be recorded correctly —
    # proves record_latest's `-1` didn't skip past the attachment commit to
    # some earlier post commit and re-ingest it.
    await client.patch(f"/posts/{post['id']}", json={"content": "after the attachment"}, headers=AUTH)

    db = await _db()
    rows = await _all_rows(db)
    await db.close()
    actions = [r["action"] for r in rows if r["post_id"] == post["id"]]
    assert actions == ["create", "update"]   # not ["create", "update", "update"] or similar duplication


# ── backfill from pre-existing history ────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_backfills_from_history_that_predates_the_table(client):
    """A vault that already had commits before this feature shipped gets a
    one-time full-history walk; a normal restart after that finds nothing new."""
    post = await _create(client, "Predates The Feature")
    async with database.connect() as db:
        await db.execute("DELETE FROM changes")
        await db.commit()
        assert await _all_rows(db) == []

        await changes.sync(db)
        rows = await _all_rows(db)
        assert any(r["post_id"] == post["id"] and r["action"] == "create" for r in rows)
        count_after_first_sync = len(rows)

        await changes.sync(db)  # normal restart: nothing new to catch up
        assert len(await _all_rows(db)) == count_after_first_sync


# ── since= filtering ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_since_seq_pages_forward(client):
    await _create(client, "First")
    r = await client.get("/changes", headers=AUTH)
    first_seq = r.json()["items"][0]["seq"]   # newest-first
    await _create(client, "Second")

    r2 = await client.get("/changes", params={"since": first_seq}, headers=AUTH)
    titles = {i["title"] for i in r2.json()["items"]}
    assert "Second" in titles
    assert "First" not in titles


@pytest.mark.asyncio
async def test_since_iso_date_filters_by_time(client):
    await _create(client, "Ancient")
    future = "2099-01-01T00:00:00Z"
    r = await client.get("/changes", params={"since": future}, headers=AUTH)
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_no_since_returns_most_recent_limit(client):
    for i in range(3):
        await _create(client, f"Post {i}")
    r = await client.get("/changes", params={"limit": 2}, headers=AUTH)
    items = r.json()["items"]
    assert len(items) == 2
    assert items[0]["seq"] > items[1]["seq"]   # newest first


# ── K-4: list_changes must clamp `limit` like every sibling paginated call ───
#
# REST is protected by FastAPI's own `Query(ge=1, le=200)`, but `changes.py`
# itself never imported/applied the `_clamp` helper every other paginated
# function (`list_posts`, `get_post_history`, `list_deleted_posts`) already
# uses — so a direct call, or the MCP tool (which passes `limit` straight
# through, no validation layer above it), was unprotected.


def test_clamp_limit_bounds_negative_zero_and_huge_values():
    assert changes._clamp_limit(-1) == 1
    assert changes._clamp_limit(0) == 1
    assert changes._clamp_limit(5) == 5
    assert changes._clamp_limit(10_000) == changes._MAX_LIMIT


@pytest.mark.asyncio
async def test_list_changes_limit_zero_is_not_silently_empty(client):
    """`limit=0` used to pass straight through to SQL's `LIMIT 0` — silently
    empty, indistinguishable from "nothing has changed"."""
    await _create(client, "Nonzero Limit Check")
    db = await _db()
    rows = await changes.list_changes(db, limit=0)
    await db.close()
    assert rows, "limit=0 must not silently look like an empty changelog"


@pytest.mark.asyncio
async def test_list_changes_negative_limit_is_bounded_not_unbounded(client):
    """SQLite treats a negative `LIMIT` as unbounded — `limit=-1` used to
    return the *entire* changelog in one response."""
    for i in range(3):
        await _create(client, f"Negative Limit Check {i}")
    db = await _db()
    rows = await changes.list_changes(db, limit=-1)
    await db.close()
    assert len(rows) <= changes._MAX_LIMIT


@pytest.mark.asyncio
async def test_list_changes_mcp_tool_clamps_limit(client):
    await _create(client, "MCP Limit Check")
    out = await mcp_server.list_changes(limit=-1)
    assert len(out["items"]) <= 200


# ── the regression test that matters most: SSE reconnect replays edits/deletes
#    to posts that already existed, not just brand-new ones (audit B-10/G-07) ─


@pytest.mark.asyncio
async def test_reconnect_replays_edit_and_delete_of_pre_existing_posts(client):
    """Before relay #198 N-4, reconnect catch-up filtered on `id > Last-Event-ID`
    — a post id, which only ever grows on *creation*. An edit or delete to a
    post that already existed when the client first connected was invisible
    on reconnect. This is the exact bug, proven fixed."""
    edited = await _create(client, "Will Be Edited", content="original")
    deleted = await _create(client, "Will Be Deleted")
    untouched = await _create(client, "Untouched")

    # The client's Last-Event-ID from before it went away: both these posts
    # already existed, so their ids are already <= this cursor under the old
    # scheme — the exact condition the old bug required.
    async with database.connect() as db:
        rows = await _all_rows(db)
        last_seq = rows[-1]["seq"]

        await client.patch(f"/posts/{edited['id']}", json={"content": "changed while away"}, headers=AUTH)
        await client.delete(f"/posts/{deleted['id']}", headers=AUTH)

        frames = await catchup_frames(db, last_seq=last_seq, tag=None)

    by_id = {}
    for f in frames:
        data = json.loads(f["data"])
        by_id[data["id"]] = f["event"]
    assert by_id[edited["id"]] == "post"
    assert by_id[deleted["id"]] == "delete"
    assert untouched["id"] not in by_id


@pytest.mark.asyncio
async def test_reconnect_collapses_multiple_edits_to_one_frame(client):
    post = await _create(client, "Edited Twice", content="v1")
    async with database.connect() as db:
        baseline = (await _all_rows(db))[-1]["seq"]
        await client.patch(f"/posts/{post['id']}", json={"content": "v2"}, headers=AUTH)
        await client.patch(f"/posts/{post['id']}", json={"content": "v3"}, headers=AUTH)

        frames = await catchup_frames(db, last_seq=baseline, tag=None)
    mine = [f for f in frames if json.loads(f["data"])["id"] == post["id"]]
    assert len(mine) == 1
    assert json.loads(mine[0]["data"])["content"] == "v3"


@pytest.mark.asyncio
async def test_reconnect_respects_tag_filter_for_both_edits_and_deletes(client):
    homelab_post = await _create(client, "Homelab Thing", tags=("homelab",))
    finance_post = await _create(client, "Finance Thing", tags=("finance",))
    async with database.connect() as db:
        baseline = (await _all_rows(db))[-1]["seq"]
        await client.patch(f"/posts/{homelab_post['id']}", json={"content": "x"}, headers=AUTH)
        await client.delete(f"/posts/{finance_post['id']}", headers=AUTH)

        frames = await catchup_frames(db, last_seq=baseline, tag="homelab")
    assert len(frames) == 1
    assert frames[0]["event"] == "post"


# ── K-3: SSE reconnect must subscribe before running catch-up ───────────────


@pytest.mark.asyncio
async def test_sse_reconnect_subscribes_before_running_catchup(client, monkeypatch):
    """K-3: `stream_events` used to run the catch-up query to completion and
    only *then* call `subscribe(tag)` — a write landing in that exact gap
    (after the catch-up SELECT, before the live queue existed) was captured
    by neither: missed by the query (already ran) and missed live (nothing
    was listening yet). Fixed by subscribing first. Pinned directly: by the
    time `catchup_frames` actually runs, this reconnect's queue must already
    be registered.
    """
    from relay import events as events_mod
    from relay.routes import events as routes_events

    await _create(client, "Gap Check Post")
    async with database.connect() as db:
        last_seq = (await _all_rows(db))[-1]["seq"]

    real_catchup = routes_events.catchup_frames
    seen_count_during_catchup = None

    async def _checking_catchup(db, *, last_seq, tag):
        nonlocal seen_count_during_catchup
        seen_count_during_catchup = events_mod.subscriber_count()
        return await real_catchup(db, last_seq=last_seq, tag=tag)

    monkeypatch.setattr(routes_events, "catchup_frames", _checking_catchup)

    class _FakeRequest:
        headers = {"last-event-id": str(last_seq)}

        async def is_disconnected(self) -> bool:
            return True  # end the live loop immediately once catch-up is done

    before = events_mod.subscriber_count()
    resp = await routes_events.stream_events(_FakeRequest(), tag=None)
    async for _frame in resp.body_iterator:
        pass  # drain to completion: catch-up runs, then the live loop exits

    assert seen_count_during_catchup == before + 1, (
        "catch-up ran before this reconnect's queue was subscribed — "
        "a write in that gap would be missed by both catch-up and live delivery"
    )
    assert events_mod.subscriber_count() == before  # unsubscribed on the way out


# ── live SSE delivery now fires on tag rename (previously silent) ────────────


@pytest.mark.asyncio
async def test_tag_rename_now_publishes_live_sse_events(client):
    post = await _create(client, "Live Rename Target", tags=("oldlive",))
    q = events.subscribe(None)
    await client.patch("/tags/oldlive", json={"new_name": "newlive"}, headers=AUTH)
    seen = []
    while not q.empty():
        seen.append(q.get_nowait())
    matching = [e for e in seen if e.get("id") == post["id"]]
    assert matching, "rename_tag did not publish an SSE event for the retagged post"
    assert matching[0]["seq"] is not None


# ── MCP surface ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_changes_mcp_tool(client):
    post = await _create(client, "Via MCP")
    out = await mcp_server.list_changes(limit=5)
    assert any(i["id"] == post["id"] and i["action"] == "create" for i in out["items"])


# ── history disabled: 503, not a silently-empty list ──────────────────────────


@pytest.mark.asyncio
async def test_list_changes_rest_is_503_when_history_disabled(client, monkeypatch):
    """The table is entirely derived from history — an empty list when
    history is off would look indistinguishable from "nothing has changed",
    same distinction `/posts/{id}/history` already draws."""
    monkeypatch.setattr(settings, "history_enabled", False)
    r = await client.get("/changes", headers=AUTH)
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_list_changes_mcp_reports_history_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "history_enabled", False)
    out = await mcp_server.list_changes()
    assert "error" in out


# ── K-2: history.commit must never run after write_lock is released ─────────
#
# Every write path used to release `vault.write_lock` before calling
# `history.commit(...)`. `history.commit` stages the *whole* work-tree
# (`git add -A`), so that gap let a concurrent writer's own already-written,
# not-yet-committed file get swept into *this* commit — and `changes._ingest`
# then attributes this commit's message/action to every post it touched,
# including the other writer's. The fix holds `write_lock` across the entire
# write-then-commit span everywhere; the two tests below pin that two ways:
# directly, across every call site, and via a real concurrent repro of the
# original misattribution.


@pytest.mark.asyncio
async def test_history_commit_always_runs_under_write_lock(client, monkeypatch):
    from relay import cleanup, vault, watcher

    real_commit = history.commit
    seen_unlocked: list[str] = []

    async def _checking_commit(message: str) -> bool:
        if not vault.write_lock.locked():
            seen_unlocked.append(message)
        return await real_commit(message)

    monkeypatch.setattr(history, "commit", _checking_commit)

    # posts.py: create / update / edit / append / delete
    post = await _create(client, "Lock Invariant Post", content="one two three", tags=("x",))
    pid = post["id"]
    await client.patch(f"/posts/{pid}", json={"content": "two"}, headers=AUTH)
    await client.post(f"/posts/{pid}/edit", json={"old_str": "two", "new_str": "three"}, headers=AUTH)
    await client.post(f"/posts/{pid}/append", json={"content": "four"}, headers=AUTH)

    # tags.py: rename_tag
    r = await client.patch("/tags/x", json={"new_name": "y"}, headers=AUTH)
    assert r.status_code == 200, r.text

    # attachments.py: add / delete
    r = await client.post(
        "/attachments",
        json={"filename": "lock-check.txt", "data": "aGVsbG8=", "folder": "Inbox"},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    r = await client.delete("/attachments/Inbox/assets/lock-check.txt", headers=AUTH)
    assert r.status_code == 200, r.text

    # revisions.py: restore_post (deleted-post recreation branch)
    hist_resp = (await client.get(f"/posts/{pid}/history", headers=AUTH)).json()
    sha = hist_resp["items"][-1]["sha"]
    await client.delete(f"/posts/{pid}", headers=AUTH)
    r = await client.post(f"/posts/{pid}/restore", json={"sha": sha}, headers=AUTH)
    assert r.status_code == 200, r.text

    # watcher.py: _reconcile (external edit batch)
    edited_path = Path(settings.vault_path) / git("ls-files", "*Lock Invariant Post*.md")
    edited_path.write_text(edited_path.read_text(encoding="utf-8") + "\nexternal line\n", encoding="utf-8")
    await watcher._reconcile([str(edited_path)])

    # cleanup.py: _delete_expired (TTL sweep)
    db = await _db()
    expiring = await _create(client, "Expiring Post")
    await db.execute(
        "UPDATE posts SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?", (expiring["id"],)
    )
    await db.commit()
    assert await cleanup._delete_expired(db) == 1
    await db.close()

    assert seen_unlocked == []


@pytest.mark.asyncio
async def test_concurrent_create_and_update_do_not_misattribute_commits(client, monkeypatch):
    """The original K-2 scenario, reproduced with real concurrency: post B
    already exists; a delay is injected into the *create* commit (widening
    the real race window that used to exist once `write_lock` was released),
    while a create (post A) and an update (post B) run concurrently via
    `asyncio.gather`. Before the fix, B's commit — not delayed — would run
    first, `git add -A`-staging A's already-written-but-uncommitted file
    alongside B's own change, and `changes._ingest` would then record post A
    under B's "update" action instead of "create". After the fix,
    `history.commit` runs while `write_lock` is still held, so B cannot even
    start writing until A's whole create-then-commit span finishes.
    """
    existing = await _create(client, "Existing Post B", content="original")
    pid_b = existing["id"]

    real_commit = history.commit

    async def _slow_commit(message: str) -> bool:
        if "create" in message:
            await asyncio.sleep(0.05)
        return await real_commit(message)

    monkeypatch.setattr(history, "commit", _slow_commit)

    async def _create_a():
        return await client.post(
            "/posts", json={"title": "New Post A", "content": "a", "tags": []}, headers=AUTH
        )

    async def _update_b():
        return await client.patch(f"/posts/{pid_b}", json={"content": "updated by B"}, headers=AUTH)

    r_a, r_b = await asyncio.gather(_create_a(), _update_b())
    assert r_a.status_code == 201, r_a.text
    assert r_b.status_code == 200, r_b.text
    pid_a = r_a.json()["id"]

    db = await _db()
    rows = await _all_rows(db)
    await db.close()
    a_rows = [r for r in rows if r["post_id"] == pid_a]
    b_rows = [r for r in rows if r["post_id"] == pid_b]
    assert len(a_rows) == 1, f"post A must have exactly one changes row, got {a_rows}"
    assert a_rows[0]["action"] == "create", (
        f"post A's own creation must never be recorded as anything else, got {a_rows[0]['action']!r}"
    )
    assert any(r["action"] == "update" for r in b_rows), f"post B's update must still be recorded, got {b_rows}"
