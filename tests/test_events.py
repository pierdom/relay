"""Unit tests for relay/events.py — the in-process SSE fan-out.

The fan-out has subtle dedup logic: a post tagged [a, b] must reach a
tag-'a' subscriber, a tag-'b' subscriber, and a global subscriber each
exactly once — not twice. These pin that, plus the basic subscribe/publish
shape, without spinning up the full ASGI stack.
"""
from __future__ import annotations

import asyncio

import pytest

from relay import events


@pytest.fixture(autouse=True)
def _clean_subscribers():
    """Ensure module-level subscriber state doesn't bleed between tests."""
    events._subscribers.clear()
    yield
    events._subscribers.clear()


# ── subscribe / unsubscribe ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscribe_returns_a_queue():
    q = events.subscribe(None)
    assert isinstance(q, asyncio.Queue)
    events.unsubscribe(q, None)


@pytest.mark.asyncio
async def test_subscriber_count_tracks_adds_and_removes():
    assert events.subscriber_count() == 0
    q1 = events.subscribe(None)
    q2 = events.subscribe("homelab")
    assert events.subscriber_count() == 2
    events.unsubscribe(q1, None)
    assert events.subscriber_count() == 1
    events.unsubscribe(q2, "homelab")
    assert events.subscriber_count() == 0


# ── tag-filtered fan-out ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tag_subscriber_receives_matching_post():
    q = events.subscribe("homelab")
    await events.publish({"id": 1, "tags": ["homelab", "dev"]})
    assert not q.empty()
    event = q.get_nowait()
    assert event["id"] == 1
    assert event["type"] == "post"
    events.unsubscribe(q, "homelab")


@pytest.mark.asyncio
async def test_tag_subscriber_does_not_receive_non_matching_post():
    q = events.subscribe("finance")
    await events.publish({"id": 1, "tags": ["homelab"]})
    assert q.empty()
    events.unsubscribe(q, "finance")


@pytest.mark.asyncio
async def test_global_subscriber_receives_any_post():
    q = events.subscribe(None)
    await events.publish({"id": 2, "tags": ["dev"]})
    assert not q.empty()
    events.unsubscribe(q, None)


# ── dedup: one delivery per subscriber per event ─────────────────────────────


@pytest.mark.asyncio
async def test_multi_tag_post_reaches_global_subscriber_exactly_once():
    """A post with two tags must not be double-delivered to a global subscriber."""
    q = events.subscribe(None)
    await events.publish({"id": 3, "tags": ["homelab", "dev"]})
    # Exactly one item in the queue.
    assert q.qsize() == 1
    events.unsubscribe(q, None)


@pytest.mark.asyncio
async def test_multi_tag_post_reaches_each_tag_subscriber_once():
    """Two subscribers on different tags both get it; neither gets it twice."""
    qa = events.subscribe("homelab")
    qb = events.subscribe("dev")
    await events.publish({"id": 4, "tags": ["homelab", "dev"]})
    assert qa.qsize() == 1
    assert qb.qsize() == 1
    events.unsubscribe(qa, "homelab")
    events.unsubscribe(qb, "dev")


@pytest.mark.asyncio
async def test_subscriber_on_both_tag_and_global_gets_event_once():
    """If a queue is somehow registered for both a specific tag and global
    (unlikely in practice), the dedup set prevents double delivery."""
    q = asyncio.Queue()
    events._subscribers["homelab"].add(q)
    events._subscribers[None].add(q)
    await events.publish({"id": 5, "tags": ["homelab"]})
    assert q.qsize() == 1
    events._subscribers["homelab"].discard(q)
    events._subscribers[None].discard(q)


# ── publish_delete ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_delete_sends_delete_type_event():
    q = events.subscribe(None)
    await events.publish_delete(99, ["homelab"])
    event = q.get_nowait()
    assert event["type"] == "delete"
    assert event["id"] == 99
    assert event["data"] == {"id": 99}
    events.unsubscribe(q, None)


@pytest.mark.asyncio
async def test_publish_delete_reaches_tag_filtered_subscriber():
    q = events.subscribe("news")
    other = events.subscribe("homelab")
    await events.publish_delete(10, ["news"])
    assert not q.empty()
    assert other.empty()
    events.unsubscribe(q, "news")
    events.unsubscribe(other, "homelab")
