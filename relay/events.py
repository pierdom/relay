from __future__ import annotations

import asyncio
from collections import defaultdict

# tag -> set of subscriber queues; None = subscribe to all tags
_subscribers: dict[str | None, set[asyncio.Queue]] = defaultdict(set)


def subscribe(tag: str | None) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers[tag].add(q)
    return q


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
                await q.put(envelope)
                notified.add(id(q))

    # Global subscribers (no tag filter)
    for q in list(_subscribers.get(None, set())):
        if id(q) not in notified:
            await q.put(envelope)
            notified.add(id(q))


async def publish(post: dict) -> None:
    """Broadcast a new-or-edited post to subscribers."""
    await _broadcast({"type": "post", "tags": post.get("tags", []), "id": post["id"], "data": post})


async def publish_delete(post_id: int, tags: list[str]) -> None:
    """Broadcast a deletion so live clients can drop the post."""
    await _broadcast({"type": "delete", "tags": tags, "id": post_id, "data": {"id": post_id}})
