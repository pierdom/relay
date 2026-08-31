"""Chunk-level embeddings: schema, content-addressed cache, sync, and
semantic/hybrid search (relay post #253, phases 2-4 — proof of concept).

Embeddings are derived data, never canonical — deleting every row here must be
a no-op recoverable by re-running sync. Everything in this module no-ops
unless both ``database.VEC_ENABLED`` (sqlite-vec extension loaded) and
``settings.embedding_enabled`` (explicit opt-in, off by default everywhere —
this is a proof of concept, not yet a shipped feature) are true, so importing
it costs the existing test suite nothing.
"""
from __future__ import annotations

import hashlib
import logging

import aiosqlite
import sqlite_vec

from . import chunking, embedding
from .config import settings
from .embedding import EMBEDDING_DIM

logger = logging.getLogger(__name__)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS embeddings_cache (
    content_hash TEXT PRIMARY KEY,
    model_id     TEXT NOT NULL,
    embedding    BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY,
    post_id      INTEGER NOT NULL,
    chunk_index  INTEGER NOT NULL,
    heading_path TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    UNIQUE(post_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_post_id ON chunks(post_id);
CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding FLOAT[{EMBEDDING_DIM}]);
"""


async def load_extension(db: aiosqlite.Connection) -> None:
    """Load the sqlite-vec extension into ``db``. A SQLite extension is loaded
    **per-connection**, not per-database-file — the schema it creates persists,
    but every fresh ``aiosqlite.connect(...)`` needs this called again before
    it can touch ``vec_chunks`` (a `no such module: vec0` error otherwise).
    Every module that opens its own connection (database.get_db, cleanup,
    mcp_server, watcher) calls this — same pattern already followed for
    ``PRAGMA busy_timeout``."""
    await db.enable_load_extension(True)
    await db.load_extension(sqlite_vec.loadable_path())
    await db.enable_load_extension(False)


async def init_vec(db: aiosqlite.Connection) -> bool:
    """Load the extension + create the schema, once, at startup. Mirrors
    ``database._init_fts``: catch, warn, degrade to disabled rather than crash.

    Runs *before* ``vault.rebuild_index`` (unlike FTS, which rebuilds in bulk
    after) — embedding sync is per-row via the same hook writes already use, so
    the tables must exist first. See relay/vault.py's index_upsert/index_insert.
    """
    try:
        await load_extension(db)
        await db.executescript(_SCHEMA)
        await db.commit()
        return True
    except (aiosqlite.Error, AttributeError, OSError) as exc:
        logger.warning("sqlite-vec unavailable — semantic search disabled (%s)", exc)
        return False


def _hash(model_id: str, body: str) -> str:
    """Content-addressed cache key. Hashes the raw chunk ``body`` only — *not*
    the title-qualified ``embed_text`` — so a rename doesn't invalidate the
    cache (relay #253: "rename/move a post costs nothing"). The stored vector
    for an unchanged chunk still reflects whatever title was current the last
    time it was actually embedded; accepted tradeoff, not a bug to "fix" here.
    """
    return hashlib.sha256(f"{model_id}\0{body}".encode()).hexdigest()


def _normalize(vec: list[float]) -> list[float]:
    """L2-normalize so sqlite-vec's L2 distance ranks identically to cosine
    similarity (‖a-b‖² = 2 − 2·cos(a,b) for unit vectors) — applied uniformly
    regardless of whether a given backend already normalizes its output."""
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec] if norm else vec


async def sync_post_chunks(db: aiosqlite.Connection, *, post_id: int, title: str, content: str) -> None:
    """Re-chunk a post and reconcile ``chunks``/``vec_chunks`` against it.
    A chunk whose body hash is already in ``embeddings_cache`` skips the model
    call entirely; only genuinely new/changed chunks get embedded. Stale chunk
    rows (edited/removed sections) are deleted."""
    from . import database  # deferred: database imports vault imports this module

    if not (database.VEC_ENABLED and settings.embedding_enabled):
        return

    chunks = chunking.chunk_post(title, content)
    backend = embedding.get_backend()
    hashed = [(c, _hash(backend.model_id, c.body)) for c in chunks]

    new_hashes = {h for _, h in hashed}
    if new_hashes:
        placeholders = ",".join("?" for _ in new_hashes)
        async with db.execute(
            f"SELECT content_hash FROM embeddings_cache WHERE content_hash IN ({placeholders})",
            list(new_hashes),
        ) as cur:
            cached = {row[0] for row in await cur.fetchall()}
    else:
        cached = set()

    missing = [(c, h) for c, h in hashed if h not in cached]
    if missing:
        embedded = backend.embed_documents([c.embed_text for c, _ in missing])
        for (_, h), vec in zip(missing, embedded, strict=True):
            blob = sqlite_vec.serialize_float32(_normalize(vec))
            await db.execute(
                "INSERT OR IGNORE INTO embeddings_cache(content_hash, model_id, embedding) VALUES (?, ?, ?)",
                (h, backend.model_id, blob),
            )

    # Drop this post's chunk rows that no longer correspond to any current chunk.
    async with db.execute("SELECT id, content_hash FROM chunks WHERE post_id = ?", (post_id,)) as cur:
        old_rows = await cur.fetchall()
    for row in old_rows:
        if row["content_hash"] not in new_hashes:
            await db.execute("DELETE FROM vec_chunks WHERE rowid = ?", (row["id"],))
            await db.execute("DELETE FROM chunks WHERE id = ?", (row["id"],))

    for idx, (c, h) in enumerate(hashed):
        async with db.execute(
            """
            INSERT INTO chunks (post_id, chunk_index, heading_path, content_hash)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(post_id, chunk_index) DO UPDATE SET
                heading_path=excluded.heading_path, content_hash=excluded.content_hash
            RETURNING id
            """,
            (post_id, idx, c.heading_path, h),
        ) as cur:
            chunk_id = (await cur.fetchone())[0]

        async with db.execute("SELECT embedding FROM embeddings_cache WHERE content_hash = ?", (h,)) as cur:
            cached_vector = (await cur.fetchone())[0]
        await db.execute("DELETE FROM vec_chunks WHERE rowid = ?", (chunk_id,))
        await db.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)", (chunk_id, cached_vector))

    await db.commit()


async def delete_post_chunks(db: aiosqlite.Connection, post_id: int) -> None:
    """Remove a post's chunk rows. ``embeddings_cache`` rows are left alone —
    another post's chunk may share the same hash, and derived data is never
    eagerly pruned (relay #253: deleting every vector must be a recoverable
    no-op, not the other way around)."""
    from . import database

    if not database.VEC_ENABLED:
        return
    async with db.execute("SELECT id FROM chunks WHERE post_id = ?", (post_id,)) as cur:
        rows = await cur.fetchall()
    for row in rows:
        await db.execute("DELETE FROM vec_chunks WHERE rowid = ?", (row[0],))
    await db.execute("DELETE FROM chunks WHERE post_id = ?", (post_id,))
    await db.commit()


async def semantic_search(db: aiosqlite.Connection, query: str, *, limit: int = 50) -> list[tuple[int, float]]:
    """(post_id, best_distance) pairs, ascending distance (= descending
    similarity). Chunk→post aggregation uses **max similarity / min distance**,
    not mean — a post is relevant if *any* section is (relay #253)."""
    from . import database

    if not (database.VEC_ENABLED and settings.embedding_enabled):
        return []

    backend = embedding.get_backend()
    query_vec = sqlite_vec.serialize_float32(_normalize(backend.embed_query(query)))
    # Request more chunks than `limit` posts: several top chunks can share a
    # post, so a 1:1 chunk:post k would under-fill the post-level ranking.
    chunk_k = max(limit * 4, 100)
    async with db.execute(
        """
        SELECT c.post_id, v.distance FROM vec_chunks v
        JOIN chunks c ON c.id = v.rowid
        WHERE v.embedding MATCH ? AND k = ?
        ORDER BY v.distance
        """,
        (query_vec, chunk_k),
    ) as cur:
        rows = await cur.fetchall()

    best: dict[int, float] = {}
    for post_id, distance in rows:
        if post_id not in best or distance < best[post_id]:
            best[post_id] = distance
    return sorted(best.items(), key=lambda kv: kv[1])[:limit]


def reciprocal_rank_fusion(list_a: list[int], list_b: list[int], *, k: int = 60) -> list[int]:
    """RRF over two ranked id lists — not score normalisation, since BM25 and
    cosine distance aren't on comparable scales (relay #253). Pure function."""
    scores: dict[int, float] = {}
    for rank, post_id in enumerate(list_a, start=1):
        scores[post_id] = scores.get(post_id, 0.0) + 1.0 / (k + rank)
    for rank, post_id in enumerate(list_b, start=1):
        scores[post_id] = scores.get(post_id, 0.0) + 1.0 / (k + rank)
    return [post_id for post_id, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]
