"""Posts: create/list/search/get/update/delete, wikilink index and backlinks."""
from __future__ import annotations

import re
import time
from pathlib import Path

import aiosqlite

from .. import database, embedding, events, folders, history, links, metrics, vault, vectors
from ..config import settings
from ..models import (
    BacklinksResponse,
    LinkIndexResponse,
    LinkTarget,
    PostCreate,
    PostListResponse,
    PostResponse,
    PostSummary,
    PostSummaryListResponse,
    PostUpdate,
    SearchTiming,
)
from ._common import (
    MAX_PAGE_LIMIT,
    InvalidSearchMode,
    PostNotFound,
    ProtectedPost,
    SemanticSearchUnavailable,
    _clamp,
    _fetch,
    _tags_from_sentinel,
    logger,
)
from .attachments import _all_referenced_attachments, referenced_attachment_names

# ── Posts ─────────────────────────────────────────────────────────────────────


async def create_post(db: aiosqlite.Connection, body: PostCreate) -> PostResponse:
    now = vault.utcnow_iso()
    # Allocate the id and claim it in one atomic step. `allocate_id` is
    # `SELECT MAX(id)+1`, so two writers that read it before either inserts would
    # pick the same id — and the old create path used `index_upsert`, whose
    # `ON CONFLICT(id) DO UPDATE` would then *silently clobber* the first post.
    # Fix: run allocate + INSERT under `BEGIN IMMEDIATE` (the MAX read takes the
    # write lock, so a concurrent writer blocks until we commit and then reads the
    # new MAX), use a plain INSERT (a surviving collision raises instead of
    # overwriting), and retry once. `write_lock` still serialises coroutines in
    # this process; the immediate txn extends the guarantee across connections.
    async with vault.write_lock:
        for attempt in range(2):
            path: Path | None = None
            await db.execute("BEGIN IMMEDIATE")
            try:
                post_id = await vault.allocate_id(db)
                path = vault.write_file(
                    id=post_id,
                    title=body.title,
                    content=body.content,
                    tags=body.tags,
                    source=body.source,
                    created_at=now,
                    updated_at=None,
                    expires_at=body.expires_at,
                )
                await vault.index_insert(
                    db, id=post_id, title=path.stem, path=path, content=body.content,
                    tags=body.tags, source=body.source, created_at=now,
                    updated_at=None, expires_at=body.expires_at,
                )
                await db.commit()
                break
            except aiosqlite.IntegrityError:
                await db.rollback()
                if path is not None:  # drop the orphaned file this attempt wrote
                    vault.delete_file(path)
                if attempt == 1:
                    raise
            except BaseException:
                await db.rollback()
                if path is not None:
                    vault.delete_file(path)
                raise
    post = PostResponse.from_row(await _fetch(db, post_id))
    await events.publish(post.model_dump())
    await history.commit(f"post {post_id} create: {post.title}")
    return post


# bm25 column weights (title, content, source, tags) — title/tags outrank body
# so the canonical post for a term surfaces above passing mentions of it.
_BM25_WEIGHTS = "10.0, 1.0, 2.0, 5.0"
_FTS_TOKEN_RE = re.compile(r"[^\w]+", re.UNICODE)


# Sort keys → SQL column. "updated" falls back to created_at for never-edited
# posts (updated_at is NULL), so it reads as a true "last modified" order.
_SORT_COLUMNS = {
    "created": "posts.created_at",
    "updated": "COALESCE(posts.updated_at, posts.created_at)",
}


def _order_clause(sort: str, order: str) -> str:
    col = _SORT_COLUMNS.get(sort, _SORT_COLUMNS["updated"])
    direction = "ASC" if order == "asc" else "DESC"
    return f"{col} {direction}, posts.id {direction}"


