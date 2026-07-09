from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends

from .. import service
from ..auth import require_api_key
from ..database import get_db
from ..models import LinkIndexResponse

router = APIRouter(tags=["links"])


@router.get(
    "/links",
    response_model=LinkIndexResponse,
    dependencies=[Depends(require_api_key)],
)
async def link_index(
    db: aiosqlite.Connection = Depends(get_db),
) -> LinkIndexResponse:
    """(id, title) for every post — clients resolve ``[[Title]]`` wikilinks with this."""
    return await service.link_index(db)
