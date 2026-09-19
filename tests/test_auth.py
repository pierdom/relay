from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from httpx import ASGITransport, AsyncClient

from relay import auth
from relay.config import settings
from relay.main import app


def test_session_roundtrip_carries_identity():
    token = auth.create_session(sub="user-123", email="Me@Example.com")
    payload = auth.verify_session(token)
    assert payload is not None
    assert payload["sub"] == "user-123"
    assert payload["email"] == "Me@Example.com"


def test_session_rejects_tampered_token():
    token = auth.create_session(sub="user-123", email="me@example.com")
    assert auth.verify_session(token + "x") is None
    assert auth.verify_session("garbage") is None


def test_session_expires(monkeypatch):
    token = auth.create_session(sub="u", email="e@x.com")
    # Negative max-age forces any token (age >= 0) to read as expired.
    monkeypatch.setattr(settings, "session_max_age_hours", -1)
    assert auth.verify_session(token) is None


def test_session_key_paste_default_subject():
    payload = auth.verify_session(auth.create_session())
    assert payload["sub"] == "apikey"


def test_allowed_emails_parsing(monkeypatch):
    monkeypatch.setattr(settings, "oidc_allowed_emails", "  A@x.com, b@Y.com ,")
    assert settings.allowed_emails == {"a@x.com", "b@y.com"}
    monkeypatch.setattr(settings, "oidc_allowed_emails", "")
    assert settings.allowed_emails == set()


def test_authorized_no_allowlist_allows_any(monkeypatch):
    from relay.routes.auth import _authorized

    monkeypatch.setattr(settings, "oidc_allowed_subs", "")
    monkeypatch.setattr(settings, "oidc_allowed_emails", "")
    assert _authorized("anyone", "x@y.com", False) is True


def test_authorized_by_sub(monkeypatch):
    from relay.routes.auth import _authorized

    monkeypatch.setattr(settings, "oidc_allowed_subs", "good-sub")
    monkeypatch.setattr(settings, "oidc_allowed_emails", "")
    assert _authorized("good-sub", "", False) is True
    assert _authorized("bad-sub", "", False) is False


def test_authorized_email_requires_verified(monkeypatch):
    from relay.routes.auth import _authorized

    monkeypatch.setattr(settings, "oidc_allowed_subs", "")
    monkeypatch.setattr(settings, "oidc_allowed_emails", "me@x.com")
    # Correct email but unverified -> denied (the spoofing bypass we're closing).
    assert _authorized("s", "me@x.com", False) is False
    # Verified allowlisted email -> allowed.
    assert _authorized("s", "me@x.com", True) is True
    # Verified but not on the list -> denied.
    assert _authorized("s", "other@x.com", True) is False


def test_oidc_enabled_flag(monkeypatch):
    monkeypatch.setattr(settings, "oidc_issuer", "")
    assert settings.oidc_enabled is False
    monkeypatch.setattr(settings, "oidc_issuer", "https://id.example.com")
    monkeypatch.setattr(settings, "oidc_client_id", "cid")
    monkeypatch.setattr(settings, "oidc_client_secret", "secret")
    assert settings.oidc_enabled is True


@pytest.mark.asyncio
async def test_auth_me_unauthenticated():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False


@pytest.mark.asyncio
async def test_auth_me_with_session_cookie():
    token = auth.create_session(sub="u", email="me@example.com")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/auth/me", cookies={auth.SESSION_COOKIE: token})
    body = r.json()
    assert body["authenticated"] is True
    assert body["email"] == "me@example.com"


