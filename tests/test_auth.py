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


@pytest.mark.asyncio
async def test_mcp_metadata_absent_when_oauth_disabled():
    # With OAuth off (the default this app was imported under), the SDK mounts no
    # auth metadata and there is no hand-rolled route — the path 404s. The
    # enabled-mode metadata is emitted by the SDK when FastMCP is constructed with
    # auth (import-time), covered in test_mcp_oauth via a fresh app build.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/.well-known/oauth-protected-resource/mcp")
    # No metadata document is served; the static-bearer gate answers 401 (never a
    # 200 discovery doc that would invite a client into an OAuth flow relay isn't
    # running).
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_mcp_accepts_public_host_and_origin(monkeypatch, tmp_path):
    # Regression: FastMCP's default host (127.0.0.1) auto-enables localhost-scoped
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
