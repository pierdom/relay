"""get_post's ``section`` and ``outline`` (relay.sections), and a search
``match`` that only repeats the excerpt coming back null."""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import mcp_server, sections
from relay.auth import require_api_key
from relay.config import settings
from relay.errors import SectionNotFound, SectionNotUnique
from relay.main import app
from relay.models import Match, match_from_chunk, match_from_snippet

AUTH = {"Authorization": "Bearer test-key"}

BODY = """# Server Build

Why this machine exists.

## Hardware
The parts.
### Disks
```
## not a heading
```
Two NVMe drives.
## Backups
Nightly to the **storage box**.
### Disks
A tape drive, someday.
"""


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    from relay import database

    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    await database.init_db()

    async def override_auth():
        return None

    app.dependency_overrides[require_api_key] = override_auth
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _create(client, title="Server Build", content=BODY) -> dict:
    r = await client.post("/posts", json={"title": title, "content": content, "tags": ["homelab"]}, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()


# ── parsing ──────────────────────────────────────────────────────────────────


def test_outline_skips_fenced_headings_and_the_title_h1():
    parsed = sections.parse("Server Build", BODY)
    assert [(s.level, s.path) for s in parsed] == [
        (2, "Hardware"), (3, "Hardware > Disks"), (2, "Backups"), (3, "Backups > Disks"),
    ]


def test_an_h1_that_is_not_the_title_is_a_section():
    parsed = sections.parse("Notes", "# Part one\n## Detail\ntext\n# Part two\nmore")
    assert [s.path for s in parsed] == ["Part one", "Part one > Detail", "Part two"]


def test_closing_hashes_are_dropped_but_a_trailing_hash_in_a_word_is_kept():
    parsed = sections.parse("T", "## Setup ##\nx\n## C#\ny")
    assert [s.path for s in parsed] == ["Setup", "C#"]


def test_a_section_includes_its_subsections_and_stops_at_the_next_peer():
    parsed = sections.parse("Server Build", BODY)
    hardware = sections.text(BODY, sections.find(parsed, "Hardware"))
    assert hardware.startswith("## Hardware\nThe parts.\n### Disks")
    assert "Two NVMe drives." in hardware
    assert "Backups" not in hardware


def test_intro_is_the_text_before_the_first_heading_or_the_whole_post():
    assert sections.intro(BODY, sections.parse("Server Build", BODY)) == "# Server Build\n\nWhy this machine exists."
    assert sections.intro("no headings here", []) == "no headings here"


def test_find_takes_a_path_suffix_without_case_or_markdown():
    parsed = sections.parse("Server Build", BODY)
    assert sections.find(parsed, "backups > DISKS").path == "Backups > Disks"
    assert sections.find(parsed, "**Backups**").path == "Backups"


def test_find_refuses_an_ambiguous_heading_and_names_the_paths():
    with pytest.raises(SectionNotUnique, match="Hardware > Disks; Backups > Disks"):
        sections.find(sections.parse("Server Build", BODY), "Disks")


def test_find_lists_the_sections_when_nothing_matches():
    with pytest.raises(SectionNotFound, match="Sections: Hardware; Hardware > Disks"):
        sections.find(sections.parse("Server Build", BODY), "Networking")


# ── REST and MCP ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rest_section_returns_only_that_section(client):
    post = await _create(client)
    r = await client.get(f"/posts/{post['id']}", params={"section": "Backups > Disks"}, headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["section"] == "Backups > Disks"
    assert data["content"] == "### Disks\nA tape drive, someday."
    # The etag stays the whole post's, so an edit_post with if_match still works.
    assert data["etag"] == post["etag"]


@pytest.mark.asyncio
async def test_rest_outline_lists_headings_and_narrows_content_to_the_intro(client):
    post = await _create(client)
    data = (await client.get(f"/posts/{post['id']}", params={"outline": "true"}, headers=AUTH)).json()
    assert [e["path"] for e in data["outline"]] == ["Hardware", "Hardware > Disks", "Backups", "Backups > Disks"]
    assert all(e["chars"] > 0 for e in data["outline"])
    assert data["content"] == "# Server Build\n\nWhy this machine exists."


@pytest.mark.asyncio
async def test_rest_plain_read_is_unchanged(client):
    post = await _create(client)
    data = (await client.get(f"/posts/{post['id']}", headers=AUTH)).json()
    assert data["content"] == post["content"]
    assert data["section"] is None and data["outline"] is None


@pytest.mark.asyncio
async def test_rest_unknown_section_is_a_422_listing_the_sections(client):
    post = await _create(client)
    r = await client.get(f"/posts/{post['id']}", params={"section": "Networking"}, headers=AUTH)
    assert r.status_code == 422
    assert "Hardware > Disks" in r.json()["detail"]


@pytest.mark.asyncio
async def test_mcp_get_post_reads_one_section(client):
    post = await _create(client)
    data = await mcp_server.get_post(id=post["id"], section="Hardware")
    assert data["section"] == "Hardware"
    assert "Two NVMe drives." in data["content"] and "storage box" not in data["content"]


@pytest.mark.asyncio
async def test_mcp_empty_section_reads_the_whole_post(client):
    """The MCP SDK can't tell an omitted argument from an empty one."""
    post = await _create(client)
    data = await mcp_server.get_post(id=post["id"], section="")
    assert data["content"] == post["content"]


# ── a match that repeats the excerpt ─────────────────────────────────────────


def test_snippet_from_the_top_of_the_post_repeats_an_excerpt_that_opens_with_it():
    m = match_from_snippet("The \x02firmware\x03 is pinned here…")
    assert m is not None and m.anchored
    assert m.repeats("The firmware is pinned here and more…")
    assert not m.repeats("Something else entirely…")


def test_mid_post_snippet_repeats_only_when_the_excerpt_shows_the_hit():
    m = match_from_snippet("…the opening words lead up to the \x02firmware\x03 pin and beyond")
    assert m is not None and not m.anchored
    assert m.repeats("Intro: the opening words lead up to the firmware pin and beyond that…")
    # Repetitive text: the words before the hit are in the excerpt, the hit isn't.
    assert not m.repeats("Intro: the opening words lead up to the end of the excerpt…")


def test_a_short_mid_post_snippet_never_counts_as_a_repeat():
    assert not Match("x", "the firmware", anchored=False).repeats("the firmware is everywhere…")


def test_a_chunk_from_the_top_of_the_post_repeats_the_excerpt_despite_its_heading():
    m = match_from_chunk("Storage", "Proxmox cheat sheet. Only the commands specific to Proxmox are listed here.")
    assert m is not None and m.text.startswith("Storage: ")
    assert m.repeats("Proxmox cheat sheet. Only the commands specific to Proxmox are listed here, tested…")
    assert not m.repeats("Proxmox VE notes. Something else first…")


@pytest.mark.asyncio
async def test_keyword_hit_in_the_opening_has_no_match(client):
    await _create(client, title="Pinning", content="The firmware is pinned to v2.3 on every box. " + "filler " * 80)
    data = (await client.get("/posts", params={"search": "firmware", "summary": "true"}, headers=AUTH)).json()
    item = next(i for i in data["items"] if i["title"] == "Pinning")
    assert "firmware" in item["excerpt"]
    assert item["match"] is None


def test_a_semantic_match_heading_is_a_path_get_post_accepts():
    m = match_from_chunk("Hardware > **Disks**", "Two NVMe drives in a mirror.")
    assert m is not None and m.text.startswith("Hardware > Disks: ")
    heading = m.text.split(": ", 1)[0]
    assert sections.find(sections.parse("Server Build", BODY), heading).path == "Hardware > Disks"


def test_a_path_with_the_wrong_parent_falls_back_to_a_unique_heading():
    """Chunking's paths skip H1s, so a semantic match can name the wrong parent."""
    content = "# Records\n## Local CNAME records\nx\n# Decision log\n### Rejected options\ny"
    parsed = sections.parse("Proxy", content)
    assert sections.find(parsed, "Local CNAME records > Rejected options").path == "Decision log > Rejected options"
