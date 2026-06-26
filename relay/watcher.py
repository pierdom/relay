"""Live filesystem watcher: external edits to the vault re-index and push SSE.

Watchdog runs in its own thread; reconciliation runs as a coroutine marshalled
onto the app event loop via ``run_coroutine_threadsafe``. Writes relay itself
made are ignored through the self-write/-delete suppression in ``relay.vault``.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import aiosqlite
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import events, frontmatter, service, vault
from .config import settings

logger = logging.getLogger(__name__)

_DEBOUNCE_SECONDS = 0.3

# Only content-changing events matter. Crucially we must ignore "opened"/"closed"
# (and "closed_no_write"): reconciling *reads* the .md file, which itself emits
# open/close events — reacting to those would feed back into an infinite loop.
_CHANGE_EVENTS = {"created", "modified", "moved", "deleted"}


class _Handler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._relay_dir = str(Path(settings.relay_dir).resolve())
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._timer: threading.Timer | None = None

    def _relevant(self, path: str) -> bool:
        if not path.endswith(".md"):
            return False
        return not str(Path(path).resolve()).startswith(self._relay_dir)

    def on_any_event(self, event) -> None:
        if event.is_directory or event.event_type not in _CHANGE_EVENTS:
            return
        candidates = [event.src_path, getattr(event, "dest_path", None)]
        with self._lock:
            added = False
            for p in candidates:
                if p and self._relevant(p):
                    self._pending.add(p)
                    added = True
            if not added:
                return
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(_DEBOUNCE_SECONDS, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            batch = list(self._pending)
            self._pending.clear()
        if not batch:
            return
        fut = asyncio.run_coroutine_threadsafe(_reconcile(batch), self._loop)
        fut.add_done_callback(_log_failure)


def _log_failure(fut) -> None:
    exc = fut.exception()
    if exc:
        logger.error("Watcher reconcile failed: %s", exc)


async def _reconcile(paths: list[str]) -> None:
    existing = [Path(p) for p in paths if Path(p).exists()]
    missing = [Path(p) for p in paths if not Path(p).exists()]
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=5000;")
        for path in existing:
            await _reconcile_file(db, path)
        for path in missing:
            await _reconcile_delete(db, path)


async def _reconcile_file(db: aiosqlite.Connection, path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    if vault.was_self_write(path, text):
        return
    meta, body = frontmatter.parse(text)
    async with vault.write_lock:
        pid = meta.get("id")
        if pid is None:
            pid = await vault.allocate_id(db)
            path = vault.write_file(
                id=pid, title=path.stem, content=body, tags=meta.get("tags") or [],
                source=meta.get("source"), created_at=meta.get("created_at") or vault.utcnow_iso(),
                updated_at=meta.get("updated_at"), expires_at=meta.get("expires_at"), old_path=path,
            )
        await vault.index_upsert(
            db, id=pid, title=path.stem, path=path, content=body, tags=meta.get("tags") or [],
            source=meta.get("source"), created_at=meta.get("created_at") or vault.utcnow_iso(),
            updated_at=meta.get("updated_at"), expires_at=meta.get("expires_at"),
        )
        await db.commit()
    post = await service.get_post(db, pid)
    if post is not None:
        await events.publish(post.model_dump())
    logger.info("Indexed external change: %s (id=%s)", path.name, pid)


async def _reconcile_delete(db: aiosqlite.Connection, path: Path) -> None:
    if vault.was_self_delete(path):
        return
    async with db.execute("SELECT * FROM posts WHERE path = ?", (vault.relpath(path),)) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    if row["id"] == vault.MASTER_ID:
        # The master document must persist — recreate it from the index copy.
        vault.write_file(
            id=vault.MASTER_ID, title=vault.MASTER_TITLE, content=row["content"], tags=[],
            source=row["source"], created_at=row["created_at"],
            updated_at=row["updated_at"], expires_at=None,
        )
        return
    await db.execute("DELETE FROM posts WHERE id = ?", (row["id"],))
    await db.commit()
    await events.publish_delete(row["id"], [t for t in row["tags"].split(",") if t])
    logger.info("Removed externally deleted note: %s (id=%s)", path.name, row["id"])


_observer: Observer | None = None


def start(loop: asyncio.AbstractEventLoop) -> None:
    global _observer
    if not settings.watch_enabled or _observer is not None:
        return
    Path(settings.vault_path).mkdir(parents=True, exist_ok=True)
    _observer = Observer()
    _observer.schedule(_Handler(loop), settings.vault_path, recursive=True)
    _observer.start()
    logger.info("Vault watcher started on %s", settings.vault_path)


def stop() -> None:
    global _observer
    if _observer is not None:
        _observer.stop()
        _observer.join(timeout=5)
        _observer = None
