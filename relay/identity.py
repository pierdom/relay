"""Per-agent identity and provenance (relay #198, B-7/N-3).

A shared vault has no answer to "who changed this" without it: every writer —
scheduled agents, the web UI human, Claude over MCP — used to share one bearer
token, and every git commit landed under the same pinned identity
(``relay <relay@localhost>``). ``Actor`` is the one identity shape every write
path threads through: resolved once at the auth boundary (``auth.py``,
``mcp_server.py``), then carried into the git commit's real ``--author`` field
(``history.commit``), the ``changes`` table (``changes.py``), and a post's own
``updated_by`` front-matter field (``vault.py``/``models.py``).

Deliberately **not** an enforcement mechanism — resolving *who* made a request
is independent of *what they're allowed to do* (per-key scopes, relay #198's
explicitly optional follow-up to this item).
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass

from .config import settings

# The break-glass primary key's identity — matches `auth.APIKEY_SUB`, the
# session subject the same key already mints via `POST /session`. Key-name
# validation itself lives in `config.named_api_keys` (parsing and validating
# together there avoids this module importing back into config for the regex).
APIKEY_NAME = "apikey"


@dataclass(frozen=True)
class Actor:
    """One identified writer. ``email`` is synthetic (``name@relay.local``)
    for a named key or a bare OIDC ``sub`` — git's ``--author`` needs the
    ``Name <email>`` shape regardless of whether a real address exists."""

    name: str
    email: str

    @property
    def git_author(self) -> str:
        return f"{self.name} <{self.email}>"


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
            return Actor(name=name, email=_synthetic_email(name))
    return None


def actor_from_session(payload: dict) -> Actor:
    """The ``Actor`` for an already-verified session cookie payload
    (``auth.verify_session``'s return). Prefers ``email`` — human-readable,
    and what a session already carries for an OIDC login — falling back to
    ``sub`` (covers the break-glass ``apikey`` session, whose ``sub`` already
    equals :data:`APIKEY_NAME`)."""
    email = payload.get("email") or ""
    sub = payload.get("sub") or APIKEY_NAME
    name = email or sub
    return Actor(name=name, email=email or _synthetic_email(sub))
