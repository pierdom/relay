"""``RelayOAuthProvider`` — the OAuth 2.1 Authorization Server state machine.

Implements ``OAuthAuthorizationServerProvider`` against the hashed store. The
SDK's ``TokenHandler`` already enforces PKCE, redirect-URI match, and code expiry
(see ``mcp/server/auth/handlers/token.py``), so this provider's job is: DCR
read/write, the **authorize-broker hook** (delegate the human login to PocketID),
and minting/rotating audience-bound tokens.

The Resource-Server verify path is ``load_access_token`` (the SDK wraps it in its
own ``ProviderTokenVerifier``, and won't accept a separate verifier alongside a
provider). It resolves relay tokens from the store and *also* accepts the static
``API_KEY`` as a synthetic full-scope bearer, so Claude Code CLI
(`--header "Authorization: Bearer <key>"`) keeps working when the flag is on.
"""
from __future__ import annotations

import hmac
import logging
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from ..config import settings
from . import pocketid
from .store import OAuthStore, PendingAuth, StoredCode, get_store, new_secret

logger = logging.getLogger(__name__)

# Loopback hosts allowed to use plain http for the redirect (native/desktop
# clients per RFC 8252). Every other host must use https, so an auth code is
# never delivered in cleartext to a remote, attacker-registered endpoint.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _redirect_uri_allowed(uri: AnyUrl) -> bool:
    """DCR redirect-URI policy. http is loopback-only (native apps, RFC 8252).
    https is restricted to the ``MCP_ALLOWED_REDIRECT_HOSTS`` allowlist so an
    attacker can't register a client pointing at their own https endpoint and
    phish an auth code out to it. An empty allowlist means 'any https' (opt-out)."""
    host = (uri.host or "").lower()
    if uri.scheme == "http" and host in _LOOPBACK_HOSTS:
        return True
    if uri.scheme == "https":
        allowed = settings.mcp_redirect_hosts
        return not allowed or host in allowed
    return False


class RelayAuthorizationCode(AuthorizationCode):
    """Auth code carrying the resolved upstream ``sub`` (not exposed to clients)."""

    sub: str


class RelayRefreshToken(RefreshToken):
    """Refresh token carrying the resolved ``sub`` for rotation."""

    sub: str


