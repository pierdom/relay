"""The MCP tools that close REST/MCP gaps: discovery, preview, backlinks, rename.

`list_deleted_posts` is the discovery half of recovery; `get_post_revision` is
the preview half. Both existed on REST first, and an agent had neither.

The UI got a recovery browser and REST got `/posts/deleted`, but an agent had
neither: `restore_post` will put back any post whose id you know, and after a
delete nobody knows it. An agent that clobbered or removed something could
therefore describe the problem perfectly and still not undo it.

Registration is asserted through `mcp.list_tools()` rather than by reading the
source, because that is what a client actually receives — a tool that exists as a
function but never reaches the manifest is invisible in exactly the way this
feature was.
"""
from __future__ import annotations

import os
import shutil

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import database, history, mcp_server
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
    monkeypatch.setattr(settings, "history_enabled", True)
    await database.init_db()
    await history.init()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_the_tool_is_advertised_to_clients():
    names = {t.name for t in await mcp_server.mcp.list_tools()}
    assert "list_deleted_posts" in names, f"not in the manifest: {sorted(names)}"


@pytest.mark.asyncio
async def test_an_agent_can_discover_a_deleted_post_and_restore_it(client):
    """The round trip an agent has to be able to make unaided."""
    r = await client.post("/posts", json={"title": "Digest mattutino — 16 agosto 2026",
                                          "content": "corpo da salvare", "tags": ["digest"]},
                          headers=HEADERS)
    pid = r.json()["id"]
    # Unrelated writes in between, so the delete commit's parent is not the
    # create commit — see test_deleted_posts.py for why that shape matters.
    for i in range(3):
        await client.post("/posts", json={"title": f"Unrelated {i}", "content": "x",
                                          "tags": ["homelab"]}, headers=HEADERS)
    assert (await client.delete(f"/posts/{pid}", headers=HEADERS)).status_code in (200, 204)

    listed = await mcp_server.list_deleted_posts()
    entry = next(d for d in listed["items"] if d["id"] == pid)
    assert entry["title"] == "Digest mattutino — 16 agosto 2026"
    assert entry["reason"] == "deleted"

    # The sha the tool hands out must be one restore_post accepts — reporting a
    # sha it refuses is the bug this whole feature shipped with once.
    out = await mcp_server.restore_post(id=pid, sha=entry["sha"])
    assert "error" not in out, out
    assert out["content"].strip() == "corpo da salvare"


@pytest.mark.asyncio
async def test_it_reports_history_being_off_rather_than_returning_nothing(client, monkeypatch):
    """An empty list and "recovery is impossible here" are different answers, and
    an agent that cannot tell them apart will report the wrong one."""
    monkeypatch.setattr(history, "enabled", lambda: False)
    out = await mcp_server.list_deleted_posts()
    assert "error" in out, out


@pytest.mark.asyncio
async def test_an_agent_can_read_a_revision_before_restoring_it(client):
    """Preview, then restore — the discipline the UI enforces and agents could not.

    `get_post_history` returns metadata only, so before this an agent could only
    restore blind and read the result afterwards. Restoring is undoable, but
    "undo it and see" is a poor way to answer "what would this give me back".
    """
    r = await client.post("/posts", json={"title": "Clobbered Note", "content": "the good version",
                                          "tags": ["homelab"]}, headers=HEADERS)
    pid = r.json()["id"]
    await client.patch(f"/posts/{pid}", json={"content": "the bad overwrite"}, headers=HEADERS)

    hist = await mcp_server.get_post_history(id=pid)
    oldest = hist["items"][-1]["sha"]

    rev = await mcp_server.get_post_revision(id=pid, sha=oldest)
    assert rev["content"].strip() == "the good version"
    assert rev["title"] == "Clobbered Note"

    # Read-only: the live post is untouched by the preview.
    live = await mcp_server.get_post(id=pid)
    assert live["content"].strip() == "the bad overwrite"


