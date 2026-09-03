"""Plumbing tests for relay.vectors — cache hit/miss, rebuild/watcher sync,
delete cleanup, semantic search ordering, RRF. All against `FakeBackend`
(relay.embedding): no model, no I/O, fast enough for the default suite.
"""
from __future__ import annotations

import asyncio
import os
import time

os.environ.setdefault("API_KEY", "test-key")

import aiosqlite
import pytest
import pytest_asyncio
import sqlite_vec

from relay import database, embedding, service, vectors, watcher
from relay.config import settings
from relay.embedding import FakeBackend
from relay.models import PostCreate, PostUpdate


class _CountingBackend(FakeBackend):
    def __init__(self) -> None:
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += 1
        return super().embed_documents(texts)


@pytest_asyncio.fixture
async def backend(monkeypatch):
    monkeypatch.setattr(settings, "embedding_enabled", True)
    counting = _CountingBackend()
    monkeypatch.setattr(embedding, "get_backend", lambda: counting)
    return counting


@pytest_asyncio.fixture
async def db(backend):
    await database.init_db()
    assert database.VEC_ENABLED, "sqlite-vec should load in this dev/CI environment"
    conn = await aiosqlite.connect(settings.database_path)
    conn.row_factory = aiosqlite.Row
    # A fresh connection needs the extension loaded again — it's per-connection,
    # not per-database-file (relay.vectors.load_extension's docstring).
    await vectors.load_extension(conn)
    # rebuild_index (run by init_db) deliberately never embeds — see
    # backfill_embeddings's docstring — so seed the master doc (id=0) the same
    # way main.py's lifespan does, then zero the counter so each test's
    # assertions are about its own post, not that baseline.
    from relay import vault
    await vault.backfill_embeddings(conn)
    backend.calls = 0
    yield conn
    await conn.close()


async def _chunk_rows(db, post_id: int):
    async with db.execute("SELECT id, heading_path, content_hash FROM chunks WHERE post_id = ?", (post_id,)) as cur:
        return await cur.fetchall()


LONG_SECTION = "content word " * 60  # safely over the 50-word runt floor


async def _vec_chunks_sql(db) -> str:
    async with db.execute("SELECT sql FROM sqlite_master WHERE name = 'vec_chunks'") as cur:
        return (await cur.fetchone())[0]


async def _insert_fake_chunk(db, dim: int) -> None:
    """Simulate a chunk embedded by a prior run, at ``dim`` — used to prove a
    later init_vec pass actually wipes (or actually preserves) it."""
    await db.execute(
        "INSERT INTO chunks (id, post_id, chunk_index, heading_path, content_hash) VALUES (999, 999, 0, '', 'x')"
    )
    blob = sqlite_vec.serialize_float32([0.0] * dim)
    await db.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES (999, ?)", (blob,))
    await db.commit()


# ── embedding dimension migration (init_vec) ────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_prebuilds_schema_at_default_dim(monkeypatch):
    monkeypatch.setattr(settings, "embedding_enabled", False)
    await database.init_db()
    assert database.VEC_ENABLED
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await vectors.load_extension(db)
        sql = await _vec_chunks_sql(db)
    assert f"FLOAT[{embedding.EMBEDDING_DIM}]" in sql


@pytest.mark.asyncio
async def test_dimension_change_rebuilds_vec_chunks_and_wipes_chunks(monkeypatch, caplog):
    monkeypatch.setattr(settings, "embedding_enabled", True)
    dims = {"model-a": 4, "model-b": 8}
    monkeypatch.setattr(embedding, "resolve_dim", lambda model_id: dims[model_id])
    monkeypatch.setattr(settings, "embedding_model", "model-a")

    await database.init_db()
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await vectors.load_extension(db)
        assert "FLOAT[4]" in await _vec_chunks_sql(db)
        await _insert_fake_chunk(db, dim=4)

    monkeypatch.setattr(settings, "embedding_model", "model-b")
    with caplog.at_level("WARNING"):
        await database.init_db()
    assert "dimension changed" in caplog.text.lower()

    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await vectors.load_extension(db)
        assert "FLOAT[8]" in await _vec_chunks_sql(db)
        async with db.execute("SELECT COUNT(*) FROM chunks") as cur:
            assert (await cur.fetchone())[0] == 0
        async with db.execute("SELECT model_id, dim FROM embedding_state WHERE id = 1") as cur:
            row = await cur.fetchone()
        assert row["model_id"] == "model-b"
        assert row["dim"] == 8


