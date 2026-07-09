from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from relay import links
from relay.auth import require_api_key
from relay.config import settings
from relay.database import init_db
from relay.main import app

AUTH = {"Authorization": "Bearer test-key"}


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    await init_db()

    async def override_auth():
        return None

    app.dependency_overrides[require_api_key] = override_auth
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _create(client, title, content="body", tags=None):
    r = await client.post(
        "/posts",
        json={"title": title, "content": content, "tags": tags or ["a"]},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    return r.json()


# ── pure resolver ────────────────────────────────────────────────────────────


def test_extract_wikilinks_exact_alias_and_broken():
    t2i = {"my note": 5}
    ls = links.extract_links("see [[My Note]], [[My Note|the note]], [[Ghost]]", t2i, {5})
    assert [(l.target, l.alias, l.resolved_id) for l in ls] == [
        ("My Note", None, 5),
        ("My Note", "the note", 5),
        ("Ghost", None, None),
    ]


def test_extract_idrefs_and_ignores_headings():
    ls = links.extract_links("# Heading\nsee #5 and #999", {}, {5})
    idrefs = [l for l in ls if l.kind == "id"]
    assert [(l.target, l.resolved_id) for l in idrefs] == [("5", 5), ("999", None)]


def test_rewrite_preserves_alias_and_heading():
    out, changed = links.rewrite_wikilink_targets(
        "[[Old]] [[old|shown]] [[Old#Sec]] [[Other]]", "Old", "New"
    )
    assert changed
    assert out == "[[New]] [[New|shown]] [[New#Sec]] [[Other]]"


# ── endpoints ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_link_index_lists_id_and_title(client):
    a = await _create(client, "Alpha")
    r = await client.get("/links", headers=AUTH)
    assert r.status_code == 200
    items = {i["id"]: i["title"] for i in r.json()["items"]}
    assert items[a["id"]] == "Alpha"
    assert 0 in items  # master document


@pytest.mark.asyncio
async def test_backlinks_via_wikilink_and_idref(client):
    target = await _create(client, "Target Note")
    b = await _create(client, "B", content="see [[Target Note]]")
    c = await _create(client, "C", content=f"ref #{target['id']}")
    await _create(client, "D", content="no links here")

    r = await client.get(f"/posts/{target['id']}/backlinks", headers=AUTH)
    assert r.status_code == 200
    ids = {i["id"] for i in r.json()["items"]}
    assert ids == {b["id"], c["id"]}


@pytest.mark.asyncio
async def test_backlinks_404_for_missing(client):
    r = await client.get("/posts/9999/backlinks", headers=AUTH)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_rename_rewrites_inbound_wikilinks(client):
    a = await _create(client, "Old Name")
    b = await _create(client, "B", content="see [[Old Name]] and [[Old Name|alias]] and #%d" % a["id"])

    r = await client.patch(f"/posts/{a['id']}", json={"title": "New Name"}, headers=AUTH)
    assert r.status_code == 200

    updated = (await client.get(f"/posts/{b['id']}", headers=AUTH)).json()
    assert "[[New Name]]" in updated["content"]
    assert "[[New Name|alias]]" in updated["content"]
    assert "[[Old Name" not in updated["content"]
    assert "#%d" % a["id"] in updated["content"]  # id-ref untouched (stable)
