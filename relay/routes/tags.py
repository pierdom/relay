from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, status

from .. import service
from ..auth import require_api_key
from ..database import get_db
from ..models import TagConfigCreate, TagConfigResponse, TagListResponse, TagRename

router = APIRouter(tags=["tags"])


@router.get(
    "/tags",
    response_model=TagListResponse,
    dependencies=[Depends(require_api_key)],
)
async def list_tags(db: aiosqlite.Connection = Depends(get_db)) -> TagListResponse:
    return await service.list_tags(db)


@router.patch(
    "/tags/{tag}",
    response_model=TagListResponse,
    dependencies=[Depends(require_api_key)],
)
async def rename_tag(
    tag: str,
    body: TagRename,
    db: aiosqlite.Connection = Depends(get_db),
) -> TagListResponse:
    return await service.rename_tag(db, tag, body.new_name)


@router.post(
    "/tags/{tag}/config",
    response_model=TagConfigResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_api_key)],
)
async def set_tag_config(
    tag: str,
    body: TagConfigCreate,
    db: aiosqlite.Connection = Depends(get_db),
) -> TagConfigResponse:
    return await service.set_tag_config(db, tag, body)