# A bare `123` or `#123` search means "find this post by id", not free text —
# the id column was never indexed by FTS. Same `#NNN` convention and 1-5 digit
# bound `links.IDREF_RE` resolves in-content, so a query and a #-mention agree
# on what counts as an id.
_ID_QUERY_RE = re.compile(r"^#?(\d{1,5})$")


def _id_query(search: str) -> int | None:
    m = _ID_QUERY_RE.match(search.strip())
    return int(m.group(1)) if m else None


def _fts_query(search: str) -> str | None:
    """Turn free text into a safe FTS5 MATCH string, or ``None`` if it has no
    searchable tokens. Every token is stripped to word characters (neutralising
    ``"`` ``*`` ``:`` ``-`` ``(`` and other FTS operators that would raise a
    syntax error), quoted as a literal, and prefix-matched, OR-joined so any
    term can hit — bm25 (see ``_BM25_WEIGHTS``) still ranks a post matching
    every term above one matching only some.

    Was implicit AND (every term required) until an eval harness showed that
    fails almost all natural-language recall queries ("what did we decide
    about the notes backend"): AND requires literal co-occurrence of every
    word including stopwords, which is rarely true even in the right post —
    and short tokens (single-letter Italian "e", "il", "la"…), prefix-matched,
    are so permissive that AND still "succeeds" against irrelevant giant posts
    that merely contain those letters somewhere, silently returning garbage
    instead of nothing. OR-with-ranking degrades gracefully instead."""
    terms: list[str] = []
    for tok in _FTS_TOKEN_RE.split(search):
        if tok and any(c.isalnum() for c in tok):
            terms.append(f'"{tok}"*')
    return " OR ".join(terms) if terms else None


async def _keyword_ranked_ids(
    db: aiosqlite.Connection, search: str, *, limit: int, tag: str | None = None, folder: str | None = None
) -> list[int]:
    """Top-``limit`` post ids by keyword relevance — feeds the RRF input list
    for ``mode="hybrid"``. Empty if FTS5 is unavailable or the query has no
    searchable tokens (no LIKE fallback here; the semantic list still carries
    the search on its own in that case).

    ``tag``/``folder`` (relay #253 usage report, Issue 4) use the exact same
    condition shape as the unranked path below (``posts.tags LIKE
    '%,tag,%'`` / ``posts.path LIKE 'folder/%'``) so a filtered hybrid query
    fuses two lists that agree on which posts are even eligible."""
    if not database.FTS_ENABLED:
        return []
    match = _fts_query(search)
    if match is None:
        return []
    conditions = ["posts_fts MATCH ?"]
    params: list[str | int] = [match]
    f_conds, f_params = database.tag_folder_filters(tag, folder)
    conditions += f_conds
    params += f_params
    params.append(limit)
    async with db.execute(
        f"SELECT posts.id FROM posts JOIN posts_fts ON posts_fts.rowid = posts.id "
        f"WHERE {' AND '.join(conditions)} ORDER BY bm25(posts_fts, {_BM25_WEIGHTS}) LIMIT ?",
        params,
    ) as cur:
        return [row[0] for row in await cur.fetchall()]


# Both rankers' candidate pool must cover offset+limit or pagination silently
# dead-ends past whatever the pool happened to hold — but sqlite-vec's KNN
# cost scales with k, so the pool can't just track an unbounded caller-
# supplied offset either (REST caps limit at 100 but not offset; MCP callers
# aren't validated at all). This caps how deep semantic/hybrid pagination can
# reach — a page past it comes back empty, same as any pagination end state.
_RANKED_POOL_CAP = 200


