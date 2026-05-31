from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

import aiosqlite

from ..auth import require_api_key
from ..database import get_db
from .. import events
from ..models import PostCreate, PostListResponse, PostResponse, PostUpdate

router = APIRouter(prefix="/posts", tags=["posts"])


@router.post(
    "",
    response_model=PostResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
async def create_post(
    body: PostCreate,
    db: aiosqlite.Connection = Depends(get_db),
) -> PostResponse:
    tags_str = "," + ",".join(body.tags) + "," if body.tags else ""
    cursor = await db.execute(
        "INSERT INTO posts (title, content, format, tags, source, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (body.title, body.content, body.format, tags_str, body.source, body.expires_at),
    )
    await db.commit()
    async with db.execute("SELECT * FROM posts WHERE id = ?", (cursor.lastrowid,)) as cur:
        row = await cur.fetchone()
    post = PostResponse.from_row(row)
    await events.publish(post.model_dump())
    return post


@router.get(
    "",
    response_model=PostListResponse,
    dependencies=[Depends(require_api_key)],
)
async def list_posts(
    tag: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    format: str | None = Query(default=None),
    search: str | None = Query(default=None),
    db: aiosqlite.Connection = Depends(get_db),
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


@router.get(
    "/{post_id}",
    response_model=PostResponse,
    dependencies=[Depends(require_api_key)],
)
async def get_post(
    post_id: int,
    db: aiosqlite.Connection = Depends(get_db),
) -> PostResponse:
    async with db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return PostResponse.from_row(row)


@router.patch(
    "/{post_id}",
    response_model=PostResponse,
    dependencies=[Depends(require_api_key)],
)
async def update_post(
    post_id: int,
    body: PostUpdate,
    db: aiosqlite.Connection = Depends(get_db),
) -> PostResponse:
    async with db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    updates: dict[str, object] = {}
    if "title" in body.model_fields_set:
        updates["title"] = body.title
    if "content" in body.model_fields_set:
        updates["content"] = body.content
    if "format" in body.model_fields_set:
        updates["format"] = body.format
    if "tags" in body.model_fields_set:
        updates["tags"] = "," + ",".join(body.tags) + "," if body.tags else ""
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


@router.delete(
    "/{post_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_api_key)],
)
async def delete_post(
    post_id: int,
    db: aiosqlite.Connection = Depends(get_db),
) -> None:
    if post_id == 0:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Master document (id=0) cannot be deleted")
    async with db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    await db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    await db.commit()
