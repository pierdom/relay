from __future__ import annotations

import asyncio
import logging

import aiosqlite

from .config import settings

logger = logging.getLogger(__name__)


async def _delete_expired(db: aiosqlite.Connection) -> int:
    async with db.execute("SELECT tag, ttl_hours, expires_at FROM tag_config") as cur:
        tag_configs = {
            row["tag"]: {"ttl_hours": row["ttl_hours"], "expires_at": row["expires_at"]}
            for row in await cur.fetchall()
        }

    deleted = 0

    # Explicit per-post expiry (overrides tag/global TTL)
    await db.execute(
        """
        DELETE FROM posts
        WHERE id != 0
          AND expires_at IS NOT NULL
          AND expires_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        """
    )
    async with db.execute("SELECT changes()") as cur:
        row = await cur.fetchone()
        deleted += row[0]

    if tag_configs:
        # Posts with no per-tag config → global TTL (skipped when default_ttl_hours=0)
        tag_likes = [f"%,{tag},%" for tag in tag_configs]
        exclusion = " OR ".join(["tags LIKE ?"] * len(tag_likes))
        if settings.default_ttl_hours:
            await db.execute(
                f"""
                DELETE FROM posts
                WHERE id != 0
                  AND expires_at IS NULL
                  AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now',
                                            '-{settings.default_ttl_hours} hours')
                  AND id NOT IN (
                      SELECT id FROM posts WHERE {exclusion}
                  )
                """,
                tag_likes,
            )
    elif settings.default_ttl_hours:
        await db.execute(
            f"""
            DELETE FROM posts
            WHERE id != 0
              AND expires_at IS NULL
              AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now',
                                        '-{settings.default_ttl_hours} hours')
            """
        )

    async with db.execute("SELECT changes()") as cur:
        row = await cur.fetchone()
        deleted += row[0]

    for tag, cfg in tag_configs.items():
        ttl_hours = cfg["ttl_hours"]
        tag_expires_at = cfg["expires_at"]

        if tag_expires_at:
            await db.execute(
                """
                DELETE FROM posts
                WHERE id != 0
                  AND expires_at IS NULL
                  AND tags LIKE ?
                  AND ? < strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                """,
                (f"%,{tag},%", tag_expires_at),
            )
            async with db.execute("SELECT changes()") as cur:
                row = await cur.fetchone()
                deleted += row[0]

        if ttl_hours:
            await db.execute(
                f"""
                DELETE FROM posts
                WHERE id != 0
                  AND expires_at IS NULL
                  AND tags LIKE ?
                  AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-{ttl_hours} hours')
                """,
                (f"%,{tag},%",),
            )
            async with db.execute("SELECT changes()") as cur:
                row = await cur.fetchone()
                deleted += row[0]

    await db.commit()
    return deleted


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
                    logger.info("Cleanup deleted %d expired post(s)", count)
        except Exception as exc:
            logger.error("Cleanup error: %s", exc)
