from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Query, Request
from sse_starlette.sse import EventSourceResponse

from .. import database
from ..auth import require_api_key
from ..events import OVERFLOW, subscribe, unsubscribe
from ..models import PostResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["events"])

_KEEPALIVE_SECONDS = 30

# changes.action values that mean "this post is gone" — everything else means
# "fetch its current state and deliver that" (relay #198, N-4).
_DELETE_ACTIONS = {"delete", "external_delete", "expiry"}


async def _current_post(db, post_id: int) -> PostResponse | None:
    async with db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)) as cur:
        row = await cur.fetchone()
    return PostResponse.from_row(row) if row is not None else None


async def catchup_frames(db, *, last_seq: int, tag: str | None) -> list[dict]:
    """Every post that changed since `last_seq` (relay #198, N-4), one SSE
    frame each, collapsed to its latest change in that window — reconnect
    answers "what does it look like now", not "what happened in between"
    (same philosophy the history-panel diff already uses for a single
    post's revisions). Split out from `stream_events` so it's testable
    without driving a live SSE connection.
    """
    conditions = ["seq > ?"]
    params: list = [last_seq]
    if tag:
        conditions.append("tags LIKE ? ESCAPE '\\'")
        params.append(f"%,{database.escape_like(tag.strip().lower())},%")
    where = " AND ".join(conditions)
    async with db.execute(
        f"""
        SELECT c.* FROM changes c
        INNER JOIN (
            SELECT post_id, MAX(seq) AS max_seq FROM changes
            WHERE {where} GROUP BY post_id
        ) latest ON c.post_id = latest.post_id AND c.seq = latest.max_seq
        ORDER BY c.seq ASC
        """,
        params,
    ) as cur:
        missed = await cur.fetchall()
    frames: list[dict] = []
    for row in missed:
        post = None if row["action"] in _DELETE_ACTIONS else await _current_post(db, row["post_id"])
        if post is None:
            # Either recorded as a delete, or recorded as a live change but
            # gone by the time we looked here (e.g. deleted moments later)
            # — either way, gone now.
            frames.append({"event": "delete", "id": str(row["seq"]), "data": json.dumps({"id": row["post_id"]})})
        else:
            frames.append({"event": "post", "id": str(row["seq"]), "data": post.model_dump_json()})
    return frames


@router.get("/events", dependencies=[Depends(require_api_key)])
async def stream_events(
    request: Request,
    tag: str | None = Query(default=None),
) -> EventSourceResponse:
    """
    SSE stream. Sends a 'post' event whenever content is published or edited,
    and a 'delete' event when one is removed. On reconnect, set the
    Last-Event-ID header (a `seq` from a previous frame's `id:`, relay #198
    N-4) to replay every change since — including an edit or delete to a
    post that already existed, not just a brand-new one (closing audit
    B-10/G-07: the old cursor was a post id, which has no way to represent
    "an existing post changed").
    Optional ?tag= filter to receive only matching content.
    Auth: relay_session cookie or Authorization Bearer header (``require_api_key``).
    """
    last_event_id = request.headers.get("last-event-id")

    async def generator():
        try:
            last_seq = int(last_event_id) if last_event_id else 0
        except ValueError:
            last_seq = 0

        # Catch-up: replay every change since last_seq.
        if last_event_id and last_seq:
            async with database.connect() as db:
                for frame in await catchup_frames(db, last_seq=last_seq, tag=tag):
                    yield frame

        # Live subscription
        q = subscribe(tag)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_SECONDS)
                    if event is OVERFLOW:
                        # Too far behind to be caught up in-band. Close; the client
                        # reconnects with Last-Event-ID and replays from `changes`.
                        logger.info("SSE client fell behind (tag=%s) — closing for replay", tag)
                        break
                    # `seq` (relay #198, N-4) is monotonic by construction, so
                    # unlike the old post-id cursor it's always safe to send —
                    # no more "only a genuinely newer id" special-casing.
                    # `None` when the write had no changes-log row (history
                    # off): the frame still goes out, just without an `id:`.
                    seq = event.get("seq")
                    frame_id = {"id": str(seq)} if seq is not None else {}
                    if event.get("type") == "delete":
                        yield {"event": "delete", "data": json.dumps(event["data"]), **frame_id}
                    else:
                        post = PostResponse(**event["data"])
                        yield {"event": "post", "data": post.model_dump_json(), **frame_id}
                except TimeoutError:
                    yield {"event": "keepalive", "data": ""}
        finally:
            unsubscribe(q, tag)
            logger.debug("SSE client disconnected (tag=%s)", tag)

    return EventSourceResponse(generator())
