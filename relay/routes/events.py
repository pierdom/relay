from __future__ import annotations

import asyncio
import hmac
import json
import logging

import aiosqlite
from fastapi import APIRouter, Cookie, Query, Request
from sse_starlette.sse import EventSourceResponse

from ..auth import verify_session
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
    relay_session: str | None = Cookie(default=None),
) -> EventSourceResponse:
    """
    SSE stream. Sends a 'post' event whenever new content is published.
    On reconnect, set the Last-Event-ID header to replay missed posts.
    Optional ?tag= filter to receive only matching content.
    Auth: relay_session cookie or Authorization Bearer header.
    """
    from fastapi import HTTPException, status as http_status

    authed = False
    if relay_session and verify_session(relay_session):
        authed = True
    if not authed:
        auth_header = request.headers.get("authorization", "")
        bearer = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        authed = bool(bearer) and hmac.compare_digest(bearer, settings.api_key)
    if not authed:
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    last_event_id = request.headers.get("last-event-id")

    async def generator():
        # High-water mark of emitted ids. The SSE id: field must only ever move
        # forward — a streamed *edit* carries the post's original (possibly low)
        # id, and emitting it would rewind the client's Last-Event-ID, triggering
        # a full replay storm on the next reconnect.
        high_water = 0
        try:
            high_water = int(last_event_id) if last_event_id else 0
        except ValueError:
            high_water = 0

        # Catch-up: replay posts published since the client was last connected
        if last_event_id and high_water:
            last_id = high_water
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
                high_water = max(high_water, post.id)
                yield {"event": "post", "id": str(post.id), "data": post.model_dump_json()}

        # Live subscription
        q = subscribe(tag)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_SECONDS)
                    if event.get("type") == "delete":
                        # No SSE id: a delete carries the post's (possibly old) id
                        # and must not rewind the client's Last-Event-ID cursor.
                        yield {"event": "delete", "data": json.dumps(event["data"])}
                    else:
                        post = PostResponse(**event["data"])
                        frame = {"event": "post", "data": post.model_dump_json()}
                        # Only advance the cursor for genuinely newer posts; an
                        # edit to an older post streams without an id: field.
                        if post.id > high_water:
                            high_water = post.id
                            frame["id"] = str(post.id)
                        yield frame
                except asyncio.TimeoutError:
                    yield {"event": "keepalive", "data": ""}
        finally:
            unsubscribe(q, tag)
            logger.debug("SSE client disconnected (tag=%s)", tag)

    return EventSourceResponse(generator())
