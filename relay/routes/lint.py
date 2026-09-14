"""GET /lint — vault lint (relay #198, N-5)."""
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends

from .. import lint as lint_module
from ..auth import require_api_key
from ..database import get_db
from ..models import LintReport

router = APIRouter(tags=["lint"])


@router.get("/lint", response_model=LintReport, dependencies=[Depends(require_api_key)])
async def get_lint(db: aiosqlite.Connection = Depends(get_db)) -> LintReport:
    """Check the vault against the rules already written down in #0 — tag
    axes, folder placement, the H1/title convention, broken cross-links,
    embedding coverage — instead of relying on someone reading every post.
    The master document (id=0) is exempt from the tag/folder/H1/staleness/
    embedding-coverage rules, but not from the link checks."""
    return await lint_module.run(db)
