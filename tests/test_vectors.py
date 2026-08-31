"""Plumbing tests for relay.vectors — cache hit/miss, rebuild/watcher sync,
delete cleanup, semantic search ordering, RRF. All against `FakeBackend`
(relay.embedding): no model, no I/O, fast enough for the default suite.
"""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import aiosqlite
import pytest
import pytest_asyncio

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
    # init_db's rebuild_index already embedded the master doc (id=0) — zero the
    # counter so each test's assertions are about its own post, not that baseline.
    backend.calls = 0
    yield conn
    await conn.close()


async def _chunk_rows(db, post_id: int):
    async with db.execute("SELECT id, heading_path, content_hash FROM chunks WHERE post_id = ?", (post_id,)) as cur:
        return await cur.fetchall()


LONG_SECTION = "content word " * 60  # safely over the 50-word runt floor


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
async def test_rebuild_index_on_unchanged_vault_is_all_cache_hits(db, backend):
    await service.create_post(db, PostCreate(title="Post A", content=f"## S\n{LONG_SECTION}", tags=["dev"]))
    calls_after_create = backend.calls
    assert calls_after_create > 0

    from relay import vault
    await vault.rebuild_index(db)

    assert backend.calls == calls_after_create  # nothing new to embed


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
