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

import aiosqlite

from . import events, folders, links, vault
from .config import settings
from .models import (
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
    PostUpdate,
    TagConfigCreate,
    TagConfigResponse,
    TagCount,
    TagListResponse,
)


class PostNotFound(Exception):
    """Raised when an operation targets a post id that does not exist."""


class ProtectedPost(Exception):
    """Raised when an operation is not allowed on a reserved post (e.g. id=0)."""


class AttachmentError(Exception):
    """Raised when an attachment can't be stored (e.g. too large)."""


async def _fetch(db: aiosqlite.Connection, post_id: int) -> aiosqlite.Row | None:
    async with db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)) as cur:
        return await cur.fetchone()


def _tags_from_sentinel(s: str) -> list[str]:
    return [t for t in s.split(",") if t]


# ── Posts ─────────────────────────────────────────────────────────────────────


async def create_post(db: aiosqlite.Connection, body: PostCreate) -> PostResponse:
    async with vault.write_lock:
        post_id = await vault.allocate_id(db)
        now = vault.utcnow_iso()
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
        await vault.index_upsert(
            db, id=post_id, title=path.stem, path=path, content=body.content,
            tags=body.tags, source=body.source, created_at=now,
            updated_at=None, expires_at=body.expires_at,
        )
        await db.commit()
    post = PostResponse.from_row(await _fetch(db, post_id))
    await events.publish(post.model_dump())
    return post


async def list_posts(
    db: aiosqlite.Connection,
    *,
    tag: str | None = None,
    folder: str | None = None,
    limit: int = 20,
    offset: int = 0,
    search: str | None = None,
) -> PostListResponse:
    conditions: list[str] = []
    params: list[str | int] = []

    if tag:
        conditions.append("tags LIKE ?")
        params.append(f"%,{tag.strip().lower()},%")
    if folder:
        conditions.append("path LIKE ?")
        params.append(f"{folder}/%")
    if search:
        q = f"%{search}%"
        conditions.append("(title LIKE ? OR content LIKE ? OR source LIKE ?)")
        params.extend([q, q, q])

    # On the unfiltered home feed, pin the master document (id=0) on top and keep
    # it out of the dated stream so pagination stays consistent across pages.
    pin_master = tag is None and search is None and folder is None
    if pin_master:
        conditions.append("id != 0")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with db.execute(f"SELECT COUNT(*) FROM posts {where}", params) as cur:
        count_row = await cur.fetchone()

    async with db.execute(
        f"SELECT * FROM posts {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ) as cur:
        rows = await cur.fetchall()

    pinned = None
    if pin_master and offset == 0:
        master = await _fetch(db, 0)
        if master is not None:
            pinned = PostResponse.from_row(master)

    return PostListResponse(
        items=[PostResponse.from_row(r) for r in rows],
        total=count_row[0],
        limit=limit,
        offset=offset,
        pinned=pinned,
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

    async with vault.write_lock:
        new_path = vault.write_file(
            id=post_id, title=title, content=content, tags=tags or [], source=source,
            created_at=row["created_at"], updated_at=now, expires_at=expires_at,
            old_path=old_path,
        )
        await vault.index_upsert(
            db, id=post_id, title=new_path.stem, path=new_path, content=content,
            tags=tags or [], source=source, created_at=row["created_at"],
            updated_at=now, expires_at=expires_at,
        )
        if new_path.stem != row["title"]:
            await _rewrite_inbound_wikilinks(db, old_title=row["title"], new_title=new_path.stem)
        await db.commit()
    return PostResponse.from_row(await _fetch(db, post_id))


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
    async with vault.write_lock:
        vault.delete_file(vault.abspath(row["path"]))
        await vault.index_delete(db, post_id)
        await db.commit()
    await events.publish_delete(post_id, _tags_from_sentinel(row["tags"]))


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


async def add_attachment(
    db: aiosqlite.Connection,
    *,
    filename: str,
    data: bytes,
    post_id: int | None = None,
    folder: str | None = None,
) -> AttachmentResponse:
    """Store an attachment in a folder's ``assets/`` dir and return its embed ref.

    With ``post_id``: the file is filed under that post's folder and its
    ``![[file]]`` embed is appended to the post's body (streamed via SSE).
    Without it: filed under ``folder`` (or ``Inbox``); the caller places the ref.
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
    else:
        target_folder = folder or folders.INBOX

    # Serialize name-allocation + write against other writers so two concurrent
    # uploads of the same filename can't resolve to the same path and clobber.
    async with vault.write_lock:
        written = vault.write_attachment(target_folder, filename, data)
    ref = f"![[{written.name}]]"

    result_post_id = None
    if row is not None:  # append outside the lock — update_post takes it itself
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
