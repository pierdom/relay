from __future__ import annotations

import secrets

from fastapi import Cookie, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

_bearer = HTTPBearer(auto_error=False)

# In-memory session store: token → True (all sessions share the same key)
_sessions: dict[str, bool] = {}


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = True
    return token


def revoke_session(token: str) -> None:
    _sessions.pop(token, None)


async def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    relay_session: str | None = Cookie(default=None),
) -> None:
    # Accept HttpOnly session cookie
    if relay_session and _sessions.get(relay_session):
        return
    # Accept Bearer token
    if credentials and credentials.credentials == settings.api_key:
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
    )
