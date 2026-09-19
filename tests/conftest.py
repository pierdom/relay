"""Shared test fixtures.

The one that matters is ``isolated_vault``: it guarantees no test can touch a
real vault, whatever the machine running the suite has in its ``.env``.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager

# Must precede the `relay.config` import below: Settings requires API_KEY, and CI
# has no .env to supply it.
os.environ.setdefault("API_KEY", "test-key")

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from relay import changes, history, vault
from relay.config import settings


@contextmanager
def _fake_mcp_auth(*, scopes: list[str] = ("relay", "write"), subject: str = "apikey"):
    """Fake an authenticated MCP request context (relay #198, B-8) for a test
    that calls ``mcp_server.mcp.list_tools()``/other fastmcp server-level APIs
    directly, bypassing the real Streamable HTTP transport that would
    otherwise have set this via ``AuthContextMiddleware``. Without it,
    ``get_access_token()`` (which ``AuthMiddleware`` reads to decide what a
    caller may see) finds no token at all and fail-closed denies every
    ``tags={"write"}`` tool — correct behavior for a genuinely unauthenticated
    request, but not what a test asserting "this tool is in the manifest"
    means to exercise. Real MCP calls always carry a token by the time a
    handler runs; this only fills the gap that exists when bypassing the
    transport in-process."""
    token = AccessToken(token="test", client_id="relay", scopes=list(scopes), subject=subject)
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


@pytest.fixture
def mcp_auth_context():
    """Fixture form of ``_fake_mcp_auth`` — injected by name (no import needed,
    ``tests/`` has no ``__init__.py`` so it isn't an importable package) as
    ``with mcp_auth_context(): ...`` in a test that needs a full-access MCP
    caller context."""
    return _fake_mcp_auth


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
    # Fresh, never-yet-contended asyncio.Lock()s per test. `asyncio.Lock` only
    # binds itself to a running event loop the first time it actually has to
    # wait (an uncontended `acquire()` takes a fast path that never touches the
    # loop at all — see `asyncio.locks.Lock.acquire`), and pytest-asyncio gives
    # each test function its own fresh loop. A production process holds one
    # event loop for its whole life, so these singletons are never reused
    # across loops there — but two *different* tests that each genuinely
    # contend one of them (real concurrent writers via `asyncio.gather`) bind
    # it to two different loops, and the second one to run raises "bound to a
    # different event loop". Reusing the module singletons directly, instead
    # of resetting them here, made this a real, order-dependent full-suite
    # failure the first time a second contending test existed (K-2's
    # concurrency regression tests in test_changes.py).
    monkeypatch.setattr(vault, "write_lock", asyncio.Lock())
    monkeypatch.setattr(history, "_lock", asyncio.Lock())
    monkeypatch.setattr(changes, "_lock", asyncio.Lock())
    yield
    # Runs before monkeypatch's undo (this fixture was set up first, so it tears
    # down last), meaning it sees whatever the test left in place — a test that
    # re-patches vault_path somewhere real fails loudly here instead of silently
    # operating on it.
    assert str(settings.vault_path).startswith(str(tmp_path)), (
        f"test pointed settings.vault_path outside tmp_path: {settings.vault_path!r}"
    )
