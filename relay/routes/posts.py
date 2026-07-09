from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

import aiosqlite

from .. import service
from ..auth import require_api_key
from ..database import get_db
from ..models import BacklinksResponse, PostCreate, PostListResponse, PostResponse, PostUpdate

router = APIRouter(prefix="/posts", tags=["posts"])


@router.post(
    "",
    response_model=PostResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
async def create_post(
    body: PostCreate,
    db: aiosqlite.Connection = Depends(get_db),
) -> PostResponse:
    return await service.create_post(db, body)


@router.get(
    "",
    response_model=PostListResponse,
    dependencies=[Depends(require_api_key)],
)
async def list_posts(
    tag: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
    db: aiosqlite.Connection = Depends(get_db),
) -> PostListResponse:
    return await service.list_posts(
        db, tag=tag, limit=limit, offset=offset, search=search
    )


@router.get(
    "/{post_id}",
    response_model=PostResponse,
    dependencies=[Depends(require_api_key)],
)
async def get_post(
    post_id: int,
    db: aiosqlite.Connection = Depends(get_db),
) -> PostResponse:
    post = await service.get_post(db, post_id)
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return post


@router.get(
    "/{post_id}/backlinks",
    response_model=BacklinksResponse,
    dependencies=[Depends(require_api_key)],
)
async def get_backlinks(
    post_id: int,
    db: aiosqlite.Connection = Depends(get_db),
) -> BacklinksResponse:
    try:
        return await service.get_backlinks(db, post_id)
    except service.PostNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")


@router.patch(
    "/{post_id}",
    response_model=PostResponse,
    dependencies=[Depends(require_api_key)],
)
async def update_post(
    post_id: int,
    body: PostUpdate,
    db: aiosqlite.Connection = Depends(get_db),
) -> PostResponse:
    try:
        return await service.update_post(db, post_id, body)
    except service.PostNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")


@router.delete(
    "/{post_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_api_key)],
)
async def delete_post(
    post_id: int,
    db: aiosqlite.Connection = Depends(get_db),
) -> None:
    try:
        await service.delete_post(db, post_id)
    except service.ProtectedPost:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Master document (id=0) cannot be deleted",
        )
    except service.PostNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
