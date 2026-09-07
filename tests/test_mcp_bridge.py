"""The stdio bridge (`relay_mcp/server.py`) forwards to the in-process MCP server.

It used to be a second, hand-written copy of every tool schema, kept honest by an
AST parity test. Now the tools *are* the server's, fetched over Streamable HTTP,
so these tests drive the bridge end to end against the ASGI app: same tool list
(plus the one documented proxy-only `path` parameter), same results, server
errors passed through.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("API_KEY", "test-key")

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import mcp.types as types
import pytest

import relay_mcp.server as bridge
from relay.mcp_server import mcp


async def _tools() -> dict:
    result = await bridge.list_tools(None, types.PaginatedRequestParams())
    return {t.name: t for t in result.tools}


async def _call(name: str, arguments: dict):
    return await bridge.call_tool(None, types.CallToolRequestParams(name=name, arguments=arguments))

API_KEY = "test-key"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def relay_server(tmp_path_factory) -> str:
    """A real uvicorn on a free port: the bridge speaks Streamable HTTP, which
    needs the session manager the app lifespan runs — an ASGI transport has no
    lifespan, and pytest-asyncio's fixture tasks trip anyio's cancel scopes."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "API_KEY": API_KEY, "RELAY_VAULT_PATH": str(tmp_path_factory.mktemp("bridge-vault")),
           "RELAY_HISTORY_ENABLED": "false", "RELAY_WATCH_ENABLED": "false"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "relay.main:app", "--port", str(port), "--log-level", "warning"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"relay exited early (rc={proc.returncode})")
            try:
                with urllib.request.urlopen(f"{base_url}/health", timeout=1) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(0.15)
        else:
            raise RuntimeError("relay did not become healthy in time")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def bridged(relay_server, monkeypatch):
    monkeypatch.setattr(bridge, "RELAY_BASE_URL", relay_server)
    monkeypatch.setattr(bridge, "_AUTH", {"Authorization": f"Bearer {API_KEY}"})


@pytest.mark.asyncio
async def test_bridge_exposes_the_servers_tools_plus_path_on_add_attachment(bridged):
    remote = {t.name: t for t in await mcp.list_tools()}
    local = await _tools()
    assert set(local) == set(remote)
    for name, tool in local.items():
        if name == "add_attachment":
            continue
        assert tool.description == remote[name].description, name
        assert tool.input_schema == remote[name].input_schema, name
    local_props = set(local["add_attachment"].input_schema["properties"])
    assert local_props - set(remote["add_attachment"].input_schema["properties"]) == {"path"}
    assert "path" not in remote["add_attachment"].input_schema["properties"]


def _payload(result) -> dict:
    """The in-process tools return plain dicts, which the SDK serialises as JSON text."""
    assert not result.is_error
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_bridge_forwards_calls_and_returns_the_servers_result(bridged):
    assert _payload(await _call("get_post", {"id": 0}))["id"] == 0
    published = await _call("publish_post", {"title": "Via bridge", "content": "hi", "tags": ["homelab"]})
    assert _payload(published)["title"] == "Via bridge"
    listed = _payload(await _call("list_posts", {"tag": "homelab"}))
    assert [p["title"] for p in listed["items"]] == ["Via bridge"]


@pytest.mark.asyncio
async def test_bridge_passes_server_errors_through(bridged):
    assert _payload(await _call("get_post", {"id": 999})) == {"error": "Post #999 not found."}


@pytest.mark.asyncio
async def test_bridge_serves_the_master_document_resource(bridged):
    result = await bridge.list_resources(None, types.PaginatedRequestParams())
    assert [str(r.uri) for r in result.resources] == ["relay://master-document"]
    read = await bridge.read_resource(None, types.ReadResourceRequestParams(uri="relay://master-document"))
    assert read.contents[0].mime_type == "text/markdown"
    assert "Master Document" in read.contents[0].text


@pytest.mark.asyncio
async def test_bridge_refuses_two_byte_sources(bridged):
    out = await _call("add_attachment", {"path": "/x", "data": "aGk="})
    assert "exactly one" in out.content[0].text


def test_bridge_declares_no_tool_schemas_of_its_own():
    """The whole point: nothing here can drift from the server."""
    src = Path(bridge.__file__).read_text(encoding="utf-8")
    assert "types.Tool(" not in src
    assert "input_schema={" not in src
    assert "inputSchema={" not in src
