from __future__ import annotations

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from pydantic import AnyUrl

from relay.config import settings

RELAY_BASE_URL = settings.relay_base_url

INSTRUCTIONS = (
    "Relay is a personal content feed. AI agents publish posts; clients subscribe in "
    "real time. Before writing, read the master document with get_post(id=0) — it holds "
    "the index, tag taxonomy, naming conventions, and house rules. Keep one canonical "
    "post per topic and update it in place rather than creating duplicates."
)

MASTER_DOC_URI = "relay://master-document"

server = Server("relay", instructions=INSTRUCTIONS)


@server.list_resources()
async def list_resources() -> list[types.Resource]:
    return [
        types.Resource(
            uri=AnyUrl(MASTER_DOC_URI),
            name="Master Document",
            description=(
                "The relay master document (post id=0): index, tag taxonomy, naming "
                "conventions, and house rules. Read before publishing."
            ),
            mimeType="text/markdown",
        )
    ]


@server.read_resource()
async def read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
    if str(uri) != MASTER_DOC_URI:
        raise ValueError(f"Unknown resource: {uri}")
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{RELAY_BASE_URL}/posts/0",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        post = response.json()
    return [ReadResourceContents(content=post["content"], mime_type="text/markdown")]


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="publish_post",
            description="Publish a post to the relay feed. The dashboard will display it in real time.",
            inputSchema={
                "type": "object",
                "required": ["title", "content"],
                "properties": {
                    "title": {"type": "string", "description": "Title — becomes the Markdown filename"},
                    "content": {"type": "string", "description": "Post body (Markdown)"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags, e.g. ['news', 'ai']",
                    },
                    "source": {"type": "string", "description": "Optional source URL or label"},
                    "expires_at": {
                        "type": "string",
                        "description": "Optional ISO 8601 datetime after which the post expires, e.g. '2026-06-30T00:00:00Z'. Overrides tag/global TTL.",
                    },
                },
            },
        ),
        types.Tool(
            name="list_tags",
            description="List all tags in the relay feed with their post counts.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_posts",
            description="List posts from the relay feed, optionally filtered by tag.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Filter by tag"},
                    "limit": {"type": "integer", "description": "Max number of posts to return (default 20)"},
                    "offset": {"type": "integer", "description": "Pagination offset (default 0)"},
                },
            },
        ),
        types.Tool(
            name="get_post",
            description="Get a single post by its ID.",
            inputSchema={
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "integer", "description": "Post ID"},
                },
            },
        ),
        types.Tool(
            name="delete_post",
            description="Delete a post from the relay feed by its ID.",
            inputSchema={
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "integer", "description": "Post ID to delete"},
                },
            },
        ),
        types.Tool(
            name="update_post",
            description=(
                "Update an existing post in the relay feed. "
                "Only fields that are explicitly provided are changed; omitted fields are left untouched. "
                "Providing tags replaces the tag list wholesale; an empty array clears all tags."
            ),
            inputSchema={
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "integer", "description": "ID of the post to update"},
                    "title": {"type": "string", "description": "New title (renames the file)"},
                    "content": {"type": "string", "description": "New post body"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Replacement tag list (empty array clears all tags)",
                    },
                    "source": {"type": "string", "description": "Source URL or label"},
                    "expires_at": {
                        "type": "string",
                        "description": "ISO 8601 datetime after which the post expires. Pass null to clear an existing expiry.",
                    },
                },
            },
        ),
        types.Tool(
            name="add_attachment",
            description=(
                "Attach a file (image, PDF, …) to the vault. 'data' is the file's bytes, "
                "base64-encoded. With 'post_id', the file is filed under that post's folder and "
                "its ![[file]] embed is appended to the post body. Without it, the file goes to "
                "'folder' (or Inbox) and you place the returned ref in a post yourself."
            ),
            inputSchema={
                "type": "object",
                "required": ["filename", "data"],
                "properties": {
                    "filename": {"type": "string", "description": "Attachment filename, e.g. 'diagram.png'"},
                    "data": {"type": "string", "description": "Base64-encoded file bytes"},
                    "post_id": {"type": "integer", "description": "Post to attach to (appends ![[file]] to its body)"},
                    "folder": {"type": "string", "description": "First-level folder for a standalone attachment (default Inbox)"},
                },
            },
        ),
        types.Tool(
            name="list_attachments",
            description=(
                "List attachments stored in the vault (filename, folder, size, and the ![[…]] "
                "embed ref). Scope with 'post_id' (that post's folder) or 'folder'; omit both to "
                "list every attachment. Use the returned filename with get_attachment."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "post_id": {"type": "integer", "description": "List attachments in this post's folder"},
                    "folder": {"type": "string", "description": "List attachments in this first-level folder"},
                },
            },
        ),
        types.Tool(
            name="get_attachment",
            description=(
                "Retrieve an attachment from the vault by its filename (as used in ![[file]]). "
                "Images are returned so they can be viewed inline."
            ),
            inputSchema={
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "description": "Attachment filename, e.g. 'diagram.png'"},
                },
            },
        ),
        types.Tool(
            name="set_tag_config",
            description=(
                "Set expiry configuration for a tag. "
                "At least one of ttl_hours or expires_at must be provided. "
                "ttl_hours is relative to each post's creation time; "
                "expires_at is an absolute cutoff for all posts with this tag. "
                "Only applies to posts that don't have their own expires_at set."
            ),
            inputSchema={
                "type": "object",
                "required": ["tag"],
                "properties": {
                    "tag": {"type": "string", "description": "Tag name to configure"},
                    "ttl_hours": {
                        "type": "integer",
                        "description": "Hours after creation before posts with this tag expire",
                    },
                    "expires_at": {
                        "type": "string",
                        "description": "Absolute ISO 8601 datetime after which all posts with this tag expire",
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(
    name: str, arguments: dict
) -> list[types.TextContent | types.ImageContent]:
    if name == "add_attachment":
        payload = {k: v for k, v in arguments.items() if v is not None}
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{RELAY_BASE_URL}/attachments",
                json=payload,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=30,
            )
            if response.status_code == 404:
                return [types.TextContent(type="text", text=f"Post #{arguments.get('post_id')} not found.")]
            if response.status_code == 400:
                return [types.TextContent(type="text", text="Attachment 'data' is not valid base64.")]
            if response.status_code == 413:
                return [types.TextContent(type="text", text=response.json().get("detail", "Attachment too large."))]
            response.raise_for_status()
            a = response.json()
        where = f" appended to post #{a['post_id']}" if a.get("post_id") is not None else ""
        return [types.TextContent(
            type="text",
            text=f"Stored attachment '{a['filename']}' in {a['folder']}/assets{where}.\nEmbed: {a['ref']}",
        )]

    if name == "list_attachments":
        params = {k: v for k, v in arguments.items() if k in ("post_id", "folder") and v is not None}
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{RELAY_BASE_URL}/attachments",
                params=params,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            if response.status_code == 404:
                return [types.TextContent(type="text", text=f"Post #{arguments.get('post_id')} not found.")]
            response.raise_for_status()
            items = response.json()["items"]
        if not items:
            return [types.TextContent(type="text", text="No attachments found.")]
        lines = [f"{a['ref']}  —  {a['folder']}/assets ({a['bytes']} bytes)" for a in items]
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "get_attachment":
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{RELAY_BASE_URL}/attachments/{arguments['name']}",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=30,
            )
            if response.status_code == 404:
                return [types.TextContent(type="text", text=f"Attachment '{arguments['name']}' not found.")]
            response.raise_for_status()
            mime = response.headers.get("content-type", "application/octet-stream").split(";")[0]
            raw = response.content
        max_bytes = settings.attachment_max_mb * 1024 * 1024
        if len(raw) > max_bytes:
            return [types.TextContent(
                type="text",
                text=f"Attachment '{arguments['name']}' is {len(raw)} bytes — too large to return inline.",
            )]
        if mime.startswith("image/"):
            import base64 as _b64
            return [types.ImageContent(type="image", data=_b64.b64encode(raw).decode(), mimeType=mime)]
        return [types.TextContent(
            type="text",
            text=f"Retrieved '{arguments['name']}' ({mime}, {len(raw)} bytes) — not an image, can't show inline.",
        )]

    if name == "list_tags":
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{RELAY_BASE_URL}/tags",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            response.raise_for_status()
            tags = response.json()["tags"]

        if not tags:
            text = "No tags yet."
        else:
            text = "\n".join(f"{t['tag']} ({t['count']} posts)" for t in tags)
        return [types.TextContent(type="text", text=text)]

    if name == "list_posts":
        params = {k: v for k, v in arguments.items() if k in ("tag", "limit", "offset")}
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{RELAY_BASE_URL}/posts",
                params=params,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
        posts = data.get("items", [])
        if not posts:
            return [types.TextContent(type="text", text="No posts found.")]
        lines = []
        for p in posts:
            header = f"#{p['id']}"
            if p.get("title"):
                header += f" — {p['title']}"
            if p.get("tags"):
                header += f" [{p['tags']}]"
            lines.append(header)
            lines.append(p["content"])
            lines.append("")
        return [types.TextContent(type="text", text="\n".join(lines).strip())]

    if name == "get_post":
        post_id = arguments["id"]
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{RELAY_BASE_URL}/posts/{post_id}",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            response.raise_for_status()
            p = response.json()
        header = f"#{p['id']}"
        if p.get("title"):
            header += f" — {p['title']}"
        if p.get("tags"):
            header += f" [{p['tags']}]"
        return [types.TextContent(type="text", text=f"{header}\n\n{p['content']}")]

    if name == "delete_post":
        post_id = arguments["id"]
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{RELAY_BASE_URL}/posts/{post_id}",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            if response.status_code == 404:
                return [types.TextContent(type="text", text=f"Post #{post_id} not found.")]
            response.raise_for_status()
        return [types.TextContent(type="text", text=f"Deleted post #{post_id}.")]

    if name == "update_post":
        post_id = arguments["id"]
        payload = {k: v for k, v in arguments.items() if k != "id"}
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{RELAY_BASE_URL}/posts/{post_id}",
                json=payload,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            if response.status_code == 404:
                return [types.TextContent(type="text", text=f"Post #{post_id} not found.")]
            response.raise_for_status()
            p = response.json()
        header = f"#{p['id']}"
        if p.get("title"):
            header += f" — {p['title']}"
        if p.get("tags"):
            header += f" [{', '.join(p['tags'])}]"
        return [types.TextContent(type="text", text=f"Updated post {header}\n\n{p['content']}")]

    if name == "set_tag_config":
        tag = arguments["tag"]
        payload = {k: v for k, v in arguments.items() if k != "tag"}
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{RELAY_BASE_URL}/tags/{tag}/config",
                json=payload,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                timeout=10,
            )
            response.raise_for_status()
            cfg = response.json()
        parts = [f"Tag '{cfg['tag']}' configured:"]
        if cfg.get("ttl_hours"):
            parts.append(f"  ttl_hours = {cfg['ttl_hours']}")
        if cfg.get("expires_at"):
            parts.append(f"  expires_at = {cfg['expires_at']}")
        return [types.TextContent(type="text", text="\n".join(parts))]

    if name != "publish_post":
        raise ValueError(f"Unknown tool: {name}")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{RELAY_BASE_URL}/posts",
            json=arguments,
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        post = response.json()

    return [
        types.TextContent(
            type="text",
            text=f"Published post #{post['id']} — tags: {post['tags'] or 'none'}",
        )
    ]


def main() -> None:
    import asyncio

    async def _run() -> None:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
