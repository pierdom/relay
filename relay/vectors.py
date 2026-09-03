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

import asyncio
import hashlib
import logging

import aiosqlite
import sqlite_vec

from . import chunking, embedding
from .config import settings
from .embedding import EMBEDDING_DIM

logger = logging.getLogger(__name__)

# vec_chunks (the vec0 virtual table) isn't in here — its column width is the
# embedding dimension, which varies by configured model, so it's created
# separately in init_vec once that dimension is known.
_SCHEMA = """
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
CREATE TABLE IF NOT EXISTS embedding_state (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    model_id TEXT NOT NULL,
    dim      INTEGER NOT NULL
);
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


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    async with db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)) as cur:
        return await cur.fetchone() is not None


async def init_vec(db: aiosqlite.Connection) -> bool:
    """Load the extension + create the schema, once, at startup. Mirrors
    ``database._init_fts``: catch, warn, degrade to disabled rather than crash.

    Runs *before* ``vault.rebuild_index`` (unlike FTS, which rebuilds in bulk
    after) — embedding sync is per-row via the same hook writes already use, so
    the tables must exist first. See relay/vault.py's index_upsert/index_insert.

    ``vec_chunks`` (the vec0 virtual table) is created here, not in the static
    ``_SCHEMA`` script, because its column width — the embedding dimension —
    depends on whichever model ``EMBEDDING_MODEL`` currently names, and
    ``CREATE VIRTUAL TABLE IF NOT EXISTS`` silently keeps an existing table's
    *old* width if the model (and thus dimension) has changed since it was
    created — there is no ALTER for a vec0 column. ``embedding_state`` tracks
    the dimension a previous run actually built the table at, so a change is
    detected and the table (plus the now-stale ``chunks`` rows that point into
    it) is dropped and rebuilt. No explicit re-embed is needed after that: the
    content-addressed cache already keys each chunk on ``(model_id, body)``
    (see ``_hash``), so *any* model change already makes every existing chunk
    a cache miss — dropping the vector table just lets the unconditional
    per-startup ``vault.backfill_embeddings`` background task (relay #253,
    v1.1.0) re-fill it against the new, correctly-sized schema instead of
    erroring on the first mismatched insert.

    The dimension check only ever runs while ``embedding_enabled`` is true.
    Toggling the flag off doesn't touch an existing table at all — the
    disabled state has no real dimension of its own to compare against
    (nothing embeds while off), and treating the placeholder default as if it
    were a genuine target would misfire a rebuild on every disable/re-enable
    cycle even when the configured model never changed.
    """
    try:
        await load_extension(db)
        await db.executescript(_SCHEMA)

        vec_chunks_exists = await _table_exists(db, "vec_chunks")

        if settings.embedding_enabled:
            target_model = settings.embedding_model
            target_dim = embedding.resolve_dim(target_model)
            async with db.execute("SELECT model_id, dim FROM embedding_state WHERE id = 1") as cur:
                state = await cur.fetchone()
            if state is not None:
                stored_dim = state["dim"]
            elif vec_chunks_exists:
                # No embedding_state row yet, but a vec_chunks table already
                # exists — either upgrading from before this table existed, or
                # embeddings were enabled once before embedding_state existed.
                # Either way it was built at the old hardcoded default.
                stored_dim = EMBEDDING_DIM
            else:
                stored_dim = target_dim  # first-ever enable, nothing to compare against

            if stored_dim != target_dim:
                logger.warning(
                    "Embedding dimension changed (%sd -> %sd, model now %r) — "
                    "rebuilding vector schema; every post will be re-embedded",
                    stored_dim,
                    target_dim,
                    target_model,
                )
                await db.executescript("DROP TABLE IF EXISTS vec_chunks; DELETE FROM chunks;")

            await db.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding FLOAT[{target_dim}])"
            )
            await db.execute(
                """
                INSERT INTO embedding_state (id, model_id, dim) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET model_id = excluded.model_id, dim = excluded.dim
                """,
                (target_model, target_dim),
            )
        elif not vec_chunks_exists:
            # Never enabled on this vault — pre-build the schema at the
            # shipped default so it's ready the moment the flag flips true,
            # same as before EMBEDDING_MODEL was configurable.
            await db.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding FLOAT[{EMBEDDING_DIM}])"
            )

        await db.commit()
        return True
    except (aiosqlite.Error, AttributeError, OSError, ValueError) as exc:
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


def _backend_model_id() -> str:
    """Sync helper run off the event loop via asyncio.to_thread (see
    sync_post_chunks) — ensures the backend is loaded (constructing it is the
    ~570MB/several-second cold path, see semantic_search's _embed_query) and
    returns its model_id, needed up front for hashing before we know which
    chunks are actually missing from the cache."""
    return embedding.get_backend().model_id


def _embed_documents(texts: list[str]) -> list[list[float]]:
    """Sync helper run off the event loop via asyncio.to_thread (see
    sync_post_chunks)."""
    return embedding.get_backend().embed_documents(texts)


async def sync_post_chunks(db: aiosqlite.Connection, *, post_id: int, title: str, content: str) -> None:
    """Re-chunk a post and reconcile ``chunks``/``vec_chunks`` against it.
    A chunk whose body hash is already in ``embeddings_cache`` skips the model
    call entirely; only genuinely new/changed chunks get embedded. Stale chunk
    rows (edited/removed sections) are deleted.

    Called inline from the write path (index_upsert/insert), not backgrounded
    like vault.backfill_embeddings — so the two model calls below go through
    asyncio.to_thread, same as semantic_search's _embed_query. Before
    idle-unload (relay #253, v1.1.3) this rarely mattered: the backend loaded
    once on the first write after startup and stayed resident. With
    idle-unload the model can now go cold again after any quiet period, so an
    edit arriving after one would otherwise reconstruct it (~570MB, several
    seconds) directly on the event loop, stalling every other in-flight
    request for the duration — the same class of bug semantic_search's own
    fix closed, just on the write side instead of search."""
    from . import database  # deferred: database imports vault imports this module

    if not (database.VEC_ENABLED and settings.embedding_enabled):
        return

    chunks = chunking.chunk_post(title, content)
    model_id = await asyncio.to_thread(_backend_model_id)
    hashed = [(c, _hash(model_id, c.body)) for c in chunks]

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
        embedded = await asyncio.to_thread(_embed_documents, [c.embed_text for c, _ in missing])
        for (_, h), vec in zip(missing, embedded, strict=True):
            blob = sqlite_vec.serialize_float32(_normalize(vec))
            await db.execute(
                "INSERT OR IGNORE INTO embeddings_cache(content_hash, model_id, embedding) VALUES (?, ?, ?)",
                (h, model_id, blob),
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


async def coverage(db: aiosqlite.Connection) -> tuple[int, int, int]:
    """``(posts_with_chunks, chunks_total, cache_entries)`` — the counts behind
    ``/status``'s embeddings section. ``posts_with_chunks`` is distinct
    ``post_id`` in ``chunks``, not a re-chunk-and-compare: the write path keeps
    a post's chunk rows in sync with its current content on every save (see
    ``sync_post_chunks``), so "has a chunk row" already means "embedded as of
    its last write" without redoing the chunking work here. ``cache_entries``
    can exceed ``chunks_total`` — old rows from a prior model or a deleted
    post's chunks are never eagerly pruned (see ``delete_post_chunks``)."""
    from . import database

    if not database.VEC_ENABLED:
        return (0, 0, 0)
    async with db.execute("SELECT COUNT(DISTINCT post_id) FROM chunks") as cur:
        posts_with_chunks = (await cur.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM chunks") as cur:
        chunks_total = (await cur.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM embeddings_cache") as cur:
        cache_entries = (await cur.fetchone())[0]
    return (posts_with_chunks, chunks_total, cache_entries)


async def current_schema_dim(db: aiosqlite.Connection) -> int:
    """The dimension ``vec_chunks`` was actually built at — the
    ``embedding_state`` row if one exists, else the disabled-state default
    (mirrors ``init_vec``'s own fallback). Used to guard a runtime enable
    (``PATCH /embeddings``, relay #253 v1.3.0) against a model whose
    dimension doesn't match the schema already on disk: that migration only
    runs in ``init_vec`` at startup, so enabling live can't safely rebuild
    the table out from under any in-flight reads."""
    async with db.execute("SELECT dim FROM embedding_state WHERE id = 1") as cur:
        row = await cur.fetchone()
    return row["dim"] if row is not None else EMBEDDING_DIM


async def reset(db: aiosqlite.Connection) -> None:
    """Wipe every embedded chunk, vector, and cache row — a full re-embed
    from scratch, not relying on the content-addressed cache at all. For
    ``POST /embeddings/backfill?force=true`` (relay #253, v1.3.0): a plain
    re-trigger is a fast no-op in steady state precisely because the cache is
    trusted; ``force`` is for when it's specifically the cache that's
    suspected stale or wrong. No-ops if ``VEC_ENABLED`` is false."""
    from . import database

    if not database.VEC_ENABLED:
        return
    await db.executescript("DELETE FROM vec_chunks; DELETE FROM chunks; DELETE FROM embeddings_cache;")
    await db.commit()


def _embed_query(query: str) -> list[float]:
    """Sync helper run off the event loop via asyncio.to_thread (see
    semantic_search) — get_backend()'s lazy model construction belongs in the
    same thread hop as the inference call, not just embed_query itself."""
    return embedding.get_backend().embed_query(query)


async def semantic_search(db: aiosqlite.Connection, query: str, *, limit: int = 50) -> list[tuple[int, float]]:
    """(post_id, best_distance) pairs, ascending distance (= descending
    similarity). Chunk→post aggregation uses **max similarity / min distance**,
    not mean — a post is relevant if *any* section is (relay #253)."""
    from . import database

    if not (database.VEC_ENABLED and settings.embedding_enabled):
        return []

    # get_backend() + embed_query() are both synchronous CPU work (ONNX
    # inference, plus a model load/download on the very first call ever) —
    # relay is single-worker, so running them inline would stall every other
    # in-flight request (other API/MCP calls, SSE delivery) for the duration.
    # to_thread hands the whole chain to a worker thread instead.
    query_embedding = await asyncio.to_thread(_embed_query, query)
    query_vec = sqlite_vec.serialize_float32(_normalize(query_embedding))
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


# Semantic's top-1 L2 distance (unit-normalized vectors, so 0=identical,
# 2=opposite) is a per-query confidence signal — and unlike a raw bm25 score,
# it's actually comparable *across* queries, since it doesn't depend on corpus
# term statistics. Threshold 1.1 confirmed by a golden-set sweep (relay #253
# phase 4) as the plateau peak of a 0.90-1.40 grid — 1.10/1.15 tie for best,
# recall@5 drops on either side. _WEIGHT_WHEN_CONFIDENT was swept 2.0-100.0 at
# that threshold: recall@5 plateaus at 0.687 from 5.0 up, MRR keeps climbing
# to 0.714 by 25.0 then flattens — but weights that high turn the "confident"
# branch into a near-total override of keyword rather than a blend, which
# risks fitting the shape of this one 21-query golden set. 8.0 is the
# conservative pick: it already clears semantic-only's own recall (0.687 vs
# 0.659) and matches its MRR (0.667), while still meaningfully blending
# keyword's ranking rather than discarding it.
_CONFIDENT_DISTANCE = 1.1
_WEIGHT_WHEN_CONFIDENT = 8.0
_WEIGHT_WHEN_UNSURE = 0.5


def semantic_confidence_weight(results: list[tuple[int, float]]) -> float:
    """How much to weight semantic's list in RRF fusion — based on semantic's
    *own* top-1 distance for this query, not a fixed global ratio.

    A fixed global ratio is zero-sum (relay #253 phase 4, measured): weighting
    toward semantic fixes queries where it's strong and breaks queries where
    keyword is strong instead — e.g. a literal multi-document cluster query
    keyword nailed perfectly (recall@5=1.00) dropped to 0.20 under a 2:1
    semantic weight, because semantic was confidently *wrong* about it.
    Gating on semantic's own confidence lets it dominate only when it has a
    real signal, and defer to keyword otherwise."""
    if not results:
        return _WEIGHT_WHEN_UNSURE
    _, top_distance = results[0]
    return _WEIGHT_WHEN_CONFIDENT if top_distance < _CONFIDENT_DISTANCE else _WEIGHT_WHEN_UNSURE


def reciprocal_rank_fusion(
    list_a: list[int], list_b: list[int], *, k: int = 60, weight_a: float = 1.0, weight_b: float = 1.0
) -> list[int]:
    """RRF over two ranked id lists — not score normalisation, since BM25 and
    cosine distance aren't on comparable scales (relay #253). Pure function.

    Equal weights are the naive form and have a real failure mode, measured in
    relay #253's phase 4 eval: RRF sums reciprocal ranks blind to *how good*
    each list is for a given query, so a post that's mediocre-but-present in
    both lists can out-accumulate one that's perfect in one list and absent
    from the other. ``weight_a``/``weight_b`` let a caller lean the fusion
    toward whichever list measured more reliable overall."""
    scores: dict[int, float] = {}
    for rank, post_id in enumerate(list_a, start=1):
        scores[post_id] = scores.get(post_id, 0.0) + weight_a / (k + rank)
    for rank, post_id in enumerate(list_b, start=1):
        scores[post_id] = scores.get(post_id, 0.0) + weight_b / (k + rank)
    return [post_id for post_id, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]
