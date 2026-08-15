"""Shared business logic for posts and tags.

Both the REST routes (``relay.routes.*``) and the in-process MCP server
(``relay.mcp_server``) call into this layer. Writes go file-first through
``relay.vault`` (canonical), then mirror into the SQLite index; reads are served
straight from the index. Every function takes an open ``aiosqlite`` connection
with ``row_factory = aiosqlite.Row``.
"""
from __future__ import annotations

import base64
import binascii
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from . import database, events, folders, history, ingest, links, metrics, vault
from .config import settings
from .models import (
    AttachmentDeleteResponse,
    AttachmentInfo,
    AttachmentListResponse,
    AttachmentResponse,
    BacklinksResponse,
    FolderCount,
    FolderListResponse,
    LinkIndexResponse,
    LinkTarget,
    PostCreate,
    PostListResponse,
    PostResponse,
    PostSummary,
    PostSummaryListResponse,
    PostUpdate,
    TagConfigCreate,
    TagConfigResponse,
    TagCount,
    TagListResponse,
    UploadSlotResponse,
)


class PostNotFound(Exception):
    """Raised when an operation targets a post id that does not exist."""


class ProtectedPost(Exception):
    """Raised when an operation is not allowed on a reserved post (e.g. id=0)."""


class AttachmentError(Exception):
    """Raised when an attachment can't be stored (e.g. too large)."""


class AttachmentSourceError(Exception):
    """Raised when the attachment's byte source fails to resolve — a source_url
    fetch error or a presigned upload slot that's unknown/expired/unfilled. Maps
    to a 400 (loud, actionable), distinct from the 413 size cap."""


async def _fetch(db: aiosqlite.Connection, post_id: int) -> aiosqlite.Row | None:
    async with db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)) as cur:
        return await cur.fetchone()


def _tags_from_sentinel(s: str) -> list[str]:
    return [t for t in s.split(",") if t]


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


def _fts_query(search: str) -> str | None:
    """Turn free text into a safe FTS5 MATCH string, or ``None`` if it has no
    searchable tokens. Every token is stripped to word characters (neutralising
    ``"`` ``*`` ``:`` ``-`` ``(`` and other FTS operators that would raise a
    syntax error), quoted as a literal, and prefix-matched; space = implicit AND,
    so ``wireguard proton`` requires both terms."""
    terms: list[str] = []
    for tok in _FTS_TOKEN_RE.split(search):
        if tok and any(c.isalnum() for c in tok):
            terms.append(f'"{tok}"*')
    return " ".join(terms) if terms else None


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
) -> PostListResponse | PostSummaryListResponse:
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

    if tag:
        conditions.append("posts.tags LIKE ?")
        params.append(f"%,{tag.strip().lower()},%")
    if folder:
        conditions.append("posts.path LIKE ?")
        params.append(f"{folder}/%")

    # On the unfiltered home feed, pin the master document (id=0) on top and keep
    # it out of the dated stream so pagination stays consistent across pages.
    pin_master = tag is None and search is None and folder is None
    if pin_master:
        conditions.append("posts.id != 0")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with db.execute(f"SELECT COUNT(*) FROM posts {joins} {where}", params) as cur:
        count_row = await cur.fetchone()

    async with db.execute(
        f"SELECT posts.* FROM posts {joins} {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
        params + [limit, offset],
    ) as cur:
        rows = await cur.fetchall()

    master = None
    if pin_master and offset == 0:
        master = await _fetch(db, 0)

    if summary:
        return PostSummaryListResponse(
            items=[PostSummary.from_row(r) for r in rows],
            total=count_row[0],
            limit=limit,
            offset=offset,
            pinned=PostSummary.from_row(master) if master is not None else None,
        )

    return PostListResponse(
        items=[PostResponse.from_row(r) for r in rows],
        total=count_row[0],
        limit=limit,
        offset=offset,
        pinned=PostResponse.from_row(master) if master is not None else None,
    )


async def get_post(db: aiosqlite.Connection, post_id: int) -> PostResponse | None:
    row = await _fetch(db, post_id)
    return PostResponse.from_row(row) if row is not None else None


