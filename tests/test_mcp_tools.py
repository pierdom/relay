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
import time

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from fastmcp.server.auth.redirect_validation import validate_redirect_uri
from httpx import ASGITransport, AsyncClient
from joserfc import jwt as _joserfc_jwt
from joserfc.errors import InvalidClaimError
from joserfc.jwk import KeySet, RSAKey
from mcp.server.auth.provider import TokenError

from relay import database, embedding, history, mcp_server, vectors
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


# A tool's name is not part of what a keyword-searching agent sees — only the
# description is (found live: `lint_vault` and `get_backlinks` both existed and
# worked, but an agent searching "lint" or "backlinks" couldn't find either one,
# because neither word ever appeared outside the tool's own name). Generic
# verbs shared by half the surface (get/list/set/…) are excluded — they carry
# no signal about *which* tool matched — leaving each name's distinctive
# domain word(s), stemmed to five characters to tolerate a plural or -ing/-ed
# form, checked against the description text a search would actually match on.
_GENERIC_NAME_WORDS = {
    "get", "list", "set", "create", "add", "update", "delete", "trigger", "new", "post", "posts",
}


def _stem(word: str) -> str:
    return word[:5] if len(word) > 5 else word


@pytest.mark.asyncio
async def test_every_tools_description_mentions_its_own_distinctive_name_words():
    for tool in await mcp_server.mcp.list_tools():
        description = (tool.description or "").lower()
        for word in tool.name.split("_"):
            if len(word) <= 2 or word in _GENERIC_NAME_WORDS:
                continue
            stem = _stem(word)
            assert stem in description, (
                f"{tool.name}: {word!r} (from its own name) doesn't appear anywhere in its "
                f"description — a keyword search for it would miss this tool. description: "
                f"{tool.description!r}"
            )


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
async def test_an_agent_can_find_related_posts_missing_a_wikilink(client, monkeypatch):
    """relay #198, N-7: a post similar enough to be worth a glance that isn't
    already cross-linked — the similarity lookup itself is exercised end to
    end in tests/test_vectors.py; this is about the tool wiring through to it
    and applying the link-exclusion."""
    monkeypatch.setattr(settings, "embedding_enabled", True)
    monkeypatch.setattr(embedding, "get_backend", lambda: embedding.FakeBackend())

    a = (await client.post("/posts", json={"title": "Alpha", "content": "x", "tags": ["homelab"]},
                           headers=HEADERS)).json()
    b = (await client.post("/posts", json={"title": "Beta", "content": "y", "tags": ["homelab"]},
                           headers=HEADERS)).json()

    async def fake_similar(db, *, exclude_post_id, title, content, **kwargs):
        return [(a["id"], 0.1)]

    monkeypatch.setattr(vectors, "find_similar_posts", fake_similar)

    out = await mcp_server.get_related(id=b["id"])
    assert [item["id"] for item in out["items"]] == [a["id"]]

    assert "error" in await mcp_server.get_related(id=999_999)

    monkeypatch.setattr(settings, "embedding_enabled", False)
    assert "error" in await mcp_server.get_related(id=b["id"])


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
async def test_rename_tag_reports_an_invalid_existing_tag_argument(client):
    """K-6: only `new_name` was validated/cleaned inline — `tag` (the existing
    tag being renamed) reached `service.rename_tag` unchecked, which raises a
    bare, message-less `InvalidTag` when it normalises to empty. Uncaught,
    that surfaced as a completely blank error for the caller."""
    out = await mcp_server.rename_tag(tag="!!!", new_name="valid")
    assert "error" in out
    assert out["error"], "the error message must not be blank"


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


@pytest.mark.asyncio
async def test_the_new_n1_tools_are_advertised_to_clients():
    names = {t.name for t in await mcp_server.mcp.list_tools()}
    assert {"edit_post", "append_post"} <= names, f"not in the manifest: {sorted(names)}"


@pytest.mark.asyncio
async def test_edit_post_replaces_the_unique_match(client):
    r = await client.post("/posts", json={"title": "MCP Edit", "content": "one two three",
                                          "tags": ["homelab"]}, headers=HEADERS)
    pid = r.json()["id"]
    out = await mcp_server.edit_post(id=pid, old_str="two", new_str="TWO")
    assert "error" not in out, out
    assert out["content"] == "one TWO three"