@pytest.mark.asyncio
async def test_session_cookie_authorizes_protected_route(monkeypatch, tmp_path):
    # A valid signed session cookie should satisfy require_api_key on real routes.
    from relay import database

    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    await database.init_db()
    token = auth.create_session(sub="u", email="me@example.com")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/posts", cookies={auth.SESSION_COOKIE: token})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_session_endpoint_rejects_wrong_key():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        bad = await c.post("/session", json={"key": "not-the-key"})
        assert bad.status_code == 401
        empty = await c.post("/session", json={})
        assert empty.status_code == 401
        ok = await c.post("/session", json={"key": "test-key"})
    assert ok.status_code == 200
    # the response set a valid, verifiable session cookie
    token = ok.cookies.get("relay_session")
    assert token and auth.verify_session(token) is not None


# ── OIDC login/callback: /id/<id> deep link survives the round trip ─────────


class _FakeOIDCClient:
    """Stands in for authlib's per-provider client — real `authorize_redirect`/
    `authorize_access_token` need a live IdP; these two are all the routes call."""

    def __init__(self, claims: dict | None = None) -> None:
        self._claims = claims or {"sub": "user-1", "email": "me@example.com", "email_verified": True}

    async def authorize_redirect(self, request, redirect_uri):
        from fastapi.responses import RedirectResponse

        return RedirectResponse("https://idp.example.com/authorize", status_code=302)

    async def authorize_access_token(self, request):
        return {"userinfo": self._claims}


async def _login_then_callback(c: AsyncClient, post: str | None):
    # `SessionMiddleware`'s `relay_oauth` cookie is `Secure` (https_only follows
    # settings.secure_cookies, baked in at app-construction time — monkeypatching
    # the setting in a test doesn't reach an already-built middleware instance),
    # so httpx's cookie jar won't forward it to the next request over this test's
    # plain-http transport. Attach it explicitly, same workaround already used by
    # test_session_cookie_authorizes_protected_route for the same reason.
    params = {"post": post} if post is not None else {}
    login = await c.get("/auth/login", params=params, follow_redirects=False)
    oauth_cookie = login.cookies.get("relay_oauth")
    return await c.get("/auth/callback", cookies={"relay_oauth": oauth_cookie}, follow_redirects=False)


@pytest.mark.asyncio
async def test_oidc_login_roundtrips_a_pending_post_id_through_the_callback(monkeypatch):
    # relay #(this PR): /id/<id> redirects to /?post=<id>, which an
    # unauthenticated visitor can only consume after finishing OIDC login —
    # a full-page redirect out to the IdP and back, unlike the in-page
    # API-key-paste login. Without stashing `post` across that round trip,
    # the callback's hardcoded "/" silently drops the deep link.
    from relay.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "_client", lambda: _FakeOIDCClient())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        callback = await _login_then_callback(c, "42")
    assert callback.status_code == 303
    assert callback.headers["location"] == "/?post=42"


@pytest.mark.asyncio
async def test_oidc_login_without_a_pending_post_id_falls_back_to_plain_root(monkeypatch):
    from relay.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "_client", lambda: _FakeOIDCClient())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        callback = await _login_then_callback(c, None)
    assert callback.headers["location"] == "/"


@pytest.mark.asyncio
async def test_oidc_login_ignores_a_malformed_post_param(monkeypatch):
    from relay.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "_client", lambda: _FakeOIDCClient())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        callback = await _login_then_callback(c, "not-a-number")
    assert callback.headers["location"] == "/"