async def _list_posts_ranked(
    db: aiosqlite.Connection,
    *,
    search: str,
    mode: str,
    limit: int,
    offset: int,
    summary: bool,
    tag: str | None = None,
    folder: str | None = None,
    id_query: int | None = None,
) -> PostListResponse | PostSummaryListResponse:
    """``mode="semantic"``/``"hybrid"`` path (relay #253 phases 2-5). Ranks by
    vector similarity, RRF-fused with keyword for hybrid. ``tag``/``folder``
    (relay #253 usage report, Issue 4) are pushed into both rankers —
    ``vectors.semantic_search`` and ``_keyword_ranked_ids`` — rather than
    applied to the fused list afterward, so a filtered page doesn't need a
    wider pool to make up for candidates dropped post-fusion.

    Raises ``SemanticSearchUnavailable`` if sqlite-vec isn't loaded or
    embeddings are off — callers must not silently fall back to keyword-only,
    since that would look identical to "no matches" for this query.

    ``total`` in the response is the size of the fused candidate pool
    actually considered (bounded by ``_RANKED_POOL_CAP``), not an exact
    corpus-wide match count like the keyword path's ``SELECT COUNT(*)`` —
    "matches" isn't binary for a similarity ranking the way it is for FTS.

    ``id_query`` (a bare-id search, see ``_id_query``) is pinned the same way
    the unranked path pins it: pulled out of the ranked pool so it can't also
    surface at whatever rank it happened to fuse to, and attached as
    ``pinned`` only on the first page."""
    if not (database.VEC_ENABLED and settings.embedding_enabled):
        raise SemanticSearchUnavailable
    metrics.search_queries.inc()
    pool_size = min(max(offset + limit, 50), _RANKED_POOL_CAP)

    # Cold-start observability (relay #253 usage report, Issue 5): sampled
    # *before* the call, since embedding.get_backend() (inside
    # vectors.semantic_search's to_thread hop) is what would load the model —
    # after the call it's unconditionally loaded, so "was it cold" only has a
    # signal if read first.
    cold_start = not embedding.is_loaded()
    t0 = time.monotonic()
    degraded = False
    try:
        semantic_results = await vectors.semantic_search(db, search, limit=pool_size, tag=tag, folder=folder)
    except Exception:
        # The feature is on but the backend failed *now* (model download blocked,
        # OOM, corrupt cache). Answer the query keyword-ranked and say so, rather
        # than 500 — "configured off" stays a loud 503 above (AUDIT.md, Q3).
        logger.warning("Semantic search failed at query time — answering keyword-only", exc_info=True)
        semantic_results = []
        degraded = True
    embedding_ms = round((time.monotonic() - t0) * 1000, 1)
    search_timing = SearchTiming(cold_start=cold_start, embedding_ms=embedding_ms, degraded=degraded)

    semantic_ranked = [pid for pid, _ in semantic_results]
    if degraded:
        ordered_ids = await _keyword_ranked_ids(db, search, limit=pool_size, tag=tag, folder=folder)
    elif mode == "semantic":
        ordered_ids = semantic_ranked
    else:
        keyword_ranked = await _keyword_ranked_ids(db, search, limit=pool_size, tag=tag, folder=folder)
        # Per-query adaptive weight, not a fixed ratio — a fixed global ratio
        # measured zero-sum (relay #253 phase 4): it fixes queries where
        # semantic is strong and breaks queries where keyword is strong
        # instead. Weight semantic by its own top-1 confidence for *this*
        # query so it dominates only when it actually has a signal.
        weight_b = vectors.semantic_confidence_weight(semantic_results)
        ordered_ids = vectors.reciprocal_rank_fusion(
            keyword_ranked, semantic_ranked, weight_a=1.0, weight_b=weight_b
        )

    if id_query is not None:
        ordered_ids = [pid for pid in ordered_ids if pid != id_query]
    page_ids = ordered_ids[offset : offset + limit]
    rows_by_id: dict[int, aiosqlite.Row] = {}
    if page_ids:
        placeholders = ",".join("?" for _ in page_ids)
        async with db.execute(f"SELECT * FROM posts WHERE id IN ({placeholders})", page_ids) as cur:
            rows_by_id = {row["id"]: row for row in await cur.fetchall()}
    rows = [rows_by_id[pid] for pid in page_ids if pid in rows_by_id]

    pin_row = await _fetch(db, id_query) if (id_query is not None and offset == 0) else None

    if summary:
        return PostSummaryListResponse(
            items=[PostSummary.from_row(r) for r in rows],
            total=len(ordered_ids), limit=limit, offset=offset,
            pinned=PostSummary.from_row(pin_row) if pin_row is not None else None,
            search_timing=search_timing,
        )
    return PostListResponse(
        items=[PostResponse.from_row(r) for r in rows],
        total=len(ordered_ids), limit=limit, offset=offset,
        pinned=PostResponse.from_row(pin_row) if pin_row is not None else None,
        search_timing=search_timing,
    )


