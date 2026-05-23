from __future__ import annotations

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

from relay.config import settings

RELAY_BASE_URL = settings.relay_base_url

server = Server("relay")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="publish_post",
            description="Publish a post to the relay feed. The dashboard will display it in real time.",
            inputSchema={
                "type": "object",
                "required": ["content"],
                "properties": {
                    "content": {"type": "string", "description": "Post body (markdown by default)"},
                    "title": {"type": "string", "description": "Optional title"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags, e.g. ['news', 'ai']",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "text", "html", "json"],
                        "description": "Content format (default: markdown)",
                    },
                    "source": {"type": "string", "description": "Optional source URL or label"},
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
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
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
        posts = data.get("posts", [])
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