@pytest.mark.asyncio
async def test_edit_post_reports_ambiguous_and_missing_matches(client):
    r = await client.post("/posts", json={"title": "MCP Edit 2", "content": "two two",
                                          "tags": ["homelab"]}, headers=HEADERS)
    pid = r.json()["id"]
    ambiguous = await mcp_server.edit_post(id=pid, old_str="two", new_str="x")
    assert "error" in ambiguous and "2 times" in ambiguous["error"]

    missing = await mcp_server.edit_post(id=pid, old_str="nope", new_str="x")
    assert "error" in missing


@pytest.mark.asyncio
async def test_edit_post_rejects_identical_old_and_new_str(client):
    """REST rejects this via PostEdit's validator; the MCP tool calls
    service.edit_post directly, bypassing that model entirely — the check
    must also live in the service layer or MCP would silently no-op instead
    of erroring like REST does for the identical request."""
    r = await client.post("/posts", json={"title": "MCP No-Op Edit", "content": "one two three",
                                          "tags": ["homelab"]}, headers=HEADERS)
    pid = r.json()["id"]
    out = await mcp_server.edit_post(id=pid, old_str="two", new_str="two")
    assert "error" in out, out


@pytest.mark.asyncio
async def test_edit_post_rejects_empty_old_str(client):
    """str.count("") is len(content)+1, not 0 — without a guard this would
    fall through to a confusing "matches N times" error instead of a clear
    reject (REST catches this via PostEdit's min_length=1; MCP calls
    service.edit_post directly, bypassing that model)."""
    r = await client.post("/posts", json={"title": "MCP Empty Old Str", "content": "one two three",
                                          "tags": ["homelab"]}, headers=HEADERS)
    pid = r.json()["id"]
    out = await mcp_server.edit_post(id=pid, old_str="", new_str="x")
    assert "error" in out, out


@pytest.mark.asyncio
async def test_append_post_adds_a_blank_line(client):
    r = await client.post("/posts", json={"title": "MCP Append", "content": "first",
                                          "tags": ["homelab"]}, headers=HEADERS)
    pid = r.json()["id"]
    out = await mcp_server.append_post(id=pid, content="second")
    assert "error" not in out, out
    assert out["content"] == "first\n\nsecond"


@pytest.mark.asyncio
async def test_update_post_if_match_conflict_reports_current_content(client):
    r = await client.post("/posts", json={"title": "MCP Conflict", "content": "v1",
                                          "tags": ["homelab"]}, headers=HEADERS)
    post = r.json()
    stale_etag = post["etag"]
    await client.patch(f"/posts/{post['id']}", json={"content": "v2 from elsewhere"}, headers=HEADERS)

    out = await mcp_server.update_post(id=post["id"], content="clobber attempt", if_match=stale_etag)
    assert "error" in out, out
    assert out["current"]["content"] == "v2 from elsewhere"

    # The clobbering write must never have landed.
    live = await mcp_server.get_post(id=post["id"])
    assert live["content"] == "v2 from elsewhere"


@pytest.mark.asyncio
async def test_edit_and_append_stale_if_match_are_rejected(client):
    r = await client.post("/posts", json={"title": "MCP Race", "content": "one two three",
                                          "tags": ["homelab"]}, headers=HEADERS)
    post = r.json()
    stale_etag = post["etag"]
    await client.patch(f"/posts/{post['id']}", json={"content": "one two three, edited"}, headers=HEADERS)

    edit_out = await mcp_server.edit_post(id=post["id"], old_str="two", new_str="TWO", if_match=stale_etag)
    assert "error" in edit_out, edit_out

    append_out = await mcp_server.append_post(id=post["id"], content="more", if_match=stale_etag)
    assert "error" in append_out, append_out


@pytest.mark.asyncio
async def test_initialize_announces_relays_own_version_and_branding():
    """`serverInfo` is what a client shows beside the server's name.

    Under mcp 1.x the version there was the *SDK's* — relay 1.6.1 announced
    itself as "1.29.0" — and leaving it unset on 2.x makes it an empty string.
    Neither is a fact about relay, so it is set explicitly; the icons and
    website URL (SEP-973) ride the same struct and are pinned here with it.
    """
    from relay import __version__

    # fastmcp (relay #313 migration) names this attribute `_mcp_server`, not the old
    # mcp SDK's `_lowlevel_server` — same LowLevelServer type underneath.
    opts = mcp_server.mcp._mcp_server.create_initialization_options()
    assert opts.server_name == "relay"
    assert opts.server_version == __version__
    assert opts.website_url == settings.relay_base_url.rstrip("/")
    assert [i.mime_type for i in opts.icons] == ["image/svg+xml", "image/png"]


