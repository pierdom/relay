"""Shared business logic for posts and tags.

Both the REST routes (``relay.routes.*``) and the in-process MCP server
(``relay.mcp_server``) call into this layer so the two interfaces never drift.
Every function takes an open ``aiosqlite`` connection with
``row_factory = aiosqlite.Row`` and does its own commit.
"""
from __future__ import annotations

import re
from collections import Counter

import aiosqlite

from . import events
from .models import (
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


def _tags_to_str(tags: list[str]) -> str:
    return "," + ",".join(tags) + "," if tags else ""


# ── Posts ─────────────────────────────────────────────────────────────────────


async def create_post(db: aiosqlite.Connection, body: PostCreate) -> PostResponse:
    cursor = await db.execute(
        "INSERT INTO posts (title, content, format, tags, source, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (body.title, body.content, body.format, _tags_to_str(body.tags), body.source, body.expires_at),
    )
    await db.commit()
    async with db.execute("SELECT * FROM posts WHERE id = ?", (cursor.lastrowid,)) as cur:
        row = await cur.fetchone()
    post = PostResponse.from_row(row)
    await events.publish(post.model_dump())
    return post


async def list_posts(
    db: aiosqlite.Connection,
    *,
    tag: str | None = None,
    limit: int = 20,
    offset: int = 0,
    format: str | None = None,
    search: str | None = None,
) -> PostListResponse:
    conditions: list[str] = []
    params: list[str | int] = []

    if tag:
        conditions.append("tags LIKE ?")
        params.append(f"%,{tag.strip().lower()},%")
    if format:
        conditions.append("format = ?")
        params.append(format)
    if search:
        q = f"%{search}%"
        conditions.append("(title LIKE ? OR content LIKE ? OR source LIKE ?)")
        params.extend([q, q, q])

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with db.execute(f"SELECT COUNT(*) FROM posts {where}", params) as cur:
        count_row = await cur.fetchone()

    async with db.execute(
        f"SELECT * FROM posts {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ) as cur:
        rows = await cur.fetchall()

    return PostListResponse(
        items=[PostResponse.from_row(r) for r in rows],
        total=count_row[0],
        limit=limit,
        offset=offset,
    )


async def get_post(db: aiosqlite.Connection, post_id: int) -> PostResponse | None:
    async with db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)) as cur:
        row = await cur.fetchone()
    return PostResponse.from_row(row) if row is not None else None


async def update_post(db: aiosqlite.Connection, post_id: int, body: PostUpdate) -> PostResponse:
    async with db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)) as cur:
        if await cur.fetchone() is None:
            raise PostNotFound

    updates: dict[str, object] = {}
    if "title" in body.model_fields_set:
        updates["title"] = body.title
    if "content" in body.model_fields_set:
        updates["content"] = body.content
    if "format" in body.model_fields_set:
        updates["format"] = body.format
    if "tags" in body.model_fields_set:
        updates["tags"] = _tags_to_str(body.tags or [])
    if "source" in body.model_fields_set:
        updates["source"] = body.source
    if "expires_at" in body.model_fields_set:
        updates["expires_at"] = body.expires_at

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        set_clause += ", updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
        await db.execute(
            f"UPDATE posts SET {set_clause} WHERE id = ?",
            list(updates.values()) + [post_id],
        )
        await db.commit()

    async with db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)) as cur:
        row = await cur.fetchone()
    return PostResponse.from_row(row)


async def delete_post(db: aiosqlite.Connection, post_id: int) -> None:
    if post_id == 0:
        raise ProtectedPost
    async with db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)) as cur:
        if await cur.fetchone() is None:
            raise PostNotFound
    await db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    await db.commit()


# ── Tags ──────────────────────────────────────────────────────────────────────


async def list_tags(db: aiosqlite.Connection) -> TagListResponse:
    async with db.execute("SELECT tags FROM posts WHERE tags != ''") as cur:
        rows = await cur.fetchall()
    counter: Counter[str] = Counter()
    for row in rows:
        for t in row["tags"].split(","):
            if t:
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
    await db.execute(
        "UPDATE posts SET tags = REPLACE(tags, ?, ?) WHERE tags LIKE ?",
        (f",{old},", f",{new_name},", f"%,{old},%"),
    )
    await db.execute("UPDATE tag_config SET tag = ? WHERE tag = ?", (new_name, old))
    await db.commit()
    return await list_tags(db)


async def set_tag_config(db: aiosqlite.Connection, tag: str, body: TagConfigCreate) -> TagConfigResponse:
    clean_tag = re.sub(r"[^a-z0-9_-]", "", tag.strip().lower())
    await db.execute(
        "INSERT INTO tag_config (tag, ttl_hours, expires_at) VALUES (?, ?, ?)"
        " ON CONFLICT(tag) DO UPDATE SET ttl_hours = excluded.ttl_hours, expires_at = excluded.expires_at",
        (clean_tag, body.ttl_hours or 0, body.expires_at),
    )
    await db.commit()
    return TagConfigResponse(tag=clean_tag, ttl_hours=body.ttl_hours, expires_at=body.expires_at)
