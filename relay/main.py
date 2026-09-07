from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, embedding, history, metrics, vault, watcher
from . import status as app_status
from .cleanup import cleanup_loop
from .config import settings
from .database import init_db
from .mcp_server import mcp, mcp_asgi_app
from .routes.attachments import router as attachments_router
from .routes.auth import router as auth_router
from .routes.embeddings import router as embeddings_router
from .routes.events import router as events_router
from .routes.folders import router as folders_router
from .routes.links import router as links_router
from .routes.metrics import router as metrics_router
from .routes.posts import router as posts_router
from .routes.status import router as status_router
from .routes.tags import router as tags_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


async def _embedding_idle_unload_loop() -> None:
    """Poll embedding.unload_if_idle() every 60s for the life of the process.

    Constructing the embedding backend costs ~570MB of RSS by itself
    (onnxruntime session + model weights) that doesn't grow with usage
    afterward — see embedding.unload_if_idle's docstring. Capping
    embedding_threads (v1.1.2) didn't touch this, confirmed on production: no
    observed reduction, because the cost was never about thread-pool buffers.
    This trades that ~570MB back to the OS during idle stretches for a
    several-second reload on the next embed call. No-ops (0 overhead beyond
    the sleep) when embedding_idle_unload_seconds is 0 or nothing has loaded
    the backend yet."""
    logger = logging.getLogger(__name__)
    while True:
        await asyncio.sleep(60)
        try:
            if embedding.unload_if_idle():
                logger.info("Embedding backend unloaded after idle timeout")
        except Exception:
            logger.exception("Embedding idle-unload check failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _concurrency = int(os.environ.get("WEB_CONCURRENCY", "1"))
    if _concurrency > 1:
        raise RuntimeError(
            f"relay requires a single worker process (WEB_CONCURRENCY={_concurrency}). "
            "Multiple workers corrupt upload-slot state and id allocation. "
            "See docs/setup.md."
        )
    app_status.mark_started()
    if settings.oidc_enabled and not settings.session_secret:
        logging.getLogger(__name__).warning(
            "SESSION_SECRET is unset: the browser session cookie is signed with API_KEY. "
            "Set a dedicated SESSION_SECRET so one secret does not serve two roles."
        )
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
    # Vault history: baseline commit of the current tree, then a commit per write.
    # Runs after the index rebuild, which may itself stamp ids into id-less notes.
    await history.init()
    task = asyncio.create_task(cleanup_loop())
    # One-shot catch-up for posts the embedding cache doesn't cover yet — never
    # inline in init_db/rebuild_index above (see vault.backfill_embeddings's
    # docstring). No-ops immediately if embeddings aren't enabled. Same
    # spawn_backfill also backs POST /embeddings/backfill (relay #253, v1.3.0).
    embedding_task = vault.spawn_backfill()
    # Gives the ~570MB embedding backend back to the OS after an idle period —
    # see _embedding_idle_unload_loop's docstring. Runs regardless of whether
    # embeddings are enabled; unload_if_idle() itself no-ops when nothing has
    # loaded the backend.
    idle_unload_task = asyncio.create_task(_embedding_idle_unload_loop())
    # Live vault watcher: external edits (e.g. from Obsidian) re-index + push SSE.
    watcher.start(asyncio.get_running_loop())
    # The Streamable HTTP MCP app needs its session manager running for the
    # lifetime of the server; mounted sub-apps don't get their lifespan run
    # automatically, so we drive it from here.
    async with mcp.session_manager.run():
        yield
    watcher.stop()
    task.cancel()
    embedding_task.cancel()
    idle_unload_task.cancel()
    for t in (task, embedding_task, idle_unload_task):
        try:
            await t
        except asyncio.CancelledError:
            pass


app = FastAPI(title="relay", version=__version__, lifespan=lifespan)

app.add_middleware(metrics.MetricsMiddleware)

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

_UI_DIR = _STATIC_DIR / "ui"


@lru_cache(maxsize=1)
def asset_version() -> str:
    """A token that changes whenever any UI file does.

    Content-derived rather than just ``__version__`` so it also moves during
    development, where the version does not.
    """
    digest = hashlib.sha256()
    for path in sorted(_UI_DIR.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(_UI_DIR)).encode("utf-8"))
            digest.update(path.read_bytes())
    return f"{__version__}.{digest.hexdigest()[:8]}"


