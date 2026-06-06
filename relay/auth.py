from __future__ import annotations

import hmac
import secrets

from fastapi import Cookie, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

_bearer = HTTPBearer(auto_error=False)

_valid_tokens: set[str] = set()


def create_session() -> str:
    token = secrets.token_hex(32)
    _valid_tokens.add(token)
    return token


def verify_session(token: str) -> bool:
    return token in _valid_tokens


def revoke_session(token: str) -> None:
    _valid_tokens.discard(token)


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
