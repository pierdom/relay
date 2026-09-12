"""Tags and folders: counts, per-tag TTL config, rename across the vault."""
from __future__ import annotations

import re
from collections import Counter

import aiosqlite

from .. import history, vault
from ..models import (
    FolderCount,
    FolderListResponse,
    TagConfigCreate,
    TagConfigResponse,
    TagCount,
    TagListResponse,
)
from ._common import InvalidTag, _tags_from_sentinel

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
    if not old or not new_name:
        raise InvalidTag
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
            row_properties = vault.decode_properties(row["properties"])
            new_path = vault.write_file(
                id=row["id"], title=row["title"], content=row["content"], tags=renamed,
                source=row["source"], created_at=row["created_at"],
                updated_at=row["updated_at"], expires_at=row["expires_at"],
                old_path=vault.abspath(row["path"]), properties=row_properties,
            )
            await vault.index_upsert(
                db, id=row["id"], title=new_path.stem, path=new_path, content=row["content"],
                tags=renamed, source=row["source"], created_at=row["created_at"],
                updated_at=row["updated_at"], expires_at=row["expires_at"], properties=row_properties,
            )
        await db.execute("UPDATE tag_config SET tag = ? WHERE tag = ?", (new_name, old))
        await vault.write_tag_config(db)
        await db.commit()
    await history.commit(f"tag rename: {old} -> {new_name} ({len(affected)} post(s))")
    return await list_tags(db)


async def set_tag_config(db: aiosqlite.Connection, tag: str, body: TagConfigCreate) -> TagConfigResponse:
    """Set a tag's expiry — or, with neither ``ttl_hours`` nor ``expires_at``,
    **remove** it. A config could be created but never deleted: clearing both
    fields left a ``ttl_hours=0`` row that kept the tag in ``list_tags`` at
    count 0 forever (AUDIT.md G-03)."""
    clean_tag = re.sub(r"[^a-z0-9_-]", "", tag.strip().lower())
    if not clean_tag:
        raise InvalidTag
    if body.ttl_hours is None and body.expires_at is None:
        await db.execute("DELETE FROM tag_config WHERE tag = ?", (clean_tag,))
    else:
        await db.execute(
            "INSERT INTO tag_config (tag, ttl_hours, expires_at) VALUES (?, ?, ?)"
            " ON CONFLICT(tag) DO UPDATE SET ttl_hours = excluded.ttl_hours, expires_at = excluded.expires_at",
            (clean_tag, body.ttl_hours or 0, body.expires_at),
        )
    await vault.write_tag_config(db)
    await db.commit()
    return TagConfigResponse(tag=clean_tag, ttl_hours=body.ttl_hours, expires_at=body.expires_at)

