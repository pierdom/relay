"""Attachments: byte transports, storage under ``<Folder>/assets/``, listing and deletion."""
from __future__ import annotations

import base64
import binascii
import re
from datetime import UTC, datetime

import aiosqlite

from .. import folders, history, ingest, vault
from ..config import settings
from ..models import (
    AttachmentDeleteResponse,
    AttachmentInfo,
    AttachmentListResponse,
    AttachmentResponse,
    PostUpdate,
    UploadSlotResponse,
)
from ._common import AttachmentError, AttachmentSourceError, PostNotFound, _fetch

_EMBED_OR_LINK_RE = re.compile(r"!?\[\[([^\]|#]+?)(?:\|[^\]]*)?\]\]")


def referenced_attachment_names(content: str) -> set[str]:
    """Lower-cased filenames a post's content embeds/links (``![[x]]`` / ``[[x.ext]]``).

    A target is treated as an attachment only when its last path segment has an
    extension — bare ``[[Note Title]]`` wikilinks are ignored.
    """
    names: set[str] = set()
    for m in _EMBED_OR_LINK_RE.finditer(content or ""):
        target = m.group(1).strip()
        if "." in target.rsplit("/", 1)[-1]:
            names.add(target.rsplit("/", 1)[-1].lower())
    return names


async def _all_referenced_attachments(db: aiosqlite.Connection) -> set[str]:
    async with db.execute("SELECT content FROM posts") as cur:
        rows = await cur.fetchall()
    referenced: set[str] = set()
    for row in rows:
        referenced |= referenced_attachment_names(row["content"])
    return referenced



_DATA_URI_RE = re.compile(r"^data:[^;,]*;base64,", re.IGNORECASE)


def decode_attachment_b64(data: str) -> bytes:
    """Decode a client-supplied base64 string, tolerating a ``data:...;base64,``
    prefix and internal whitespace/newlines. Raises ``ValueError`` on bad input."""
    s = _DATA_URI_RE.sub("", (data or "").strip())
    s = re.sub(r"\s+", "", s)
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("data is not valid base64") from exc


async def _resolve_attachment_bytes(
    *,
    filename: str | None,
    data: str | None,
    source_url: str | None,
    upload_id: str | None,
) -> tuple[bytes, str]:
    """Turn whichever transport the caller used into ``(raw_bytes, filename)``.

    - ``data``: inline base64 (``ValueError`` on garbage → 400).
    - ``source_url``: the server fetches it (SSRF-guarded, capped); the name falls
      back to the response's Content-Disposition / URL basename.
    - ``upload_id``: claim a filled presigned slot (single-use).
    """
    if data is not None:
        raw = decode_attachment_b64(data)  # ValueError → 400
        name = filename
    elif source_url is not None:
        try:
            raw, derived = await ingest.fetch_url(
                source_url, max_bytes=settings.attachment_max_bytes
            )
        except ingest.FetchError as exc:
            raise AttachmentSourceError(str(exc)) from exc
        name = filename or derived
    elif upload_id is not None:
        claimed = ingest.registry.claim_slot(upload_id)
        if claimed is None:
            raise AttachmentSourceError(
                f"upload slot '{upload_id}' is unknown, expired, or has no bytes yet"
            )
        raw, name = claimed, filename
    else:
        raise AttachmentSourceError("no attachment source provided")
    if not name:
        raise AttachmentSourceError(
            "filename could not be determined from the source; pass an explicit filename"
        )
    return raw, name


async def ingest_attachment(
    db: aiosqlite.Connection,
    *,
    filename: str | None = None,
    data: str | None = None,
    source_url: str | None = None,
    upload_id: str | None = None,
    post_id: int | None = None,
    folder: str | None = None,
    tags: list[str] | None = None,
    embed: bool = True,
) -> AttachmentResponse:
    """Resolve any of the three byte transports (base64 / source_url / upload_id)
    then store via ``add_attachment``. The single entry point REST + MCP share."""
    raw, name = await _resolve_attachment_bytes(
        filename=filename, data=data, source_url=source_url, upload_id=upload_id
    )
    return await add_attachment(
        db, filename=name, data=raw, post_id=post_id, folder=folder, tags=tags, embed=embed
    )


