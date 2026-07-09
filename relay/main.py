from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse

from . import watcher
from .auth import create_session, revoke_session
from .cleanup import cleanup_loop
from .config import settings
from .database import init_db
from .mcp_server import mcp, mcp_asgi_app
from .routes.events import router as events_router
from .routes.folders import router as folders_router
from .routes.links import router as links_router
from .routes.posts import router as posts_router
from .routes.tags import router as tags_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
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


app = FastAPI(title="relay", version="0.1.0", lifespan=lifespan)

_UI_PATH = Path(__file__).parent / "static" / "index.html"


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    return {"status": "ok"}


@app.get("/ui", include_in_schema=False)
async def ui() -> FileResponse:
    return FileResponse(_UI_PATH, media_type="text/html")


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
    if key != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    token = create_session()
    response.set_cookie(
        key="relay_session",
        value=token,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
        max_age=86400 * 30,
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


app.include_router(posts_router)
app.include_router(tags_router)
app.include_router(events_router)
app.include_router(links_router)
app.include_router(folders_router)

# Remote MCP endpoint (Streamable HTTP). Any MCP client can connect to /mcp
# with the relay bearer key; shares relay.service with the REST routes. The
# MCP route is at /mcp so the path matches exactly (no trailing-slash redirect);
# mounted last so every declared route above takes priority over this catch-all.
app.mount("/", mcp_asgi_app())