async def list_posts(
    db: aiosqlite.Connection,
    *,
    tag: str | None = None,
    folder: str | None = None,
    limit: int = 20,
    offset: int = 0,
    search: str | None = None,
    summary: bool = False,
    sort: str = "updated",
    order: str = "desc",
    mode: str = "keyword",
) -> PostListResponse | PostSummaryListResponse:
    if mode not in ("keyword", "semantic", "hybrid"):
        raise InvalidSearchMode
    # ``?tag=`` / ``?search=`` (empty) mean "no filter", not "filter on nothing";
    # without this the empty string skipped the master-doc pin while filtering
    # on nothing (AUDIT.md B-14).
    tag, folder, search = tag or None, folder or None, search or None
    limit = _clamp(limit, low=1, high=MAX_PAGE_LIMIT)
    offset = max(0, int(offset))
    id_query = _id_query(search) if search else None
    if search and mode in ("semantic", "hybrid"):
        return await _list_posts_ranked(
            db, search=search, mode=mode, limit=limit, offset=offset, summary=summary,
            tag=tag, folder=folder, id_query=id_query,
        )

    conditions: list[str] = []
    params: list[str | int] = []
    joins = ""
    # Default: last-modified first; an FTS search reorders by relevance instead.
    order_by = _order_clause(sort, order)

    if search:
        metrics.search_queries.inc()
        match = _fts_query(search) if database.FTS_ENABLED else None
        if database.FTS_ENABLED and match is not None:
            joins = "JOIN posts_fts ON posts_fts.rowid = posts.id"
            conditions.append("posts_fts MATCH ?")
            params.append(match)
            order_by = f"bm25(posts_fts, {_BM25_WEIGHTS}), {_order_clause(sort, order)}"
        elif database.FTS_ENABLED:
            # Query had only punctuation/operators → no searchable tokens.
            conditions.append("0")
        else:  # FTS5 unavailable — substring fallback
            q = f"%{search}%"
            conditions.append("(posts.title LIKE ? OR posts.content LIKE ? OR posts.source LIKE ?)")
            params.extend([q, q, q])

    f_conds, f_params = database.tag_folder_filters(tag, folder)
    conditions += f_conds
    params += f_params

    # On the unfiltered home feed, pin the master document (id=0) on top and keep
    # it out of the dated stream so pagination stays consistent across pages.
    # A bare-id search (`_id_query`) pins that post the same way, for the same
    # reason: shown once, above the fold, not duplicated wherever it happened
    # to rank in the dated/relevance-ordered results below.
    pin_master = tag is None and search is None and folder is None
    if pin_master:
        conditions.append("posts.id != 0")
    elif id_query is not None:
        conditions.append("posts.id != ?")
        params.append(id_query)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with db.execute(f"SELECT COUNT(*) FROM posts {joins} {where}", params) as cur:
        count_row = await cur.fetchone()

    async with db.execute(
        f"SELECT posts.* FROM posts {joins} {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
        params + [limit, offset],
    ) as cur:
        rows = await cur.fetchall()

    pin_row = None
    if offset == 0:
        if pin_master:
            pin_row = await _fetch(db, 0)
        elif id_query is not None:
            pin_row = await _fetch(db, id_query)

    if summary:
        return PostSummaryListResponse(
            items=[PostSummary.from_row(r) for r in rows],
            total=count_row[0],
            limit=limit,
            offset=offset,
            pinned=PostSummary.from_row(pin_row) if pin_row is not None else None,
        )

    return PostListResponse(
        items=[PostResponse.from_row(r) for r in rows],
        total=count_row[0],
        limit=limit,
        offset=offset,
        pinned=PostResponse.from_row(pin_row) if pin_row is not None else None,
    )


