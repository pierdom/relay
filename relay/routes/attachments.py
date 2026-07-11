from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from .. import vault
from ..auth import require_api_key

router = APIRouter(tags=["attachments"])


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
    return FileResponse(path)
