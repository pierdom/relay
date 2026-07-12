"""Tests for the remote MCP OAuth Authorization Server (relay post #201).

Covers the store (hashing, single-use, expiry, revoke), the provider state
machine (DCR, authorize-broker, code/token exchange, refresh rotation, static-key
fallback, audience binding), and the broker callback (allowlist reuse).
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from relay.config import settings
from relay.mcp_oauth import pocketid
from relay.mcp_oauth.provider import RelayOAuthProvider
from relay.mcp_oauth.store import OAuthStore, PendingAuth


@pytest_asyncio.fixture
async def store(tmp_path):
    s = OAuthStore(str(tmp_path / "oauth.db"))
    await s.init()
    return s


@pytest.fixture
def provider(store):
    return RelayOAuthProvider(store=store)


def _client(client_id="c1", redirect="https://claude.ai/cb"):
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[AnyUrl(redirect)],
        token_endpoint_auth_method="none",
    )


def _pending(client_id="c1", redirect="https://claude.ai/cb"):
    return PendingAuth(
        client_id=client_id,
        redirect_uri=redirect,
        redirect_uri_explicit=True,
        code_challenge="chal",
        scopes=["relay"],
        resource=settings.mcp_resource_url,
        client_state="xyz",
        up_verifier="up-verifier",
        up_nonce="up-nonce",
    )


# --- config invariant -------------------------------------------------------
def test_mcp_oauth_active_requires_oidc(monkeypatch):
    # The flag alone can't broker a login — active iff OIDC is also configured.
    # Guards against store-init / cleanup guards drifting apart.
    monkeypatch.setattr(settings, "mcp_oauth_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", "")
    assert settings.mcp_oauth_active is False
    monkeypatch.setattr(settings, "oidc_issuer", "https://id.example.com")
    monkeypatch.setattr(settings, "oidc_client_id", "cid")
    monkeypatch.setattr(settings, "oidc_client_secret", "sec")
    assert settings.mcp_oauth_active is True
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False)
    assert settings.mcp_oauth_active is False


# --- store ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tokens_stored_hashed_not_plaintext(store, tmp_path):
    await store.save_token(
        "super-secret-token", kind="access", sub="u", client_id="c1",
        scopes=["relay"], resource="r", expires_at=time.time() + 100,
    )
    raw = (tmp_path / "oauth.db").read_bytes()
    assert b"super-secret-token" not in raw  # never persisted in the clear
    # ...but it resolves back via the hash lookup.
    t = await store.get_token("super-secret-token")
    assert t is not None and t.sub == "u"


@pytest.mark.asyncio
async def test_pending_is_single_use_and_expiring(store):
    await store.save_pending("txn1", _pending(), ttl_seconds=600)
    assert await store.pop_pending("txn1") is not None
    assert await store.pop_pending("txn1") is None  # consumed

    await store.save_pending("txn2", _pending(), ttl_seconds=-1)  # already expired
    assert await store.pop_pending("txn2") is None


@pytest.mark.asyncio
async def test_revoke_and_cleanup(store):
    await store.save_token(
        "acc", kind="access", sub="u", client_id="c1", scopes=[], resource=None,
        expires_at=time.time() - 1,  # expired
    )
    removed = await store.cleanup_expired()
    assert removed >= 1
    assert await store.get_token("acc") is None


# --- provider: DCR ----------------------------------------------------------
@pytest.mark.asyncio
async def test_register_and_get_client(provider):
    await provider.register_client(_client())
    got = await provider.get_client("c1")
    assert got is not None
    assert str(got.redirect_uris[0]) == "https://claude.ai/cb"
    assert await provider.get_client("nope") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redirect,ok",
    [
        ("https://claude.ai/cb", True),
        ("http://localhost:41000/cb", True),  # loopback http allowed (native apps)
        ("http://127.0.0.1:8080/cb", True),
        ("http://evil.example.com/cb", False),  # remote cleartext -> rejected
        ("ftp://evil/cb", False),
    ],
)
async def test_register_redirect_scheme_policy(provider, redirect, ok):
    from mcp.server.auth.provider import RegistrationError

    client = _client(redirect=redirect)
    if ok:
        await provider.register_client(client)
        assert await provider.get_client("c1") is not None
    else:
        with pytest.raises(RegistrationError):
            await provider.register_client(client)


@pytest.mark.asyncio
async def test_auth_code_replay_is_rejected(provider):
    from mcp.server.auth.provider import TokenError

    code = await provider.mint_authorization_code(_pending(), sub="u")
    loaded = await provider.load_authorization_code(_client(), code)
    await provider.exchange_authorization_code(_client(), loaded)
    # Second exchange of the same (already-loaded) code must not mint again.
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(_client(), loaded)


@pytest.mark.asyncio
async def test_refresh_reuse_is_rejected(provider):
    from mcp.server.auth.provider import TokenError

    code = await provider.mint_authorization_code(_pending(), sub="u")
    loaded = await provider.load_authorization_code(_client(), code)
    tokens = await provider.exchange_authorization_code(_client(), loaded)
    rt = await provider.load_refresh_token(_client(), tokens.refresh_token)
    await provider.exchange_refresh_token(_client(), rt, scopes=[])
    # Reusing the now-rotated refresh token must be rejected.
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(_client(), rt, scopes=[])


@pytest.mark.asyncio
async def test_access_token_wrong_audience_rejected(provider, store):
    # A token minted for a different resource must not verify at our /mcp.
    await store.save_token(
        "foreign", kind="access", sub="u", client_id="c1", scopes=["relay"],
        resource="https://other.example.com/mcp", expires_at=time.time() + 100,
    )
    assert await provider.load_access_token("foreign") is None


# --- provider: authorize broker --------------------------------------------
@pytest.mark.asyncio
async def test_authorize_persists_pending_and_redirects_upstream(provider, store, monkeypatch):
    async def fake_build(txn_id, verifier, nonce):
        return f"https://id.example.com/authorize?state={txn_id}"

    monkeypatch.setattr(pocketid, "build_authorize_url", fake_build)
    from mcp.server.auth.provider import AuthorizationParams

    params = AuthorizationParams(
        state="client-state",
        scopes=["relay"],
        code_challenge="the-challenge",
        redirect_uri=AnyUrl("https://claude.ai/cb"),
        redirect_uri_provided_explicitly=True,
        resource=settings.mcp_resource_url,
    )
    url = await provider.authorize(_client(), params)
    assert url.startswith("https://id.example.com/authorize?state=")
    txn_id = url.rsplit("=", 1)[1]
    pending = await store.pop_pending(txn_id)
    assert pending is not None
    assert pending.code_challenge == "the-challenge"
    assert pending.client_state == "client-state"
    assert pending.redirect_uri == "https://claude.ai/cb"


# --- provider: code + token exchange ---------------------------------------
@pytest.mark.asyncio
async def test_auth_code_roundtrip_and_single_use(provider):
    code = await provider.mint_authorization_code(_pending(), sub="user-42")
    loaded = await provider.load_authorization_code(_client(), code)
    assert loaded is not None
    assert loaded.sub == "user-42"
    assert loaded.code_challenge == "chal"
    assert loaded.resource == settings.mcp_resource_url

    tokens = await provider.exchange_authorization_code(_client(), loaded)
    assert tokens.access_token and tokens.refresh_token
    # code is burned (single-use)
    assert await provider.load_authorization_code(_client(), code) is None


@pytest.mark.asyncio
async def test_load_code_wrong_client_is_none(provider):
    code = await provider.mint_authorization_code(_pending(client_id="c1"), sub="u")
    assert await provider.load_authorization_code(_client("other"), code) is None


@pytest.mark.asyncio
async def test_issued_access_token_verifies_with_audience(provider):
    code = await provider.mint_authorization_code(_pending(), sub="user-42")
    loaded = await provider.load_authorization_code(_client(), code)
    tokens = await provider.exchange_authorization_code(_client(), loaded)

    access = await provider.load_access_token(tokens.access_token)
    assert access is not None
    assert access.client_id == "c1"
    assert access.resource == settings.mcp_resource_url  # RFC 8707 audience
    assert "relay" in access.scopes


@pytest.mark.asyncio
async def test_expired_and_revoked_access_tokens_rejected(provider, store):
    await store.save_token(
        "expired", kind="access", sub="u", client_id="c1", scopes=["relay"],
        resource=settings.mcp_resource_url, expires_at=time.time() - 1,
    )
    assert await provider.load_access_token("expired") is None

    await store.save_token(
        "live", kind="access", sub="u", client_id="c1", scopes=["relay"],
        resource=settings.mcp_resource_url, expires_at=time.time() + 100,
    )
    await store.revoke_token("live")
    assert await provider.load_access_token("live") is None


# --- provider: refresh rotation --------------------------------------------
@pytest.mark.asyncio
async def test_refresh_rotates_and_revokes_old(provider, store):
    code = await provider.mint_authorization_code(_pending(), sub="user-42")
    loaded = await provider.load_authorization_code(_client(), code)
    tokens = await provider.exchange_authorization_code(_client(), loaded)

    rt = await provider.load_refresh_token(_client(), tokens.refresh_token)
    assert rt is not None and rt.sub == "user-42"

    new_tokens = await provider.exchange_refresh_token(_client(), rt, scopes=[])
    assert new_tokens.access_token != tokens.access_token
    assert new_tokens.refresh_token != tokens.refresh_token
    # old refresh token no longer loads (rotated/revoked)
    assert await provider.load_refresh_token(_client(), tokens.refresh_token) is None


# --- provider: static-key back-compat --------------------------------------
@pytest.mark.asyncio
async def test_static_api_key_is_synthetic_full_scope_bearer(provider):
    access = await provider.load_access_token(settings.api_key)
    assert access is not None
    assert access.client_id == "apikey"
    assert access.resource == settings.mcp_resource_url
    assert access.scopes == list(settings.mcp_scopes)
    assert await provider.load_access_token("not-the-key") is None


# --- broker callback --------------------------------------------------------
def _request(query: str):
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "query_string": query.encode(), "headers": []}
    return Request(scope)


@pytest.mark.asyncio
async def test_broker_callback_mints_code_on_authorized_login(provider, store, monkeypatch):
    from relay.mcp_oauth import broker

    monkeypatch.setattr(broker, "get_store", lambda: store)
    monkeypatch.setattr(broker, "get_provider", lambda: provider)

    async def fake_validate(code, verifier, nonce):
        assert verifier == "up-verifier" and nonce == "up-nonce"
        return {"sub": "user-42", "email": "me@x.com", "email_verified": True}

    monkeypatch.setattr(broker.pocketid, "exchange_and_validate", fake_validate)
    monkeypatch.setattr(settings, "oidc_allowed_subs", "user-42")
    monkeypatch.setattr(settings, "oidc_allowed_emails", "")

    await store.save_pending("txn9", _pending(), ttl_seconds=600)
    resp = await broker.handle_callback(_request("state=txn9&code=upstream-code"))

    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith("https://claude.ai/cb?")
    assert "code=" in loc and "state=xyz" in loc


@pytest.mark.asyncio
async def test_broker_callback_denies_unlisted_sub(provider, store, monkeypatch):
    from relay.mcp_oauth import broker

    monkeypatch.setattr(broker, "get_store", lambda: store)
    monkeypatch.setattr(broker, "get_provider", lambda: provider)

    async def fake_validate(code, verifier, nonce):
        return {"sub": "intruder", "email": "e@x.com", "email_verified": True}

    monkeypatch.setattr(broker.pocketid, "exchange_and_validate", fake_validate)
    monkeypatch.setattr(settings, "oidc_allowed_subs", "user-42")
    monkeypatch.setattr(settings, "oidc_allowed_emails", "")

    await store.save_pending("txn10", _pending(), ttl_seconds=600)
    resp = await broker.handle_callback(_request("state=txn10&code=c"))

    assert resp.status_code == 302
    assert "error=access_denied" in resp.headers["location"]


@pytest.mark.asyncio
async def test_broker_callback_unknown_state_is_400(store, monkeypatch):
    from relay.mcp_oauth import broker

    monkeypatch.setattr(broker, "get_store", lambda: store)
    resp = await broker.handle_callback(_request("state=nonexistent&code=c"))
    assert resp.status_code == 400
