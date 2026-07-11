from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from .. import service, vault
from ..auth import require_api_key
from ..database import get_db
from ..models import AttachmentCreate, AttachmentListResponse, AttachmentResponse

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
    except service.PostNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Post #{post_id} not found")


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
    """Store a base64 attachment in a folder's ``assets/``; with ``post_id`` the
    ``![[file]]`` embed is appended to that post's body."""
    try:
        data = service.decode_attachment_b64(body.data)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="data is not valid base64")
    try:
        return await service.add_attachment(
            db, filename=body.filename, data=data, post_id=body.post_id,
            folder=body.folder, tags=body.tags, embed=body.embed,
        )
    except service.PostNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Post #{body.post_id} not found")
    except service.AttachmentError as exc:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc))


@router.get(
    "/attachments/{name:path}",
    dependencies=[Depends(require_api_key)],
    include_in_schema=False,
)
async def get_attachment(name: str) -> FileResponse:
    """Serve a vault attachment (image/PDF/…) embedded via Obsidian ``![[file]]``.

    Resolution + path-traversal protection live in ``vault.resolve_attachment``;
    served same-origin so the browser UI's session cookie authenticates ``<img>``.
    """
    path = vault.resolve_attachment(name)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    # nosniff: don't let the browser MIME-sniff (e.g. a .txt) into executable HTML.
    return FileResponse(path, headers={"X-Content-Type-Options": "nosniff"})
