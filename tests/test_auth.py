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
async def test_mcp_metadata_gated(monkeypatch):
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 404

    monkeypatch.setattr(settings, "mcp_oauth_enabled", True)
    monkeypatch.setattr(settings, "relay_base_url", "https://relay.example.com")
    monkeypatch.setattr(settings, "mcp_required_scopes", "relay.read,relay.write")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    body = r.json()
    assert body["resource"] == "https://relay.example.com/mcp"
    assert body["authorization_servers"] == ["https://relay.example.com"]
    assert body["scopes_supported"] == ["relay.read", "relay.write"]
