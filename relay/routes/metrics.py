"""GET /metrics — Prometheus/OpenMetrics text exposition.

Gated behind the same ``require_api_key`` dependency as the rest of the API (a
scraper sends ``Authorization: Bearer <API_KEY>``). relay sits behind a public
reverse proxy, so an unauthenticated /metrics would leak vault size and activity
publicly; reusing the bearer gate keeps it private with no new config. On a
trusted-network deployment you could instead bind it to loopback/tailnet — but
the API-key gate is the safe default here.
"""
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends
from starlette.responses import PlainTextResponse

from .. import events, metrics
from ..auth import require_api_key
from ..database import get_db

router = APIRouter(tags=["metrics"])

# Prometheus text format 0.0.4 content type (what scrapers expect).
_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics", dependencies=[Depends(require_api_key)], include_in_schema=False)
async def metrics_endpoint(db: aiosqlite.Connection = Depends(get_db)) -> PlainTextResponse:
    async with db.execute("SELECT COUNT(*) FROM posts") as cur:
        posts_total = (await cur.fetchone())[0]
    # Distinct tags across all posts (sentinel-comma encoded), counted the same
    # way service.list_tags splits them — cheap enough at this vault's scale.
    async with db.execute("SELECT tags FROM posts WHERE tags != ''") as cur:
        distinct: set[str] = set()
        for row in await cur.fetchall():
            distinct.update(t for t in row[0].split(",") if t)
    tags_total = len(distinct)

    families = [
        metrics.build_info_family(),
        metrics.gauge("relay_posts_total", "Posts currently in the vault index.", posts_total),
        metrics.gauge("relay_tags_total", "Distinct tags in use across all posts.", tags_total),
        metrics.gauge("relay_sse_clients", "Currently connected SSE subscribers.", events.subscriber_count()),
        metrics.http_requests.family(),
        metrics.mcp_tool_calls.family(),
        metrics.search_queries.family(),
        metrics.cleanup_deletions.family(),
        metrics.upload_slots_purged.family(),
    ]
    return PlainTextResponse(metrics.render(families), media_type=_CONTENT_TYPE)
