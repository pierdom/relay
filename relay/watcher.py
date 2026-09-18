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

from . import changes, database, events, frontmatter, history, service, vault
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
        p = Path(path).resolve()
        if str(p).startswith(self._relay_dir) or vault.is_hidden_path(p):
            return False
        # Syncthing conflict copies (.sync-conflict-YYYYMMDD-HHMMSS-DEVICEID.md)
        # carry the original post's id: in front-matter — ingesting them would
        # silently create a second index entry under an existing id. Skip them;
        # the human resolves the conflict in Obsidian/Syncthing.
        if ".sync-conflict-" in p.name:
            return False
        # Syncthing file versioning stores old copies under .stversions/ — same
        # risk as conflict copies: stale ids, not canonical vault state.
        return ".stversions" not in p.parts

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
    async with database.connect() as db:
        # The whole batch — every per-file write plus the commit below — runs
        # under one `write_lock` acquisition (K-2): committing after releasing
        # the lock left a gap where another writer's own already-written,
        # not-yet-committed file could get swept into *this* commit, and
        # `changes._ingest` would then misattribute this batch's action to that
        # other writer's post. `_reconcile_file_locked`/`_reconcile_delete`
        # assume the lock is already held — only `_reconcile_file` (the direct
        # single-file entry point tests use) acquires it itself.
        async with vault.write_lock:
            for path in existing:
                await _reconcile_file_locked(db, path)
            for path in missing:
                await _reconcile_delete(db, path)
            # One commit per debounced batch, so a bulk edit in Obsidian is one
            # revision rather than a commit per file. This is the path that
            # captures *human* edits — the ones relay never sees through its own API.
            await history.commit(_batch_message(existing, missing))
        # relay #198, N-4: the batch commit — and so the changes-log row(s) it
        # produces — only exists *after* every per-file events.publish above, so
        # (unlike posts.py/tags.py/revisions.py) those live frames can't carry
        # this write's `seq`; they keep going out with no `id:`, same as before
        # this feature. What matters here is that the row still lands, so a
        # *reconnecting* client's catch-up query (which reads this table, not the
        # live broadcast) sees the external edit/delete either way.
        await changes.record_latest(db)


def _batch_message(existing: list[Path], missing: list[Path]) -> str:
    if len(existing) == 1 and not missing:
        return f"external edit: {existing[0].name}"
    if len(missing) == 1 and not existing:
        return f"external delete: {missing[0].name}"
    return f"external change: {len(existing)} edited, {len(missing)} removed"


async def _reconcile_file(db: aiosqlite.Connection, path: Path) -> None:
    """Single-file entry point (used directly by tests): acquires `write_lock`
    itself. `_reconcile`'s batch path calls `_reconcile_file_locked` instead,
    holding one lock across the whole batch — see its comment."""
    async with vault.write_lock:
        await _reconcile_file_locked(db, path)


async def _reconcile_file_locked(db: aiosqlite.Connection, path: Path) -> None:
    """Body of `_reconcile_file`, assuming `vault.write_lock` is already held."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    if vault.was_self_write(path, text):
        return
    meta, body = frontmatter.parse(text)
    pid = meta.get("id")
    if pid is not None and await _id_taken_elsewhere(db, pid, path):
        # A copy carrying another live note's id (Obsidian "Duplicate note", a
        # backup dropped beside the original). Upserting would repoint the id
        # at the copy and drop the original from the index (AUDIT.md B-03);
        # stamp a fresh id instead, exactly as rebuild_index does at startup.
        logger.warning("External note %s carries id %s already used by another file — re-stamping", path.name, pid)
        pid = None
    if pid is None:
        pid = await vault.allocate_id(db)
        path = vault.write_file(
            id=pid, title=path.stem, content=body, tags=meta.get("tags") or [],
            source=meta.get("source"), created_at=meta.get("created_at") or vault.utcnow_iso(),
            updated_at=meta.get("updated_at"), expires_at=meta.get("expires_at"), old_path=path,
            properties=meta.get("properties"), updated_by=meta.get("updated_by"),
        )
    # An external editor rewrites the body but leaves the front-matter stamp
    # alone, so take the last-modified time from the file itself — otherwise
    # an Obsidian edit never moves the post in the default "updated" sort.
    # `updated_by` (relay #198, B-7) round-trips from the file's own
    # front-matter unchanged — files are canonical, and an external edit has
    # no authenticated identity of its own to attribute to.
    await vault.index_upsert(
        db, id=pid, title=path.stem, path=path, content=body, tags=meta.get("tags") or [],
        source=meta.get("source"), created_at=meta.get("created_at") or vault.utcnow_iso(),
        updated_at=vault.effective_updated_at(path, meta), expires_at=meta.get("expires_at"),
        properties=meta.get("properties"), updated_by=meta.get("updated_by"),
    )
    await db.commit()
    post = await service.get_post(db, pid)
    if post is not None:
        await events.publish(post.model_dump())
    logger.info("Indexed external change: %s (id=%s)", path.name, pid)


async def _id_taken_elsewhere(db: aiosqlite.Connection, pid: int, path: Path) -> bool:
    """Whether ``pid`` is indexed at a *different* path whose file still exists.
    A rename (old path gone) is not a collision; a second file is."""
    async with db.execute("SELECT path FROM posts WHERE id = ?", (pid,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return False
    other = vault.abspath(row["path"])
    return other != path.resolve() and other.exists()


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
            properties=vault.decode_properties(row["properties"]),
            updated_by=row["updated_by"],
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


def is_running() -> bool:
    """Whether the filesystem observer thread is actually up (not just enabled)."""
    return _observer is not None


def stop() -> None:
    global _observer
    if _observer is not None:
        _observer.stop()
        _observer.join(timeout=5)
        _observer = None
