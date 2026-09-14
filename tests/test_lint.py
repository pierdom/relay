"""GET /lint — vault lint (relay #198, N-5).

Each rule is a transcription of a convention already written down in #0
(tag axes, folder placement, H1/title, hub/plan freshness) or already
computable elsewhere (broken links via relay.links, embedding coverage via
relay.vectors). One test per rule, written to fail before the rule exists.
"""
from __future__ import annotations

import os
import shutil

os.environ.setdefault("API_KEY", "test-key")

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import database, embedding, history, mcp_server, vectors
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
    app.dependency_overrides[require_api_key] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(settings.database_path)
    db.row_factory = aiosqlite.Row
    if database.VEC_ENABLED:
        await vectors.load_extension(db)
    return db


async def _create(client, **fields) -> dict:
    r = await client.post("/posts", json={"title": "t", "content": "body", **fields}, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()


async def _lint(client) -> dict:
    r = await client.get("/lint", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def _find(report: dict, rule: str, post_id: int | None = None) -> dict | None:
    for item in report["items"]:
        if item["rule"] == rule and (post_id is None or item["post_id"] == post_id):
            return item
    return None


@pytest.mark.asyncio
async def test_clean_vault_has_no_findings(client):
    # The freshly seeded master doc (relay.vault.MASTER_CONTENT) carries no
    # tags out of the box — give it some, as a real vault's #0 would have.
    r = await client.patch("/posts/0", json={"tags": ["index", "meta", "reference"]}, headers=AUTH)
    assert r.status_code == 200, r.text
    await _create(client, title="Fine", tags=["homelab", "reference"])
    # Link to it from #0 so it isn't also flagged zero_backlinks.
    r = await client.patch("/posts/0", json={"content": "# Master Document\n\nSee [[Fine]].\n"}, headers=AUTH)
    assert r.status_code == 200, r.text
    report = await _lint(client)
    # The seeded master doc (id=0) and the one well-formed post above.
    assert report["checked_posts"] == 2
    assert report["items"] == []


@pytest.mark.asyncio
async def test_master_doc_is_exempt_from_convention_rules(client):
    # The freshly seeded master doc has no tags, no domain/type tag, and an
    # H1 ("# Master Document") that would mismatch a retitled #0 — every one
    # of these would be a finding on an ordinary post. None should fire here.
    r = await client.patch(
        "/posts/0", json={"title": "Vault Index", "content": "# Master Document\n\nAn index.\n"}, headers=AUTH,
    )
    assert r.status_code == 200, r.text
    report = await _lint(client)
    for rule in (
        "zero_tags", "missing_domain_tag", "missing_type_tag",
        "stale_inbox", "h1_title_mismatch", "stale_last_updated", "zero_backlinks",
    ):
        assert _find(report, rule, 0) is None, f"{rule} fired on the exempt master document"


@pytest.mark.asyncio
async def test_master_doc_is_not_exempt_from_link_checks(client):
    """The convention exemption is partial: a broken cross-link in #0 is a
    factual defect, not a convention #0 might reasonably skip, and matters
    more there than anywhere else — an index's whole job is linking right."""
    r = await client.patch(
        "/posts/0",
        json={"content": "# Master Document\n\nSee [[Nonexistent Post]] and #99999.\n"},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    report = await _lint(client)
    broken = [i for i in report["items"] if i["rule"] == "broken_link" and i["post_id"] == 0]
    assert len(broken) == 2


@pytest.mark.asyncio
async def test_master_docs_outbound_links_still_count_as_backlinks(client):
    """The exemption skips *generating findings about* #0 — it must not also
    make #0 invisible as a source of backlinks for posts it references."""
    target = await _create(client, title="Linked From Index", tags=["dev", "reference"])
    content = f"# Master Document\n\nSee [[Linked From Index]] (#{target['id']}).\n"
    r = await client.patch("/posts/0", json={"content": content}, headers=AUTH)
    assert r.status_code == 200, r.text
    report = await _lint(client)
    assert _find(report, "zero_backlinks", target["id"]) is None


@pytest.mark.asyncio
async def test_lint_vault_is_advertised_and_matches_the_rest_report(client):
    names = {t.name for t in await mcp_server.mcp.list_tools()}
    assert "lint_vault" in names, f"not in the manifest: {sorted(names)}"

    post = await _create(client, title="Untagged-ish", tags=["third-party"])
    rest_report = await _lint(client)
    mcp_report = await mcp_server.lint_vault()
    assert mcp_report == rest_report
    assert _find(mcp_report, "missing_domain_tag", post["id"]) is not None


@pytest.mark.asyncio
async def test_missing_domain_and_type_tags(client):
    post = await _create(client, title="Untagged-ish", tags=["third-party"])
    report = await _lint(client)
    assert _find(report, "missing_domain_tag", post["id"]) is not None
    assert _find(report, "missing_type_tag", post["id"]) is not None


@pytest.mark.asyncio
async def test_zero_tags(client):
    post = await _create(client, title="No tags", tags=[])
    report = await _lint(client)
    assert _find(report, "zero_tags", post["id"]) is not None
    # Zero-tag posts aren't also double-counted for the tag-axis rules.
    assert _find(report, "missing_domain_tag", post["id"]) is None


@pytest.mark.asyncio
async def test_h1_title_mismatch_after_rename(client):
    post = await _create(
        client, title="Old: Title", content="# Old: Title\n\nbody", tags=["dev", "reference"]
    )
    # Simulate the classic case: relay's filename sanitizer strips the colon on
    # rename, but the body's H1 is untouched.
    r = await client.patch(f"/posts/{post['id']}", json={"title": "Old Title"}, headers=AUTH)
    assert r.status_code == 200, r.text
    report = await _lint(client)
    finding = _find(report, "h1_title_mismatch", post["id"])
    assert finding is not None
    assert "Old: Title" in finding["detail"]


@pytest.mark.asyncio
async def test_broken_wikilink_and_idref(client):
    post = await _create(
        client,
        title="Linker",
        content="See [[Nonexistent Post]] and #99999.",
        tags=["dev", "reference"],
    )
    report = await _lint(client)
    broken = [i for i in report["items"] if i["rule"] == "broken_link" and i["post_id"] == post["id"]]
    assert len(broken) == 2


@pytest.mark.asyncio
async def test_broken_link_findings_carry_the_exact_matched_text(client):
    """`match` is what a client (the lint pane's editor) locates in the
    content to jump straight to the broken span — it must be the literal
    substring, not a paraphrase, or a naive `content.indexOf(match)` misses."""
    await _create(
        client, title="Linker Two", content="See [[Nonexistent Post]] and #99999.", tags=["dev", "reference"],
    )
    report = await _lint(client)
    broken = [i for i in report["items"] if i["rule"] == "broken_link"]
    matches = {i["match"] for i in broken}
    assert matches == {"[[Nonexistent Post]]", "#99999"}

    # Every other rule has no single in-content location to point at.
    for item in report["items"]:
        if item["rule"] not in ("broken_link", "link_to_deleted_post"):
            assert item["match"] is None, f"{item['rule']} unexpectedly carries a match"


@pytest.mark.asyncio
async def test_zero_backlinks_excludes_exempt_tags(client):
    lonely = await _create(client, title="Lonely", tags=["dev", "reference"])
    await _create(client, title="Briefing 2026-01-01", tags=["finance", "briefing"])
    # daily-digest/news-digest route to Digests/ exactly like digest/news do
    # (folders.FALLBACK) — the exemption must name all four, not just two.
    await _create(client, title="Daily Digest 2026-01-01", tags=["daily-digest"])
    report = await _lint(client)
    assert _find(report, "zero_backlinks", lonely["id"]) is not None
    # A briefing/daily-digest-tagged post is exempt: nobody is expected to
    # link a dated one.
    items = [i for i in report["items"] if i["rule"] == "zero_backlinks"]
    exempt_titles = {"Briefing 2026-01-01", "Daily Digest 2026-01-01"}
    assert not exempt_titles & {i["title"] for i in items}


@pytest.mark.asyncio
async def test_zero_backlinks_cleared_by_a_wikilink(client):
    target = await _create(client, title="Linked Target", tags=["dev", "reference"])
    await _create(client, title="Linker Two", content="See [[Linked Target]].", tags=["dev", "reference"])
    report = await _lint(client)
    assert _find(report, "zero_backlinks", target["id"]) is None


@pytest.mark.asyncio
async def test_empty_tag_config(client):
    r = await client.post("/tags/never-used/config", json={"ttl_hours": 24}, headers=AUTH)
    assert r.status_code == 200, r.text
    report = await _lint(client)
    finding = _find(report, "empty_tag_config")
    assert finding is not None
    assert "never-used" in finding["detail"]


@pytest.mark.asyncio
async def test_stale_inbox_flags_a_domain_tagged_post_stuck_in_inbox(client):
    post = await _create(client, title="Stuck", tags=["dev", "reference"])
    db = await _db()
    try:
        await db.execute("UPDATE posts SET path = ? WHERE id = ?", ("Inbox/Stuck.md", post["id"]))
        await db.commit()
    finally:
        await db.close()
    report = await _lint(client)
    assert _find(report, "stale_inbox", post["id"]) is not None


@pytest.mark.asyncio
async def test_stale_last_updated_on_hub_post(client):
    post = await _create(client, title="Old Hub", tags=["dev", "hub"])
    db = await _db()
    try:
        await db.execute(
            "UPDATE posts SET updated_at = ? WHERE id = ?", ("2020-01-01T00:00:00Z", post["id"])
        )
        await db.commit()
    finally:
        await db.close()
    report = await _lint(client)
    assert _find(report, "stale_last_updated", post["id"]) is not None


@pytest.mark.asyncio
async def test_stale_last_updated_falls_back_to_created_at_when_never_edited(client):
    """updated_at is NULL until a post's first edit (service/posts.py never
    backfills it at creation) — a hub/plan post created once and never
    touched again is the single most common way one goes stale, and it must
    not be silently exempt just because updated_at itself is empty."""
    post = await _create(client, title="Old Hub Never Edited", tags=["dev", "hub"])
    db = await _db()
    try:
        await db.execute(
            "UPDATE posts SET created_at = ? WHERE id = ?", ("2020-01-01T00:00:00Z", post["id"])
        )
        await db.commit()
    finally:
        await db.close()
    report = await _lint(client)
    assert _find(report, "stale_last_updated", post["id"]) is not None


@pytest.mark.asyncio
async def test_master_doc_post_count_drift(client):
    r = await client.patch(
        "/posts/0", json={"content": "# Master Document\n\n*Last updated: today · 999 post*\n"}, headers=AUTH
    )
    assert r.status_code == 200, r.text
    report = await _lint(client)
    finding = _find(report, "master_doc_post_count", 0)
    assert finding is not None
    assert "999" in finding["detail"]


@pytest.mark.asyncio
async def test_master_doc_post_count_ignores_an_unrelated_number_before_the_real_count(client):
    """A bare `\\d+\\s*post\\b` would match the *first* "N post ..." substring
    in the header regardless of what it's counting — a per-domain breakdown
    mentioned before the real, middot-prefixed total must not be mistaken
    for it. Deliberately singular ("post entries", not "posts"): the plural
    doesn't even reach `\\bpost\\b`'s own word boundary, so a pluralized
    example would pass whether or not this rule is fixed."""
    r = await client.patch(
        "/posts/0",
        json={"content": "# Master Document\n\nHomelab has 12 post entries below.\n\n*Last updated: today · 1 post*\n"},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    report = await _lint(client)
    # 1 matches reality (only #0 exists in this fresh vault), so no drift
    # finding — and specifically not one complaining about "12", which isn't
    # the middot-prefixed total claim this rule is about.
    finding = _find(report, "master_doc_post_count", 0)
    assert finding is None, f"matched the wrong number: {finding}"


@pytest_asyncio.fixture
async def fake_embeddings(monkeypatch):
    assert database.VEC_ENABLED, "sqlite-vec should load in this dev/CI environment"
    monkeypatch.setattr(settings, "embedding_enabled", True)
    monkeypatch.setattr(embedding, "get_backend", lambda: embedding.FakeBackend())


@pytest.mark.asyncio
async def test_zero_chunks_skipped_when_embeddings_disabled(client):
    report = await _lint(client)
    assert any(s.startswith("zero_chunks:") for s in report["skipped_rules"])


@pytest.mark.asyncio
async def test_zero_chunks_flags_a_code_fence_only_post(client, fake_embeddings):
    # chunking._strip_code_fences drops fenced code before chunking; a post
    # whose entire body is one fenced code block chunks to zero rows —
    # sync_post_chunks runs inline on create, so no backfill call is needed.
    post = await _create(
        client, title="Just Code", content="```\nrclone sync a b\n```", tags=["homelab", "reference"]
    )
    report = await _lint(client)
    assert _find(report, "zero_chunks", post["id"]) is not None


# ── link_to_deleted_post — needs real git history, so a separate client ─────


@pytest.fixture(autouse=True)
def _reset_history_probe():
    history.reset_state_for_tests()
    yield
    history.reset_state_for_tests()


@pytest_asyncio.fixture
async def git_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "git_vault"))
    monkeypatch.setattr(settings, "history_enabled", True)  # isolated_vault turns it off
    await database.init_db()
    await history.init()
    app.dependency_overrides[require_api_key] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None, reason="needs a git binary")
async def test_link_to_deleted_post_is_distinguished_from_a_plain_broken_link(git_client):
    gone = await _create(git_client, title="Gone Post", tags=["dev", "reference"])
    r = await git_client.delete(f"/posts/{gone['id']}", headers=AUTH)
    assert r.status_code in (200, 204), r.text
    await _create(
        git_client,
        title="Refers To Gone",
        content=f"See [[Gone Post]] and #{gone['id']}, also #99999.",
        tags=["dev", "reference"],
    )
    report = await _lint(git_client)
    to_deleted = [i for i in report["items"] if i["rule"] == "link_to_deleted_post"]
    broken = [i for i in report["items"] if i["rule"] == "broken_link"]
    assert len(to_deleted) == 2  # [[Gone Post]] and #<gone id>
    assert len(broken) == 1  # #99999, never existed