@pytest.mark.asyncio
async def test_model_change_at_the_same_dimension_does_not_rebuild(monkeypatch, caplog):
    """A model swap that keeps the same dimension is already handled by the
    content-addressed cache (every hash misses since it's keyed on model_id) —
    no schema rebuild needed, so existing chunk rows must survive untouched."""
    monkeypatch.setattr(settings, "embedding_enabled", True)
    dims = {"model-a": 4, "model-c": 4}
    monkeypatch.setattr(embedding, "resolve_dim", lambda model_id: dims[model_id])
    monkeypatch.setattr(settings, "embedding_model", "model-a")

    await database.init_db()
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await vectors.load_extension(db)
        await _insert_fake_chunk(db, dim=4)

    monkeypatch.setattr(settings, "embedding_model", "model-c")
    with caplog.at_level("WARNING"):
        await database.init_db()
    assert "dimension changed" not in caplog.text.lower()

    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await vectors.load_extension(db)
        async with db.execute("SELECT COUNT(*) FROM chunks WHERE id = 999") as cur:
            assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_disabling_after_enabled_leaves_existing_schema_untouched(monkeypatch):
    """Toggling embedding_enabled off must not be mistaken for a dimension
    change back to the disabled-state placeholder default — nothing reads or
    writes vec_chunks while disabled, so there's nothing to migrate."""
    monkeypatch.setattr(settings, "embedding_enabled", True)
    monkeypatch.setattr(embedding, "resolve_dim", lambda model_id: 4)
    monkeypatch.setattr(settings, "embedding_model", "model-a")

    await database.init_db()
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await vectors.load_extension(db)
        await _insert_fake_chunk(db, dim=4)

    monkeypatch.setattr(settings, "embedding_enabled", False)
    await database.init_db()

    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await vectors.load_extension(db)
        assert "FLOAT[4]" in await _vec_chunks_sql(db)
        async with db.execute("SELECT COUNT(*) FROM chunks WHERE id = 999") as cur:
            assert (await cur.fetchone())[0] == 1


# ── sync on create/update ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_post_embeds_and_links_vec_chunks(db, backend):
    post = await service.create_post(db, PostCreate(
        title="Post A", content=f"## Section\n{LONG_SECTION}", tags=["dev"],
    ))
    rows = await _chunk_rows(db, post.id)
    assert len(rows) == 1
    async with db.execute("SELECT COUNT(*) FROM vec_chunks WHERE rowid = ?", (rows[0]["id"],)) as cur:
        assert (await cur.fetchone())[0] == 1
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_unchanged_update_is_a_cache_hit(db, backend):
    post = await service.create_post(db, PostCreate(
        title="Post A", content=f"## Section\n{LONG_SECTION}", tags=["dev"],
    ))
    assert backend.calls == 1
    # Re-save byte-identical content — same hash, must not call the backend again.
    await service.update_post(db, post.id, PostUpdate(content=f"## Section\n{LONG_SECTION}"))
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_edit_reembeds_only_the_changed_chunk(db, backend):
    content = f"## First\n{LONG_SECTION}\n\n## Second\n{LONG_SECTION} original"
    post = await service.create_post(db, PostCreate(title="Post A", content=content, tags=["dev"]))
    assert backend.calls == 1  # one batch call embedding both chunks

    edited = f"## First\n{LONG_SECTION}\n\n## Second\n{LONG_SECTION} changed"
    await service.update_post(db, post.id, PostUpdate(content=edited))
    # Second sync's batch call only had the one changed chunk as a miss.
    assert backend.calls == 2
    rows = await _chunk_rows(db, post.id)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_removed_section_drops_its_chunk_row(db, backend):
    content = f"## First\n{LONG_SECTION}\n\n## Second\n{LONG_SECTION} extra"
    post = await service.create_post(db, PostCreate(title="Post A", content=content, tags=["dev"]))
    assert len(await _chunk_rows(db, post.id)) == 2

    await service.update_post(db, post.id, PostUpdate(content=f"## First\n{LONG_SECTION}"))
    rows = await _chunk_rows(db, post.id)
    assert len(rows) == 1
    assert rows[0]["heading_path"] == "First"


