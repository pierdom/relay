"""GET /status — runtime diagnostics as JSON.

Bearer-gated for the same reason as ``/metrics``: it reports vault size, the vault
path, and which auth features are live, none of which should be public on a relay
sitting behind an open reverse proxy. ``/health`` stays unauthenticated and
trivial — it is probed every 30s by the Dockerfile HEALTHCHECK and compose, and
must not grow work.
"""
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends

from .. import status as status_module
from ..auth import require_api_key
from ..database import get_db
from ..models import StatusResponse

router = APIRouter(tags=["status"])


@router.get("/status", response_model=StatusResponse, dependencies=[Depends(require_api_key)])
async def get_status(db: aiosqlite.Connection = Depends(get_db)) -> StatusResponse:
    """Version, uptime, vault counts, and which features are *actually* working."""
    return await status_module.build(db)
