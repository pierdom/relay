from __future__ import annotations

import hashlib
import hmac

from fastapi import Cookie, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

_bearer = HTTPBearer(auto_error=False)

_SESSION_LABEL = b"relay-session-v1"


def _derive_token() -> str:
    """Deterministic session token derived from the API key — survives restarts."""
    return hmac.new(settings.api_key.encode(), _SESSION_LABEL, hashlib.sha256).hexdigest()


def create_session() -> str:
    return _derive_token()


def verify_session(token: str) -> bool:
    return hmac.compare_digest(token, _derive_token())


def revoke_session(token: str) -> None:
    pass  # stateless — revocation happens by deleting the cookie


async def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    relay_session: str | None = Cookie(default=None),
) -> None:
    if relay_session and verify_session(relay_session):
        return
    if credentials and credentials.credentials == settings.api_key:
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
    )
