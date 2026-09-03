"""POST /embeddings/backfill and PATCH /embeddings — runtime control over the
semantic-search subsystem (relay #253, v1.3.0), alongside GET /status's
existing read-only diagnostics (``status.embedding_status``).
"""
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, status

from .. import status as status_module
from ..auth import require_api_key
from ..database import get_db
from ..models import EmbeddingStatus, EmbeddingToggle

router = APIRouter(prefix="/embeddings", tags=["embeddings"])


@router.post(
    "/backfill",
    response_model=EmbeddingStatus,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def trigger_backfill(
    force: bool = Query(
        default=False,
        description=(
            "Wipe every embedded chunk/vector/cache row first and re-embed from scratch, instead of "
            "resuming from the content-addressed cache."
        ),
    ),
    db: aiosqlite.Connection = Depends(get_db),
) -> EmbeddingStatus:
    try:
        return await status_module.trigger_backfill(db, force=force)
    except status_module.EmbeddingsUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Semantic search is not enabled on this relay",
        ) from None
    except status_module.BackfillAlreadyRunning:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A backfill is already running",
        ) from None


@router.patch("", response_model=EmbeddingStatus, dependencies=[Depends(require_api_key)])
async def set_enabled(
    body: EmbeddingToggle,
    db: aiosqlite.Connection = Depends(get_db),
) -> EmbeddingStatus:
    try:
        return await status_module.set_embeddings_enabled(db, body.enabled)
    except status_module.EmbeddingsUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="sqlite-vec is not available on this relay, or EMBEDDING_MODEL is not a known fastembed model",
        ) from None
    except status_module.EmbeddingDimensionMismatch:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "EMBEDDING_MODEL's dimension doesn't match the vector schema already on disk. "
                "Restart relay to rebuild it before enabling."
            ),
        ) from None
