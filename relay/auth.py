from __future__ import annotations

import hashlib
import hmac

from fastapi import Cookie, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

_bearer = HTTPBearer(auto_error=False)


def create_session() -> str:
    return hmac.new(settings.api_key.encode(), b"relay-session", hashlib.sha256).hexdigest()


def verify_session(token: str) -> bool:
    expected = hmac.new(settings.api_key.encode(), b"relay-session", hashlib.sha256).hexdigest()
    return hmac.compare_digest(token, expected)


def revoke_session(token: str) -> None:
    pass  # cookie deletion in the endpoint is sufficient; no server-side state to clear


async def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    relay_session: str | None = Cookie(default=None),
) -> None:
    if relay_session and verify_session(relay_session):
        return
    if credentials and hmac.compare_digest(credentials.credentials, settings.api_key):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
    )