async def update_post(db: aiosqlite.Connection, post_id: int, body: PostUpdate) -> PostResponse:
    row = await _fetch(db, post_id)
    if row is None:
        raise PostNotFound

    fields = body.model_fields_set
    title = body.title if "title" in fields else row["title"]
    content = body.content if "content" in fields else row["content"]
    tags = body.tags if "tags" in fields else _tags_from_sentinel(row["tags"])
    source = body.source if "source" in fields else row["source"]
    expires_at = body.expires_at if "expires_at" in fields else row["expires_at"]
    now = vault.utcnow_iso()
    old_path = vault.abspath(row["path"])

    # Auto-file out of Inbox: a note created without a domain tag lands in Inbox;
    # when its first domain tag arrives, move it (and its own attachments) to that
    # folder. Only ever *out of* Inbox — other folders stay human-owned.
    rel = vault.relpath(old_path)
    old_folder = rel.split("/", 1)[0] if "/" in rel else ""
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
    await history.commit(f"post {post_id} update: {post.title}")
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


# Matches ![[file]] embeds and [[file]] links (Obsidian), capturing the target.
_EMBED_OR_LINK_RE = re.compile(r"!?\[\[([^\]|#]+?)(?:\|[^\]]*)?\]\]")


def referenced_attachment_names(content: str) -> set[str]:
    """Lower-cased filenames a post's content embeds/links (``![[x]]`` / ``[[x.ext]]``).

    A target is treated as an attachment only when its last path segment has an
    extension — bare ``[[Note Title]]`` wikilinks are ignored.
    """
    names: set[str] = set()
    for m in _EMBED_OR_LINK_RE.finditer(content or ""):
        target = m.group(1).strip()
        if "." in target.rsplit("/", 1)[-1]:
            names.add(target.rsplit("/", 1)[-1].lower())
    return names


async def _all_referenced_attachments(db: aiosqlite.Connection) -> set[str]:
    async with db.execute("SELECT content FROM posts") as cur:
        rows = await cur.fetchall()
    referenced: set[str] = set()
    for row in rows:
        referenced |= referenced_attachment_names(row["content"])
    return referenced


async def delete_post(db: aiosqlite.Connection, post_id: int) -> None:
    if post_id == 0:
        raise ProtectedPost
    row = await _fetch(db, post_id)
    if row is None:
        raise PostNotFound
    path_str = row["path"]
    folder = path_str.split("/", 1)[0] if "/" in path_str else folders.INBOX
    async with vault.write_lock:
        vault.delete_file(vault.abspath(path_str))
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


# ── Attachments ───────────────────────────────────────────────────────────────

_DATA_URI_RE = re.compile(r"^data:[^;,]*;base64,", re.IGNORECASE)


def decode_attachment_b64(data: str) -> bytes:
    """Decode a client-supplied base64 string, tolerating a ``data:...;base64,``
    prefix and internal whitespace/newlines. Raises ``ValueError`` on bad input."""
    s = _DATA_URI_RE.sub("", (data or "").strip())
    s = re.sub(r"\s+", "", s)
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("data is not valid base64") from exc


async def _resolve_attachment_bytes(
    *,
    filename: str | None,
    data: str | None,
    source_url: str | None,
    upload_id: str | None,
) -> tuple[bytes, str]:
    """Turn whichever transport the caller used into ``(raw_bytes, filename)``.

    - ``data``: inline base64 (``ValueError`` on garbage → 400).
    - ``source_url``: the server fetches it (SSRF-guarded, capped); the name falls
      back to the response's Content-Disposition / URL basename.
    - ``upload_id``: claim a filled presigned slot (single-use).
    """
    if data is not None:
        raw = decode_attachment_b64(data)  # ValueError → 400
        name = filename
    elif source_url is not None:
        try:
            raw, derived = await ingest.fetch_url(
                source_url, max_bytes=settings.attachment_max_bytes
            )
        except ingest.FetchError as exc:
            raise AttachmentSourceError(str(exc)) from exc
        name = filename or derived
    elif upload_id is not None:
        claimed = ingest.registry.claim_slot(upload_id)
        if claimed is None:
            raise AttachmentSourceError(
                f"upload slot '{upload_id}' is unknown, expired, or has no bytes yet"
            )
        raw, name = claimed, filename
    else:
        raise AttachmentSourceError("no attachment source provided")
    if not name:
        raise AttachmentSourceError(
            "filename could not be determined from the source; pass an explicit filename"
        )
    return raw, name


