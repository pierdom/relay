"""Tests for the remote MCP OAuth Authorization Server (relay post #201).

Covers the store (hashing, single-use, expiry, revoke), the provider state
machine (DCR, authorize-broker, code/token exchange, refresh rotation, static-key
fallback, audience binding), the broker callback (allowlist reuse), and the
per-client consent gate (relay #313 Stopgap).
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
async def test_pending_get_does_not_consume(store):
    # Unlike pop_pending, get_pending is a peek: the consent gate needs to
    # read it on GET and again on POST-approve without burning it early.
    await store.save_pending("txn-peek", _pending(), ttl_seconds=600)
    assert await store.get_pending("txn-peek") is not None
    assert await store.get_pending("txn-peek") is not None  # still there
    assert await store.pop_pending("txn-peek") is not None  # now consumed
    assert await store.get_pending("txn-peek") is None


@pytest.mark.asyncio
async def test_client_approval_persists(store):
    assert await store.is_client_approved("c1") is False
    await store.approve_client("c1")
    assert await store.is_client_approved("c1") is True
    assert await store.is_client_approved("c2") is False  # per-client, not global


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
        ("https://claude.ai/cb", True),  # allowlisted host
        ("https://chatgpt.com/oauth/callback", True),  # allowlisted (default set)
        ("http://localhost:41000/cb", True),  # loopback http allowed (native apps)
        ("http://127.0.0.1:8080/cb", True),
        ("https://www.perplexity.ai/rest/connections/oauth_callback", False),  # not in default -> opt-in
        ("https://evil.example.com/cb", False),  # non-allowlisted https -> rejected
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
async def test_register_redirect_host_allowlist_opt_out(provider, monkeypatch):
    from mcp.server.auth.provider import RegistrationError

    # Non-allowlisted https is rejected under the default allowlist...
    with pytest.raises(RegistrationError):
        await provider.register_client(_client(redirect="https://evil.example.com/cb"))
    # ...but an empty allowlist opts out (any https allowed again).
    monkeypatch.setattr(settings, "mcp_allowed_redirect_hosts", "")
    await provider.register_client(_client(redirect="https://anything.example.com/cb"))
    assert await provider.get_client("c1") is not None


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
def _params(**overrides):
    from mcp.server.auth.provider import AuthorizationParams

    defaults = {
        "state": "client-state",
        "scopes": ["relay"],
        "code_challenge": "the-challenge",
        "redirect_uri": AnyUrl("https://claude.ai/cb"),
        "redirect_uri_provided_explicitly": True,
        "resource": settings.mcp_resource_url,
    }
    defaults.update(overrides)
    return AuthorizationParams(**defaults)


@pytest.mark.asyncio
async def test_authorize_of_approved_client_persists_pending_and_redirects_upstream(provider, store, monkeypatch):
    async def fake_build(txn_id, verifier, nonce):
        return f"https://id.example.com/authorize?state={txn_id}"

    monkeypatch.setattr(pocketid, "build_authorize_url", fake_build)
    await store.approve_client("c1")  # previously approved -> skips the consent gate

    url = await provider.authorize(_client(), _params())
    assert url.startswith("https://id.example.com/authorize?state=")
    txn_id = url.rsplit("=", 1)[1]
    pending = await store.pop_pending(txn_id)
    assert pending is not None
    assert pending.code_challenge == "the-challenge"
    assert pending.client_state == "client-state"
    assert pending.redirect_uri == "https://claude.ai/cb"


@pytest.mark.asyncio
async def test_authorize_of_unapproved_client_routes_to_consent_gate(provider, store):
    # relay #313 Stopgap: a client with no prior human approval must not reach
    # PocketID directly, regardless of whether the human's PocketID session is
    # already live (which is exactly the confused-deputy shape §0 describes).
    url = await provider.authorize(_client(), _params())
    assert url.startswith(f"{settings.relay_base_url.rstrip('/')}/mcp/oauth/consent?txn_id=")
    txn_id = url.rsplit("=", 1)[1]
    # the pending auth still exists (unconsumed) so the consent page can read it
    pending = await store.get_pending(txn_id)
    assert pending is not None
    assert pending.client_id == "c1"


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


@pytest.mark.asyncio
async def test_refresh_denied_after_sub_removed_from_allowlist(provider, monkeypatch):
    # #2: allowlist is re-evaluated on the refresh grant, so a de-authorized sub
    # loses access at the next rotation instead of persisting for the 30-day TTL.
    from mcp.server.auth.provider import TokenError

    monkeypatch.setattr(settings, "oidc_allowed_subs", "user-42")
    code = await provider.mint_authorization_code(_pending(), sub="user-42")
    loaded = await provider.load_authorization_code(_client(), code)
    tokens = await provider.exchange_authorization_code(_client(), loaded)
    rt = await provider.load_refresh_token(_client(), tokens.refresh_token)

    monkeypatch.setattr(settings, "oidc_allowed_subs", "someone-else")  # de-authorized
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(_client(), rt, scopes=[])
    # the family is revoked too, so the leftover refresh token is dead
    assert await provider.load_refresh_token(_client(), tokens.refresh_token) is None


@pytest.mark.asyncio
async def test_revoke_access_token_cascades_to_paired_refresh(provider):
    # #3a: revoking an access token also kills its paired refresh token (RFC 7009).
    code = await provider.mint_authorization_code(_pending(), sub="user-42")
    loaded = await provider.load_authorization_code(_client(), code)
    tokens = await provider.exchange_authorization_code(_client(), loaded)

    access = await provider.load_access_token(tokens.access_token)
    assert access is not None
    await provider.revoke_token(access)

    assert await provider.load_access_token(tokens.access_token) is None
    assert await provider.load_refresh_token(_client(), tokens.refresh_token) is None


@pytest.mark.asyncio
async def test_refresh_reuse_revokes_whole_family(provider):
    # #3b: replaying a rotated refresh token revokes the attacker's live
    # descendants too (RFC 6819 containment), not just the replayed token.
    code = await provider.mint_authorization_code(_pending(), sub="user-42")
    loaded = await provider.load_authorization_code(_client(), code)
    t0 = await provider.exchange_authorization_code(_client(), loaded)  # AT0/RT0

    rt0 = await provider.load_refresh_token(_client(), t0.refresh_token)
    t1 = await provider.exchange_refresh_token(_client(), rt0, scopes=[])  # AT1/RT1

    # attacker replays the already-rotated RT0
    assert await provider.load_refresh_token(_client(), t0.refresh_token) is None
    # containment: the live descendants (RT1/AT1) are now revoked as well
    assert await provider.load_refresh_token(_client(), t1.refresh_token) is None
    assert await provider.load_access_token(t1.access_token) is None


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


# --- consent gate (relay #313 Stopgap) ---------------------------------------
def _consent_get_request(query: str):
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "query_string": query.encode(), "headers": []}
    return Request(scope)


def _consent_post_request(form: dict, cookie: str | None = None):
    from urllib.parse import urlencode

    from starlette.requests import Request

    body = urlencode(form).encode()
    headers = [(b"content-type", b"application/x-www-form-urlencoded")]
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {"type": "http", "method": "POST", "headers": headers}
    return Request(scope, receive)


@pytest.mark.asyncio
async def test_consent_get_unknown_txn_is_400(store, monkeypatch):
    from relay.mcp_oauth import consent

    monkeypatch.setattr(consent, "get_store", lambda: store)
    resp = await consent.handle_consent_get(_consent_get_request("txn_id=nonexistent"))
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_consent_get_renders_prompt_and_sets_binding_cookie(store, monkeypatch):
    from relay.mcp_oauth import consent

    monkeypatch.setattr(consent, "get_store", lambda: store)
    await store.register_client(
        "c1", '{"client_id": "c1", "client_name": "Evil Corp Connector", "redirect_uris": []}'
    )
    await store.save_pending("txn-gate", _pending(), ttl_seconds=600)

    resp = await consent.handle_consent_get(_consent_get_request("txn_id=txn-gate"))
    assert resp.status_code == 200
    assert b"Evil Corp Connector" in resp.body
    assert b"claude.ai/cb" in resp.body
    # the pending auth is untouched by a mere GET (still readable, not burned)
    assert await store.get_pending("txn-gate") is not None

    set_cookie = resp.headers.get("set-cookie", "")
    assert consent._cookie_name() in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()


@pytest.mark.asyncio
async def test_consent_post_approve_without_binding_cookie_is_rejected(store, monkeypatch):
    from relay.mcp_oauth import consent

    monkeypatch.setattr(consent, "get_store", lambda: store)
    await store.save_pending("txn-nocookie", _pending(), ttl_seconds=600)

    resp = await consent.handle_consent_post(_consent_post_request({"txn_id": "txn-nocookie", "action": "approve"}))
    assert resp.status_code == 403
    # rejected before ever touching approval state or the pending auth
    assert await store.is_client_approved("c1") is False
    assert await store.get_pending("txn-nocookie") is not None


@pytest.mark.asyncio
async def test_consent_post_approve_with_mismatched_cookie_is_rejected(store, monkeypatch):
    # Simulates a captured consent URL opened in a different browser: that
    # browser never saw the GET, so it has no cookie bound to this txn_id.
    from relay.mcp_oauth import consent

    monkeypatch.setattr(consent, "get_store", lambda: store)
    await store.save_pending("txn-a", _pending(), ttl_seconds=600)
    await store.save_pending("txn-b", _pending(client_id="c2"), ttl_seconds=600)

    wrong_cookie = f"{consent._cookie_name()}={consent._sign('txn-b')}"
    resp = await consent.handle_consent_post(
        _consent_post_request({"txn_id": "txn-a", "action": "approve"}, cookie=wrong_cookie)
    )
    assert resp.status_code == 403
    assert await store.is_client_approved("c1") is False


@pytest.mark.asyncio
async def test_consent_post_approve_forwards_upstream_without_granting_approval(store, monkeypatch):
    # Consent's own POST must NOT itself grant approval (relay #313 audit
    # finding): it only proves same-browser continuity with the GET, which an
    # attacker can satisfy against their own DCR registration with zero relay
    # credentials. Approval is earned later, in broker.handle_callback, only
    # after a real PocketID login clears the allowlist.
    from relay.mcp_oauth import consent, pocketid

    monkeypatch.setattr(consent, "get_store", lambda: store)

    async def fake_build(txn_id, verifier, nonce):
        assert verifier == "up-verifier" and nonce == "up-nonce"
        return f"https://id.example.com/authorize?state={txn_id}"

    monkeypatch.setattr(pocketid, "build_authorize_url", fake_build)

    await store.save_pending("txn-approve", _pending(), ttl_seconds=600)
    cookie = f"{consent._cookie_name()}={consent._sign('txn-approve')}"

    resp = await consent.handle_consent_post(
        _consent_post_request({"txn_id": "txn-approve", "action": "approve"}, cookie=cookie)
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://id.example.com/authorize?state=txn-approve"
    assert await store.is_client_approved("c1") is False
    # approval does not itself consume the pending auth — the broker callback
    # (after PocketID) still needs to pop it.
    assert await store.get_pending("txn-approve") is not None


@pytest.mark.asyncio
async def test_self_approval_attack_is_blocked(provider, store, monkeypatch):
    """The confirmed relay #313 audit finding: an attacker who registers their
    own client and clicks through their own consent page (no relay
    credentials, no PocketID login) must NOT be able to grant that client
    permanent approval. A subsequent authorize() call for the same client
    must still be gated — an unrelated victim must still see the prompt."""
    from relay.mcp_oauth import consent, pocketid

    monkeypatch.setattr(consent, "get_store", lambda: store)

    async def fake_build(txn_id, verifier, nonce):
        return f"https://id.example.com/authorize?state={txn_id}"

    monkeypatch.setattr(pocketid, "build_authorize_url", fake_build)

    # "attacker" runs the entire consent gate against their own client, alone.
    url = await provider.authorize(_client(), _params())
    txn_id = url.rsplit("=", 1)[1]
    cookie = f"{consent._cookie_name()}={consent._sign(txn_id)}"
    approve_resp = await consent.handle_consent_post(
        _consent_post_request({"txn_id": txn_id, "action": "approve"}, cookie=cookie)
    )
    assert approve_resp.status_code == 302  # forwarded to PocketID...
    assert await store.is_client_approved("c1") is False  # ...but never approved

    # a later authorize() for the same client (e.g. a link sent to a victim)
    # is still gated to consent — the attacker's self-click bought them nothing.
    url2 = await provider.authorize(_client(), _params())
    assert "/mcp/oauth/consent?txn_id=" in url2


@pytest.mark.asyncio
async def test_consent_post_deny_discards_pending_and_redirects_with_error(store, monkeypatch):
    from relay.mcp_oauth import consent

    monkeypatch.setattr(consent, "get_store", lambda: store)
    await store.save_pending("txn-deny", _pending(), ttl_seconds=600)
    cookie = f"{consent._cookie_name()}={consent._sign('txn-deny')}"

    resp = await consent.handle_consent_post(
        _consent_post_request({"txn_id": "txn-deny", "action": "deny"}, cookie=cookie)
    )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://claude.ai/cb?")
    assert "error=access_denied" in resp.headers["location"]
    assert await store.is_client_approved("c1") is False
    assert await store.get_pending("txn-deny") is None  # discarded, not just unapproved


@pytest.mark.asyncio
async def test_full_stopgap_flow_unapproved_then_approved(provider, store, monkeypatch):
    """End-to-end: an unapproved client is gated to consent; a human clicking
    Approve and then *actually completing PocketID login as an allowlisted
    user* is what lets the *next* authorize() call for the same client skip
    straight to PocketID. Consent's Approve click alone is not enough —
    matches broker.handle_callback granting approval, not consent.py."""
    from relay.mcp_oauth import broker, consent, pocketid

    monkeypatch.setattr(consent, "get_store", lambda: store)
    monkeypatch.setattr(broker, "get_store", lambda: store)
    monkeypatch.setattr(broker, "get_provider", lambda: provider)

    async def fake_build(txn_id, verifier, nonce):
        return f"https://id.example.com/authorize?state={txn_id}"

    monkeypatch.setattr(pocketid, "build_authorize_url", fake_build)

    async def fake_validate(code, verifier, nonce):
        # provider.authorize() (unlike the _pending() fixture) generates a real
        # PKCE verifier/nonce, so just accept whatever it produced.
        return {"sub": "user-42", "email": "me@x.com", "email_verified": True}

    monkeypatch.setattr(broker.pocketid, "exchange_and_validate", fake_validate)
    monkeypatch.setattr(settings, "oidc_allowed_subs", "user-42")
    monkeypatch.setattr(settings, "oidc_allowed_emails", "")

    # first authorize(): unapproved -> routed to the consent gate
    url = await provider.authorize(_client(), _params())
    assert "/mcp/oauth/consent?txn_id=" in url
    txn_id = url.rsplit("=", 1)[1]

    # human clicks Approve in-browser...
    cookie = f"{consent._cookie_name()}={consent._sign(txn_id)}"
    approve_resp = await consent.handle_consent_post(
        _consent_post_request({"txn_id": txn_id, "action": "approve"}, cookie=cookie)
    )
    assert approve_resp.status_code == 302
    assert approve_resp.headers["location"].startswith("https://id.example.com/authorize?state=")
    assert await store.is_client_approved("c1") is False  # not yet — still needs real login

    # ...and then actually completes PocketID login as an allowlisted user.
    callback_resp = await broker.handle_callback(_request(f"state={txn_id}&code=upstream-code"))
    assert callback_resp.status_code == 302
    assert await store.is_client_approved("c1") is True  # now granted

    # second authorize() for the same client: now pre-approved, straight to PocketID
    url2 = await provider.authorize(_client(), _params())
    assert url2.startswith("https://id.example.com/authorize?state=")


@pytest.mark.asyncio
async def test_broker_callback_denied_login_does_not_grant_approval(provider, store, monkeypatch):
    # An unauthorized sub reaching the callback (e.g. someone without a
    # relay-allowlisted PocketID identity) must not grant approval either —
    # only a successful, allowlisted login does.
    from relay.mcp_oauth import broker

    monkeypatch.setattr(broker, "get_store", lambda: store)
    monkeypatch.setattr(broker, "get_provider", lambda: provider)

    async def fake_validate(code, verifier, nonce):
        return {"sub": "intruder", "email": "e@x.com", "email_verified": True}

    monkeypatch.setattr(broker.pocketid, "exchange_and_validate", fake_validate)
    monkeypatch.setattr(settings, "oidc_allowed_subs", "user-42")
    monkeypatch.setattr(settings, "oidc_allowed_emails", "")

    await store.save_pending("txn-denied", _pending(), ttl_seconds=600)
    resp = await broker.handle_callback(_request("state=txn-denied&code=c"))

    assert resp.status_code == 302
    assert "error=access_denied" in resp.headers["location"]
    assert await store.is_client_approved("c1") is False
