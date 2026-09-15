"""Broker callback — the return leg of the upstream PocketID authorization.

Registered as an *unauthenticated* custom route on the MCP app
(``GET /mcp/oauth/callback``); it's the one piece the SDK can't own, because it
terminates the upstream OIDC leg and resumes the pending MCP authorization.

Flow: validate the PocketID id_token → enforce the **same** ``_authorized()`` sub
allowlist as the web UI → **grant this pending flow's client the consent-gate
approval** (relay #313 Stopgap; see ``consent.py``) → mint a relay auth code
bound to the original client request → 302 back to the client's
``redirect_uri`` with ``code`` + ``state``.

Approval is granted here, not in ``consent.py``'s own ``POST`` handler,
deliberately: this is the one point in the whole flow that has actually
verified a real human authenticated as an allowlisted identity. Consent's own
``POST`` only proves same-browser continuity with its ``GET`` — nowhere near
enough to grant a *permanent* bypass, since DCR + ``/authorize`` are both
unauthenticated and an attacker could satisfy that check against their own
registration. Granting it here instead means an attacker's self-approval
attempt dead-ends at PocketID's login screen, where they'd need real,
allowlisted credentials they don't have.
"""
from __future__ import annotations

import logging

from mcp.server.auth.provider import construct_redirect_uri
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from ..routes.auth import _authorized
from . import pocketid
from .provider import get_provider
from .store import get_store

logger = logging.getLogger(__name__)


def _redirect_error(redirect_uri: str, state: str | None, error: str) -> RedirectResponse:
    url = construct_redirect_uri(redirect_uri, error=error, state=state)
    return RedirectResponse(url, status_code=302)


async def handle_callback(request: Request) -> Response:
    params = request.query_params
    txn_id = params.get("state")
    code = params.get("code")

    if not txn_id:
        return JSONResponse({"error": "invalid_request", "detail": "missing state"}, status_code=400)

    pending = await get_store().pop_pending(txn_id)
    if pending is None:
        return JSONResponse(
            {"error": "invalid_request", "detail": "unknown or expired authorization"},
            status_code=400,
        )

    # Upstream reported an error (user denied, etc.) — relay it back to the client.
    if params.get("error"):
        return _redirect_error(pending.redirect_uri, pending.client_state, params.get("error"))
    if not code:
        return _redirect_error(pending.redirect_uri, pending.client_state, "invalid_request")

    try:
        claims = await pocketid.exchange_and_validate(code, pending.up_verifier, pending.up_nonce)
    except Exception as exc:  # noqa: BLE001 — any upstream failure is an auth failure
        logger.warning("MCP OAuth: upstream token exchange/validation failed: %s", exc)
        return _redirect_error(pending.redirect_uri, pending.client_state, "access_denied")

    sub = claims.get("sub") or ""
    email = (claims.get("email") or "").lower()
    email_verified = claims.get("email_verified") is True
    if not sub or not _authorized(sub, email, email_verified):
        logger.warning("MCP OAuth: login denied for sub=%s email=%s (not in allowlist)", sub, email)
        return _redirect_error(pending.redirect_uri, pending.client_state, "access_denied")

    # A real, allowlisted human has now authenticated through this client —
    # the only point in the flow where that's actually true. Grant the
    # consent-gate approval here (idempotent: a client that skipped the gate
    # because it was already approved just gets its approved_at refreshed).
    await get_store().approve_client(pending.client_id)

    relay_code = await get_provider().mint_authorization_code(pending, sub)
    url = construct_redirect_uri(pending.redirect_uri, code=relay_code, state=pending.client_state)
    return RedirectResponse(url, status_code=302)
