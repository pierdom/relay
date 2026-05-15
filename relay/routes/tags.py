from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, status

import aiosqlite

from ..auth import require_api_key
from ..database import get_db
from ..models import TagConfigCreate, TagConfigResponse, TagCount, TagListResponse

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
    return TagListResponse(tags=[TagCount(tag=t, count=c) for t, c in counter.most_common()])


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
        "INSERT INTO tag_config (tag, ttl_hours) VALUES (?, ?)"
        " ON CONFLICT(tag) DO UPDATE SET ttl_hours = excluded.ttl_hours",
        (clean_tag, body.ttl_hours),
    )
    await db.commit()
    return TagConfigResponse(tag=clean_tag, ttl_hours=body.ttl_hours)