# ── delete ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_chunk_and_vec_rows(db, backend):
    post = await service.create_post(db, PostCreate(
        title="Post A", content=f"## Section\n{LONG_SECTION}", tags=["dev"],
    ))
    chunk_id = (await _chunk_rows(db, post.id))[0]["id"]

    await service.delete_post(db, post.id)

    assert await _chunk_rows(db, post.id) == []
    async with db.execute("SELECT COUNT(*) FROM vec_chunks WHERE rowid = ?", (chunk_id,)) as cur:
        assert (await cur.fetchone())[0] == 0


# ── rebuild_index reuses the cache ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_rebuild_index_never_calls_the_embedding_backend(db, backend):
    """rebuild_index runs inline during app startup and must stay fast — it
    must never touch the embedding backend at all, regardless of cache state.
    Embedding sync is deliberately deferred to backfill_embeddings, run as a
    background task instead (relay #253: sequentially embedding a real
    vault's worth of posts inline blocked every HTTP route, /health included,
    until the whole backlog finished — real production downtime, not a
    theoretical concern)."""
    await service.create_post(db, PostCreate(title="Post A", content=f"## S\n{LONG_SECTION}", tags=["dev"]))
    calls_after_create = backend.calls
    assert calls_after_create > 0

    from relay import vault
    await vault.rebuild_index(db)

    assert backend.calls == calls_after_create  # rebuild_index must not embed at all


@pytest.mark.asyncio
async def test_backfill_embeddings_on_unchanged_vault_is_all_cache_hits(db, backend):
    await service.create_post(db, PostCreate(title="Post A", content=f"## S\n{LONG_SECTION}", tags=["dev"]))
    calls_after_create = backend.calls
    assert calls_after_create > 0

    from relay import vault
    await vault.backfill_embeddings(db)

    assert backend.calls == calls_after_create  # nothing new to embed


@pytest.mark.asyncio
async def test_backfill_embeddings_catches_up_what_rebuild_index_skipped(db, backend):
    """The actual promise of the startup-blocking fix: a post that only ever
    went through rebuild_index (sync_embeddings=False) has no chunks until
    backfill_embeddings runs — proving the split doesn't just move the
    embedding call somewhere that never fires."""
    from relay import vault

    path = vault.write_file(
        id=501, title="Rebuilt Only", content=f"## S\n{LONG_SECTION}", tags=["dev"],
        source=None, created_at=vault.utcnow_iso(), updated_at=None, expires_at=None,
    )
    await vault.rebuild_index(db)
    assert await _chunk_rows(db, 501) == []
    assert backend.calls == 0

    await vault.backfill_embeddings(db)

    assert backend.calls > 0
    assert await _chunk_rows(db, 501) != []
    path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_backfill_embeddings_logs_progress_on_a_time_based_cadence(db, backend, monkeypatch, caplog):
    """Not count-based — a fixed post-count interval would either spam the log
    on a cache-hit-heavy run or go silent for minutes on a cold one (see the
    function's own docstring). Simulates the clock jumping 16s on every call
    (past the 15s threshold every time) rather than sleeping for real."""
    import itertools

    from relay import vault

    for i in range(3):
        await service.create_post(
            db, PostCreate(title=f"Progress {i}", content=f"## S\n{LONG_SECTION} item{i}", tags=["dev"])
        )
    # 3 just-created posts + the master doc the db fixture already seeded.
    total = 4

    clock = itertools.count(start=0, step=16)
    monkeypatch.setattr(vault.time, "monotonic", lambda: next(clock))

    with caplog.at_level("INFO", logger="relay.vault"):
        await vault.backfill_embeddings(db)

    progress = [r.message for r in caplog.records if r.message.startswith("Embedding backfill progress")]
    # Every iteration's elapsed time (a constant 16s step) clears the 15s
    # threshold, so every one of the `total` posts gets its own line.
    assert progress == [f"Embedding backfill progress: {i}/{total} posts checked" for i in range(1, total + 1)]


# ── watcher: same code path as the API ───────────────────────────────────────