async def get_post(db: aiosqlite.Connection, post_id: int) -> PostResponse | None:
    row = await _fetch(db, post_id)
    return PostResponse.from_row(row) if row is not None else None


async def update_post(
    db: aiosqlite.Connection, post_id: int, body: PostUpdate, *, commit_message: str | None = None
) -> PostResponse:
    row = await _fetch(db, post_id)
    if row is None:
        raise PostNotFound

    fields = body.model_fields_set
    title: str = body.title if "title" in fields and body.title is not None else row["title"]
    content: str = body.content if "content" in fields and body.content is not None else row["content"]
    tags = body.tags if "tags" in fields else _tags_from_sentinel(row["tags"])
    source = body.source if "source" in fields else row["source"]
    expires_at = body.expires_at if "expires_at" in fields else row["expires_at"]
    now = vault.utcnow_iso()
    old_path = vault.abspath(row["path"])

    # Auto-file out of Inbox: a note created without a domain tag lands in Inbox;
    # when its first domain tag arrives, move it (and its own attachments) to that
    # folder. Only ever *out of* Inbox — other folders stay human-owned.
    old_folder = folders.folder_of(vault.relpath(old_path))
    move_to = None
    if "tags" in fields and old_folder == folders.INBOX:
        desired = folders.folder_for(post_id, tags or [])
        if desired and desired != folders.INBOX:
            move_to = desired

    async with vault.write_lock:
        new_path = vault.write_file(
            id=post_id, title=title, content=content, tags=tags or [], source=source,
            created_at=row["created_at"], updated_at=now, expires_at=expires_at,
            old_path=old_path, move_to_folder=move_to,
        )
        await vault.index_upsert(
            db, id=post_id, title=new_path.stem, path=new_path, content=content,
            tags=tags or [], source=source, created_at=row["created_at"],
            updated_at=now, expires_at=expires_at,
        )
        if new_path.stem != row["title"]:
            await _rewrite_inbound_wikilinks(db, old_title=row["title"], new_title=new_path.stem)
        if move_to:
            await _relocate_note_attachments(db, content, old_folder, move_to, post_id)
        await db.commit()
    post = PostResponse.from_row(await _fetch(db, post_id))
    # Stream the edit so other live clients update in place. The SSE layer emits a
    # `post` event without an `id:` field for a known id, so it can't rewind the
    # reconnect cursor. Self-write suppression already covers the vault write, so
    # this is the only path that propagates API/MCP edits (incl. Inbox→domain moves).
    await events.publish(post.model_dump())
    await history.commit(commit_message or f"post {post_id} update: {post.title}")
    return post


async def _relocate_note_attachments(
    db: aiosqlite.Connection, content: str, from_folder: str, to_folder: str, post_id: int
) -> None:
    """Move the note's own attachments from ``from_folder`` to ``to_folder`` when the
    note is relocated. Only files this note references and no *other* post does are
    moved — shared assets stay put (refs still resolve by global-unique name)."""
    wanted = referenced_attachment_names(content)
    if not wanted:
        return
    async with db.execute("SELECT content FROM posts WHERE id != ?", (post_id,)) as cur:
        rows = await cur.fetchall()
    used_elsewhere: set[str] = set()
    for r in rows:
        used_elsewhere |= referenced_attachment_names(r["content"])
    for name, _folder, _size in vault.list_attachments(from_folder):
        if name.lower() in wanted and name.lower() not in used_elsewhere:
            vault.move_attachment(from_folder, to_folder, name)