@pytest.mark.asyncio
async def test_mcp_rejects_oversized_request_bodies(client):
    """relay #313 Phase 1: fastmcp's `http_app()` has no parameter to configure a
    request-body-size cap, but still enforces one — it builds the old mcp SDK's
    `TransportSecuritySettings` internally and only overrides the DNS-rebinding half
    of it (see `mcp_server.py`'s comment above `mcp_http_app`). That's an unexposed
    internal of a wrapped third-party library, not a documented relay setting, so pin
    it here: a future fastmcp version silently dropping it should fail a test, not
    surface as `add_attachment(data=…)` quietly accepting an unbounded body."""
    oversized = "x" * (5 * 1024 * 1024)  # over the 4 MiB default
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "update_post", "arguments": {"id": 0, "content": oversized}},
    }
    resp = await client.post(
        "/mcp",
        json=payload,
        headers={**HEADERS, "Accept": "application/json, text/event-stream"},
    )
    assert resp.status_code == 413


# ── relay #313 Phase 2: _RelayOIDCProxy's sub/email allowlist enforcement ──
#
# _extract_upstream_claims doesn't touch `self`, so these call it directly on the
# class rather than constructing a real _RelayOIDCProxy — that constructor makes a
# live network call to OIDC discovery (see mcp_server._build_auth's docstring),
# which has no place in a unit test. _oidc_jwks_cache/_oidc_metadata_cache are
# pre-populated instead of going through _load_pocketid_jwks, so no network call
# happens here either.

_TEST_ISSUER = "https://id.example.com"
_TEST_AUDIENCE = "relay-mcp-client"


def _signed_id_token(**claim_overrides) -> tuple[str, KeySet]:
    """A real, signed id_token plus the KeySet that verifies it — exercises
    _RelayOIDCProxy's actual jwt.decode + JWTClaimsRegistry path, not a mock of it."""
    key = RSAKey.generate_key(2048, parameters={"kid": "test-kid"}, private=True)
    now = int(time.time())
    claims = {
        "iss": _TEST_ISSUER,
        "aud": _TEST_AUDIENCE,
        "sub": "user-123",
        "email": "user@example.com",
        "email_verified": True,
        "iat": now,
        "nbf": now - 5,
        "exp": now + 600,
        **claim_overrides,
    }
    token = _joserfc_jwt.encode({"alg": "RS256", "kid": "test-kid"}, claims, key)
    return token, KeySet([key])


def _prime_jwks_cache(monkeypatch, keyset: KeySet):
    monkeypatch.setattr(mcp_server, "_oidc_metadata_cache", {"issuer": _TEST_ISSUER, "jwks_uri": "unused"})
    monkeypatch.setattr(mcp_server, "_oidc_jwks_cache", keyset)
    monkeypatch.setattr(settings, "oidc_client_id", _TEST_AUDIENCE)


@pytest.mark.asyncio
async def test_relay_oidc_proxy_allows_a_sub_on_the_allowlist(monkeypatch):
    id_token, keyset = _signed_id_token(sub="user-123")
    _prime_jwks_cache(monkeypatch, keyset)
    monkeypatch.setattr(settings, "oidc_allowed_subs", "user-123")
    monkeypatch.setattr(settings, "oidc_allowed_emails", "")

    claims = await mcp_server._RelayOIDCProxy._extract_upstream_claims(object(), {"id_token": id_token})
    assert claims == {"sub": "user-123", "email": "user@example.com"}


@pytest.mark.asyncio
async def test_relay_oidc_proxy_denies_a_sub_not_on_the_allowlist(monkeypatch):
    id_token, keyset = _signed_id_token(sub="uninvited-user")
    _prime_jwks_cache(monkeypatch, keyset)
    monkeypatch.setattr(settings, "oidc_allowed_subs", "user-123")
    monkeypatch.setattr(settings, "oidc_allowed_emails", "")

    with pytest.raises(TokenError) as exc_info:
        await mcp_server._RelayOIDCProxy._extract_upstream_claims(object(), {"id_token": id_token})
    assert exc_info.value.error == "unauthorized_client"