@pytest.mark.asyncio
async def test_watcher_external_edit_resyncs_chunks(db, backend):
    post = await service.create_post(db, PostCreate(title="Post A", content=f"## S\n{LONG_SECTION}", tags=["dev"]))
    async with db.execute("SELECT path FROM posts WHERE id = ?", (post.id,)) as cur:
        from relay import vault
        path = vault.abspath((await cur.fetchone())["path"])

    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("## S", "## Renamed Section"), encoding="utf-8")
    os.utime(path, None)

    await watcher._reconcile_file(db, path)

    rows = await _chunk_rows(db, post.id)
    assert rows[0]["heading_path"] == "Renamed Section"


# ── semantic_search ordering ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_semantic_search_ranks_exact_match_first(db, backend):
    a = await service.create_post(db, PostCreate(title="Alpha", content=f"## S\n{LONG_SECTION} alpha", tags=["dev"]))
    await service.create_post(db, PostCreate(title="Beta", content=f"## S\n{LONG_SECTION} beta", tags=["dev"]))

    # FakeBackend hashes text deterministically with no query/passage distinction,
    # so querying with a chunk's own embed_text reproduces that exact vector.
    # chunks stores only heading_path + content_hash (no raw text), so
    # reconstructing embed_text just re-runs the same pure chunking function.
    from relay.chunking import chunk_post
    embed_text = chunk_post("Alpha", f"## S\n{LONG_SECTION} alpha")[0].embed_text

    results = await vectors.semantic_search(db, embed_text, limit=5)
    assert results[0][0] == a.id
    assert results[0][1] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_semantic_search_disabled_returns_empty(db, backend, monkeypatch):
    monkeypatch.setattr(settings, "embedding_enabled", False)
    assert await vectors.semantic_search(db, "anything") == []


@pytest.mark.asyncio
async def test_semantic_search_does_not_block_the_event_loop(db, backend, monkeypatch):
    """A slow embed call (real ONNX inference, in production) must not stall
    other concurrent work — relay is single-worker, so a blocking call inline
    would freeze every other in-flight request/SSE delivery for its duration
    (relay #253 phase 5's event-loop-blocking finding). Regression test for
    vectors._embed_query being run via asyncio.to_thread."""

    class SlowBackend(FakeBackend):
        def embed_query(self, text):
            time.sleep(0.2)  # a *blocking* sleep, standing in for ONNX inference
            return super().embed_query(text)

    monkeypatch.setattr(embedding, "get_backend", lambda: SlowBackend())

    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    ticker_task = asyncio.create_task(ticker())
    await vectors.semantic_search(db, "anything")
    # If embed_query ran inline on the event loop, the ticker couldn't have
    # been scheduled at all during those 200ms — it would still read 0 here.
    assert ticks >= 10, "ticker made no progress while semantic_search ran — embedding call is blocking the loop"
    await ticker_task


@pytest.mark.asyncio
async def test_sync_post_chunks_does_not_block_the_event_loop(db, backend, monkeypatch):
    """Same class of bug as semantic_search's, on the write path instead of
    search: create_post/update_post -> index_upsert/insert -> sync_post_chunks
    must not block the event loop either. Mattered less before idle-unload
    (v1.1.3) — the backend loaded once on the first write after startup and
    stayed resident; idle-unload means a cold reload can now happen on any
    write, not just the first one ever. Regression test for
    _backend_model_id/_embed_documents being run via asyncio.to_thread."""

    class SlowBackend(FakeBackend):
        def embed_documents(self, texts):
            time.sleep(0.2)  # a *blocking* sleep, standing in for ONNX inference
            return super().embed_documents(texts)

    monkeypatch.setattr(embedding, "get_backend", lambda: SlowBackend())

    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    ticker_task = asyncio.create_task(ticker())
    await vectors.sync_post_chunks(db, post_id=1, title="Slow", content=f"## S\n{LONG_SECTION}")
    assert ticks >= 10, "ticker made no progress while sync_post_chunks ran — embedding call is blocking the loop"
    await ticker_task


# ── service.list_posts(mode=...) — the REST/MCP-facing entrypoint (relay #253 phase 5) ──


