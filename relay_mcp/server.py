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