@pytest.mark.asyncio
async def test_get_post_revision_accepts_a_short_sha_and_reports_a_bad_one(client):
    r = await client.post("/posts", json={"title": "Shortened", "content": "v1",
                                          "tags": ["homelab"]}, headers=HEADERS)
    pid = r.json()["id"]
    hist = await mcp_server.get_post_history(id=pid)
    short = hist["items"][-1]["short_sha"]
    assert (await mcp_server.get_post_revision(id=pid, sha=short))["content"].strip() == "v1"

    bad = await mcp_server.get_post_revision(id=pid, sha="0" * 12)
    assert "error" in bad, bad


@pytest.mark.asyncio
async def test_an_agent_can_see_what_links_to_a_post_before_breaking_it(client):
    """The house rule is one canonical post per topic, cross-linked by id. An
    agent about to rewrite or delete a post needs to know what points at it."""
    target = (await client.post("/posts", json={"title": "Canonical Topic", "content": "x",
                                                "tags": ["homelab"]}, headers=HEADERS)).json()
    pid = target["id"]
    await client.post("/posts", json={"title": "By Title", "content": "see [[Canonical Topic]]",
                                      "tags": ["homelab"]}, headers=HEADERS)
    await client.post("/posts", json={"title": "By Id", "content": f"see #{pid}",
                                      "tags": ["homelab"]}, headers=HEADERS)
    await client.post("/posts", json={"title": "Unrelated", "content": "nothing here",
                                      "tags": ["homelab"]}, headers=HEADERS)

    out = await mcp_server.get_backlinks(id=pid)
    titles = {i["title"] for i in out["items"]}
    assert titles == {"By Title", "By Id"}, titles   # both link syntaxes, and only those

    assert "error" in await mcp_server.get_backlinks(id=999_999)


@pytest.mark.asyncio
async def test_an_agent_can_rename_a_tag_across_every_post(client):
    """One atomic pass. Retagging post by post is slower and leaves the vault
    half-migrated if it stops partway."""
    ids = [(await client.post("/posts", json={"title": f"Tagged {i}", "content": "x",
                                              "tags": ["homelb", "radio"]},
                              headers=HEADERS)).json()["id"] for i in range(3)]

    out = await mcp_server.rename_tag(tag="homelb", new_name="homelab")
    assert "error" not in out, out
    names = {t["tag"] for t in out["tags"]}
    assert "homelab" in names and "homelb" not in names, names

    for pid in ids:
        post = await mcp_server.get_post(id=pid)
        assert "homelab" in post["tags"] and "homelb" not in post["tags"]
        assert "radio" in post["tags"], "an unrelated tag was disturbed"


@pytest.mark.asyncio
async def test_rename_tag_normalises_and_rejects_an_empty_name(client):
    """The proxy sends the raw string to REST, which normalises it in `TagRename`;
    the in-process tool must normalise identically or the two surfaces disagree
    about what a tag is called."""
    await client.post("/posts", json={"title": "N", "content": "x", "tags": ["old"]},
                      headers=HEADERS)
    out = await mcp_server.rename_tag(tag="old", new_name="  Mixed Case!  ")
    assert "error" not in out, out
    assert "mixedcase" in {t["tag"] for t in out["tags"]}

    assert "error" in await mcp_server.rename_tag(tag="mixedcase", new_name="!!!")


@pytest.mark.asyncio
async def test_list_posts_can_browse_by_folder_and_reverse_the_sort(client):
    """Three parameters REST had and MCP did not: folder, sort, order."""
    await client.post("/posts", json={"title": "Radio One", "content": "x", "tags": ["radio"]},
                      headers=HEADERS)
    await client.post("/posts", json={"title": "Radio Two", "content": "x", "tags": ["radio"]},
                      headers=HEADERS)
    await client.post("/posts", json={"title": "Home One", "content": "x", "tags": ["homelab"]},
                      headers=HEADERS)

    radio = await mcp_server.list_posts(folder="Radio")
    assert {p["title"] for p in radio["items"]} == {"Radio One", "Radio Two"}

    asc = await mcp_server.list_posts(sort="created", order="asc")
    desc = await mcp_server.list_posts(sort="created", order="desc")
    asc_titles = [p["title"] for p in asc["items"]]
    assert asc_titles == list(reversed([p["title"] for p in desc["items"]]))
