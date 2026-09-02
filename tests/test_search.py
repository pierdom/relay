from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import database, vault
from relay.auth import require_api_key
from relay.config import settings
from relay.main import app
from relay.service import _fts_query

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


async def _create(client, title, content, tags=None):
    payload = {"title": title, "content": content, "tags": tags or ["dev"]}
    r = await client.post("/posts", json=payload, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()


async def _titles(client, q):
    r = await client.get(f"/posts?search={q}", headers=AUTH)
    assert r.status_code == 200, r.text
    return [i["title"] for i in r.json()["items"]]


# ── _fts_query sanitizer (unit) ──────────────────────────────────────────────


def test_fts_query_multi_term_prefix():
    assert _fts_query("wireguard proton") == '"wireguard"* OR "proton"*'


def test_fts_query_strips_operators():
    # quotes, parens, colons, stars, dashes must not reach FTS5 as syntax
    assert _fts_query('"wire-guard": (proton*)') == '"wire"* OR "guard"* OR "proton"*'


def test_fts_query_keeps_underscore_identifiers():
    assert _fts_query("RELAY_VAULT_PATH") == '"RELAY_VAULT_PATH"*'


def test_fts_query_empty_when_no_tokens():
    assert _fts_query("") is None
    assert _fts_query("   ") is None
    assert _fts_query('"()*:-') is None


# ── FTS behaviour (integration) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fts_is_enabled(vault_dir):
    # the whole suite assumes the SQLite build ships FTS5 (bundled since 3.9)
    assert database.FTS_ENABLED is True


@pytest.mark.asyncio
async def test_multi_term_ranks_full_match_first(client):
    await _create(client, "Both", "wireguard over proton VPN")
    await _create(client, "OnlyOne", "wireguard tunnel only")
    titles = await _titles(client, "wireguard proton")
    # OR-joined, bm25-ranked: a post matching every term still outranks one
    # matching only some, but the partial match now surfaces instead of
    # vanishing entirely (see _fts_query's docstring for why AND was dropped).
    assert titles[0] == "Both"
    assert "OnlyOne" in titles


@pytest.mark.asyncio
async def test_or_matches_when_not_every_term_present(client):
    await _create(client, "Proton note", "notes about proton VPN")
    # "spaceship" appears nowhere in the vault — under the old AND semantics
    # this would return nothing; OR still surfaces the post on 'proton'.
    assert "Proton note" in await _titles(client, "proton spaceship")


@pytest.mark.asyncio
async def test_prefix_matching(client):
    await _create(client, "Prefixed", "notes on wireguard config")
    assert "Prefixed" in await _titles(client, "wire")


@pytest.mark.asyncio
async def test_ranking_weights_title_above_body(client):
    await _create(client, "Mentions grafana in passing", "a long body that name-drops grafana once")
    await _create(client, "Grafana", "dashboards")  # term in the title
    # title-weighted bm25 should surface the canonical 'Grafana' post first
    assert (await _titles(client, "grafana"))[0] == "Grafana"


@pytest.mark.asyncio
async def test_special_char_query_does_not_500(client):
    await _create(client, "Doc", "some content")
    for q in ['"', "(proton", "a AND b", "foo:bar", "-x", "***"]:
        r = await client.get("/posts", params={"search": q}, headers=AUTH)
        assert r.status_code == 200, f"{q!r} -> {r.status_code}"


@pytest.mark.asyncio
async def test_search_reflects_update_and_delete(client):
    post = await _create(client, "Mutable", "initial kryptonite text")
    assert "Mutable" in await _titles(client, "kryptonite")

    # update removes the term → trigger keeps FTS in sync
    await client.patch(f"/posts/{post['id']}", json={"content": "now about vibranium"}, headers=AUTH)
    assert await _titles(client, "kryptonite") == []
    assert "Mutable" in await _titles(client, "vibranium")

    # delete drops it entirely
    await client.delete(f"/posts/{post['id']}", headers=AUTH)
    assert await _titles(client, "vibranium") == []


@pytest.mark.asyncio
async def test_fts_survives_index_rebuild(client, vault_dir):
    await _create(client, "Persisted", "content mentioning obscureword")
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await vault.rebuild_index(db)
    # triggers keep posts_fts consistent through the wipe/repopulate
    assert "Persisted" in await _titles(client, "obscureword")


@pytest.mark.asyncio
async def test_search_matches_tags(client):
    await _create(client, "Tagged", "body text", tags=["homelab", "reference"])
    assert "Tagged" in await _titles(client, "homelab")


@pytest.mark.asyncio
async def test_mode_keyword_is_the_default_and_still_works(client):
    await _create(client, "Explicit mode", "wireguard config notes")
    assert "Explicit mode" in await _titles(client, "wireguard")
    r = await client.get("/posts", params={"search": "wireguard", "mode": "keyword"}, headers=AUTH)
    assert r.status_code == 200
    assert "Explicit mode" in [i["title"] for i in r.json()["items"]]


@pytest.mark.asyncio
async def test_mode_rejects_unknown_value(client):
    r = await client.get("/posts", params={"search": "x", "mode": "banana"}, headers=AUTH)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_semantic_mode_503_when_embeddings_disabled(client):
    # settings.embedding_enabled defaults False and this fixture doesn't turn it
    # on (relay.vectors's proof-of-concept scope, see CLAUDE.md) — must error
    # loud, not return an empty/keyword-only list (see SemanticSearchUnavailable).
    r = await client.get("/posts", params={"search": "anything", "mode": "semantic"}, headers=AUTH)
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_hybrid_mode_503_when_embeddings_disabled(client):
    r = await client.get("/posts", params={"search": "anything", "mode": "hybrid"}, headers=AUTH)
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_like_fallback_when_fts_disabled(client, monkeypatch):
    # simulate a SQLite build without FTS5 — service must fall back to LIKE
    monkeypatch.setattr(database, "FTS_ENABLED", False)
    await _create(client, "Fallback", "content with substringy text")
    # substring (not a whole token) — only LIKE can match this
    assert "Fallback" in await _titles(client, "stringy")
    # a raw FTS operator must not blow up the LIKE path either
    r = await client.get("/posts", params={"search": 'quote " here'}, headers=AUTH)
    assert r.status_code == 200
