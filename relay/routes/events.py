from __future__ import annotations

import asyncio
import logging

import aiosqlite
from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

from ..config import settings
from ..events import subscribe, unsubscribe
from ..models import PostResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["events"])

_KEEPALIVE_SECONDS = 30



@router.get("/events")
async def stream_events(
    request: Request,
    tag: str | None = Query(default=None),
    key: str | None = Query(default=None),
) -> EventSourceResponse:
    """
    SSE stream. Sends a 'post' event whenever new content is published.
    On reconnect, set the Last-Event-ID header to replay missed posts.
    Optional ?tag= filter to receive only matching content.
    Auth: Authorization: Bearer <key> header or ?key= query param (for EventSource).
    """
    auth_header = request.headers.get("authorization")
    token = key
    if token is None and auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if token != settings.api_key:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    last_event_id = request.headers.get("last-event-id")

    async def generator():
        # Catch-up: replay posts published since the client was last connected
        if last_event_id:
            try:
                last_id = int(last_event_id)
                conditions = ["id > ?"]
                params: list = [last_id]
                if tag:
                    conditions.append("tags LIKE ?")
                    params.append(f"%,{tag.strip().lower()},%")
                where = "WHERE " + " AND ".join(conditions)
                async with aiosqlite.connect(settings.database_path) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.execute(
                        f"SELECT * FROM posts {where} ORDER BY created_at ASC",
                        params,
                    ) as cur:
                        missed = await cur.fetchall()
                for row in missed:
                    post = PostResponse.from_row(row)
                    yield {"event": "post", "id": str(post.id), "data": post.model_dump_json()}
            except ValueError:
                pass

        # Live subscription
        q = subscribe(tag)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_SECONDS)
                    post = PostResponse(**event)
                    yield {"event": "post", "id": str(post.id), "data": post.model_dump_json()}
                except asyncio.TimeoutError:
                    yield {"event": "keepalive", "data": ""}
        finally:
            unsubscribe(q, tag)
            logger.debug("SSE client disconnected (tag=%s)", tag)

    return EventSourceResponse(generator())
