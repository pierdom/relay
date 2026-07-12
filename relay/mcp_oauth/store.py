"""Persistent, hashed OAuth store at ``.relay/oauth.db``.

A **separate** SQLite file from the disposable ``index.db`` — the startup index
rebuild must never touch it. Holds the state that is *not* reconstructable from
vault files: DCR clients, in-flight authorizations, single-use auth codes, and
issued access/refresh tokens.

Secrets at rest are **hashed** (SHA-256): a DB leak yields no usable token or
code. Rows are looked up by the hash of the value the client presents.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiosqlite

from ..config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id  TEXT PRIMARY KEY,
    info_json  TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_auth (
    txn_id                 TEXT PRIMARY KEY,
    client_id              TEXT NOT NULL,
    redirect_uri           TEXT NOT NULL,
    redirect_uri_explicit  INTEGER NOT NULL DEFAULT 1,
    code_challenge         TEXT NOT NULL,
    scopes                 TEXT NOT NULL DEFAULT '',
    resource               TEXT,
    client_state           TEXT,
    up_verifier            TEXT NOT NULL,
    up_nonce               TEXT NOT NULL,
    expires_at             REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
    code_hash              TEXT PRIMARY KEY,
    client_id              TEXT NOT NULL,
    redirect_uri           TEXT NOT NULL,
    redirect_uri_explicit  INTEGER NOT NULL DEFAULT 1,
    code_challenge         TEXT NOT NULL,
    scopes                 TEXT NOT NULL DEFAULT '',
    resource               TEXT,
    sub                    TEXT NOT NULL,
    expires_at             REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,           -- 'access' | 'refresh'
    sub        TEXT NOT NULL,
    client_id  TEXT NOT NULL,
    scopes     TEXT NOT NULL DEFAULT '',
    resource   TEXT,
    expires_at REAL,
    revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tokens_client ON tokens (client_id);
"""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret(nbytes: int = 32) -> str:
    """A URL-safe random secret (~256 bits at the default) for codes/tokens."""
    return secrets.token_urlsafe(nbytes)


@dataclass(slots=True)
class StoredCode:
    client_id: str
    redirect_uri: str
    redirect_uri_explicit: bool
    code_challenge: str
    scopes: list[str]
    resource: str | None
    sub: str
    expires_at: float


@dataclass(slots=True)
class StoredToken:
    kind: str
    sub: str
    client_id: str
    scopes: list[str]
    resource: str | None
    expires_at: float | None
    revoked: bool


@dataclass(slots=True)
class PendingAuth:
    client_id: str
    redirect_uri: str
    redirect_uri_explicit: bool
    code_challenge: str
    scopes: list[str]
    resource: str | None
    client_state: str | None
    up_verifier: str
    up_nonce: str


