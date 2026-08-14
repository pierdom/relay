from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, metrics, watcher
from .auth import create_session, revoke_session
from .cleanup import cleanup_loop
from .config import settings
from .database import init_db
from .mcp_server import mcp, mcp_asgi_app
from .routes.attachments import router as attachments_router
from .routes.auth import router as auth_router
from .routes.events import router as events_router
from .routes.folders import router as folders_router
from .routes.links import router as links_router
from .routes.metrics import router as metrics_router
from .routes.posts import router as posts_router
from .routes.tags import router as tags_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Presigned upload slots are in-memory + disk-staged; any bytes left in the
    # staging dir from a prior run belong to slots that no longer exist. Wipe them.
    from . import ingest

    ingest.registry.reset()
    # Persistent OAuth store (DCR clients + tokens) lives beside the index but is
    # never rebuilt from files; create its schema once at startup when enabled.
    if settings.mcp_oauth_active:
        from .mcp_oauth.store import get_store

        await get_store().init()
    elif settings.mcp_oauth_enabled:
        # Flag set but the upstream OIDC client isn't configured, so OAuth can't
        # broker a login — fall back to static-bearer. Warn so it's not silent.
        logging.getLogger(__name__).warning(
            "MCP_OAUTH_ENABLED is set but OIDC is not configured; remote MCP OAuth "
            "is inactive and /mcp still uses the static API key."
        )
    task = asyncio.create_task(cleanup_loop())
    # Live vault watcher: external edits (e.g. from Obsidian) re-index + push SSE.
    watcher.start(asyncio.get_running_loop())
    # The Streamable HTTP MCP app needs its session manager running for the
    # lifetime of the server; mounted sub-apps don't get their lifespan run
    # automatically, so we drive it from here.
    async with mcp.session_manager.run():
        yield
    watcher.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="relay", version=__version__, lifespan=lifespan)

# Top-level path segments we count under a stable label. Anything else (e.g. a
# 404 probe at /random/xyz) buckets to "other" so /metrics cardinality can't be
# blown up by unmatched paths.
_KNOWN_SEGMENTS = frozenset({
    "posts", "tags", "folders", "links", "events", "attachments", "mcp",
    "auth", "session", "health", "metrics", "assets", "favicon.ico",
})


def _metric_path(scope) -> str:
    """Stable, low-cardinality path label for an HTTP request.

    A matched FastAPI route exposes its template (``/posts/{post_id}``); a mounted
    sub-app (the MCP app at ``/mcp``) or an unmatched path has no APIRoute, so we
    bucket by the first path segment (allowlisted, else ``other``)."""
    route = scope.get("route")
    template = getattr(route, "path", None)
    if template:  # APIRoute template — already parameterised, bounded cardinality
        return template
    segment = scope.get("path", "/").strip("/").split("/", 1)[0]
    if not segment:
        return "/"
    return f"/{segment}" if segment in _KNOWN_SEGMENTS else "/other"


class MetricsMiddleware:
    """Pure-ASGI request counter.

    Kept as raw ASGI (not ``BaseHTTPMiddleware``) so it never buffers a response
    body — that would break the SSE ``/events`` stream and the MCP Streamable HTTP
    transport. It only peeks at the response-start status message."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status_code = 500  # assume failure until we see a response start
        method = scope.get("method", "GET")

        async def send_wrapper(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            metrics.http_requests.inc(
                method=method, path=_metric_path(scope), status=str(status_code)
            )


app.add_middleware(MetricsMiddleware)

# Holds transient OAuth state (state/nonce/PKCE verifier) between /auth/login and
# /auth/callback. SameSite=lax so it survives the top-level redirect back from
# PocketID; short-lived. Signed with the session key. Only written during login.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_signing_key,
    https_only=settings.secure_cookies,
    same_site="lax",
    max_age=600,
    session_cookie="relay_oauth",
)

_STATIC_DIR = Path(__file__).parent / "static"
_UI_PATH = _STATIC_DIR / "index.html"

# Brand assets (logos, favicons) — public, no auth, so <link rel="icon"> and the
# README can reference them directly.
app.mount("/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="assets")


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(_STATIC_DIR / "assets" / "favicon-64.png", media_type="image/png")


@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    return FileResponse(_UI_PATH, media_type="text/html")


@app.get("/ui", include_in_schema=False)
async def ui() -> RedirectResponse:
    return RedirectResponse("/", status_code=status.HTTP_301_MOVED_PERMANENTLY)


@app.post("/session", include_in_schema=False)
async def session_create(request: Request, response: Response) -> dict:
    key = ""
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            body = await request.json()
            key = body.get("key", "")
        except Exception:
            pass
    auth = request.headers.get("authorization", "")
    if not key and auth.startswith("Bearer "):
        key = auth[7:]
    if not (key and hmac.compare_digest(key, settings.api_key)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    token = create_session()
    response.set_cookie(
        key="relay_session",
        value=token,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
        max_age=settings.session_max_age_hours * 3600,
    )
    return {"ok": True}


@app.delete("/session", include_in_schema=False)
async def session_delete(
    response: Response,
    relay_session: str | None = Cookie(default=None),
) -> dict:
    if relay_session:
        revoke_session(relay_session)
    response.delete_cookie("relay_session")
    return {"ok": True}


app.include_router(auth_router)
app.include_router(posts_router)
app.include_router(tags_router)
app.include_router(events_router)
app.include_router(links_router)
app.include_router(folders_router)
app.include_router(attachments_router)
app.include_router(metrics_router)

# Remote MCP endpoint (Streamable HTTP). Any MCP client can connect to /mcp
# with the relay bearer key; shares relay.service with the REST routes. The
# MCP route is at /mcp so the path matches exactly (no trailing-slash redirect);
# mounted last so every declared route above takes priority over this catch-all.
app.mount("/", mcp_asgi_app())