# Versioned asset URLs — /static/<version>/js/main.js.
#
# The version lives in the **path**, not a query string, because main.js does
# `import './status.js'`: a `?v=` on the entry point does not propagate to its
# imports, so they would keep being served from cache, while a path segment does
# propagate — the browser resolves the relative import against the versioned
# directory.
#
# This exists because splitting the UI out of index.html introduced a version
# skew that could not happen when everything was inline. `/` always revalidates,
# but a proxy in front of relay may cache /static aggressively (bespin's adds
# `max-age` ~4h), so a deploy could hand a browser the new markup with the old
# script — a button present with no handler behind it. A URL that changes with
# the content makes a stale copy unreachable rather than merely unlikely.
#
# Registered **before** the unversioned mount below: Starlette matches routes in
# registration order, and the mount would otherwise swallow this path.
@app.get("/static/{version}/{path:path}", include_in_schema=False)
async def versioned_asset(version: str, path: str) -> FileResponse:
    root = _UI_DIR.resolve()

    def resolve(rel: str) -> Path | None:
        candidate = (root / rel).resolve()
        return candidate if candidate.is_file() and candidate.is_relative_to(root) else None

    # Versioned URL: the first segment is a version token to be discarded.
    target = resolve(path)
    if target is not None:
        # Safe to cache hard: the URL changes whenever the bytes do.
        return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    # Not a version after all — this path pattern also swallows the plain
    # /static/js/main.js form, which is precisely what a browser holding a cached
    # index.html asks for. Treat the first segment as a real directory and serve it
    # without the immutable header, since that URL does not change with content.
    target = resolve(f"{version}/{path}")
    if target is not None:
        return FileResponse(target, headers={"Cache-Control": "no-cache"})

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


# Unversioned fallback, kept deliberately. A browser holding a cached index.html
# still asks for /static/js/main.js, and 404ing that would break its UI outright
# until the cache expired.
app.mount("/static", StaticFiles(directory=_UI_DIR), name="static")


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(_STATIC_DIR / "assets" / "favicon-64.png", media_type="image/png")


_INLINE_SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.DOTALL)


@lru_cache(maxsize=1)
def ui_csp() -> str:
    """Content-Security-Policy for the UI shell.

    Scripts come from this origin only — the Markdown renderer and the sanitiser
    are vendored, not CDN-loaded — plus the one inline theme-bootstrap script in
    index.html, allowed by hash so 'unsafe-inline' never appears in script-src.
    Styles need 'unsafe-inline' for the `style=` attributes the SPA renders;
    images may come from any https origin because posts embed external pictures.
    `frame-ancestors 'none'` doubles as X-Frame-Options.
    """
    html = _UI_PATH.read_text(encoding="utf-8")
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(m.group(1).encode("utf-8")).digest()).decode() + "'"
        for m in _INLINE_SCRIPT_RE.finditer(html)
    )
    return (
        "default-src 'self'; "
        f"script-src 'self' {hashes}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https:; "
        "media-src 'self'; connect-src 'self'; object-src 'none'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )


@app.get("/", include_in_schema=False)
async def root() -> HTMLResponse:
    """The UI shell, with the asset version stamped into its URLs.

    Explicitly `no-cache`: this document is what carries the current asset
    version, so a cached copy would keep pointing at the previous release's
    scripts. It revalidates cheaply (a few KB, and usually a 304).
    """
    html = _UI_PATH.read_text(encoding="utf-8").replace("__ASSETS__", asset_version())
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-cache",
            "Content-Security-Policy": ui_csp(),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
        },
    )


@app.get("/ui", include_in_schema=False)
async def ui() -> RedirectResponse:
    return RedirectResponse("/", status_code=status.HTTP_301_MOVED_PERMANENTLY)


@app.get("/id/{post_id}", include_in_schema=False)
async def open_post(post_id: int) -> RedirectResponse:
    """Deep link to a post by id — `/id/123` opens it in the app.

    A plain redirect, not a lookup: existence/auth are the client's job once it
    lands on `/`, same as any other in-app navigation. FastAPI's `int`
    parameter already rejects anything non-numeric with a 422, so garbage
    never reaches the redirect.
    """
    return RedirectResponse(f"/?post={post_id}", status_code=status.HTTP_302_FOUND)


app.include_router(auth_router)
app.include_router(posts_router)
app.include_router(tags_router)
app.include_router(events_router)
app.include_router(links_router)
app.include_router(folders_router)
app.include_router(attachments_router)
app.include_router(metrics_router)
app.include_router(status_router)
app.include_router(embeddings_router)

# Remote MCP endpoint (Streamable HTTP). Any MCP client can connect to /mcp
# with the relay bearer key; shares relay.service with the REST routes. The
# MCP route is at /mcp so the path matches exactly (no trailing-slash redirect);
# mounted last so every declared route above takes priority over this catch-all.
app.mount("/", mcp_asgi_app())