@pytest.mark.asyncio
async def test_mcp_metadata_absent_when_oauth_disabled():
    # With OAuth off (the default this app was imported under), fastmcp's `auth=` is a
    # plain TokenVerifier (relay #313 Phase 1's `_StaticBearerAuth` — fastmcp's own
    # docstring: token verifiers "typically don't provide authentication routes by
    # default"), not a full OAuthProvider/RemoteAuthProvider — only those mount the
    # OAuth discovery routes, so this path is never mounted at all: an ordinary 404
    # from Starlette's own routing, not an auth decision. The enabled-mode metadata
    # (this path serving real content once a real OAuthProvider is built, Phase 2)
    # was verified live against a mock OIDC server, not by an automated test here —
    # a real gap, not a documented-elsewhere one; worth a dedicated test later.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/.well-known/oauth-protected-resource/mcp")
    # No metadata document is served — never a 200 discovery doc that would invite a
    # client into an OAuth flow relay isn't running.
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_mcp_accepts_public_host_and_origin(monkeypatch, tmp_path):
    # Regression: the SDK's default host (127.0.0.1) auto-enables localhost-scoped
    # DNS-rebinding protection, which 421s any real Host (e.g. relay.geon.im) and
    # 403s a browser Origin — breaking remote /mcp entirely. We disable it (auth +
    # HTTPS + proxy are the real controls), so a real Host/Origin must pass through
    # to the transport, not be blocked at 421/403.
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}},
    }
    headers = {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Origin": "https://claude.ai",
    }
    # The MCP streamable-HTTP session manager only runs inside the app lifespan.
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://relay.geon.im") as c:
            r = await c.post("/mcp", headers=headers, json=body)
    assert r.status_code not in (421, 403)  # not blocked by Host/Origin validation
    assert r.status_code == 200


# ── deauthorization: the allowlist must revoke live sessions ─────────────────


def test_session_dies_when_sub_leaves_the_allowlist(monkeypatch):
    """Dropping a sub from OIDC_ALLOWED_SUBS must revoke sessions already minted.

    `_authorized()` only runs at the OIDC callback, so without a per-request
    re-check a removed user would coast for the full SESSION_MAX_AGE_HOURS (30d).
    Same guarantee the MCP OAuth refresh grant gives.
    """
    monkeypatch.setattr(settings, "oidc_allowed_subs", "user-123,other")
    token = auth.create_session(sub="user-123", email="me@example.com")
    assert auth.verify_session(token) is not None

    monkeypatch.setattr(settings, "oidc_allowed_subs", "other")
    assert auth.verify_session(token) is None


def test_session_survives_while_sub_stays_allowlisted(monkeypatch):
    monkeypatch.setattr(settings, "oidc_allowed_subs", "user-123,other")
    token = auth.create_session(sub="user-123", email="me@example.com")
    assert auth.verify_session(token)["sub"] == "user-123"


def test_apikey_session_is_exempt_from_the_allowlist(monkeypatch):
    """Break-glass: the API-key paste proves possession of API_KEY, so it must
    keep working even though `sub=apikey` is in no OIDC allowlist."""
    monkeypatch.setattr(settings, "oidc_allowed_subs", "user-123")
    assert auth.verify_session(auth.create_session())["sub"] == auth.APIKEY_SUB


def test_no_allowlist_leaves_sessions_untouched(monkeypatch):
    monkeypatch.setattr(settings, "oidc_allowed_subs", "")
    monkeypatch.setattr(settings, "oidc_allowed_emails", "someone@else.com")
    assert auth.verify_session(auth.create_session(sub="anyone")) is not None


@pytest.mark.asyncio
async def test_deauthorized_session_is_401_on_a_protected_route(monkeypatch, tmp_path):
    from relay import database

    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    await database.init_db()
    monkeypatch.setattr(settings, "oidc_allowed_subs", "user-123")
    token = auth.create_session(sub="user-123", email="me@example.com")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set(auth.SESSION_COOKIE, token)
        assert (await c.get("/posts")).status_code == 200
        monkeypatch.setattr(settings, "oidc_allowed_subs", "somebody-else")
        assert (await c.get("/posts")).status_code == 401


@pytest.mark.asyncio
async def test_auth_me_reports_deauthorized_session_as_logged_out(monkeypatch):
    """So the SPA drops back to the login control instead of showing a dead session."""
    monkeypatch.setattr(settings, "oidc_allowed_subs", "user-123")
    token = auth.create_session(sub="user-123", email="me@example.com")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set(auth.SESSION_COOKIE, token)
        assert (await c.get("/auth/me")).json()["authenticated"] is True
        monkeypatch.setattr(settings, "oidc_allowed_subs", "somebody-else")
        assert (await c.get("/auth/me")).json()["authenticated"] is False


