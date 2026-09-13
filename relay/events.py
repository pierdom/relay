from __future__ import annotations

import asyncio
from collections import defaultdict

# tag -> set of subscriber queues; None = subscribe to all tags
_subscribers: dict[str | None, set[asyncio.Queue]] = defaultdict(set)

# A subscriber that stops reading (a phone tab asleep) must not grow a queue
# without bound (AUDIT.md S-06). Past this many undelivered events the oldest is
# dropped and an OVERFLOW marker queued; the SSE generator ends the stream on it
# and the client reconnects with Last-Event-ID, replaying what it missed.
QUEUE_MAXSIZE = 256
OVERFLOW = {"type": "overflow"}


def subscribe(tag: str | None) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    _subscribers[tag].add(q)
    return q


def _offer(q: asyncio.Queue, envelope: dict) -> None:
    try:
        q.put_nowait(envelope)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        q.put_nowait(OVERFLOW)


def unsubscribe(q: asyncio.Queue, tag: str | None) -> None:
    _subscribers[tag].discard(q)


def subscriber_count() -> int:
    """Number of currently connected SSE subscribers (across all tag filters)."""
    return sum(len(queues) for queues in _subscribers.values())


async def _broadcast(envelope: dict) -> None:
    """Fan an event envelope out to tag-matched and global subscribers.

    Envelope shape: ``{"type": "post"|"delete", "tags": [...], "id": int, "data": {...}}``.
    """
    tags: list[str] = envelope.get("tags", [])
    notified: set[int] = set()

    for tag in tags:
        for q in list(_subscribers.get(tag, set())):
            if id(q) not in notified:
                _offer(q, envelope)
                notified.add(id(q))

    # Global subscribers (no tag filter)
    for q in list(_subscribers.get(None, set())):
        if id(q) not in notified:
            _offer(q, envelope)
            notified.add(id(q))


async def publish(post: dict, *, seq: int | None = None) -> None:
    """Broadcast a new-or-edited post to subscribers.

    ``seq`` (relay #198, N-4) is the row `changes.record_latest` just
    assigned this write — the SSE frame's `id:` field, so every event
    (not just a brand-new post id) carries a cursor that only ever moves
    forward. ``None`` when the caller has no changes-log row for this
    write (history disabled, or the call predates a `changes` wiring) —
    the frame is then sent with no `id:`, same as today.
    """
    await _broadcast({"type": "post", "tags": post.get("tags", []), "id": post["id"], "data": post, "seq": seq})


async def publish_delete(post_id: int, tags: list[str], *, seq: int | None = None) -> None:
    """Broadcast a deletion so live clients can drop the post. See ``publish``
    for ``seq``."""
    await _broadcast({"type": "delete", "tags": tags, "id": post_id, "data": {"id": post_id}, "seq": seq})