def create_upload_slot() -> UploadSlotResponse:
    """Mint a presigned upload slot: the caller PUTs raw bytes to ``upload_url``
    out-of-band, then finalizes with ``ingest_attachment(upload_id=…)``. Keeps the
    bytes out of the model context entirely."""
    slot = ingest.registry.create_slot()
    base = settings.relay_base_url.rstrip("/")
    return UploadSlotResponse(
        upload_id=slot.id,
        upload_url=f"{base}/attachments/uploads/{slot.id}",
        max_bytes=settings.attachment_max_bytes,
        expires_at=datetime.fromtimestamp(slot.expires_at, tz=UTC).isoformat(),
    )


async def add_attachment(
    db: aiosqlite.Connection,
    *,
    filename: str,
    data: bytes,
    post_id: int | None = None,
    folder: str | None = None,
    tags: list[str] | None = None,
    embed: bool = True,
) -> AttachmentResponse:
    """Store an attachment in a folder's ``assets/`` dir and return its embed ref.

    Folder precedence: ``post_id`` (the post's own folder) → explicit ``folder`` →
    ``tags`` (same placement policy as a post via ``folders.folder_for``, so a
    compose-time upload lands beside where the note will file) → ``Inbox``.

    With ``post_id`` and ``embed`` true, the ``![[file]]`` embed is also appended to
    the post's body (streamed via SSE). With ``embed`` false (e.g. the UI, which
    inserts the ref itself) the post is left untouched.
    """
    if not data:
        raise AttachmentError("attachment is empty")
    if len(data) > settings.attachment_max_bytes:
        raise AttachmentError(f"attachment exceeds the {settings.attachment_max_mb} MB limit")

    row = None
    if post_id is not None:
        row = await _fetch(db, post_id)
        if row is None:
            raise PostNotFound
        target_folder = folders.folder_of(row["path"], default=folders.INBOX)
    elif folder:
        target_folder = folder
    elif tags:
        target_folder = folders.folder_for(1, tags) or folders.INBOX
    else:
        target_folder = folders.INBOX

    # Serialize name-allocation + write against other writers so two concurrent
    # uploads of the same filename can't resolve to the same path and clobber.
    async with vault.write_lock:
        written = vault.write_attachment(target_folder, filename, data)
    ref = f"![[{written.name}]]"
    # Before the embed below: the upload and the post edit that references it read
    # as two steps in the log instead of the file appearing inside a post update.
    await history.commit(f"attachment add: {written.name}")

    result_post_id = None
    if row is not None and embed:  # append outside the lock — update_post takes it itself
        from .posts import update_post  # posts imports this module; keep the cycle out of import time

        new_content = row["content"].rstrip() + f"\n\n{ref}\n"
        await update_post(db, row["id"], PostUpdate(content=new_content))
        result_post_id = row["id"]

    return AttachmentResponse(
        filename=written.name, ref=ref, folder=target_folder, post_id=result_post_id
    )


async def list_attachments(
    db: aiosqlite.Connection, *, post_id: int | None = None, folder: str | None = None
) -> AttachmentListResponse:
    """List attachment files under ``assets/`` dirs. ``post_id`` scopes to that
    post's folder; ``folder`` scopes to a named folder; neither scans the vault."""
    if post_id is not None:
        row = await _fetch(db, post_id)
        if row is None:
            raise PostNotFound
        folder = folders.folder_of(row["path"], default=folders.INBOX)
    items = [
        AttachmentInfo(filename=n, folder=f, bytes=s, ref=f"![[{n}]]")
        for (n, f, s) in vault.list_attachments(folder)
    ]
    return AttachmentListResponse(items=items)


async def delete_attachment(db: aiosqlite.Connection, name: str) -> AttachmentDeleteResponse | None:
    """Delete an attachment file. Returns the removed name plus any post ids that
    still embed/link it (now dangling), or ``None`` if it didn't resolve."""
    async with vault.write_lock:
        removed = vault.delete_attachment(name)
    if removed is None:
        return None
    fname = removed.name.lower()
    async with db.execute("SELECT id, content FROM posts") as cur:
        rows = await cur.fetchall()
    referenced_by = [r["id"] for r in rows if fname in referenced_attachment_names(r["content"])]
    await history.commit(f"attachment delete: {removed.name}")
    return AttachmentDeleteResponse(filename=removed.name, referenced_by=sorted(referenced_by))

