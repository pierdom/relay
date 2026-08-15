from __future__ import annotations

import asyncio
import logging

import aiosqlite

from . import events, history, ingest, metrics, vault
from .config import settings

logger = logging.getLogger(__name__)


async def _ids_where(db: aiosqlite.Connection, clause: str, params: list) -> set[int]:
    async with db.execute(f"SELECT id FROM posts WHERE {clause}", params) as cur:
        return {row[0] for row in await cur.fetchall()}


async def _delete_expired(db: aiosqlite.Connection) -> int:
    """Collect every expired post id, unlink its file, drop the index rows, then
    publish an SSE ``delete`` for each.

    Files are canonical, so expiry deletes the file too (the watcher ignores it
    via self-delete suppression — which is why this path has to emit the events
    itself).
    """
    async with db.execute("SELECT tag, ttl_hours, expires_at FROM tag_config") as cur:
        tag_configs = {
            row["tag"]: {"ttl_hours": row["ttl_hours"], "expires_at": row["expires_at"]}
            for row in await cur.fetchall()
        }

    now = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
    to_delete: set[int] = set()

    # Explicit per-post expiry (overrides tag/global TTL).
    to_delete |= await _ids_where(
        db, f"id != 0 AND expires_at IS NOT NULL AND expires_at < {now}", []
    )

    # Global TTL for posts without their own expires_at and without a configured tag.
    if settings.default_ttl_hours:
        ttl_modifier = f"-{settings.default_ttl_hours} hours"
        configured = [t for t, c in tag_configs.items() if c["ttl_hours"] or c["expires_at"]]
        if configured:
            likes = " OR ".join(["tags LIKE ?"] * len(configured))
            clause = (
                f"id != 0 AND expires_at IS NULL AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) "
                f"AND id NOT IN (SELECT id FROM posts WHERE {likes})"
            )
            params = [ttl_modifier] + [f"%,{t},%" for t in configured]
        else:
            clause = "id != 0 AND expires_at IS NULL AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)"
            params = [ttl_modifier]
        to_delete |= await _ids_where(db, clause, params)

    # Per-tag expiry (only posts without their own expires_at).
    for tag, cfg in tag_configs.items():
        if cfg["expires_at"]:
            to_delete |= await _ids_where(
                db,
                f"id != 0 AND expires_at IS NULL AND tags LIKE ? AND ? < {now}",
                [f"%,{tag},%", cfg["expires_at"]],
            )
        if cfg["ttl_hours"]:
            to_delete |= await _ids_where(
                db,
                "id != 0 AND expires_at IS NULL AND tags LIKE ? "
                "AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
                [f"%,{tag},%", f"-{cfg['ttl_hours']} hours"],
            )

    if not to_delete:
        return 0

    # Unlink the canonical files, then drop the index rows.
    async with db.execute(
        f"SELECT id, path, tags FROM posts WHERE id IN ({','.join('?' * len(to_delete))})",
        list(to_delete),
    ) as cur:
        expired = [(row["id"], row["path"], row["tags"]) for row in await cur.fetchall()]
    for _id, rel, _tags in expired:
        vault.delete_file(vault.abspath(rel))
    await db.execute(
        f"DELETE FROM posts WHERE id IN ({','.join('?' * len(to_delete))})", list(to_delete)
    )
    await db.commit()
    # Tell live clients. The file unlink is self-delete-suppressed, so the watcher
    # won't emit for these — without this a TTL'd post lingers in every connected
    # UI/TUI until the next reload. Deletes stream without an SSE `id:`, so they
    # can't rewind a client's replay cursor.
    for post_id, _rel, tags in expired:
        await events.publish_delete(post_id, [t for t in tags.split(",") if t])
    await history.commit(f"ttl expiry: {len(expired)} post(s)")
    return len(to_delete)


async def cleanup_loop() -> None:
    interval = settings.cleanup_interval_minutes * 60
    logger.info(
        "Cleanup loop started — interval=%dm, default_ttl=%dh",
        settings.cleanup_interval_minutes,
        settings.default_ttl_hours,
    )
    while True:
        await asyncio.sleep(interval)
        try:
            async with aiosqlite.connect(settings.database_path) as db:
                db.row_factory = aiosqlite.Row
                count = await _delete_expired(db)
                if count:
                    metrics.cleanup_deletions.inc(count)
                    logger.info("Cleanup deleted %d expired post(s)", count)
        except Exception as exc:
            logger.error("Cleanup error: %s", exc)

        # Sweep expired presigned upload slots (staged bytes never finalized).
        try:
            dropped = ingest.registry.purge_expired()
            if dropped:
                metrics.upload_slots_purged.inc(dropped)
                logger.info("Cleanup purged %d expired upload slot(s)", dropped)
        except Exception as exc:
            logger.error("Upload-slot cleanup error: %s", exc)

        # Piggyback OAuth store hygiene: drop expired pending auths, codes, and
        # access tokens (refresh tokens live until their own expiry). Gate on
        # mcp_oauth_active — the store only exists when OAuth actually ran.
        if settings.mcp_oauth_active:
            try:
                from .mcp_oauth.store import get_store

                removed = await get_store().cleanup_expired()
                if removed:
                    logger.info("Cleanup removed %d expired OAuth row(s)", removed)
            except Exception as exc:
                logger.error("OAuth cleanup error: %s", exc)