@pytest.mark.asyncio
async def test_missing_authorization_header_is_401(tmp_path, monkeypatch):
    from relay import database

    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    await database.init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/posts")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_api_key_is_401(tmp_path, monkeypatch):
    from relay import database

    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    await database.init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/posts", headers={"Authorization": "Bearer wrong-key"})
    assert r.status_code == 401


# ── per-agent identity (relay #198, B-7) ───────────────────────────────────────


def test_named_api_keys_parses_valid_entries(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", "news-agent:sk-news,finance-agent:sk-finance")
    assert settings.named_api_keys == {"news-agent": "sk-news", "finance-agent": "sk-finance"}
    assert settings.all_api_keys == {
        "apikey": "test-key", "news-agent": "sk-news", "finance-agent": "sk-finance",
    }


def test_named_api_keys_skips_malformed_and_reserved_and_duplicate_entries(monkeypatch):
    monkeypatch.setattr(
        settings, "api_keys",
        "good:sk-good,no-colon,:blank-name,apikey:sk-shadow,good:sk-second-good,Bad Name:sk-x",
    )
    # only the one well-formed, non-reserved, non-duplicate entry survives
    assert settings.named_api_keys == {"good": "sk-good"}


def test_named_api_keys_empty_by_default():
    assert settings.named_api_keys == {}
    assert settings.all_api_keys == {"apikey": "test-key"}


def test_resolve_bearer_matches_the_primary_and_named_keys(monkeypatch):
    from relay.identity import Actor, resolve_bearer

    monkeypatch.setattr(settings, "api_keys", "news-agent:sk-news")
    assert resolve_bearer("test-key") == Actor(name="apikey", email="apikey@relay.local")
    assert resolve_bearer("sk-news") == Actor(name="news-agent", email="news-agent@relay.local")
    assert resolve_bearer("sk-nobody") is None
    assert resolve_bearer(None) is None
    assert resolve_bearer("") is None


def test_actor_from_session_prefers_email_then_sub_then_apikey():
    from relay.identity import actor_from_session

    assert actor_from_session({"sub": "u1", "email": "me@example.com"}).name == "me@example.com"
    assert actor_from_session({"sub": "u1", "email": ""}).name == "u1"
    assert actor_from_session({"sub": "apikey", "email": ""}).name == "apikey"


@pytest.mark.asyncio
async def test_a_named_key_authenticates_and_is_attributed_by_name(tmp_path, monkeypatch):
    """End to end: a request bearing a *named* key (not the primary API_KEY)
    both authenticates and stamps its own identity into the write — the
    scenario B-7 exists for (five schedulers, one relay, distinguishable)."""
    from relay import database

    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    monkeypatch.setattr(settings, "api_keys", "news-agent:sk-news")
    await database.init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/posts",
            json={"title": "Filed By News Agent", "content": "x", "tags": []},
            headers={"Authorization": "Bearer sk-news"},
        )
    assert r.status_code == 201, r.text
    assert r.json()["updated_by"] == "news-agent"


# ── per-key scopes (relay #198, B-8) ────────────────────────────────────────


def test_key_scopes_parses_read_and_write_and_full_entries(monkeypatch):
    monkeypatch.setattr(
        settings, "api_key_scopes",
        "ro-agent:read,news-agent:write:news+briefing,admin-agent:full",
    )
    from relay.config import ApiKeyScope

    assert settings.key_scopes == {
        "ro-agent": ApiKeyScope(mode="read"),
        "news-agent": ApiKeyScope(mode="write", tags=frozenset({"news", "briefing"})),
        "admin-agent": ApiKeyScope(mode="full"),
    }


