"""Shared test fixtures.

The one that matters is ``isolated_vault``: it guarantees no test can touch a
real vault, whatever the machine running the suite has in its ``.env``.
"""
from __future__ import annotations

import os

# Must precede the `relay.config` import below: Settings requires API_KEY, and CI
# has no .env to supply it.
os.environ.setdefault("API_KEY", "test-key")

import pytest

from relay.config import settings


@pytest.fixture(autouse=True)
def isolated_vault(tmp_path, monkeypatch):
    """Point every test at a throwaway vault.

    ``Settings`` loads the developer's real ``.env``, so an unpatched
    ``settings.vault_path`` resolves to whatever ``RELAY_VAULT_PATH`` is set to on
    the machine running the suite — in the usual dev setup, a live Obsidian vault.
    A test that forgets to patch it reads, and on any write path *modifies*, real
    notes. That was a live footgun: isolation was per-test boilerplate, so it was
    only ever one omission away from failing.

    Every vault-derived path (``.relay/index.db``, ``tags.yml``, ``.relay/uploads``,
    ``oauth.db``) hangs off this single setting, so pinning it here isolates all of
    them at once.

    Tests that patch ``vault_path`` themselves still win — they share this
    ``monkeypatch`` instance and apply after this fixture — and the teardown assert
    holds them to somewhere under ``tmp_path``.
    """
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "_vault"))
    # Vault history is on by default in production but off for the suite: it
    # shells out to git on every write, which would make unrelated tests slower
    # and dependent on a git binary. tests/test_history.py turns it back on.
    monkeypatch.setattr(settings, "history_enabled", False)
    yield
    # Runs before monkeypatch's undo (this fixture was set up first, so it tears
    # down last), meaning it sees whatever the test left in place — a test that
    # re-patches vault_path somewhere real fails loudly here instead of silently
    # operating on it.
    assert str(settings.vault_path).startswith(str(tmp_path)), (
        f"test pointed settings.vault_path outside tmp_path: {settings.vault_path!r}"
    )