@pytest.mark.asyncio
async def test_relay_oidc_proxy_allows_any_identity_when_no_allowlist_configured(monkeypatch):
    id_token, keyset = _signed_id_token(sub="anyone-at-all")
    _prime_jwks_cache(monkeypatch, keyset)
    monkeypatch.setattr(settings, "oidc_allowed_subs", "")
    monkeypatch.setattr(settings, "oidc_allowed_emails", "")

    claims = await mcp_server._RelayOIDCProxy._extract_upstream_claims(object(), {"id_token": id_token})
    assert claims["sub"] == "anyone-at-all"


@pytest.mark.asyncio
async def test_relay_oidc_proxy_denies_a_forged_audience(monkeypatch):
    """The id_token's signature is genuine (signed by the test key relay would
    fetch from PocketID's own JWKS), but its `aud` doesn't match this relay's
    client_id — JWTClaimsRegistry must catch this, not just the signature check."""
    id_token, keyset = _signed_id_token(sub="user-123", aud="a-different-relay")
    _prime_jwks_cache(monkeypatch, keyset)
    monkeypatch.setattr(settings, "oidc_allowed_subs", "")
    monkeypatch.setattr(settings, "oidc_allowed_emails", "")

    with pytest.raises(InvalidClaimError):  # joserfc's own claims-validation error, not TokenError
        await mcp_server._RelayOIDCProxy._extract_upstream_claims(object(), {"id_token": id_token})


@pytest.mark.asyncio
async def test_relay_oidc_proxy_denies_missing_id_token():
    with pytest.raises(TokenError) as exc_info:
        await mcp_server._RelayOIDCProxy._extract_upstream_claims(object(), {"access_token": "opaque"})
    assert exc_info.value.error == "invalid_grant"


def test_mcp_allowed_client_redirect_uri_patterns_permits_loopback(monkeypatch):
    """relay #313 Phase 4: caught while writing docs, not by a test — fastmcp's
    `validate_redirect_uri` gives loopback URIs no automatic exemption once
    `allowed_patterns` is a real list (confirmed by reading it), unlike the old
    hand-rolled `mcp_oauth/provider.py` (deleted in this same phase), which
    explicitly allowed loopback http regardless of the https host allowlist. Uses fastmcp's
    real matcher, not a re-implementation of it — this proves the patterns
    actually work against the library that consumes them, not just that the
    Python list looks right."""
    monkeypatch.setattr(settings, "mcp_allowed_redirect_hosts", "claude.ai,*.mistral.ai")
    patterns = settings.mcp_allowed_client_redirect_uri_patterns

    assert validate_redirect_uri("http://localhost:41000/cb", patterns)
    assert validate_redirect_uri("http://127.0.0.1:8080/cb", patterns)
    assert validate_redirect_uri("https://claude.ai/cb", patterns)
    assert validate_redirect_uri("https://sub.mistral.ai/cb", patterns)
    assert not validate_redirect_uri("https://mistral.ai/cb", patterns)  # wildcard excludes its own apex
    assert not validate_redirect_uri("https://evilmistral.ai/cb", patterns)  # dot-boundary
    assert not validate_redirect_uri("https://evil.example.com/cb", patterns)
    assert not validate_redirect_uri("http://evil.example.com/cb", patterns)  # remote cleartext


def test_mcp_allowed_client_redirect_uri_patterns_none_when_hosts_empty(monkeypatch):
    """Empty MCP_ALLOWED_REDIRECT_HOSTS must translate to fastmcp's `None`
    (trust each DCR client's own declared URI), not `[]` (allow nothing) —
    relay's own "empty = opt-out" default, preserved."""
    monkeypatch.setattr(settings, "mcp_allowed_redirect_hosts", "")
    assert settings.mcp_allowed_client_redirect_uri_patterns is None


# ── K-5: attachment tools must catch InvalidFolder, like REST already does ──


@pytest.mark.asyncio
async def test_list_attachments_reports_an_invalid_folder(client):
    """K-5: only `PostNotFound` was caught — an invalid `folder` (one that
    fails `folders.is_valid_name`, e.g. `".."`) reached the caller as an
    uncaught exception instead of this codebase's own `{"error": ...}`
    convention every other MCP error path follows. No traversal actually
    occurs (the exception fires before any filesystem access); this is about
    the error surfacing cleanly, not a path-traversal fix."""
    out = await mcp_server.list_attachments(folder="..")
    assert "error" in out
    assert out["error"]
