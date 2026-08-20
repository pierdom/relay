"""Upstream PocketID (OIDC) broker leg.

``provider.authorize()`` returns only a URL string and cannot set a cookie, so
the upstream OAuth state (PKCE verifier + nonce) is persisted in the
``pending_auth`` row rather than the Starlette session that Phase-1 web login
uses. These helpers drive the upstream leg directly against PocketID's discovered
endpoints, reusing the Phase-1 OIDC client credentials (``settings.oidc_*``).
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.jwk import KeySet

from ..config import settings

# OIDC discovery + JWKS are cached for the process lifetime. Note: if PocketID
# rotates its signing keys, the cached JWKS goes stale and newly-issued id_tokens
# fail validation until relay is restarted. Key rotation is rare and a restart is
# cheap on this single-user deployment; we deliberately don't refetch on a kid miss
# to avoid an attacker forcing repeated JWKS fetches with bogus `kid`s.
_metadata: dict | None = None
_jwks: KeySet | None = None

# Upstream scopes: same as Phase-1 web login.
UPSTREAM_SCOPE = "openid email profile"


def pkce_pair() -> tuple[str, str]:
    """Return (verifier, S256 challenge) for the upstream leg."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def new_nonce() -> str:
    return secrets.token_urlsafe(32)


def callback_uri() -> str:
    """Relay's own redirect URI for the broker leg (must be registered on the
    PocketID client alongside the Phase-1 ``/auth/callback``)."""
    return f"{settings.relay_base_url.rstrip('/')}/mcp/oauth/callback"


async def _load_metadata() -> dict:
    global _metadata
    if _metadata is None:
        url = f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            _metadata = resp.json()
    return _metadata


async def _load_jwks() -> KeySet:
    global _jwks
    if _jwks is None:
        meta = await _load_metadata()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(meta["jwks_uri"])
            resp.raise_for_status()
            _jwks = KeySet.import_key_set(resp.json())
    return _jwks


async def build_authorize_url(txn_id: str, verifier: str, nonce: str) -> str:
    """Build the PocketID authorize URL for the upstream leg. ``txn_id`` rides the
    upstream ``state`` so the callback can resume the pending authorization."""
    meta = await _load_metadata()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": callback_uri(),
        "scope": UPSTREAM_SCOPE,
        "state": txn_id,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{meta['authorization_endpoint']}?{urlencode(params)}"


async def exchange_and_validate(code: str, verifier: str, nonce: str) -> dict:
    """Exchange the upstream code for tokens and return the validated ID-token
    claims. Raises on any validation failure (caller treats as auth failure)."""
    meta = await _load_metadata()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": callback_uri(),
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret,
                "code_verifier": verifier,
            },
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        token = resp.json()

    id_token = token.get("id_token")
    if not id_token:
        raise ValueError("upstream token response had no id_token")

    jwks = await _load_jwks()
    token = jwt.decode(id_token, jwks)
    registry = jwt.JWTClaimsRegistry(
        iss={"essential": True, "value": meta["issuer"]},
        aud={"essential": True, "value": settings.oidc_client_id},
        nonce={"essential": True, "value": nonce},
    )
    registry.validate(token.claims)  # enforces exp/nbf/iat + iss/aud/nonce
    return dict(token.claims)


def reset_cache() -> None:
    """Drop cached discovery/JWKS (used by tests)."""
    global _metadata, _jwks
    _metadata = None
    _jwks = None
