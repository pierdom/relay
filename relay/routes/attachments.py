from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse

from .. import ingest, service, vault
from ..auth import require_api_key
from ..config import settings
from ..database import get_db
from ..models import (
    AttachmentCreate,
    AttachmentDeleteResponse,
    AttachmentListResponse,
    AttachmentResponse,
    UploadSlotResponse,
    UploadStatusResponse,
)

router = APIRouter(tags=["attachments"])


@router.get(
    "/attachments",
    response_model=AttachmentListResponse,
    dependencies=[Depends(require_api_key)],
)
async def list_attachments(
    folder: str | None = None,
    post_id: int | None = None,
    db: aiosqlite.Connection = Depends(get_db),
) -> AttachmentListResponse:
    """List attachments under ``assets/`` dirs (optionally scoped to a folder or post)."""
    try:
        return await service.list_attachments(db, post_id=post_id, folder=folder)
    except service.InvalidFolder as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except service.PostNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Post #{post_id} not found") from None


@router.post(
    "/attachments",
    response_model=AttachmentResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
async def create_attachment(
    body: AttachmentCreate,
    db: aiosqlite.Connection = Depends(get_db),
) -> AttachmentResponse:
    """Store an attachment in a folder's ``assets/``; with ``post_id`` the
    ``![[file]]`` embed is appended to that post's body. The bytes come from
    exactly one of ``data`` (inline base64), ``source_url`` (server fetches), or
    ``upload_id`` (a filled presigned slot — see ``POST /attachments/uploads``)."""
    try:
        return await service.ingest_attachment(
            db, filename=body.filename, data=body.data, source_url=body.source_url,
            upload_id=body.upload_id, post_id=body.post_id, folder=body.folder,
            tags=body.tags, embed=body.embed,
        )
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="data is not valid base64") from None
    except (service.AttachmentSourceError, service.InvalidFolder) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except service.PostNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Post #{body.post_id} not found") from None
    except service.AttachmentError as exc:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc


@router.post(
    "/attachments/uploads",
    response_model=UploadSlotResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
async def create_upload_slot() -> UploadSlotResponse:
    """Mint a presigned upload slot. PUT the raw bytes to the returned
    ``upload_url``, then call ``POST /attachments`` with ``upload_id`` to file it.
    Lets real files reach the vault without base64 in the request body."""
    return service.create_upload_slot()


@router.put(
    "/attachments/uploads/{upload_id}",
    response_model=UploadStatusResponse,
    dependencies=[Depends(require_api_key)],
)
async def put_upload_bytes(upload_id: str, request: Request) -> UploadStatusResponse:
    """Stream raw bytes into a presigned upload slot (single, capped body)."""
    try:
        size = await ingest.stage_upload(
            upload_id, request.stream(), max_bytes=settings.attachment_max_bytes
        )
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"upload slot '{upload_id}' is unknown or expired",
        ) from None
    except ingest.FetchError as exc:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc
    return UploadStatusResponse(upload_id=upload_id, bytes=size, ready=True)


# MIME types a browser may render inline. Everything else is served as a download:
# HTML, XHTML, SVG-as-document, XML/XSLT and any future active type get
# `Content-Disposition: attachment`, so an uploaded file can never execute
# script in the UI's origin. Decided on the *resolved* MIME, not the suffix —
# the old suffix list missed `.xht`, `.svgz`, `.shtml` and `.xsl`, all of which
# `mimetypes` maps to an active type (AUDIT.md S-01).
_INLINE_MIME_PREFIXES = ("image/", "audio/", "video/")
# Unknown types (`application/octet-stream`) download too: the MIME table
# differs between hosts (the slim image has no /etc/mime.types), so "unknown
# here" can be "active there".
_INLINE_MIMES = frozenset({"application/pdf", "text/plain", "text/markdown", "text/csv", "application/json"})


def _renders_inline(mime: str) -> bool:
    if mime == "image/svg+xml":
        return False
    return mime in _INLINE_MIMES or mime.startswith(_INLINE_MIME_PREFIXES)


@router.get(
    "/attachments/{name:path}",
    dependencies=[Depends(require_api_key)],
    include_in_schema=False,
)
async def get_attachment(name: str) -> FileResponse:
    """Serve a vault attachment (image/PDF/…) embedded via Obsidian ``![[file]]``.

    Resolution + path-traversal protection live in ``vault.resolve_attachment``;
    served same-origin so the browser UI's session cookie authenticates ``<img>``.
    Anything a browser would execute (see ``_renders_inline``) is forced to
    download; ``FileResponse(filename=…)`` RFC 5987-encodes non-ASCII names.
    """
    path = vault.resolve_attachment(name)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    mime = vault.attachment_mime(path)
    headers = {"X-Content-Type-Options": "nosniff"}
    if _renders_inline(mime):
        return FileResponse(path, media_type=mime, headers=headers)
    return FileResponse(path, media_type=mime, headers=headers, filename=path.name)


@router.delete(
    "/attachments/{name:path}",
    response_model=AttachmentDeleteResponse,
    dependencies=[Depends(require_api_key)],
)
async def delete_attachment(
    name: str,
    db: aiosqlite.Connection = Depends(get_db),
) -> AttachmentDeleteResponse:
    """Delete an attachment file; reports any posts that still reference it."""
    result = await service.delete_attachment(db, name)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    return result
