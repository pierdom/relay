"""POST /embeddings/backfill and PATCH /embeddings — runtime control over the
semantic-search subsystem (relay #253, v1.3.0), alongside GET /status's
existing read-only diagnostics (``status.embedding_status``).
"""
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, Query, status

from .. import status as status_module
from ..auth import require_api_key
from ..database import get_db
from ..identity import Actor
from ..models import EmbeddingStatus, EmbeddingToggle

router = APIRouter(prefix="/embeddings", tags=["embeddings"])


@router.post(
    "/backfill",
    response_model=EmbeddingStatus,
    status_code=status.HTTP_202_ACCEPTED,
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
    actor: Actor = Depends(require_api_key),
) -> EmbeddingStatus:
    return await status_module.trigger_backfill(db, force=force, actor=actor)


@router.patch("", response_model=EmbeddingStatus)
async def set_enabled(
    body: EmbeddingToggle,
    db: aiosqlite.Connection = Depends(get_db),
    actor: Actor = Depends(require_api_key),
) -> EmbeddingStatus:
    return await status_module.set_embeddings_enabled(db, body.enabled, actor=actor)