async def _rewrite_inbound_wikilinks(
    db: aiosqlite.Connection, *, old_title: str, new_title: str
) -> None:
    """Point every ``[[old_title]]`` across the vault at ``new_title`` (rename).

    Mirrors Obsidian's rename behaviour. ``#NNN`` id-refs need no rewrite — the id
    is stable. Runs inside the caller's ``write_lock``; commit is the caller's.
    """
    async with db.execute("SELECT * FROM posts WHERE content LIKE '%[[%'") as cur:
        rows = await cur.fetchall()
    for row in rows:
        new_content, changed = links.rewrite_wikilink_targets(row["content"], old_title, new_title)
        if not changed:
            continue
        row_tags = _tags_from_sentinel(row["tags"])
        new_path = vault.write_file(
            id=row["id"], title=row["title"], content=new_content, tags=row_tags,
            source=row["source"], created_at=row["created_at"],
            updated_at=row["updated_at"], expires_at=row["expires_at"],
            old_path=vault.abspath(row["path"]),
        )
        await vault.index_upsert(
            db, id=row["id"], title=new_path.stem, path=new_path, content=new_content,
            tags=row_tags, source=row["source"], created_at=row["created_at"],
            updated_at=row["updated_at"], expires_at=row["expires_at"],
        )


async def link_index(db: aiosqlite.Connection) -> LinkIndexResponse:
    """All (id, title) pairs — clients build a title→id map to resolve wikilinks."""
    async with db.execute("SELECT id, title FROM posts ORDER BY id") as cur:
        rows = await cur.fetchall()
    return LinkIndexResponse(items=[LinkTarget(id=r["id"], title=r["title"]) for r in rows])


async def get_backlinks(db: aiosqlite.Connection, post_id: int) -> BacklinksResponse:
    """Posts that link to ``post_id`` via ``[[title]]`` or ``#id`` (linked mentions)."""
    if await _fetch(db, post_id) is None:
        raise PostNotFound
    async with db.execute("SELECT id, title, content FROM posts") as cur:
        rows = await cur.fetchall()
    title_to_id = {links.norm_title(r["title"]): r["id"] for r in rows}
    ids = {r["id"] for r in rows}
    items = [
        LinkTarget(id=r["id"], title=r["title"])
        for r in rows
        if r["id"] != post_id and post_id in links.target_ids(r["content"], title_to_id, ids)
    ]
    items.sort(key=lambda t: t.id)
    return BacklinksResponse(items=items)



async def delete_post(db: aiosqlite.Connection, post_id: int) -> None:
    if post_id == 0:
        raise ProtectedPost
    row = await _fetch(db, post_id)
    if row is None:
        raise PostNotFound
    folder = folders.folder_of(row["path"], default=folders.INBOX)
    async with vault.write_lock:
        vault.delete_file(vault.abspath(row["path"]))
        await vault.index_delete(db, post_id)
        await db.commit()
    await events.publish_delete(post_id, _tags_from_sentinel(row["tags"]))
    # Orphan cleanup: drop the attachments *this post* referenced that no
    # remaining post references. Scoped to the deleted post's own embeds on
    # purpose — a folder's assets/ dir also holds files a human dropped in from
    # Obsidian but hasn't embedded yet, and sweeping every unreferenced file in
    # the folder would delete those bystanders. Shared assets (still referenced
    # elsewhere) stay.
    own = referenced_attachment_names(row["content"])
    if own:
        referenced = await _all_referenced_attachments(db)
        for name, _f, _s in vault.list_attachments(folder):
            lowered = name.lower()
            if lowered in own and lowered not in referenced:
                vault.delete_attachment(f"{folder}/{vault.ATTACHMENTS_DIRNAME}/{name}")
    # After the orphan sweep, so the note and the assets it took with it are one
    # commit — restoring the post restores its images in the same revert.
    await history.commit(f"post {post_id} delete: {row['title']}")
