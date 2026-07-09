from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends

from .. import service
from ..auth import require_api_key
from ..database import get_db
from ..models import FolderListResponse

router = APIRouter(tags=["folders"])


@router.get(
    "/folders",
    response_model=FolderListResponse,
    dependencies=[Depends(require_api_key)],
)
async def list_folders(
    db: aiosqlite.Connection = Depends(get_db),
) -> FolderListResponse:
    """First-level vault folders with post counts — for the sidebar tree view."""
    return await service.list_folders(db)