async def ingest_attachment(
    db: aiosqlite.Connection,
    *,
    filename: str | None = None,
    data: str | None = None,
    source_url: str | None = None,
    upload_id: str | None = None,
    post_id: int | None = None,
    folder: str | None = None,
    tags: list[str] | None = None,
    embed: bool = True,
) -> AttachmentResponse:
    """Resolve any of the three byte transports (base64 / source_url / upload_id)
    then store via ``add_attachment``. The single entry point REST + MCP share."""
    raw, name = await _resolve_attachment_bytes(
        filename=filename, data=data, source_url=source_url, upload_id=upload_id
    )
    return await add_attachment(
        db, filename=name, data=raw, post_id=post_id, folder=folder, tags=tags, embed=embed
    )


def create_upload_slot() -> UploadSlotResponse:
    """Mint a presigned upload slot: the caller PUTs raw bytes to ``upload_url``
    out-of-band, then finalizes with ``ingest_attachment(upload_id=…)``. Keeps the
    bytes out of the model context entirely."""
    slot = ingest.registry.create_slot()
    base = settings.relay_base_url.rstrip("/")
    return UploadSlotResponse(
        upload_id=slot.id,
        upload_url=f"{base}/attachments/uploads/{slot.id}",
        max_bytes=settings.attachment_max_bytes,
        expires_at=datetime.fromtimestamp(slot.expires_at, tz=UTC).isoformat(),
    )


async def add_attachment(
    db: aiosqlite.Connection,
    *,
    filename: str,
    data: bytes,
    post_id: int | None = None,
    folder: str | None = None,
    tags: list[str] | None = None,
    embed: bool = True,
) -> AttachmentResponse:
    """Store an attachment in a folder's ``assets/`` dir and return its embed ref.

    Folder precedence: ``post_id`` (the post's own folder) → explicit ``folder`` →
    ``tags`` (same placement policy as a post via ``folders.folder_for``, so a
    compose-time upload lands beside where the note will file) → ``Inbox``.

    With ``post_id`` and ``embed`` true, the ``![[file]]`` embed is also appended to
    the post's body (streamed via SSE). With ``embed`` false (e.g. the UI, which
    inserts the ref itself) the post is left untouched.
    """
    if not data:
        raise AttachmentError("attachment is empty")
    if len(data) > settings.attachment_max_bytes:
        raise AttachmentError(f"attachment exceeds the {settings.attachment_max_mb} MB limit")

    row = None
    if post_id is not None:
        row = await _fetch(db, post_id)
        if row is None:
            raise PostNotFound
        path_str = row["path"]
        target_folder = path_str.split("/", 1)[0] if "/" in path_str else folders.INBOX
    elif folder:
        target_folder = folder
    elif tags:
        target_folder = folders.folder_for(1, tags) or folders.INBOX
    else:
        target_folder = folders.INBOX

    # Serialize name-allocation + write against other writers so two concurrent
    # uploads of the same filename can't resolve to the same path and clobber.
    async with vault.write_lock:
        written = vault.write_attachment(target_folder, filename, data)
    ref = f"![[{written.name}]]"
    # Before the embed below: the upload and the post edit that references it read
    # as two steps in the log instead of the file appearing inside a post update.
    await history.commit(f"attachment add: {written.name}")

    result_post_id = None
    if row is not None and embed:  # append outside the lock — update_post takes it itself
        new_content = row["content"].rstrip() + f"\n\n{ref}\n"
        await update_post(db, post_id, PostUpdate(content=new_content))
        result_post_id = post_id

    return AttachmentResponse(
        filename=written.name, ref=ref, folder=target_folder, post_id=result_post_id
    )


async def list_attachments(
    db: aiosqlite.Connection, *, post_id: int | None = None, folder: str | None = None
) -> AttachmentListResponse:
    """List attachment files under ``assets/`` dirs. ``post_id`` scopes to that
    post's folder; ``folder`` scopes to a named folder; neither scans the vault."""
    if post_id is not None:
        row = await _fetch(db, post_id)
        if row is None:
            raise PostNotFound
        path_str = row["path"]
        folder = path_str.split("/", 1)[0] if "/" in path_str else folders.INBOX
    items = [
        AttachmentInfo(filename=n, folder=f, bytes=s, ref=f"![[{n}]]")
        for (n, f, s) in vault.list_attachments(folder)
    ]
    return AttachmentListResponse(items=items)


