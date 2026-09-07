"""stdio ↔ Streamable HTTP bridge for MCP clients that can't speak remote MCP.

Runs on the client machine (``uv run relay-mcp``), speaks stdio to the client
and forwards every MCP request to the relay's own in-process server at
``<RELAY_BASE_URL>/mcp`` with the bearer key. Tools, parameters, descriptions
and results are the server's — nothing is re-declared here, so the two
surfaces cannot drift (the 900-line hand-copied tool manifest this replaced
needed a CI parity test to stay honest).

The one local addition: ``add_attachment(path=…)`` reads a file on *this*
machine and uploads it through the presigned-slot REST flow, so bytes never pass
through the model. The in-process server must never gain ``path`` — that would
be an arbitrary file read on the relay host.

Connections are per request. relay's ``/mcp`` is stateless, so opening a fresh
Streamable HTTP session for each call costs one extra round trip and buys a
bridge that survives a relay restart without the client noticing.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import httpx2
import mcp.server.stdio
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server

from relay import __version__
from relay.config import settings

RELAY_BASE_URL = settings.relay_base_url
_AUTH = {"Authorization": f"Bearer {settings.api_key}"}

# Kept for clients that resolve it before the first remote call; the live
# instructions come from the server's initialize result.
INSTRUCTIONS = (
    "Relay is a personal knowledge base kept as a plain-Markdown vault; posts are files "
    "a human also edits directly in Obsidian, so write them to be read by a person. "
    "Clients subscribe to changes in real time. Before writing, read the master document "
    "with get_post(id=0) — it holds the index, tag taxonomy, naming conventions, and "
    "house rules. Keep one canonical post per topic and update it in place rather than "
    "creating duplicates."
)

_PATH_PARAM = {
    "type": "string",
    "description": (
        "Path to a file on the machine running this proxy; the proxy reads and uploads it "
        "(streamed, no base64). Provide exactly one of path, data, source_url, upload_id."
    ),
}


@asynccontextmanager
async def _remote() -> AsyncIterator[ClientSession]:
    """One Streamable HTTP session against the relay's own /mcp.

    mcp 2.x takes the transport's HTTP client rather than headers and a factory,
    and that client must be httpx2 — the SDK's own dependency. The local upload
    below still uses httpx, which is what the rest of relay speaks; the two
    coexist deliberately rather than migrating unrelated modules here.
    """
    async with httpx2.AsyncClient(
        headers=_AUTH, timeout=httpx2.Timeout(30.0, read=300.0), follow_redirects=True
    ) as http_client:
        async with streamable_http_client(f"{RELAY_BASE_URL}/mcp", http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def _upload_local_path(arguments: dict) -> list[types.TextContent]:
    """Handle add_attachment(path=…): read a file on the machine running this proxy
    and upload it to relay via a presigned slot (create → PUT bytes → finalize), so
    no base64 blob ever passes through the model. Proxy-only — the remote HTTP MCP
    server can't (and mustn't) read the client's filesystem."""
    p = Path(arguments["path"]).expanduser()
    if not p.is_file():
        return [types.TextContent(type="text", text=f"Local file not found: {arguments['path']}")]
    filename = arguments.get("filename") or p.name
    async with httpx.AsyncClient() as client:
        slot_resp = await client.post(f"{RELAY_BASE_URL}/attachments/uploads", headers=_AUTH, timeout=10)
        slot_resp.raise_for_status()
        slot = slot_resp.json()
        size = p.stat().st_size
        if size > slot["max_bytes"]:
            return [types.TextContent(
                type="text",
                text=f"'{p.name}' is {size} bytes — over the server's {slot['max_bytes']}-byte limit.",
            )]
        put = await client.put(
            f"{RELAY_BASE_URL}/attachments/uploads/{slot['upload_id']}",
            content=p.read_bytes(), headers=_AUTH, timeout=120,
        )
        if put.status_code == 413:
            return [types.TextContent(type="text", text=put.json().get("detail", "Attachment too large."))]
        put.raise_for_status()
        final = {"upload_id": slot["upload_id"], "filename": filename}
        for k in ("post_id", "folder"):
            if arguments.get(k) is not None:
                final[k] = arguments[k]
        response = await client.post(f"{RELAY_BASE_URL}/attachments", json=final, headers=_AUTH, timeout=30)
        if response.status_code == 404:
            return [types.TextContent(type="text", text=f"Post #{arguments.get('post_id')} not found.")]
        if response.status_code in (400, 413):
            return [types.TextContent(type="text", text=response.json().get("detail", "Attachment error."))]
        response.raise_for_status()
        a = response.json()
    where = f" appended to post #{a['post_id']}" if a.get("post_id") is not None else ""
    return [types.TextContent(
        type="text",
        text=f"Uploaded '{a['filename']}' ({size} bytes) to {a['folder']}/assets{where}.\nEmbed: {a['ref']}",
    )]


def _with_local_path(tool: types.Tool) -> types.Tool:
    """The server's ``add_attachment`` plus the proxy-only ``path`` parameter.

    mcp 2.x renamed the wire's camelCase fields to snake_case attributes with
    camelCase aliases, so ``model_copy`` has to update ``input_schema``; the
    old ``inputSchema`` key would be dropped silently as an unknown field."""
    schema = dict(tool.input_schema)
    schema["properties"] = {**schema.get("properties", {}), "path": _PATH_PARAM}
    description = (tool.description or "") + (
        " From this proxy you may instead pass `path` — a file on the machine running it — "
        "and the proxy uploads it for you (streamed, no base64)."
    )
    return tool.model_copy(update={"input_schema": schema, "description": description})


# mcp 2.x registers handlers through the constructor instead of decorators, and
# they take (ctx, params) and return the result object itself. That suits a
# bridge: the remote's result *is* the result, so most of these hand it straight
# back rather than unpacking and rebuilding it.


async def list_resources(ctx, params: types.PaginatedRequestParams) -> types.ListResourcesResult:
    async with _remote() as remote:
        return await remote.list_resources()


async def read_resource(ctx, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
    async with _remote() as remote:
        return await remote.read_resource(params.uri)


async def list_tools(ctx, params: types.PaginatedRequestParams) -> types.ListToolsResult:
    async with _remote() as remote:
        result = await remote.list_tools()
    return result.model_copy(
        update={"tools": [_with_local_path(t) if t.name == "add_attachment" else t for t in result.tools]}
    )


async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    arguments = params.arguments or {}
    if params.name == "add_attachment":
        if sum(bool(arguments.get(k)) for k in ("path", "data", "source_url", "upload_id")) > 1:
            return _text_result("Provide exactly one of: path, data, source_url, upload_id.")
        if arguments.get("path"):
            return types.CallToolResult(content=await _upload_local_path(arguments))
    async with _remote() as remote:
        # Returned verbatim: content, structured_content and is_error are the server's.
        return await remote.call_tool(params.name, arguments)


def _text_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=True)


server = Server(
    "relay",
    # Same reasoning as the in-process server: serverInfo.version is what the
    # client shows next to the name, and relay's version is the useful answer.
    version=__version__,
    instructions=INSTRUCTIONS,
    on_list_resources=list_resources,
    on_read_resource=read_resource,
    on_list_tools=list_tools,
    on_call_tool=call_tool,
)


def main() -> None:
    async def _run() -> None:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