@pytest.mark.asyncio
async def test_list_posts_semantic_mode_unavailable_raises(db, backend, monkeypatch):
    # embedding_enabled off (even with sqlite-vec loaded) must error loud, not
    # silently return an empty/keyword-only list — see SemanticSearchUnavailable's
    # docstring for why.
    monkeypatch.setattr(settings, "embedding_enabled", False)
    with pytest.raises(service.SemanticSearchUnavailable):
        await service.list_posts(db, search="anything", mode="semantic")
    with pytest.raises(service.SemanticSearchUnavailable):
        await service.list_posts(db, search="anything", mode="hybrid")


@pytest.mark.asyncio
async def test_list_posts_rejects_invalid_mode(db, backend):
    # The in-process MCP server calls service.list_posts directly, with none of
    # REST's Query(pattern=...) validation in front of it — a typo'd mode must
    # still error here rather than silently falling back to keyword (relay
    # #253 phase 5 finding: mode also gates SemanticSearchUnavailable, so a
    # silent fallback would swallow both the caller's intent and that error).
    with pytest.raises(service.InvalidSearchMode):
        await service.list_posts(db, search="anything", mode="symantic")


@pytest.mark.asyncio
async def test_list_posts_ranked_mode_rejects_tag_filter(db, backend):
    # _list_posts_ranked doesn't apply SQL filters — silently ignoring tag
    # would return unfiltered results with no signal anything was dropped.
    with pytest.raises(service.RankedSearchFilterUnsupported):
        await service.list_posts(db, search="anything", mode="semantic", tag="dev")


@pytest.mark.asyncio
async def test_list_posts_ranked_mode_rejects_folder_filter(db, backend):
    with pytest.raises(service.RankedSearchFilterUnsupported):
        await service.list_posts(db, search="anything", mode="hybrid", folder="Dev")


@pytest.mark.asyncio
async def test_list_posts_semantic_mode_ranks_via_service(db, backend):
    a = await service.create_post(db, PostCreate(title="Alpha", content=f"## S\n{LONG_SECTION} alpha", tags=["dev"]))
    await service.create_post(db, PostCreate(title="Beta", content=f"## S\n{LONG_SECTION} beta", tags=["dev"]))

    from relay.chunking import chunk_post
    embed_text = chunk_post("Alpha", f"## S\n{LONG_SECTION} alpha")[0].embed_text

    result = await service.list_posts(db, search=embed_text, mode="semantic", summary=True)
    assert result.items[0].id == a.id


@pytest.mark.asyncio
async def test_list_posts_hybrid_mode_fuses_via_service(db, backend):
    a = await service.create_post(db, PostCreate(title="Alpha", content=f"## S\n{LONG_SECTION} alpha", tags=["dev"]))
    await service.create_post(db, PostCreate(title="Beta", content=f"## S\n{LONG_SECTION} beta", tags=["dev"]))

    result = await service.list_posts(db, search="Alpha", mode="hybrid", summary=True)
    assert a.id in [item.id for item in result.items]


# ── ranked-mode candidate pool sizing (relay #253 phase 5 finding #2) ───────


@pytest.mark.asyncio
async def test_ranked_pool_covers_offset_plus_limit(db, backend, monkeypatch):
    """The candidate pool handed to each ranker must grow with the caller's
    offset/limit — a pool stuck at a flat default silently dead-ends
    pagination past it, indistinguishable from "no more results"."""
    calls: list[int] = []
    real_semantic_search = vectors.semantic_search

    async def spy(db_, query, *, limit=50):
        calls.append(limit)
        return await real_semantic_search(db_, query, limit=limit)

    monkeypatch.setattr(vectors, "semantic_search", spy)
    await service.list_posts(db, search="anything", mode="semantic", limit=10, offset=60)
    assert calls[-1] == 70  # offset + limit, comfortably under the cap


@pytest.mark.asyncio
async def test_ranked_pool_size_is_capped(db, backend, monkeypatch):
    """A caller-supplied offset is unvalidated at the MCP layer — the pool must
    not scale unbounded with it (sqlite-vec KNN cost scales with k)."""
    calls: list[int] = []
    real_semantic_search = vectors.semantic_search

    async def spy(db_, query, *, limit=50):
        calls.append(limit)
        return await real_semantic_search(db_, query, limit=limit)

    monkeypatch.setattr(vectors, "semantic_search", spy)
    await service.list_posts(db, search="anything", mode="semantic", limit=50, offset=10_000)
    assert calls[-1] == service._RANKED_POOL_CAP


