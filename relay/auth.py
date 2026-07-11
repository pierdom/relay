from __future__ import annotations

import hmac

from fastapi import Cookie, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings

_bearer = HTTPBearer(auto_error=False)

SESSION_COOKIE = "relay_session"
_SALT = "relay-session"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_signing_key, salt=_SALT)


def create_session(sub: str = "apikey", email: str = "") -> str:
    """Sign an identity-carrying, expiring session token.

    The payload holds who the session is for; expiry is enforced at verify time
    via the signed timestamp (``max_age``), not just the browser cookie.
    """
    return _serializer().dumps({"sub": sub, "email": email})


def verify_session(token: str) -> dict | None:
    """Return the session payload if the token is validly signed and unexpired."""
    try:
        return _serializer().loads(token, max_age=settings.session_max_age_hours * 3600)
    except (BadSignature, SignatureExpired):
        return None


def revoke_session(token: str) -> None:
    pass  # stateless signed cookie; deletion in the endpoint is sufficient


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