async def delete_attachment(db: aiosqlite.Connection, name: str) -> AttachmentDeleteResponse | None:
    """Delete an attachment file. Returns the removed name plus any post ids that
    still embed/link it (now dangling), or ``None`` if it didn't resolve."""
    async with vault.write_lock:
        removed = vault.delete_attachment(name)
    if removed is None:
        return None
    fname = removed.name.lower()
    async with db.execute("SELECT id, content FROM posts") as cur:
        rows = await cur.fetchall()
    referenced_by = [r["id"] for r in rows if fname in referenced_attachment_names(r["content"])]
    await history.commit(f"attachment delete: {removed.name}")
    return AttachmentDeleteResponse(filename=removed.name, referenced_by=sorted(referenced_by))


# ── Tags ──────────────────────────────────────────────────────────────────────


async def list_folders(db: aiosqlite.Connection) -> FolderListResponse:
    """First-level vault folders with post counts (for the sidebar tree view)."""
    async with db.execute("SELECT path FROM posts") as cur:
        rows = await cur.fetchall()
    counter: Counter[str] = Counter()
    for row in rows:
        path = row["path"]
        if "/" in path:  # root files (e.g. the master doc) are not a folder
            counter[path.split("/", 1)[0]] += 1
    return FolderListResponse(
        folders=[FolderCount(folder=f, count=c) for f, c in sorted(counter.items())]
    )


async def list_tags(db: aiosqlite.Connection) -> TagListResponse:
    async with db.execute("SELECT tags FROM posts WHERE tags != ''") as cur:
        rows = await cur.fetchall()
    counter: Counter[str] = Counter()
    for row in rows:
        for t in _tags_from_sentinel(row["tags"]):
            counter[t] += 1
    async with db.execute("SELECT tag FROM tag_config") as cur:
        for row in await cur.fetchall():
            if row["tag"] not in counter:
                counter[row["tag"]] = 0
    return TagListResponse(tags=[TagCount(tag=t, count=c) for t, c in counter.most_common()])


async def rename_tag(db: aiosqlite.Connection, tag: str, new_name: str) -> TagListResponse:
    old = re.sub(r"[^a-z0-9_-]", "", tag.strip().lower())
    if old == new_name:
        return await list_tags(db)

    async with db.execute(
        "SELECT * FROM posts WHERE tags LIKE ?", (f"%,{old},%",)
    ) as cur:
        affected = await cur.fetchall()

    async with vault.write_lock:
        for row in affected:
            tags = _tags_from_sentinel(row["tags"])
            renamed: list[str] = []
            for t in tags:
                t = new_name if t == old else t
                if t not in renamed:
                    renamed.append(t)
            new_path = vault.write_file(
                id=row["id"], title=row["title"], content=row["content"], tags=renamed,
                source=row["source"], created_at=row["created_at"],
                updated_at=row["updated_at"], expires_at=row["expires_at"],
                old_path=vault.abspath(row["path"]),
            )
            await vault.index_upsert(
                db, id=row["id"], title=new_path.stem, path=new_path, content=row["content"],
                tags=renamed, source=row["source"], created_at=row["created_at"],
                updated_at=row["updated_at"], expires_at=row["expires_at"],
            )
        await db.execute("UPDATE tag_config SET tag = ? WHERE tag = ?", (new_name, old))
        await vault.write_tag_config(db)
        await db.commit()
    await history.commit(f"tag rename: {old} -> {new_name} ({len(affected)} post(s))")
    return await list_tags(db)


async def set_tag_config(db: aiosqlite.Connection, tag: str, body: TagConfigCreate) -> TagConfigResponse:
    clean_tag = re.sub(r"[^a-z0-9_-]", "", tag.strip().lower())
    await db.execute(
        "INSERT INTO tag_config (tag, ttl_hours, expires_at) VALUES (?, ?, ?)"
        " ON CONFLICT(tag) DO UPDATE SET ttl_hours = excluded.ttl_hours, expires_at = excluded.expires_at",
        (clean_tag, body.ttl_hours or 0, body.expires_at),
    )
    await vault.write_tag_config(db)
    await db.commit()
    return TagConfigResponse(tag=clean_tag, ttl_hours=body.ttl_hours, expires_at=body.expires_at)
