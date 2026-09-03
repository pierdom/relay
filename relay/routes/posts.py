from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, status

from .. import service
from ..auth import require_api_key
from ..database import get_db
from ..models import (
    BacklinksResponse,
    DeletedPostsResponse,
    PostCreate,
    PostHistoryResponse,
    PostListResponse,
    PostResponse,
    PostRestore,
    PostRevisionContent,
    PostSummaryListResponse,
    PostUpdate,
)

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
    response_model=PostListResponse | PostSummaryListResponse,
    dependencies=[Depends(require_api_key)],
)
async def list_posts(
    tag: str | None = Query(default=None),
    folder: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
    summary: bool = Query(
        default=False,
        description="Return metadata-only items (id/title/tags/folder + excerpt), no full content.",
    ),
    sort: str = Query(
        default="updated",
        pattern="^(created|updated)$",
        description="Sort field: 'updated' (last-modified) or 'created'.",
    ),
    order: str = Query(
        default="desc",
        pattern="^(asc|desc)$",
        description="Sort direction: 'desc' (newest first) or 'asc'.",
    ),
    mode: str = Query(
        default="keyword",
        pattern="^(keyword|semantic|hybrid)$",
        description=(
            "Ranking mode for 'search' (relay #253, proof of concept): 'keyword' (default, FTS5/bm25), "
            "'semantic' (embedding similarity), or 'hybrid' (RRF fusion of both) — can be combined with "
            "'tag'/'folder'. 'semantic'/'hybrid' 503 if this relay hasn't got embeddings enabled."
        ),
    ),
    db: aiosqlite.Connection = Depends(get_db),
) -> PostListResponse | PostSummaryListResponse:
    try:
        return await service.list_posts(
            db,
            tag=tag,
            folder=folder,
            limit=limit,
            offset=offset,
            search=search,
            summary=summary,
            sort=sort,
            order=order,
            mode=mode,
        )
    except service.SemanticSearchUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Semantic search is not enabled on this relay",
        ) from None
    except service.InvalidSearchMode:
        # Defensive — Query(pattern=...) above already 422s this before the
        # request reaches service.list_posts. Kept in sync in case that ever
        # changes, and so this path isn't silently different from the
        # in-process MCP server's, which has no such Query-layer validation.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="mode must be 'keyword', 'semantic', or 'hybrid'",
        ) from None


@router.get(
    "/deleted",
    response_model=DeletedPostsResponse,
    dependencies=[Depends(require_api_key)],
)
async def list_deleted_posts(
    limit: int = Query(default=50, ge=1, le=200),
    include_expiry: bool = Query(
        default=False,
        description="Include posts removed by the TTL sweep. Off by default: they are "
                    "intentional and would bury an accidental delete.",
    ),
    db: aiosqlite.Connection = Depends(get_db),
) -> DeletedPostsResponse:
    """Posts that no longer exist but can still be restored.

    ⚠️ **Declared before `/{post_id}` on purpose.** FastAPI matches in
    declaration order and `post_id` is an `int`, so with this route below it
    `/posts/deleted` answers 422 rather than falling through to here.
    """
    try:
        return await service.list_deleted_posts(db, limit=limit, include_expiry=include_expiry)
    except service.HistoryUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vault history is disabled or git is unavailable",
        ) from None


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found") from None


@router.get(
    "/{post_id}/history",
    response_model=PostHistoryResponse,
    dependencies=[Depends(require_api_key)],
)
async def get_post_history(
    post_id: int,
    limit: int = Query(default=20, ge=1, le=200),
    db: aiosqlite.Connection = Depends(get_db),
) -> PostHistoryResponse:
    """Revisions of a post, newest first.

    Answers for a **deleted** post too (`exists: false`) — that's the case worth
    recovering — so this is deliberately not a 404 when the post is gone.
    """
    try:
        return await service.get_post_history(db, post_id, limit=limit)
    except service.HistoryUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vault history is disabled or git is unavailable",
        ) from None


@router.get(
    "/{post_id}/history/{sha}",
    response_model=PostRevisionContent,
    dependencies=[Depends(require_api_key)],
)
async def get_post_revision(
    post_id: int,
    sha: str,
    db: aiosqlite.Connection = Depends(get_db),
) -> PostRevisionContent:
    """The post as it was at one revision, so a restore can be previewed.

    Read-only, and answers for a deleted post too. A short sha is accepted.
    """
    try:
        return await service.get_post_revision(db, post_id, sha)
    except service.HistoryUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vault history is disabled or git is unavailable",
        ) from None
    except service.RevisionNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No revision '{sha}' in the history of post #{post_id}",
        ) from None


@router.post(
    "/{post_id}/restore",
    response_model=PostResponse,
    dependencies=[Depends(require_api_key)],
)
async def restore_post(
    post_id: int,
    body: PostRestore,
    db: aiosqlite.Connection = Depends(get_db),
) -> PostResponse:
    """Roll a post back to a revision from its history, recreating it if deleted.

    The restore is itself committed, so it can be undone the same way.
    """
    try:
        return await service.restore_post(db, post_id, body.sha)
    except service.HistoryUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vault history is disabled or git is unavailable",
        ) from None
    except service.RevisionNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No revision '{body.sha}' in the history of post #{post_id}",
        ) from None


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found") from None


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
        ) from None
    except service.PostNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found") from None