@pytest.mark.asyncio
async def test_list_posts_semantic_pagination_reaches_past_the_old_flat_pool(db, backend):
    """Regression test: before the fix, both rankers' pool was a flat 50
    regardless of offset/limit, so a page starting past index 50 came back
    empty even with far more real matches in the vault. 60 posts here, page
    at offset=55/limit=10 must still return the tail 5."""
    for i in range(60):
        await service.create_post(
            db, PostCreate(title=f"Post {i}", content=f"## S\n{LONG_SECTION} item{i}", tags=["dev"])
        )

    result = await service.list_posts(db, search="item", mode="semantic", limit=10, offset=55, summary=True)
    # total includes the master doc (id=0) the db fixture already seeded, so
    # don't hardcode 60 — assert against the tail the pool should now reach.
    assert result.total > 55
    assert len(result.items) == result.total - 55


# ── semantic_confidence_weight (pure) ────────────────────────────────────────


def test_confidence_weight_high_for_a_close_top_match():
    weight = vectors.semantic_confidence_weight([(1, 0.2), (2, 0.9)])
    assert weight == vectors._WEIGHT_WHEN_CONFIDENT


def test_confidence_weight_low_for_a_distant_top_match():
    weight = vectors.semantic_confidence_weight([(1, 1.4), (2, 1.45)])
    assert weight == vectors._WEIGHT_WHEN_UNSURE


def test_confidence_weight_only_looks_at_the_top_result():
    # A weak #1 with a strong #2 is still "unsure" — the top rank is what a
    # caller would actually trust, not the best distance anywhere in the list.
    weight = vectors.semantic_confidence_weight([(1, 1.4), (2, 0.1)])
    assert weight == vectors._WEIGHT_WHEN_UNSURE


def test_confidence_weight_empty_list_is_unsure():
    assert vectors.semantic_confidence_weight([]) == vectors._WEIGHT_WHEN_UNSURE


# ── reciprocal_rank_fusion (pure) ────────────────────────────────────────────


def test_rrf_favors_ids_ranked_high_in_both_lists():
    keyword = [1, 2, 3]
    semantic = [3, 1, 4]
    fused = vectors.reciprocal_rank_fusion(keyword, semantic)
    assert fused[0] == 1  # rank 1 in keyword, rank 2 in semantic — best combined
    assert set(fused) == {1, 2, 3, 4}


def test_rrf_matches_the_sum_of_reciprocal_ranks_formula():
    # k=60: id 5 at rank 1 in both lists → 2/(60+1); id 6 only in list_a at
    # rank 2 → 1/(60+2). Not testing argument-order symmetry: RRF's tie-break
    # is insertion order, so swapping which list is "a" isn't a real invariant.
    fused = vectors.reciprocal_rank_fusion([5, 6], [5], k=60)
    assert fused[0] == 5
    assert fused == [5, 6]


def test_rrf_default_weights_are_backward_compatible():
    a, b = [1, 2], [2, 1]
    assert (
        vectors.reciprocal_rank_fusion(a, b, weight_a=1.0, weight_b=1.0)
        == vectors.reciprocal_rank_fusion(a, b)
    )


def test_rrf_weighting_can_flip_which_list_dominates():
    # id 8 is rank1-in-a/rank2-in-b (present in both, never best); id 9 is
    # rank1-in-b only (absent from a) — the exact shape of relay #253's phase 4
    # finding: a post that's mediocre-but-present-everywhere can beat one
    # that's perfect in a single list under equal weights. k=1 (not the
    # production k=60) so the rank1-vs-rank2 gap is large enough to flip
    # within a small, hand-checkable weight — this tests the mechanism only,
    # not service.py's production weight/k choice, which the eval validates.
    list_a, list_b = [8], [9, 8]
    assert vectors.reciprocal_rank_fusion(list_a, list_b, k=1)[0] == 8
    assert vectors.reciprocal_rank_fusion(list_a, list_b, k=1, weight_b=4.0)[0] == 9
