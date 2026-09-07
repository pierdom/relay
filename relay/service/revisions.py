"""Vault history: revisions, previews, deleted-post discovery and restore."""
from __future__ import annotations

from pathlib import Path

import aiosqlite

from .. import events, folders, frontmatter, history, vault
from ..models import (
    DeletedPost,
    DeletedPostsResponse,
    PostHistoryResponse,
    PostResponse,
    PostRevision,
    PostRevisionContent,
    PostUpdate,
)
from ._common import HistoryUnavailable, RevisionNotFound, _fetch
from .posts import update_post

# How deep to look when resolving a sha for a restore. Larger than the listing
# default: the caller may hold a sha from an older page of history.
_RESTORE_SCAN_LIMIT = 200


async def get_post_history(
    db: aiosqlite.Connection, post_id: int, *, limit: int = 20
) -> PostHistoryResponse:
    """A post's revisions, newest first.

    Deliberately does **not** require the post to exist — a deleted post is the
    case most worth recovering, and its history is still in the repo.
    """
    if not history.enabled():
        raise HistoryUnavailable
    row = await _fetch(db, post_id)
    revs = await history.revisions(
        post_id, current_path=row["path"] if row is not None else None, limit=limit
    )
    return PostHistoryResponse(
        id=post_id,
        exists=row is not None,
        items=[
            PostRevision(
                sha=r.sha, short_sha=r.short_sha, when=r.when, message=r.message, path=r.path
            )
            for r in revs
        ],
    )


async def _resolve_revision(db: aiosqlite.Connection, post_id: int, sha: str):
    """Find a revision of ``post_id`` and read its file back.

    Shared by the read-only preview and the restore so they can never disagree
    about which revision a sha means, or about whether it legitimately belongs to
    this post. Returns ``(revision, front_matter, body, current_row_or_None)``.
    """
    if not history.enabled():
        raise HistoryUnavailable
    row = await _fetch(db, post_id)
    revs = await history.revisions(
        post_id,
        current_path=row["path"] if row is not None else None,
        limit=_RESTORE_SCAN_LIMIT,
    )
    match = next((r for r in revs if r.sha == sha or r.sha.startswith(sha)), None)
    if match is None:
        raise RevisionNotFound
    text = await history.blob(match.sha, match.path)
    if text is None:
        raise RevisionNotFound
    meta, body = frontmatter.parse(text)
    # Titles are filenames, so a path can be reused by a different post; the id in
    # the file is the only thing that actually proves ownership.
    if meta.get("id") != post_id:
        raise RevisionNotFound
    return match, meta, body, row


async def list_deleted_posts(
    db: aiosqlite.Connection, *, limit: int = 50, include_expiry: bool = False
) -> DeletedPostsResponse:
    """Posts that can be restored but no longer exist.

    History already records every delete and `restore_post` already puts one
    back; the only thing missing was a way to *discover* the id of something you
    deleted. This is that, and nothing more — there is no trash folder and
    delete still unlinks.

    Ids that exist again are dropped: this is a list of what is *gone*, not a
    log of deletions, so a restored post leaves it.

    TTL expiries are excluded by default. This vault sheds fourteen digests a
    week on a rolling schedule, and left in they bury the one accident the view
    exists for.
    """
    if not history.enabled():
        raise HistoryUnavailable()
    found = await history.deletions(limit=limit if include_expiry else limit * 3)
    if not include_expiry:
        found = [d for d in found if d.reason != "expiry"]
    live = {row[0] for row in await (await db.execute("SELECT id FROM posts")).fetchall()}
    items = [
        DeletedPost(
            id=d.post_id, title=d.title, sha=d.sha, short_sha=d.sha[:7],
            when=d.when, reason=d.reason, path=d.path,
        )
        for d in found
        if d.post_id not in live
    ][:limit]
    return DeletedPostsResponse(items=items)


async def get_post_revision(
    db: aiosqlite.Connection, post_id: int, sha: str
) -> PostRevisionContent:
    """A post exactly as it was at one revision — read-only.

    Exists so a restore can be previewed rather than taken on faith: the history
    listing carries only metadata, and picking a sha out of it blind is a poor way
    to undo something. Works for a deleted post too.
    """
    match, meta, body, _row = await _resolve_revision(db, post_id, sha)
    return PostRevisionContent(
        id=post_id,
        sha=match.sha,
        short_sha=match.short_sha,
        when=match.when,
        message=match.message,
        path=match.path,
        title=Path(match.path).stem,
        content=body,
        tags=meta.get("tags") or [],
        source=meta.get("source"),
    )


async def restore_post(db: aiosqlite.Connection, post_id: int, sha: str) -> PostResponse:
    """Roll a post back to an earlier revision, recreating it if it was deleted.

    A restore is an ordinary write, so it is committed like any other — a restore
    can itself be restored. ``history.revisions`` has already verified that every
    revision's front-matter id matches, so a filename later reused by a different
    post cannot smuggle that post's body in under this id; the id is re-checked
    here anyway because this one writes.
    """
    match, meta, body, row = await _resolve_revision(db, post_id, sha)
    title = Path(match.path).stem
    tags = meta.get("tags") or []
    source = meta.get("source")
    expires_at = meta.get("expires_at")
    label = f"post {post_id} restore: {title} (from {match.short_sha})"

    if row is not None:
        # Reuse the normal edit path: it handles the rename back to the old title,
        # rewrites inbound [[wikilinks]], mirrors the index and streams SSE.
        return await update_post(
            db,
            post_id,
            PostUpdate(title=title, content=body, tags=tags, source=source, expires_at=expires_at),
            commit_message=label,
        )

    # Deleted: recreate the file and its index row, keeping the original id so
    # inbound [[links]] and #id references resolve again.
    #
    # Placement goes through move_to_folder rather than old_path. write_file's
    # `exclude=old_path` treats that path as free even when a file sits there, so
    # passing the deleted post's old path would overwrite whatever now owns that
    # filename; folder placement keeps the original directory and still
    # collision-suffixes.
    folder = folders.folder_of(match.path, default=folders.INBOX)
    now = vault.utcnow_iso()
    created_at = meta.get("created_at") or now
    async with vault.write_lock:
        path = vault.write_file(
            id=post_id, title=title, content=body, tags=tags, source=source,
            created_at=created_at, updated_at=now, expires_at=expires_at,
            move_to_folder=folder,
        )
        await vault.index_insert(
            db, id=post_id, title=path.stem, path=path, content=body, tags=tags,
            source=source, created_at=created_at, updated_at=now, expires_at=expires_at,
        )
        await db.commit()
    post = PostResponse.from_row(await _fetch(db, post_id))
    await events.publish(post.model_dump())
    await history.commit(label)
    return post


