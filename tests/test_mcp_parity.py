"""The stdio proxy and the in-process MCP server must expose the same tools.

CLAUDE.md states the rule ("tool names, parameters and descriptions must match
exactly across both files") and a `PostToolUse` hook nudges whoever edits either
file — but nothing enforced it, and the descriptions had drifted apart in nine of
the twelve tools before this test existed. A reminder aimed at an agent is not a
gate; CI is.

Parsed with `ast` rather than imported, so the check never needs a running server
or the mcp package's import side effects.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
HTTP_SERVER = _ROOT / "relay" / "mcp_server.py"
STDIO_SERVER = _ROOT / "relay_mcp" / "server.py"

# The one intentional divergence, documented in CLAUDE.md: only the stdio proxy
# runs on the client machine, so only it can read a local file and stream it to
# relay. The in-process server must never gain `path` — that would be an
# arbitrary file read on the relay host. Its description necessarily differs too,
# since it has to document the extra parameter.
PROXY_ONLY_PARAMS = {"add_attachment": {"path"}}
DESCRIPTION_EXEMPT = {"add_attachment"}


def _http_tools() -> dict[str, tuple[set[str], str]]:
    """{name: (param names, description)} for @mcp.tool functions."""
    tree = ast.parse(HTTP_SERVER.read_text(encoding="utf-8"))
    tools: dict[str, tuple[set[str], str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and "mcp.tool" in ast.unparse(dec.func)):
                continue
            description = ""
            for kw in dec.keywords:
                if kw.arg == "description":
                    description = ast.literal_eval(kw.value)
            params = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
            tools[node.name] = (params, description)
    return tools


def _stdio_tools() -> dict[str, tuple[set[str], str]]:
    """{name: (schema property names, description)} for types.Tool(...) literals."""
    tree = ast.parse(STDIO_SERVER.read_text(encoding="utf-8"))
    tools: dict[str, tuple[set[str], str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = getattr(node.func, "attr", getattr(node.func, "id", None))
        if func != "Tool":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        name = ast.literal_eval(kwargs["name"])
        schema = ast.literal_eval(kwargs["inputSchema"])
        tools[name] = (set(schema.get("properties", {})), ast.literal_eval(kwargs["description"]))
    return tools


@pytest.fixture(scope="module")
def surfaces() -> tuple[dict, dict]:
    return _http_tools(), _stdio_tools()


def test_both_servers_expose_the_same_tool_names(surfaces):
    http, stdio = surfaces
    assert http, "no @mcp.tool functions found — the parser has gone stale"
    assert set(http) == set(stdio), (
        f"only in-process: {sorted(set(http) - set(stdio))}\n"
        f"only stdio: {sorted(set(stdio) - set(http))}"
    )


def test_tool_parameters_match(surfaces):
    http, stdio = surfaces
    for name in sorted(set(http) & set(stdio)):
        http_params, _ = http[name]
        stdio_params, _ = stdio[name]
        allowed = PROXY_ONLY_PARAMS.get(name, set())
        assert http_params == stdio_params - allowed, (
            f"{name}: in-process-only={sorted(http_params - stdio_params)} "
            f"stdio-only={sorted(stdio_params - http_params - allowed)}"
        )


def test_tool_descriptions_match(surfaces):
    """Descriptions are the tool contract an agent reads — drift here means the
    two servers behave differently in practice even when the schemas agree."""
    http, stdio = surfaces
    mismatched = []
    for name in sorted(set(http) & set(stdio)):
        if name in DESCRIPTION_EXEMPT:
            continue
        if http[name][1] != stdio[name][1]:
            mismatched.append(
                f"\n--- {name} ---\n  in-process: {http[name][1]!r}\n  stdio     : {stdio[name][1]!r}"
            )
    assert not mismatched, "tool descriptions have drifted:" + "".join(mismatched)


def test_the_http_server_never_gains_a_local_path_parameter(surfaces):
    """Reading a server-host path over an authenticated call would be an arbitrary
    file read on the relay host. This exception is stdio-only, permanently."""
    http, _ = surfaces
    assert "path" not in http["add_attachment"][0]
