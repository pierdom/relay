"""In-process MCP server exposed over Streamable HTTP at ``/mcp``.

Unlike the stdio proxy in ``relay_mcp/server.py`` (which runs on the client
machine and talks to a relay over REST), this server runs *inside* the relay
process and calls the shared ``relay.service`` layer directly — no network
hop, no schema duplication. Any MCP client that supports the Streamable HTTP
transport can connect remotely with the relay's bearer key.
"""
from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

import aiosqlite
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from . import service
from .config import settings
from .models import FormatEnum, PostCreate, PostUpdate, TagConfigCreate

INSTRUCTIONS = (
    "Relay is a personal content feed. AI agents publish posts; clients subscribe in "
    "real time. Before writing, read the master document with get_post(id=0) — it holds "
    "the index, tag taxonomy, naming conventions, and house rules. Keep one canonical "
    "post per topic and update it in place rather than creating duplicates."
)

mcp = FastMCP(
    "relay",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    streamable_http_path="/mcp",
)


@asynccontextmanager
async def _db():
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=5000;")
        yield db


@mcp.tool(description="Publish a post to the relay feed. Subscribers receive it in real time.")
async def publish_post(
    content: str,
    title: str | None = None,
    tags: list[str] | None = None,
    format: FormatEnum = "markdown",
    source: str | None = None,
    expires_at: str | None = None,
) -> dict:
    """`expires_at`: optional ISO 8601 datetime; overrides tag/global TTL."""
    body = PostCreate(
        content=content,
        title=title,
        tags=tags or [],
        format=format,
        source=source,
        expires_at=expires_at,
    )
    async with _db() as db:
        post = await service.create_post(db, body)
    return post.model_dump()


@mcp.tool(description="List posts from the relay feed, optionally filtered by tag or search term.")
async def list_posts(
    tag: str | None = None,
    search: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    async with _db() as db:
        result = await service.list_posts(db, tag=tag, search=search, limit=limit, offset=offset)
    return result.model_dump()


@mcp.tool(description="Get a single post by its ID. Use id=0 for the master document.")
async def get_post(id: int) -> dict:
    async with _db() as db:
        post = await service.get_post(db, id)
    if post is None:
        return {"error": f"Post #{id} not found."}
    return post.model_dump()


@mcp.tool(
    description=(
        "Update an existing post. Only provided fields change; omitted fields are left "
        "untouched. Providing tags replaces the list wholesale; an empty array clears them. "
        "Pass expires_at=null to clear an existing expiry."
    )
)
async def update_post(
    id: int,
    title: str | None = None,
    content: str | None = None,
    tags: list[str] | None = None,
    format: FormatEnum | None = None,
    source: str | None = None,
    expires_at: str | None = None,
) -> dict:
    fields = {
        "title": title,
        "content": content,
        "tags": tags,
        "format": format,
        "source": source,
        "expires_at": expires_at,
    }
    body = PostUpdate(**{k: v for k, v in fields.items() if v is not None})
    async with _db() as db:
        try:
            post = await service.update_post(db, id, body)
        except service.PostNotFound:
            return {"error": f"Post #{id} not found."}
    return post.model_dump()


@mcp.tool(description="Delete a post from the relay feed by its ID. The master document (id=0) cannot be deleted.")
async def delete_post(id: int) -> dict:
    async with _db() as db:
        try:
            await service.delete_post(db, id)
        except service.ProtectedPost:
            return {"error": "Master document (id=0) cannot be deleted."}
        except service.PostNotFound:
            return {"error": f"Post #{id} not found."}
    return {"ok": True, "deleted": id}


@mcp.tool(description="List all tags in the relay feed with their post counts.")
async def list_tags() -> dict:
    async with _db() as db:
        result = await service.list_tags(db)
    return result.model_dump()


@mcp.tool(
    description=(
        "Set expiry configuration for a tag. Provide ttl_hours (relative to each post's "
        "creation), expires_at (absolute cutoff), or both. Only affects posts without their "
        "own expires_at."
    )
)
async def set_tag_config(
    tag: str,
    ttl_hours: int | None = None,
    expires_at: str | None = None,
) -> dict:
    body = TagConfigCreate(ttl_hours=ttl_hours, expires_at=expires_at)
    async with _db() as db:
        result = await service.set_tag_config(db, tag, body)
    return result.model_dump()


class BearerAuthASGI:
    """Minimal ASGI wrapper that gates the MCP app behind the static bearer key."""

    def __init__(self, app, api_key: str) -> None:
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not (token and hmac.compare_digest(token, self.api_key)):
            await JSONResponse({"detail": "Invalid API key"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def mcp_asgi_app():
    """Return the Streamable HTTP MCP app, bearer-protected, to mount on FastAPI."""
    return BearerAuthASGI(mcp.streamable_http_app(), settings.api_key)
