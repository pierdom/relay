from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("API_KEY", "test-key")

import aiosqlite
import pytest
import pytest_asyncio
import yaml
from httpx import ASGITransport, AsyncClient

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


# ── SSE propagation of edits (backlog #198 item 3) ───────────────────────────


@pytest.mark.asyncio
async def test_update_publishes_sse_post_event(client):
    from relay import events

    post = await _create_post(client)
    q = events.subscribe(None)
    while not q.empty():  # drain the create event
        q.get_nowait()

    r = await client.patch(f"/posts/{post['id']}", json={"content": "edited"}, headers=AUTH)
    assert r.status_code == 200

    assert not q.empty(), "update_post should broadcast an SSE event"
    envelope = q.get_nowait()
    events.unsubscribe(q, None)
    assert envelope["type"] == "post"
    assert envelope["id"] == post["id"]
    assert envelope["data"]["content"] == "edited"


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


# ── unknown front-matter properties (N-2) ────────────────────────────────────


@pytest.mark.asyncio
async def test_update_preserves_unknown_frontmatter_properties(client, vault_dir):
    """Obsidian Properties (aliases, cssclasses, custom fields) must survive a
    relay-initiated write, not be silently dropped."""
    post = await _create_post(client, title="Props Note")
    path = vault_dir / "Inbox" / "Props Note.md"
    meta, body = frontmatter.parse(path.read_text(encoding="utf-8"))
    path.write_text(
        frontmatter.serialize(meta, body, {"aliases": ["Alt Name"], "cssclasses": "wide"}),
        encoding="utf-8",
    )
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await vault.rebuild_index(db)  # picks up the hand-edit, as a relay restart would

    r = await client.patch(f"/posts/{post['id']}", json={"content": "edited via API"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["properties"] == {"aliases": ["Alt Name"], "cssclasses": "wide"}

    meta2, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
    assert meta2["properties"] == {"aliases": ["Alt Name"], "cssclasses": "wide"}


@pytest.mark.asyncio
async def test_create_leaves_properties_empty(client):
    post = await _create_post(client, title="Fresh Note")
    assert post["properties"] == {}


@pytest.mark.asyncio
async def test_idless_note_with_a_bare_date_property_does_not_crash_rebuild(vault_dir):
    """A hand-made, id-less note (rebuild_index's needs_id stamping path) with a
    custom property that YAML auto-parses into a `datetime.date` must not
    crash the id-stamping write or the index insert."""
    (vault_dir / "Hand Made.md").write_text(
        "---\ntags: [dev]\nreview_date: 2024-01-15\n---\n\nbody\n", encoding="utf-8"
    )
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await vault.rebuild_index(db)  # must not raise
        async with db.execute("SELECT properties FROM posts WHERE title = 'Hand Made'") as cur:
            row = await cur.fetchone()
    assert vault.decode_properties(row["properties"]) == {"review_date": "2024-01-15T00:00:00Z"}
    meta, _ = frontmatter.parse((vault_dir / "Hand Made.md").read_text(encoding="utf-8"))
    assert meta["properties"] == {"review_date": "2024-01-15T00:00:00Z"}


# ── partial edits + optimistic concurrency (N-1) ──────────────────────────────


@pytest.mark.asyncio
async def test_response_includes_an_etag(client):
    post = await _create_post(client)
    assert isinstance(post["etag"], str) and post["etag"]


@pytest.mark.asyncio
async def test_etag_header_matches_body_field_and_changes_after_a_write(client):
    r = await client.post("/posts", json={"title": "Tagged", "content": "v1"}, headers=AUTH)
    post = r.json()
    assert r.headers["etag"] == f'"{post["etag"]}"'

    r2 = await client.patch(f"/posts/{post['id']}", json={"content": "v2"}, headers=AUTH)
    updated = r2.json()
    assert r2.headers["etag"] == f'"{updated["etag"]}"'
    assert updated["etag"] != post["etag"]


@pytest.mark.asyncio
async def test_patch_without_if_match_behaves_exactly_as_before(client):
    """Backward compatibility is the load-bearing invariant here — every
    existing caller omits if_match."""
    post = await _create_post(client)
    r = await client.patch(f"/posts/{post['id']}", json={"content": "new content"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["content"] == "new content"


@pytest.mark.asyncio
async def test_patch_with_matching_if_match_succeeds(client):
    post = await _create_post(client)
    r = await client.patch(
        f"/posts/{post['id']}", json={"content": "new content", "if_match": post["etag"]}, headers=AUTH
    )
    assert r.status_code == 200
    assert r.json()["content"] == "new content"


@pytest.mark.asyncio
async def test_patch_with_stale_if_match_returns_409_with_current_post(client):
    post = await _create_post(client)
    stale_etag = post["etag"]
    # Someone else updates the post first...
    await client.patch(f"/posts/{post['id']}", json={"content": "from elsewhere"}, headers=AUTH)
    # ...then a write based on the stale read is rejected, not silently applied.
    r = await client.patch(
        f"/posts/{post['id']}", json={"content": "clobber attempt", "if_match": stale_etag}, headers=AUTH
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["current"]["content"] == "from elsewhere"
    # The clobbering write must never have landed.
    current = (await client.get(f"/posts/{post['id']}", headers=AUTH)).json()
    assert current["content"] == "from elsewhere"


@pytest.mark.asyncio
async def test_if_match_header_takes_precedence_over_body_field(client):
    post = await _create_post(client)
    stale_etag = post["etag"]
    await client.patch(f"/posts/{post['id']}", json={"content": "from elsewhere"}, headers=AUTH)
    # A matching body if_match would pass on its own; the stale header must win and 409.
    r = await client.patch(
        f"/posts/{post['id']}",
        json={"content": "clobber attempt", "if_match": "whatever-matches-nothing"},
        headers={**AUTH, "If-Match": f'"{stale_etag}"'},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_edit_post_replaces_the_unique_match(client):
    post = await _create_post(client, content="one two three")
    r = await client.post(f"/posts/{post['id']}/edit", json={"old_str": "two", "new_str": "TWO"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["content"] == "one TWO three"


@pytest.mark.asyncio
async def test_edit_post_422_when_text_not_found(client):
    post = await _create_post(client, content="one two three")
    r = await client.post(f"/posts/{post['id']}/edit", json={"old_str": "nope", "new_str": "x"}, headers=AUTH)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_edit_post_422_when_text_matches_more_than_once(client):
    post = await _create_post(client, content="two two")
    r = await client.post(f"/posts/{post['id']}/edit", json={"old_str": "two", "new_str": "x"}, headers=AUTH)
    assert r.status_code == 422
    assert "2 times" in r.json()["detail"]


@pytest.mark.asyncio
async def test_edit_post_rejects_identical_old_and_new(client):
    post = await _create_post(client, content="one two three")
    r = await client.post(f"/posts/{post['id']}/edit", json={"old_str": "two", "new_str": "two"}, headers=AUTH)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_edit_post_internal_protection_catches_a_race_without_caller_if_match(client, monkeypatch):
    """The central safety claim of edit_post: it protects its own
    read-modify-write even when the *caller* never opts into if_match, by
    always passing the etag from its own read through to update_post. Proven
    here by injecting a concurrent write between edit_post's read and its
    eventual call into update_post — a real race would interleave here;
    monkeypatching `_fetch` forces it deterministically instead of relying on
    real (flaky) timing."""
    from relay.service import posts as posts_module

    post = await _create_post(client, content="one two three")
    pid = post["id"]
    original_fetch = posts_module._fetch
    calls = {"n": 0}

    async def racing_fetch(db, post_id):
        calls["n"] += 1
        row = await original_fetch(db, post_id)
        if calls["n"] == 1 and post_id == pid:
            # A concurrent writer completes here, between edit_post's own
            # read (this call) and update_post's fresh re-read inside the lock.
            await client.patch(f"/posts/{pid}", json={"content": "raced in"}, headers=AUTH)
        return row

    monkeypatch.setattr(posts_module, "_fetch", racing_fetch)
    r = await client.post(f"/posts/{pid}/edit", json={"old_str": "two", "new_str": "TWO"}, headers=AUTH)
    assert r.status_code == 409
    # racing_fetch is a transparent passthrough for every call after the
    # first, so no need to restore _fetch before this plain GET.
    current = (await client.get(f"/posts/{pid}", headers=AUTH)).json()
    assert current["content"] == "raced in"  # the edit never applied


@pytest.mark.asyncio
async def test_edit_post_stale_if_match_returns_409_and_does_not_apply(client):
    post = await _create_post(client, content="one two three")
    stale_etag = post["etag"]
    await client.patch(f"/posts/{post['id']}", json={"content": "one two three, edited"}, headers=AUTH)
    r = await client.post(
        f"/posts/{post['id']}/edit",
        json={"old_str": "two", "new_str": "TWO", "if_match": stale_etag},
        headers=AUTH,
    )
    assert r.status_code == 409
    current = (await client.get(f"/posts/{post['id']}", headers=AUTH)).json()
    assert current["content"] == "one two three, edited"


@pytest.mark.asyncio
async def test_edit_post_history_diff_is_minimal(client, vault_dir):
    """The whole point of edit_post over PATCH: only the target substring
    changes on disk, not incidental reformatting of the rest of the body."""
    post = await _create_post(client, title="Diff Check", content="line one\nline two\nline three\n")
    await client.post(
        f"/posts/{post['id']}/edit", json={"old_str": "line two", "new_str": "LINE TWO"}, headers=AUTH
    )
    text = (vault_dir / "Inbox" / "Diff Check.md").read_text(encoding="utf-8")
    assert "line one" in text and "line three" in text and "LINE TWO" in text


@pytest.mark.asyncio
async def test_append_post_adds_a_blank_line_between_old_and_new(client):
    post = await _create_post(client, content="first paragraph")
    r = await client.post(f"/posts/{post['id']}/append", json={"content": "second paragraph"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["content"] == "first paragraph\n\nsecond paragraph"


@pytest.mark.asyncio
async def test_append_post_to_empty_content_has_no_leading_blank_line(client):
    post = await _create_post(client, content="")
    r = await client.post(f"/posts/{post['id']}/append", json={"content": "first paragraph"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["content"] == "first paragraph"


@pytest.mark.asyncio
async def test_append_post_stale_if_match_returns_409(client):
    post = await _create_post(client, content="first")
    stale_etag = post["etag"]
    await client.patch(f"/posts/{post['id']}", json={"content": "changed elsewhere"}, headers=AUTH)
    r = await client.post(
        f"/posts/{post['id']}/append", json={"content": "second", "if_match": stale_etag}, headers=AUTH
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_edit_and_append_404_on_missing_post(client):
    r1 = await client.post("/posts/999999/edit", json={"old_str": "a", "new_str": "b"}, headers=AUTH)
    assert r1.status_code == 404
    r2 = await client.post("/posts/999999/append", json={"content": "x"}, headers=AUTH)
    assert r2.status_code == 404


# ── folder placement (derive from primary tag; never auto-move) ──────────────


@pytest.mark.asyncio
async def test_create_files_post_by_primary_domain_tag(client, vault_dir):
    await _create_post(client, title="QTH", tags=["radio", "reference"])
    assert (vault_dir / "Radio" / "QTH.md").exists()
    assert not (vault_dir / "QTH.md").exists()


@pytest.mark.asyncio
async def test_first_domain_tag_wins_over_leading_type_tag(client, vault_dir):
    # non-domain tags are skipped; the first *domain* tag decides the folder
    await _create_post(client, title="My Game Setup", tags=["reference", "gear", "gaming"])
    assert (vault_dir / "Gaming" / "My Game Setup.md").exists()


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


@pytest.mark.asyncio
async def test_set_tag_config_rejects_zero_ttl(client):
    r = await client.post("/tags/news/config", json={"ttl_hours": 0}, headers=AUTH)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_set_tag_config_rejects_negative_ttl(client):
    r = await client.post("/tags/news/config", json={"ttl_hours": -5}, headers=AUTH)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_delete_nonexistent_post_is_404(client):
    r = await client.delete("/posts/99999", headers=AUTH)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_backlinks_nonexistent_post_is_404(client):
    r = await client.get("/posts/99999/backlinks", headers=AUTH)
    assert r.status_code == 404
