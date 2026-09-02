"""Fixtures for the search-quality eval harness.

Deliberately opt-in: these tests need the *real* vault's content (golden.yaml's
expected ids only mean anything against real posts), pulled read-only over REST
via ``scripts/export_vault.fetch_all`` into a throwaway snapshot — never by
pointing ``settings.vault_path`` at the live vault (that's what the root
``isolated_vault`` fixture in tests/conftest.py exists to prevent). Pointing
this at a live relay is gated behind two env vars distinct from the app's own
``RELAY_BASE_URL``/``API_KEY``, so it never fires by accident in CI or on a dev
box that just has a normal ``.env``.
"""
from __future__ import annotations

import os

import aiosqlite
import pytest
import pytest_asyncio

EVAL_URL = os.environ.get("RELAY_EVAL_URL")
EVAL_KEY = os.environ.get("RELAY_EVAL_KEY")


def pytest_collection_modifyitems(config, items):
    if EVAL_URL and EVAL_KEY:
        return
    skip = pytest.mark.skip(
        reason="RELAY_EVAL_URL/RELAY_EVAL_KEY not set — point them at a live relay to run the eval suite"
    )
    for item in items:
        if "tests/eval/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(skip)


@pytest_asyncio.fixture
async def live_snapshot(tmp_path, monkeypatch):
    """Export the live relay's posts into a throwaway vault under ``tmp_path``,
    build its index — embedding every post with the **real** backend (relay
    #253 phases 2-4), so this is where a real model actually downloads/runs,
    never in default CI — and hand back an open connection to query it."""
    from relay import database, vault, vectors
    from relay.config import settings
    from scripts.export_vault import backfill_title, fetch_all

    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "snapshot"))
    monkeypatch.setattr(settings, "embedding_enabled", True)

    posts = fetch_all(EVAL_URL.rstrip("/"), EVAL_KEY)
    for p in posts:
        title = vault.MASTER_TITLE if p["id"] == vault.MASTER_ID else backfill_title(p)
        vault.write_file(
            id=p["id"],
            title=title,
            content=p.get("content", ""),
            tags=p.get("tags", []),
            source=p.get("source"),
            created_at=p.get("created_at") or vault.utcnow_iso(),
            updated_at=p.get("updated_at"),
            expires_at=p.get("expires_at"),
        )
    await database.init_db()

    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        if database.VEC_ENABLED:
            await vectors.load_extension(db)
        yield db