class RelayOAuthProvider:
    def __init__(self, store: OAuthStore | None = None) -> None:
        self._store = store or get_store()

    # --- Dynamic Client Registration ----------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info_json = await self._store.get_client(client_id)
        if info_json is None:
            return None
        return OAuthClientInformationFull.model_validate_json(info_json)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not _redirect_uri_allowed(uri):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description="redirect_uri must be https (or http on a loopback host)",
                )
        await self._store.register_client(client_info.client_id, client_info.model_dump_json())

    # --- Authorize (broker to PocketID) -------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Persist the pending client authorization and hand the human off to
        PocketID; the return leg lands on ``/mcp/oauth/callback``."""
        txn_id = new_secret(24)
        verifier, _ = pocketid.pkce_pair()
        nonce = pocketid.new_nonce()
        # Single-user, single-resource server: guarantee the required scope so a
        # client that omits it still yields a usable token.
        scopes = params.scopes or list(settings.mcp_scopes)
        pending = PendingAuth(
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_explicit=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            scopes=scopes,
            resource=params.resource or settings.mcp_resource_url,
            client_state=params.state,
            up_verifier=verifier,
            up_nonce=nonce,
        )
        await self._store.save_pending(txn_id, pending, ttl_seconds=600)
        return await pocketid.build_authorize_url(txn_id, verifier, nonce)

    # --- Authorization code exchange ----------------------------------------
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> RelayAuthorizationCode | None:
        c = await self._store.get_auth_code(authorization_code)
        if c is None or c.client_id != client.client_id:
            return None
        return RelayAuthorizationCode(
            code=authorization_code,
            scopes=c.scopes,
            expires_at=c.expires_at,
            client_id=c.client_id,
            code_challenge=c.code_challenge,
            redirect_uri=AnyUrl(c.redirect_uri),
            redirect_uri_provided_explicitly=c.redirect_uri_explicit,
            resource=c.resource,
            sub=c.sub,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: RelayAuthorizationCode
    ) -> OAuthToken:
        # Single-use: atomically burn the code first. If a concurrent exchange
        # already claimed it, reject rather than mint a second token set.
        if not await self._store.claim_auth_code(authorization_code.code):
            raise TokenError("invalid_grant", "authorization code already used")
        # Always bind to our own resource — relay is the only RS, so we never emit
        # a token carrying a client-chosen (possibly foreign) audience.
        return await self._issue_tokens(
            client_id=client.client_id,
            sub=authorization_code.sub,
            scopes=authorization_code.scopes,
            resource=settings.mcp_resource_url,
        )

    # --- Refresh -------------------------------------------------------------
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RelayRefreshToken | None:
        t = await self._store.get_token(refresh_token)
        if t is None or t.kind != "refresh" or t.revoked or t.client_id != client.client_id:
            return None
        return RelayRefreshToken(
            token=refresh_token,
            client_id=t.client_id,
            scopes=t.scopes,
            expires_at=int(t.expires_at) if t.expires_at else None,
            sub=t.sub,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RelayRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: atomically revoke the presented refresh token, then issue a fresh
        # pair. If it was already consumed (reuse), reject — the standard
        # refresh-token-reuse guard.
        if not await self._store.claim_refresh_token(refresh_token.token):
            raise TokenError("invalid_grant", "refresh token already used")
        return await self._issue_tokens(
            client_id=client.client_id,
            sub=refresh_token.sub,
            scopes=scopes or refresh_token.scopes,
            resource=settings.mcp_resource_url,
        )

    # --- Resource-server verify path ----------------------------------------
    async def load_access_token(self, token: str) -> AccessToken | None:
        """Verify a bearer presented to ``/mcp``. The SDK wraps this in its
        ``ProviderTokenVerifier`` (it won't accept a separate token_verifier
        alongside a provider), so the static-``API_KEY`` back-compat lives here:
        Claude Code CLI (`--header "Authorization: Bearer <key>"`) keeps working
        with the flag on, as a synthetic full-scope bearer bound to our resource."""
        t = await self._store.get_token(token)
        if t is not None and t.kind == "access" and not t.revoked:
            # Audience defense-in-depth: the SDK's RequireAuthMiddleware only checks
            # scopes, so enforce RFC 8707 binding here — a token is valid only at the
            # resource it was minted for. (We only ever mint for our own /mcp, so this
            # is belt-and-suspenders against a future multi-resource setup.)
            in_time = t.expires_at is None or t.expires_at >= time.time()
            if in_time and t.resource == settings.mcp_resource_url:
                return AccessToken(
                    token=token,
                    client_id=t.client_id,
                    scopes=t.scopes,
                    expires_at=int(t.expires_at) if t.expires_at else None,
                    resource=t.resource,
                )
        if token and hmac.compare_digest(token, settings.api_key):
            return AccessToken(
                token=token,
                client_id="apikey",
                scopes=list(settings.mcp_scopes),
                expires_at=None,
                resource=settings.mcp_resource_url,
            )
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await self._store.revoke_token(token.token)

    # --- helpers -------------------------------------------------------------
    async def _issue_tokens(
        self, *, client_id: str, sub: str, scopes: list[str], resource: str
    ) -> OAuthToken:
        access = new_secret()
        refresh = new_secret()
        now = time.time()
        await self._store.save_token(
            access, kind="access", sub=sub, client_id=client_id, scopes=scopes,
            resource=resource, expires_at=now + settings.mcp_access_token_ttl_seconds,
        )
        await self._store.save_token(
            refresh, kind="refresh", sub=sub, client_id=client_id, scopes=scopes,
            resource=resource, expires_at=now + settings.mcp_refresh_token_ttl_seconds,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=settings.mcp_access_token_ttl_seconds,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def mint_authorization_code(self, pending: PendingAuth, sub: str) -> str:
        """Called by the broker callback once PocketID has authenticated the human.
        Binds the code to {client, redirect_uri, code_challenge, sub, resource}."""
        code = new_secret()
        await self._store.save_auth_code(
            code,
            StoredCode(
                client_id=pending.client_id,
                redirect_uri=pending.redirect_uri,
                redirect_uri_explicit=pending.redirect_uri_explicit,
                code_challenge=pending.code_challenge,
                scopes=pending.scopes,
                resource=pending.resource,
                sub=sub,
                expires_at=time.time() + settings.mcp_auth_code_ttl_seconds,
            ),
        )
        return code


_provider: RelayOAuthProvider | None = None


def get_provider() -> RelayOAuthProvider:
    global _provider
    if _provider is None:
        _provider = RelayOAuthProvider()
    return _provider
