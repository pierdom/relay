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


async def publish(event: dict) -> None:
    tags: list[str] = event.get("tags", [])
    notified: set[int] = set()

    for tag in tags:
        for q in list(_subscribers.get(tag, set())):
            qid = id(q)
            if qid not in notified:
                await q.put(event)
                notified.add(qid)

    # Global subscribers (no tag filter)
    for q in list(_subscribers.get(None, set())):
        qid = id(q)
        if qid not in notified:
            await q.put(event)
            notified.add(qid)
