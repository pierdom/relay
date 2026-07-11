from __future__ import annotations

import base64
import binascii

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from .. import service, vault
from ..auth import require_api_key
from ..database import get_db
from ..models import AttachmentCreate, AttachmentResponse

router = APIRouter(tags=["attachments"])


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
        data = base64.b64decode(body.data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="data is not valid base64")
    try:
        return await service.add_attachment(
            db, filename=body.filename, data=data, post_id=body.post_id, folder=body.folder
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
