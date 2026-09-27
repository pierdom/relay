from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Cookie, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings
from .identity import Actor, actor_from_session, resolve_bearer

_bearer = HTTPBearer(auto_error=False)

SESSION_COOKIE = "relay_session"
_SALT = "relay-session"

# The primary API_KEY's identity, and the subject a pasted primary key's session
# carries (`POST /session`).
APIKEY_SUB = "apikey"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_signing_key, salt=_SALT)


def create_session(sub: str = APIKEY_SUB, email: str = "", *, key: bool = False) -> str:
    """Sign an identity-carrying, expiring session token.

    The payload holds who the session is for; expiry is enforced at verify time
    via the signed timestamp (``max_age``), not just the browser cookie. ``key``
    marks a session minted from a pasted API key (``POST /session``), which
    stays valid only while that key is still configured.
    """
    payload: dict = {"sub": sub, "email": email}
    if key:
        payload["key"] = True
    return _serializer().dumps(payload)


def still_authorized(payload: dict) -> bool:
    """Whether the session's subject is *currently* allowed in.

    ``routes.auth._authorized()`` runs once, at the OIDC callback — but the cookie
    then stays valid for ``SESSION_MAX_AGE_HOURS`` (30d default), so without a
    re-check, dropping a sub from ``OIDC_ALLOWED_SUBS`` wouldn't revoke a session
    already in the wild: the documented access-control knob would silently not be
    one. Mirrors the same re-check the MCP OAuth refresh grant does
    (``mcp_server._RelayOIDCProxy._extract_upstream_claims``, relay #313) so
    deauthorization behaves alike on both surfaces.

    Sub-allowlist only, exactly like the refresh grant: the session carries
    ``email`` but not ``email_verified``, so an email allowlist can't be
    re-evaluated safely here and is left to login-time enforcement.

    A pasted-key session is checked against the configured keys instead: the
    OIDC allowlist doesn't apply to it (possession of the key was the
    credential), and removing or renaming that key revokes it immediately.
    """
    sub = payload.get("sub", "")
    if payload.get("key") or sub == APIKEY_SUB:
        return sub in settings.all_api_keys
    return not settings.allowed_subs or sub in settings.allowed_subs


def verify_session(token: str) -> dict | None:
    """Return the session payload if the token is validly signed, unexpired, and
    its subject is still authorized."""
    try:
        payload = _serializer().loads(token, max_age=settings.session_max_age_hours * 3600)
    except (BadSignature, SignatureExpired):
        return None
    return payload if still_authorized(payload) else None


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_cross_site(request: Request) -> bool:
    """Whether a browser sent this request from another site.

    ``Sec-Fetch-Site`` is authoritative when present (every current browser
    sends it); otherwise fall back to comparing ``Origin`` with ``Host``. A
    request with neither header is a non-browser client and passes.
    """
    site = request.headers.get("sec-fetch-site")
    if site:
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin:
        return urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower()
    return False


async def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    relay_session: str | None = Cookie(default=None),
) -> Actor:
    """Authenticate the request and resolve *who* made it (relay #198, B-7).

    Most routes use this as a bare ``dependencies=[Depends(require_api_key)]``
    and never look at the return value — only the write routes that need
    provenance (``routes.posts``/``attachments``/``tags``) capture it as
    ``actor: Actor = Depends(require_api_key)``.

    Also the single REST enforcement point for the coarse read/write scope
    gate (relay #198, B-8): a read-only key 403s here, before reaching any
    route body — including routes that don't capture ``actor`` for
    provenance at all (``set_tag_config``, the presigned-upload routes), so
    every write route is covered with zero per-route wiring. The finer
    write-restricted-to-tags check can't happen here (it needs the specific
    post/attachment's tags, which only the route/service layer knows) — see
    ``service._common._require_write_scope``.
    """
    actor: Actor | None = None
    if relay_session:
        payload = verify_session(relay_session)
        if payload is not None:
            # The cookie is a browser credential, so a state-changing request
            # must come from this site. SameSite=Strict already keeps the
            # cookie off cross-site requests in current browsers; this is the
            # second lock.
            if request.method not in _SAFE_METHODS and is_cross_site(request):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-site request rejected")
            actor = actor_from_session(payload)
    if actor is None and credentials:
        actor = resolve_bearer(credentials.credentials)
    if actor is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    if request.method not in _SAFE_METHODS and not actor.can_write:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This API key is read-only")
    return actor
