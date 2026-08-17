"""`list_deleted_posts` over MCP — the discovery half, for agents.

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