def test_key_scopes_skips_malformed_and_duplicate_entries(monkeypatch):
    monkeypatch.setattr(
        settings, "api_key_scopes",
        "good:read,Bad Name:read,no-mode,unknown:bogus,empty-tags:write,"
        "bad-tag:write:Not_Valid!,good:write:other,",
    )
    # only the one well-formed, non-duplicate entry survives
    assert set(settings.key_scopes) == {"good"}
    from relay.config import ApiKeyScope

    assert settings.key_scopes["good"] == ApiKeyScope(mode="read")


def test_key_scopes_empty_by_default():
    assert settings.key_scopes == {}


def test_key_scopes_lowercases_tags_like_every_other_vault_tag(monkeypatch):
    """A mixed-case tag must not be silently dropped (and the key silently
    left at FULL_SCOPE) just because vault tags are conventionally lowercase
    but this config value wasn't normalised to match (found in review)."""
    monkeypatch.setattr(settings, "api_key_scopes", "news-agent:write:News+Briefing")
    from relay.config import ApiKeyScope

    assert settings.key_scopes == {
        "news-agent": ApiKeyScope(mode="write", tags=frozenset({"news", "briefing"})),
    }


def test_key_scopes_excludes_the_reserved_apikey_name(monkeypatch):
    """The break-glass primary key must not be scopable via this variable,
    mirroring `named_api_keys`'s own reserved-name guard (found in review)."""
    monkeypatch.setattr(settings, "api_key_scopes", "apikey:read")
    assert settings.key_scopes == {}


def test_resolve_bearer_attaches_scope_to_actor(monkeypatch):
    from relay.config import FULL_SCOPE, ApiKeyScope
    from relay.identity import resolve_bearer

    monkeypatch.setattr(settings, "api_keys", "ro-agent:sk-ro,news-agent:sk-news,plain-agent:sk-plain")
    monkeypatch.setattr(settings, "api_key_scopes", "ro-agent:read,news-agent:write:news")
    assert resolve_bearer("sk-ro").scope == ApiKeyScope(mode="read")
    assert resolve_bearer("sk-news").scope == ApiKeyScope(mode="write", tags=frozenset({"news"}))
    # No scope entry at all -> full access, same as before this feature existed.
    assert resolve_bearer("sk-plain").scope == FULL_SCOPE
    assert resolve_bearer("test-key").scope == FULL_SCOPE


async def _init(tmp_path, monkeypatch):
    from relay import database

    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    await database.init_db()


