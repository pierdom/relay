from __future__ import annotations

import hmac

from fastapi import Cookie, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings

_bearer = HTTPBearer(auto_error=False)

SESSION_COOKIE = "relay_session"
_SALT = "relay-session"

# The break-glass API-key paste (`POST /session`) mints this subject. Possession
# of API_KEY is itself the credential there, so the OIDC allowlist doesn't apply.
APIKEY_SUB = "apikey"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_signing_key, salt=_SALT)


def create_session(sub: str = "apikey", email: str = "") -> str:
    """Sign an identity-carrying, expiring session token.

    The payload holds who the session is for; expiry is enforced at verify time
    via the signed timestamp (``max_age``), not just the browser cookie.
    """
    return _serializer().dumps({"sub": sub, "email": email})


def still_authorized(payload: dict) -> bool:
    """Whether the session's subject is *currently* allowed in.

    ``routes.auth._authorized()`` runs once, at the OIDC callback — but the cookie
    then stays valid for ``SESSION_MAX_AGE_HOURS`` (30d default), so without a
    re-check, dropping a sub from ``OIDC_ALLOWED_SUBS`` wouldn't revoke a session
    already in the wild: the documented access-control knob would silently not be
    one. Mirrors the same re-check the MCP OAuth refresh grant does
    (``mcp_oauth/provider.py``) so deauthorization behaves alike on both surfaces.

    Sub-allowlist only, exactly like the refresh grant: the session carries
    ``email`` but not ``email_verified``, so an email allowlist can't be
    re-evaluated safely here and is left to login-time enforcement.
    """
    if not settings.allowed_subs:
        return True
    sub = payload.get("sub", "")
    return sub == APIKEY_SUB or sub in settings.allowed_subs


def verify_session(token: str) -> dict | None:
    """Return the session payload if the token is validly signed, unexpired, and
    its subject is still authorized."""
    try:
        payload = _serializer().loads(token, max_age=settings.session_max_age_hours * 3600)
    except (BadSignature, SignatureExpired):
        return None
    return payload if still_authorized(payload) else None


def revoke_session(token: str) -> None:
    """No-op: the session is a stateless signed cookie, so there is nothing to
    delete server-side — the endpoint clearing the cookie is the logout.

    Note this means a *captured* token stays valid until it expires; there is no
    per-token revocation. Deauthorization (removing a sub from the allowlist) is
    handled by :func:`still_authorized` on every request instead.
    """


async def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    relay_session: str | None = Cookie(default=None),
) -> None:
    if relay_session and verify_session(relay_session) is not None:
        return
    if credentials and hmac.compare_digest(credentials.credentials, settings.api_key):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
    )
