from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from .cleanup import cleanup_loop
from .database import init_db
from .routes.events import router as events_router
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
    yield
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


app.include_router(posts_router)
app.include_router(tags_router)
app.include_router(events_router)