@pytest.mark.asyncio
async def test_read_only_key_rejected_on_write_route_but_not_read_routes(tmp_path, monkeypatch):
    await _init(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "api_keys", "ro-agent:sk-ro")
    monkeypatch.setattr(settings, "api_key_scopes", "ro-agent:read")
    headers = {"Authorization": "Bearer sk-ro"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/posts", json={"title": "Nope", "content": "x", "tags": []}, headers=headers)
        assert r.status_code == 403
        r = await c.get("/posts", headers=headers)
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_tag_scoped_key_can_write_only_within_its_allowed_tags(tmp_path, monkeypatch):
    await _init(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "api_keys", "news-agent:sk-news")
    monkeypatch.setattr(settings, "api_key_scopes", "news-agent:write:news")
    headers = {"Authorization": "Bearer sk-news"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/posts", json={"title": "In Scope", "content": "x", "tags": ["news"]}, headers=headers)
        assert r.status_code == 201, r.text

        r = await c.post(
            "/posts", json={"title": "Out Of Scope", "content": "x", "tags": ["finance"]}, headers=headers
        )
        assert r.status_code == 403

        # ALL-of semantics: a second tag outside the allowed set still denies,
        # even alongside an in-scope one.
        r = await c.post(
            "/posts", json={"title": "Mixed", "content": "x", "tags": ["news", "finance"]}, headers=headers
        )
        assert r.status_code == 403

        # Empty tags never satisfy a tag-restricted key (the "subset of
        # anything" loophole is closed deliberately).
        r = await c.post("/posts", json={"title": "Untagged", "content": "x", "tags": []}, headers=headers)
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_tag_scoped_key_cannot_touch_an_existing_out_of_scope_post(tmp_path, monkeypatch):
    await _init(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "api_keys", "news-agent:sk-news")
    monkeypatch.setattr(settings, "api_key_scopes", "news-agent:write:news")
    admin_headers = {"Authorization": "Bearer test-key"}
    scoped_headers = {"Authorization": "Bearer sk-news"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/posts", json={"title": "Finance Post", "content": "x", "tags": ["finance"]}, headers=admin_headers
        )
        post_id = r.json()["id"]

        r = await c.patch(f"/posts/{post_id}", json={"content": "hijacked"}, headers=scoped_headers)
        assert r.status_code == 403
        r = await c.delete(f"/posts/{post_id}", headers=scoped_headers)
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_tag_scoped_key_cannot_retag_its_own_post_outside_scope(tmp_path, monkeypatch):
    await _init(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "api_keys", "news-agent:sk-news")
    monkeypatch.setattr(settings, "api_key_scopes", "news-agent:write:news")
    headers = {"Authorization": "Bearer sk-news"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/posts", json={"title": "Mine", "content": "x", "tags": ["news"]}, headers=headers)
        post_id = r.json()["id"]
        r = await c.patch(f"/posts/{post_id}", json={"tags": ["other"]}, headers=headers)
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_tag_scoped_key_cannot_rename_tag_or_set_tag_config(tmp_path, monkeypatch):
    await _init(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "api_keys", "news-agent:sk-news")
    monkeypatch.setattr(settings, "api_key_scopes", "news-agent:write:news")
    headers = {"Authorization": "Bearer sk-news"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/posts", json={"title": "Seed", "content": "x", "tags": ["news"]}, headers={
            "Authorization": "Bearer test-key",
        })
        r = await c.patch("/tags/news", json={"new_name": "newsy"}, headers=headers)
        assert r.status_code == 403
        r = await c.post("/tags/news/config", json={"ttl_hours": 24}, headers=headers)
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_full_access_keys_are_unaffected_by_scope_gates(tmp_path, monkeypatch):
    await _init(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "api_keys", "admin-agent:sk-admin")
    # explicit "full" entry, and the primary API_KEY with no entry at all
    monkeypatch.setattr(settings, "api_key_scopes", "admin-agent:full")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for headers in ({"Authorization": "Bearer sk-admin"}, {"Authorization": "Bearer test-key"}):
            r = await c.post(
                "/posts", json={"title": f"Free {headers['Authorization']}", "content": "x", "tags": ["anything"]},
                headers=headers,
            )
            assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_malformed_scope_config_does_not_break_the_app_the_key_stays_full_access(tmp_path, monkeypatch):
    await _init(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "api_keys", "some-agent:sk-some")
    monkeypatch.setattr(settings, "api_key_scopes", "some-agent:not-a-real-mode")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/posts", json={"title": "Still Works", "content": "x", "tags": []},
            headers={"Authorization": "Bearer sk-some"},
        )
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_web_ui_paste_login_carries_the_pasted_keys_scope(tmp_path, monkeypatch):
    """A read-only or tag-scoped key pasted into the browser must not become a
    full-access session — otherwise the web UI is a bypass of every other
    scope gate (relay #198, B-8)."""
    await _init(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "api_keys", "ro-agent:sk-ro")
    monkeypatch.setattr(settings, "api_key_scopes", "ro-agent:read")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/session", json={"key": "sk-ro"})
        assert r.status_code == 200
        cookie = r.cookies.get(auth.SESSION_COOKIE)
        assert cookie is not None
        c.cookies.set(auth.SESSION_COOKIE, cookie)
        r = await c.post("/posts", json={"title": "Via Session", "content": "x", "tags": []})
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_tag_scoped_key_cannot_smuggle_an_attachment_into_an_arbitrary_folder(tmp_path, monkeypatch):
    """`folder` picks the actual write target; `tags` (if also passed) says
    nothing about it. Pairing an in-scope `tags` value with an unrelated
    `folder` must still be denied (found in review) — otherwise the folder
    branch's scope check was checking the wrong thing entirely."""
    await _init(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "api_keys", "news-agent:sk-news")
    monkeypatch.setattr(settings, "api_key_scopes", "news-agent:write:news")
    headers = {"Authorization": "Bearer sk-news"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/attachments",
            json={"filename": "x.png", "data": "aGk=", "folder": "Finance", "tags": ["news"]},
            headers=headers,
        )
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_apikey_scope_entry_is_ignored_primary_key_stays_full_access(tmp_path, monkeypatch):
    """An admin accidentally (or deliberately) writing an `apikey:...` entry
    must not restrict the break-glass primary key (found in review)."""
    await _init(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "api_key_scopes", "apikey:read")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/posts", json={"title": "Still Full Access", "content": "x", "tags": []},
            headers={"Authorization": "Bearer test-key"},
        )
        assert r.status_code == 201, r.text