class OAuthStore:
    """Async wrapper around ``oauth.db``. One connection per call keeps it simple
    for a single-user server; volume is tiny."""

    def __init__(self, db_path: str | None = None) -> None:
        self._path = db_path or settings.mcp_oauth_db_path

    @asynccontextmanager
    async def _connect(self):
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout=5000;")
            yield db

    async def init(self) -> None:
        async with self._connect() as db:
            await db.executescript(_SCHEMA)
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.commit()

    # --- clients (DCR) -------------------------------------------------------
    async def register_client(self, client_id: str, info_json: str) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT OR REPLACE INTO oauth_clients (client_id, info_json, created_at) VALUES (?, ?, ?)",
                (client_id, info_json, time.time()),
            )
            await db.commit()

    async def get_client(self, client_id: str) -> str | None:
        async with self._connect() as db:
            async with db.execute(
                "SELECT info_json FROM oauth_clients WHERE client_id = ?", (client_id,)
            ) as cur:
                row = await cur.fetchone()
        return row["info_json"] if row else None

    # --- pending authorizations (broker leg) ---------------------------------
    async def save_pending(self, txn_id: str, p: PendingAuth, ttl_seconds: int) -> None:
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO pending_auth
                   (txn_id, client_id, redirect_uri, redirect_uri_explicit, code_challenge,
                    scopes, resource, client_state, up_verifier, up_nonce, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    txn_id, p.client_id, p.redirect_uri, int(p.redirect_uri_explicit),
                    p.code_challenge, " ".join(p.scopes), p.resource, p.client_state,
                    p.up_verifier, p.up_nonce, time.time() + ttl_seconds,
                ),
            )
            await db.commit()

    async def pop_pending(self, txn_id: str) -> PendingAuth | None:
        """Atomically claim a pending authorization (single-use).

        The DELETE is the claim: SQLite serializes writers, so of two concurrent
        callers exactly one sees ``rowcount == 1`` — the loser gets ``None`` rather
        than a duplicate. Guards against a replayed broker callback."""
        async with self._connect() as db:
            async with db.execute("SELECT * FROM pending_auth WHERE txn_id = ?", (txn_id,)) as cur:
                row = await cur.fetchone()
            cur = await db.execute("DELETE FROM pending_auth WHERE txn_id = ?", (txn_id,))
            claimed = cur.rowcount == 1
            await db.commit()
        if row is None or not claimed or row["expires_at"] < time.time():
            return None
        return PendingAuth(
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_explicit=bool(row["redirect_uri_explicit"]),
            code_challenge=row["code_challenge"],
            scopes=row["scopes"].split() if row["scopes"] else [],
            resource=row["resource"],
            client_state=row["client_state"],
            up_verifier=row["up_verifier"],
            up_nonce=row["up_nonce"],
        )

    # --- auth codes ----------------------------------------------------------
    async def save_auth_code(self, code: str, c: StoredCode) -> None:
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO auth_codes
                   (code_hash, client_id, redirect_uri, redirect_uri_explicit, code_challenge,
                    scopes, resource, sub, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _hash(code), c.client_id, c.redirect_uri, int(c.redirect_uri_explicit),
                    c.code_challenge, " ".join(c.scopes), c.resource, c.sub, c.expires_at,
                ),
            )
            await db.commit()

    async def get_auth_code(self, code: str) -> StoredCode | None:
        async with self._connect() as db:
            async with db.execute(
                "SELECT * FROM auth_codes WHERE code_hash = ?", (_hash(code),)
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return StoredCode(
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_explicit=bool(row["redirect_uri_explicit"]),
            code_challenge=row["code_challenge"],
            scopes=row["scopes"].split() if row["scopes"] else [],
            resource=row["resource"],
            sub=row["sub"],
            expires_at=row["expires_at"],
        )

    async def claim_auth_code(self, code: str) -> bool:
        """Atomically consume an auth code (single-use). Returns True iff this call
        deleted it — a concurrent second exchange of the same code gets False, so
        the caller can reject the replay instead of minting a second token set."""
        async with self._connect() as db:
            cur = await db.execute("DELETE FROM auth_codes WHERE code_hash = ?", (_hash(code),))
            claimed = cur.rowcount == 1
            await db.commit()
        return claimed

    # --- tokens --------------------------------------------------------------
    async def save_token(
        self,
        token: str,
        *,
        kind: str,
        sub: str,
        client_id: str,
        scopes: list[str],
        resource: str | None,
        expires_at: float | None,
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                """INSERT OR REPLACE INTO tokens
                   (token_hash, kind, sub, client_id, scopes, resource, expires_at, revoked)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                (_hash(token), kind, sub, client_id, " ".join(scopes), resource, expires_at),
            )
            await db.commit()

    async def get_token(self, token: str) -> StoredToken | None:
        async with self._connect() as db:
            async with db.execute(
                "SELECT * FROM tokens WHERE token_hash = ?", (_hash(token),)
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return StoredToken(
            kind=row["kind"],
            sub=row["sub"],
            client_id=row["client_id"],
            scopes=row["scopes"].split() if row["scopes"] else [],
            resource=row["resource"],
            expires_at=row["expires_at"],
            revoked=bool(row["revoked"]),
        )

    async def revoke_token(self, token: str) -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE tokens SET revoked = 1 WHERE token_hash = ?", (_hash(token),)
            )
            await db.commit()

    async def revoke_all_for(self, client_id: str, sub: str) -> None:
        """Revoke every token (access + refresh) for one principal on one client.
        Used to make revocation cascade to the paired token, and to contain a
        detected refresh-token reuse by killing the whole family (RFC 6819)."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE tokens SET revoked = 1 WHERE client_id = ? AND sub = ?",
                (client_id, sub),
            )
            await db.commit()

    async def claim_refresh_token(self, token: str) -> bool:
        """Atomically revoke a refresh token as part of rotation. Returns True iff
        it was live (revoked in this call). A concurrent re-use of the same refresh
        token gets False → the caller rejects it (refresh-token reuse detection)."""
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE tokens SET revoked = 1 "
                "WHERE token_hash = ? AND kind = 'refresh' AND revoked = 0",
                (_hash(token),),
            )
            claimed = cur.rowcount == 1
            await db.commit()
        return claimed

    async def cleanup_expired(self) -> int:
        """Drop expired pending auths, auth codes, and access tokens. Refresh
        tokens are kept until their own expiry. Returns rows removed."""
        now = time.time()
        async with self._connect() as db:
            cur = await db.execute("DELETE FROM pending_auth WHERE expires_at < ?", (now,))
            removed = cur.rowcount or 0
            cur = await db.execute("DELETE FROM auth_codes WHERE expires_at < ?", (now,))
            removed += cur.rowcount or 0
            cur = await db.execute(
                "DELETE FROM tokens WHERE expires_at IS NOT NULL AND expires_at < ?", (now,)
            )
            removed += cur.rowcount or 0
            await db.commit()
        return removed


_store: OAuthStore | None = None


def get_store() -> OAuthStore:
    """Process-wide store bound to the configured ``oauth.db`` path."""
    global _store
    if _store is None:
        _store = OAuthStore()
    return _store
