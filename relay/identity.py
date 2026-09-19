"""Per-agent identity and provenance (relay #198, B-7/N-3).

A shared vault has no answer to "who changed this" without it: every writer —
scheduled agents, the web UI human, Claude over MCP — used to share one bearer
token, and every git commit landed under the same pinned identity
(``relay <relay@localhost>``). ``Actor`` is the one identity shape every write
path threads through: resolved once at the auth boundary (``auth.py``,
``mcp_server.py``), then carried into the git commit's real ``--author`` field
(``history.commit``), the ``changes`` table (``changes.py``), and a post's own
``updated_by`` front-matter field (``vault.py``/``models.py``).

Also carries per-key **scope** (relay #198, B-8): resolving *who* made a
request is independent of *what they're allowed to do*, but the two are
looked up together here so every call site gets both from one place. Scope
enforcement itself lives elsewhere (``auth.require_api_key``'s coarse
read/write gate, ``service._common``'s tag-restriction helpers,
``mcp_server``'s ``AuthMiddleware`` wiring) — this module only resolves the
``ApiKeyScope`` a token or session carries, same as it only resolves the
``Actor`` identity.
"""
from __future__ import annotations

import hmac
from collections.abc import Iterable
from dataclasses import dataclass

from .config import FULL_SCOPE, ApiKeyScope, settings

# The break-glass primary key's identity — matches `auth.APIKEY_SUB`, the
# session subject the same key already mints via `POST /session`. Key-name
# validation itself lives in `config.named_api_keys` (parsing and validating
# together there avoids this module importing back into config for the regex).
APIKEY_NAME = "apikey"


@dataclass(frozen=True)
class Actor:
    """One identified writer. ``email`` is synthetic (``name@relay.local``)
    for a named key or a bare OIDC ``sub`` — git's ``--author`` needs the
    ``Name <email>`` shape regardless of whether a real address exists.

    ``scope`` defaults to :data:`config.FULL_SCOPE` so every pre-B-8 call
    site (tests constructing ``Actor`` directly, an OIDC/cookie session)
    keeps its existing full-access behavior without change."""

    name: str
    email: str
    scope: ApiKeyScope = FULL_SCOPE

    @property
    def git_author(self) -> str:
        return f"{self.name} <{self.email}>"

    @property
    def can_write(self) -> bool:
        """Coarse gate (relay #198, B-8): ``False`` only for a read-only key.
        A tag-restricted ``write`` key still counts as "can write" here —
        which specific writes it may perform is ``can_write_tags``'s job."""
        return self.scope.mode != "read"

    def can_write_tags(self, tags: Iterable[str]) -> bool:
        """Whether this actor may write a post/attachment carrying exactly
        this tag set (relay #198, B-8; ALL-of semantics). ``full`` always
        yes; ``read`` always no (defense in depth — should already be
        unreachable via the coarser ``can_write`` gate); ``write`` requires
        every tag in ``tags`` to be in the key's allowed set, **and**
        ``tags`` to be non-empty — an empty set trivially satisfies "subset
        of anything", so without this an untagged post/attachment would
        silently escape a tag-restricted key's own restriction."""
        if self.scope.mode == "full":
            return True
        if self.scope.mode == "read":
            return False
        tagset = set(tags)
        return bool(tagset) and tagset <= self.scope.tags


def _synthetic_email(name: str) -> str:
    return f"{name}@relay.local"


def resolve_bearer(token: str | None) -> Actor | None:
    """The ``Actor`` behind a presented bearer token, or ``None`` if it
    matches no configured key (primary ``API_KEY`` or a ``RELAY_API_KEYS``
    entry). Constant-time per candidate, same convention as the single
    comparison this replaces (``auth.bearer_matches``) — every configured key
    is an independent, long, random secret, so which one a token happens to
    match (if any) isn't a meaningful side channel."""
    if not token:
        return None
    token_bytes = token.encode("utf-8")
    for name, key in settings.all_api_keys.items():
        if hmac.compare_digest(token_bytes, key.encode("utf-8")):
            return Actor(name=name, email=_synthetic_email(name), scope=settings.key_scopes.get(name, FULL_SCOPE))
    return None


def actor_from_session(payload: dict) -> Actor:
    """The ``Actor`` for an already-verified session cookie payload
    (``auth.verify_session``'s return). Prefers ``email`` — human-readable,
    and what a session already carries for an OIDC login — falling back to
    ``sub`` (covers the break-glass ``apikey`` session, whose ``sub`` already
    equals :data:`APIKEY_NAME`).

    ``scope`` (relay #198, B-8) is looked up by ``sub`` the same way
    ``resolve_bearer`` looks it up by key name — a session minted from the
    web UI's key-paste login (``routes.auth``) carries ``sub`` set to the
    pasted key's own resolved name, so a read-only or tag-scoped key can't
    get a full-access session by going through the browser instead of a
    bearer header. A genuine OIDC human login's ``sub``/email won't collide
    with an admin-configured scope name unless the admin deliberately makes
    it collide, which is within the admin's own control."""
    email = payload.get("email") or ""
    sub = payload.get("sub") or APIKEY_NAME
    name = email or sub
    return Actor(name=name, email=email or _synthetic_email(sub), scope=settings.key_scopes.get(sub, FULL_SCOPE))
