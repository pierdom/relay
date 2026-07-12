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
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import service, vault
from .config import settings
from .models import PostCreate, PostUpdate, TagConfigCreate

INSTRUCTIONS = (
    "Relay is a personal content feed. AI agents publish posts; clients subscribe in "
    "real time. Before writing, read the master document with get_post(id=0) — it holds "
    "the index, tag taxonomy, naming conventions, and house rules. Keep one canonical "
    "post per topic and update it in place rather than creating duplicates."
)


def _auth_kwargs() -> dict:
    """When MCP OAuth is enabled (and the upstream OIDC client is configured),
    turn the MCP server into an OAuth 2.1 Authorization + Resource Server: the SDK
    mounts /authorize /token /register /revoke + metadata and wraps /mcp in
    RequireAuthMiddleware, using our provider (which also honors the static key).
    Off => today's static-bearer BearerAuthASGI, unchanged."""
    if not settings.mcp_oauth_active:
        return {}
    from .mcp_oauth.provider import get_provider

    scopes = list(settings.mcp_scopes)
    return {
        "auth": AuthSettings(
            issuer_url=settings.relay_base_url,
            resource_server_url=settings.mcp_resource_url,
            required_scopes=scopes,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=scopes,
                default_scopes=scopes,
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        "auth_server_provider": get_provider(),
    }


def _brand_icons() -> list[Icon]:
    """Advertise relay's logo in the initialize serverInfo (MCP SEP-973).

    Clients that read `serverInfo.icons` (spec 2025-11-25) show these instead of
    the generic globe. `/assets` is public (no auth), so the src URLs resolve for
    an unauthenticated fetch. Claude's remote connectors don't render this yet
    (anthropics/claude-ai-mcp#152) but other clients already do, and it's the
    spec-correct place for it — so it lights up automatically when Claude ships.
    """
    base = settings.relay_base_url.rstrip("/")
    return [
        Icon(src=f"{base}/assets/relay-mark.svg", mimeType="image/svg+xml"),
        Icon(src=f"{base}/assets/relay-mark-512.png", mimeType="image/png", sizes=["512x512"]),
    ]


mcp = FastMCP(
    "relay",
    instructions=INSTRUCTIONS,
    website_url=settings.relay_base_url.rstrip("/"),
    icons=_brand_icons(),
    stateless_http=True,
    streamable_http_path="/mcp",
    # We mount into FastAPI behind a public reverse proxy, not FastMCP's own
    # uvicorn. FastMCP's default host (127.0.0.1) otherwise auto-enables DNS-
    # rebinding protection scoped to localhost, which 421s every real Host header
    # (e.g. relay.geon.im) and 403s a browser Origin — so remote /mcp never worked
    # over the network. DNS rebinding is a localhost-dev threat; our actual
    # controls are bearer/OAuth auth + HTTPS + the proxy, so disable that check
    # (this matches the SDK's own default for a non-localhost host).
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    **_auth_kwargs(),
)


@mcp.custom_route("/mcp/oauth/callback", methods=["GET"], include_in_schema=False)
async def mcp_oauth_callback(request: Request) -> Response:
    """Return leg of the upstream PocketID login (unauthenticated by design)."""
    from .mcp_oauth.broker import handle_callback

    return await handle_callback(request)


@asynccontextmanager
async def _db():
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=5000;")
        yield db


@mcp.resource(
    "relay://master-document",
    name="Master Document",
    description=(
        "The relay master document (post id=0): index, tag taxonomy, naming "
        "conventions, and house rules. Read before publishing."
    ),
    mime_type="text/markdown",
)
async def master_document() -> str:
    async with _db() as db:
        post = await service.get_post(db, 0)
    return post.content if post is not None else "Master document not found."


@mcp.tool(description="Publish a post to the relay feed. Subscribers receive it in real time.")
async def publish_post(
    title: str,
    content: str,
    tags: list[str] | None = None,
    source: str | None = None,
    expires_at: str | None = None,
) -> dict:
    """`title` becomes the Markdown filename. `expires_at`: optional ISO 8601 datetime; overrides tag/global TTL."""
    body = PostCreate(
        content=content,
        title=title,
        tags=tags or [],
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
    source: str | None = None,
    expires_at: str | None = None,
) -> dict:
    fields = {
        "title": title,
        "content": content,
        "tags": tags,
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


@mcp.tool(
    description=(
        "Attach a file (image, PDF, …) to the vault. `data` is the file's bytes, "
        "base64-encoded. With `post_id`, the file is filed under that post's folder and "
        "its ![[file]] embed is appended to the post body. Without it, the file goes to "
        "`folder` (or Inbox) and you place the returned `ref` in a post yourself."
    )
)
async def add_attachment(
    filename: str,
    data: str,
    post_id: int | None = None,
    folder: str | None = None,
) -> dict:
    """Returns {filename, ref, folder, post_id}. `ref` is the ![[…]] embed to drop into a post."""
    try:
        raw = service.decode_attachment_b64(data)
    except ValueError as exc:
        return {"error": str(exc)}
    async with _db() as db:
        try:
            result = await service.add_attachment(
                db, filename=filename, data=raw, post_id=post_id, folder=folder
            )
        except service.PostNotFound:
            return {"error": f"Post #{post_id} not found."}
        except service.AttachmentError as exc:
            return {"error": str(exc)}
    return result.model_dump()


@mcp.tool(
    description=(
        "Retrieve an attachment from the vault by its filename (as used in ![[file]]). "
        "Images are returned so they can be viewed inline; other files return a note with "
        "the vault path."
    )
)
async def get_attachment(name: str):
    """Returns image content for images, else a dict describing the file."""
    try:
        result = vault.read_attachment(name, max_bytes=settings.attachment_max_bytes)
    except ValueError:
        return {"error": f"Attachment '{name}' is too large to return inline "
                         f"(over {settings.attachment_max_mb} MB)."}
    if result is None:
        return {"error": f"Attachment '{name}' not found."}
    path, raw, mime = result
    if mime.startswith("image/"):
        # Derive format from the mime (image/jpeg → 'jpeg') so Image doesn't emit
        # a non-standard type like image/jpg from the '.jpg' suffix.
        return Image(data=raw, format=mime.split("/", 1)[1])
    return {"filename": path.name, "mime": mime, "bytes": len(raw),
            "note": "Non-image attachment; not shown inline."}


@mcp.tool(
    description=(
        "Delete an attachment from the vault by its filename. Returns the removed name and "
        "any post ids that still embed/link it (now dangling) so you can fix them."
    )
)
async def delete_attachment(name: str) -> dict:
    async with _db() as db:
        result = await service.delete_attachment(db, name)
    if result is None:
        return {"error": f"Attachment '{name}' not found."}
    return result.model_dump()


@mcp.tool(
    description=(
        "List attachments stored in the vault (filename, folder, size, and the ![[…]] "
        "embed ref). Scope with `post_id` (that post's folder) or `folder`; omit both to "
        "list every attachment. Use the returned filename with get_attachment."
    )
)
async def list_attachments(post_id: int | None = None, folder: str | None = None) -> dict:
    async with _db() as db:
        try:
            result = await service.list_attachments(db, post_id=post_id, folder=folder)
        except service.PostNotFound:
            return {"error": f"Post #{post_id} not found."}
    return result.model_dump()


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
    """Return the Streamable HTTP MCP app to mount on FastAPI.

    With OAuth enabled the SDK already wraps /mcp in RequireAuthMiddleware (and our
    verifier still accepts the static key), so we mount it bare. Otherwise we keep
    the minimal static-bearer gate.
    """
    app = mcp.streamable_http_app()
    if settings.mcp_oauth_active:
        return app
    return BearerAuthASGI(app, settings.api_key)
