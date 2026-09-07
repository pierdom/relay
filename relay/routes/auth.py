from __future__ import annotations

import logging
import re

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from ..auth import SESSION_COOKIE, bearer_matches, create_session, revoke_session, verify_session
from ..config import settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])

# An /id/<id> deep link's `?post=` survives the OIDC round trip via this, not
# a generic `next=` redirect target (which would need its own open-redirect
# validation this doesn't need: the result is always exactly `/?post=<id>`).
# Deliberately *not* the 1-5 digit bound links.IDREF_RE / service.posts.
# _ID_QUERY_RE share for the `#NNN` in-content convention — that bound has
# nothing to do with this one. This one only needs to match what /id/{id}
# itself accepts (`main.py`'s `Path(ge=0)`, no upper bound), so a post id
# outside 1-99999 doesn't silently lose its deep link on this path alone
# while `/id/<id>` and the search-box bare-id shortcut both still work at
# that size. Bounded anyway (not bare `\d+`) so an attacker can't stuff an
# arbitrarily long digit string into the session via a crafted login link.
_POST_ID_RE = re.compile(r"^\d{1,15}$")

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
    # Round-trip a pending /id/<id> deep link through the OIDC redirect, which
    # otherwise drops it: the login button navigates the whole page away, and
    # PocketID's callback always lands back on plain `/`. Stashed in the same
    # short-lived `relay_oauth` session authlib already uses for PKCE/state,
    # not the querystring, so it survives a provider that doesn't echo unknown
    # authorize params back unchanged. Explicitly cleared when absent so a
    # retry without `?post=` can't resurrect a stale value from an earlier
    # attempt. Shares that session's own pre-existing limitation: it's one
    # cookie per browser, not per tab, so two logins started from two tabs
    # before either completes can clobber each other's stashed `post` (and,
    # already, authlib's own state/PKCE verifier) — not a regression this
    # introduces, just not something it fixes either.
    post_id = request.query_params.get("post")
    if post_id and _POST_ID_RE.match(post_id):
        request.session["post"] = post_id
    else:
        request.session.pop("post", None)
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

    post_id = request.session.pop("post", None)
    target = f"/?post={post_id}" if post_id else "/"
    resp = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
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


# ── Break-glass API-key session (the browser's paste-the-key login) ──────────


@router.post("/session", include_in_schema=False)
async def session_create(request: Request, response: Response) -> dict:
    key = ""
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            body = await request.json()
            key = body.get("key", "")
        except Exception:
            pass
    auth = request.headers.get("authorization", "")
    if not key and auth.startswith("Bearer "):
        key = auth[7:]
    if not bearer_matches(key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    token = create_session()
    response.set_cookie(
        key="relay_session",
        value=token,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
        max_age=settings.session_max_age_hours * 3600,
    )
    return {"ok": True}


@router.delete("/session", include_in_schema=False)
async def session_delete(
    response: Response,
    relay_session: str | None = Cookie(default=None),
) -> dict:
    if relay_session:
        revoke_session(relay_session)
    response.delete_cookie("relay_session")
    return {"ok": True}
