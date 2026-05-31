from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, status

import aiosqlite

from ..auth import require_api_key
from ..database import get_db
from ..models import TagConfigCreate, TagConfigResponse, TagCount, TagListResponse, TagRename

router = APIRouter(tags=["tags"])


@router.get(
    "/tags",
    response_model=TagListResponse,
    dependencies=[Depends(require_api_key)],
)
async def list_tags(db: aiosqlite.Connection = Depends(get_db)) -> TagListResponse:
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


@router.patch(
    "/tags/{tag}",
    response_model=TagListResponse,
    dependencies=[Depends(require_api_key)],
)
async def rename_tag(
    tag: str,
    body: TagRename,
    db: aiosqlite.Connection = Depends(get_db),
) -> TagListResponse:
    old = tag.strip().lower()
    new = body.new_name
    if old == new:
        return await list_tags(db)
    await db.execute(
        "UPDATE posts SET tags = REPLACE(tags, ?, ?) WHERE tags LIKE ?",
        (f",{old},", f",{new},", f"%,{old},%"),
    )
    await db.execute("UPDATE tag_config SET tag = ? WHERE tag = ?", (new, old))
    await db.commit()
    return await list_tags(db)


@router.post(
    "/tags/{tag}/config",
    response_model=TagConfigResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_api_key)],
)
async def set_tag_config(
    tag: str,
    body: TagConfigCreate,
    db: aiosqlite.Connection = Depends(get_db),
) -> TagConfigResponse:
    clean_tag = tag.strip().lower()
    await db.execute(
        "INSERT INTO tag_config (tag, ttl_hours, expires_at) VALUES (?, ?, ?)"
        " ON CONFLICT(tag) DO UPDATE SET ttl_hours = excluded.ttl_hours, expires_at = excluded.expires_at",
        (clean_tag, body.ttl_hours or 0, body.expires_at),
    )
    await db.commit()
    return TagConfigResponse(tag=clean_tag, ttl_hours=body.ttl_hours, expires_at=body.expires_at)