# ── per-key scopes on the MCP surface (relay #198, B-8) ─────────────────────


@pytest.mark.asyncio
async def test_mcp_read_only_key_is_denied_the_write_tool_and_it_vanishes_from_the_manifest(
    tmp_path, monkeypatch
):
    from relay import mcp_server
    from relay.mcp_server import mcp

    await _init(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "api_keys", "ro-agent:sk-ro")
    monkeypatch.setattr(settings, "api_key_scopes", "ro-agent:read")

    token = await mcp_server._StaticBearerAuth().verify_token("sk-ro")
    assert token is not None
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        names = {t.name for t in await mcp.list_tools()}
        assert "publish_post" not in names
        assert "list_posts" in names  # read tools are unaffected

        with pytest.raises(Exception, match="insufficient scope"):
            await mcp.call_tool("publish_post", {"title": "x", "content": "y"})
    finally:
        auth_context_var.reset(reset)


@pytest.mark.asyncio
async def test_mcp_tag_scoped_key_succeeds_in_scope_and_fails_out_of_scope(tmp_path, monkeypatch):
    from relay import mcp_server
    from relay.mcp_server import mcp

    await _init(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "api_keys", "news-agent:sk-news")
    monkeypatch.setattr(settings, "api_key_scopes", "news-agent:write:news")

    token = await mcp_server._StaticBearerAuth().verify_token("sk-news")
    assert token is not None
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        ok = await mcp.call_tool("publish_post", {"title": "In Scope", "content": "x", "tags": ["news"]})
        assert "error" not in ok.structured_content

        denied = await mcp.call_tool(
            "publish_post", {"title": "Out Of Scope", "content": "x", "tags": ["finance"]}
        )
        assert denied.structured_content.get("error") == "This API key's scope does not permit this write."
    finally:
        auth_context_var.reset(reset)


def test_current_actor_logs_loudly_when_a_real_token_has_no_derivable_identity(caplog):
    """Since B-8, `_current_actor()` returning None also skips every scope
    check for the call (actor=None is the same signal used for an internal
    caller that bypasses auth entirely) — not reachable via any auth path
    today, but if it ever is, it must fail loudly rather than silently
    granting an authenticated-but-unattributable write (found in review)."""
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    from relay import mcp_server

    # A token that passed authentication but carries no usable identity claim.
    token = AccessToken(token="t", client_id="relay", scopes=["relay", "write"], subject=None, claims={})
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        with caplog.at_level("ERROR"):
            assert mcp_server._current_actor() is None
        assert any("no derivable identity" in r.message for r in caplog.records)
    finally:
        auth_context_var.reset(reset)
