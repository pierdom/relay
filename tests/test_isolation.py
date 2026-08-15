"""The autouse ``isolated_vault`` fixture is a safety property, so it gets tests.

These deliberately do **not** patch ``vault_path`` themselves — that's the whole
point: a test which forgets must still land in a throwaway vault rather than the
developer's real one.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from relay import database
from relay.auth import require_api_key
from relay.config import Settings, settings
from relay.main import app

AUTH = {"Authorization": "Bearer test-key"}


def test_vault_and_every_derived_path_are_isolated(tmp_path):
    root = str(tmp_path)
    for name, value in (
        ("vault_path", settings.vault_path),
        ("relay_dir", settings.relay_dir),
        ("database_path", settings.database_path),
        ("tags_config_path", settings.tags_config_path),
        ("uploads_dir", settings.uploads_dir),
        ("mcp_oauth_db_path", settings.mcp_oauth_db_path),
    ):
        assert value.startswith(root), f"{name} escaped tmp_path: {value!r}"


def test_configured_vault_path_is_overridden():
    """Whatever the machine's .env / RELAY_VAULT_PATH says, tests don't use it."""
    assert settings.vault_path != Settings().vault_path


@pytest.mark.asyncio
async def test_a_write_from_an_unpatched_test_lands_in_the_throwaway_vault(tmp_path):
    """End-to-end: the scenario that used to be dangerous. No vault_path patching
    here, yet the post's canonical file must be written under tmp_path."""
    # Checked *before* writing: if the fixture ever stops working, this test would
    # otherwise create a post in whatever real vault .env points at — exactly the
    # accident it exists to prevent. Fail dry instead.
    assert settings.vault_path.startswith(str(tmp_path)), "isolated_vault fixture is not active"

    await database.init_db()
    app.dependency_overrides[require_api_key] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/posts", json={"title": "Isolated", "content": "x", "tags": ["homelab"]}, headers=AUTH
            )
            assert r.status_code == 201, r.text
    finally:
        app.dependency_overrides.clear()

    written = list(Path(settings.vault_path).rglob("Isolated.md"))
    assert written, "post file was not written into the isolated vault"
    assert str(written[0]).startswith(str(tmp_path))
