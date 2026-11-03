"""Request-body size cap for the REST routes.

Pure ASGI, like ``metrics.MetricsMiddleware``, so it never buffers a body or a
streamed response. Without it a REST body was read whole into memory at any
size — an inline base64 attachment before its own size check ran.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, status
from starlette.responses import JSONResponse

from .config import settings

# The same cap the MCP transport applies to every tool call; the largest posts
# are well under 100 KB.
MAX_BODY_BYTES = 4 * 1024 * 1024
# Room for the JSON around an inline attachment's base64 `data`.
_ENVELOPE_BYTES = 64 * 1024


def limit_for(method: str, path: str) -> int | None:
    """The body cap for one request, or None where something else enforces one."""
    if path == "/mcp" or path.startswith("/mcp/"):
        return None  # the MCP SDK caps its own bodies (mcp_server.py)
    if method == "PUT" and path.startswith("/attachments/uploads/"):
        return None  # streamed to disk under attachment_max_bytes (ingest.stage_upload)
    if method == "POST" and path == "/attachments":
        return -(-settings.attachment_max_bytes // 3) * 4 + _ENVELOPE_BYTES
    return MAX_BODY_BYTES


class _BodyTooLarge(HTTPException):
    # An HTTPException so FastAPI re-raises it as-is: any other exception out of
    # `receive` while it parses a body becomes a 400 "error parsing the body".
    def __init__(self, limit: int) -> None:
        super().__init__(status.HTTP_413_CONTENT_TOO_LARGE, f"Request body is larger than {limit} bytes")


class BodySizeLimitMiddleware:
    def __init__(self, app: Callable[..., Any]) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        limit = limit_for(scope["method"], scope["path"]) if scope["type"] == "http" else None
        if limit is None:
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await _reject(scope, receive, send, limit)
            return

        # A chunked body has no Content-Length: count what actually arrives.
        received = 0
        started = False

        async def counting_receive() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge(limit)
            return message

        async def tracking_send(message: dict[str, Any]) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            if not started:  # read outside a route's body parsing
                await _reject(scope, receive, send, limit)


async def _reject(scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any], limit: int) -> None:
    exc = _BodyTooLarge(limit)
    await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
