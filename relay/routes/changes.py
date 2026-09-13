"""GET /changes — the vault changelog (relay #198, N-4)."""
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, status

from .. import changes as changes_module
from ..auth import require_api_key
from ..database import get_db
from ..models import ChangeEntry, ChangeListResponse

router = APIRouter(tags=["changes"])


@router.get(
    "/changes",
    response_model=ChangeListResponse,
    dependencies=[Depends(require_api_key)],
)
async def list_changes(
    since: str | None = Query(
        default=None,
        description="A `seq` from a prior response to page forward, or an ISO 8601 timestamp. "
        "Omit for the most recent `limit`.",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    db: aiosqlite.Connection = Depends(get_db),
) -> ChangeListResponse:
    """Every post-affecting write, newest first — a flat feed of `history.git`
    (create/update/edit/append/delete/restore/tag rename/external edit or
    delete/TTL expiry), so an agent can catch up on what moved since it last
    looked instead of re-reading the whole vault."""
    try:
        rows = await changes_module.list_changes(db, since=since, limit=limit)
    except changes_module.HistoryUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vault history is disabled or git is unavailable",
        ) from None
    return ChangeListResponse(items=[ChangeEntry.from_row(r) for r in rows])
