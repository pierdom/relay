from __future__ import annotations

import logging

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Cookie, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from ..auth import SESSION_COOKIE, create_session, revoke_session, verify_session
from ..config import settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])

# Registered lazily on first use so import never touches the network and a
# missing/rotated OIDC config doesn't break app startup.
_oauth: OAuth | None = None


def _client():
    """Return the configured OIDC client, or None if OIDC isn't enabled."""
    global _oauth
    if not settings.oidc_enabled:
        return None
    if _oauth is None:
        oauth = OAuth()
        oauth.register(
            name="pocketid",
            server_metadata_url=f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            client_kwargs={
                "scope": "openid email profile",
                "code_challenge_method": "S256",  # enforce PKCE
            },
        )
        _oauth = oauth
    return _oauth.pocketid


def _redirect_uri() -> str:
    # Deterministic and proxy-safe: must match the redirect URI registered in
    # PocketID. Derived from RELAY_BASE_URL rather than the request host.
    return f"{settings.relay_base_url.rstrip('/')}/auth/callback"


def _authorized(sub: str, email: str, email_verified: bool) -> bool:
    """Whether this identity may obtain a relay session.

    Prefer the immutable `sub`; email matching requires a *verified* email so a
    user who can edit their own profile email on the IdP can't spoof their way
    onto the allowlist. No allowlist configured => any authenticated user.
    """
    subs = settings.allowed_subs
    emails = settings.allowed_emails
    if not subs and not emails:
        return True
    if subs and sub in subs:
        return True
    return bool(emails and email_verified and email in emails)


def _set_session_cookie(resp: RedirectResponse, sub: str, email: str) -> None:
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=create_session(sub=sub, email=email),
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
        max_age=settings.session_max_age_hours * 3600,
    )


@router.get("/auth/login", include_in_schema=False)
async def auth_login(request: Request):
    client = _client()
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OIDC not configured")
    return await client.authorize_redirect(request, _redirect_uri())


@router.get("/auth/callback", name="auth_callback", include_in_schema=False)
async def auth_callback(request: Request):
    client = _client()
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OIDC not configured")
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as exc:
        logger.warning("OIDC callback failed: %s", exc)
        return RedirectResponse("/?auth_error=1", status_code=status.HTTP_303_SEE_OTHER)

    claims = token.get("userinfo") or {}
    email = (claims.get("email") or "").lower()
    email_verified = claims.get("email_verified") is True
    sub = claims.get("sub") or ""
    if not sub:
        logger.warning("OIDC callback: token had no subject")
        return RedirectResponse("/?auth_error=1", status_code=status.HTTP_303_SEE_OTHER)

    if not _authorized(sub, email, email_verified):
        logger.warning("OIDC login denied for sub=%s email=%s (not in allowlist)", sub, email)
        return RedirectResponse("/?auth_error=forbidden", status_code=status.HTTP_303_SEE_OTHER)

    resp = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(resp, sub=sub, email=email)
    return resp


@router.get("/auth/me", include_in_schema=False)
async def auth_me(relay_session: str | None = Cookie(default=None)) -> dict:
    """Unauthenticated bootstrap: tells the SPA whether a session cookie is live
    and whether the PocketID button should be shown."""
    payload = verify_session(relay_session) if relay_session else None
    return {
        "authenticated": payload is not None,
        "email": (payload or {}).get("email", ""),
        "oidc": settings.oidc_enabled,
    }


@router.get("/auth/logout", include_in_schema=False)
async def auth_logout(relay_session: str | None = Cookie(default=None)):
    if relay_session:
        revoke_session(relay_session)
    resp = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(SESSION_COOKIE)
    return resp
