"""Fixtures for the browser smoke suite.

These drive a **real uvicorn** in a subprocess against a throwaway vault, then a
real Chromium, because the thing under test is the browser UI — an ASGI transport
would exercise the API and prove nothing about the page.

The suite skips cleanly when Playwright's browser binaries are absent, so a
checkout without `playwright install chromium` still runs the rest of the tests.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="pytest-playwright is not installed")


def _chromium_available() -> bool:
    """Whether Playwright's Chromium is actually downloaded.

    Checked at collection time rather than in a fixture: pytest-playwright's own
    session-scoped `browser` fixture launches before any fixture of ours could
    catch the failure, so a checkout without `playwright install chromium` would
    otherwise report nine errors instead of nine skips.
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    if _chromium_available():
        return
    skip = pytest.mark.skip(reason="playwright chromium missing — run `uv run playwright install chromium`")
    for item in items:
        if "tests/ui/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(skip)

API_KEY = "ui-smoke-key"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"relay exited early (rc={proc.returncode})")
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.15)
    raise RuntimeError("relay did not become healthy in time")


@pytest.fixture(scope="session")
def relay_server(tmp_path_factory) -> str:
    """A real relay on a free port, serving a throwaway vault.

    Session-scoped: booting uvicorn per test would dominate the runtime. Tests
    that mutate the vault create their own posts rather than assuming a fixture
    state, so sharing is safe.
    """
    vault = tmp_path_factory.mktemp("ui-vault")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "API_KEY": API_KEY,
        "RELAY_VAULT_PATH": str(vault),
        "SECURE_COOKIES": "false",   # plain http in tests; the cookie must still be set
        "RELAY_HISTORY_ENABLED": "true",   # the post-history panel needs real revisions
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "relay.main:app", "--port", str(port), "--log-level", "warning"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_health(base_url, proc)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def seed(relay_server):
    """Create posts over the API so the feed has something to render."""
    import json

    def _post(path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{relay_server}{path}",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    made = [
        _post("/posts", {"title": f"Smoke Post {i}", "content": f"body number {i}", "tags": ["homelab"]})
        for i in range(3)
    ]
    made.append(_post("/posts", {"title": "Radio Log", "content": "a note about radio", "tags": ["radio"]}))
    # A deliberately hostile card: the grid-tile invariant only bites when a child
    # cannot shrink, so plain posts would make the overflow smoke vacuous. This
    # carries the three things CLAUDE.md names — a long nowrap source, a wide
    # table, and an unbreakable token — plus a long title to squeeze the header.
    made.append(_post("/posts", {
        "title": "Overflow Stress " + "Wide" * 8,
        "content": (
            "| column one | column two | column three | column four | column five |\n"
            "|---|---|---|---|---|\n"
            "| a fairly long cell value | another long one | and a third | and fourth | fifth |\n\n"
            "supercalifragilisticexpialidocious_" + "x" * 90 + "\n"
        ),
        "source": "https://example.com/" + "very-long-path-segment/" * 6,
        "tags": ["homelab"],
    }))
    return made


@pytest.fixture
def page(relay_server, seed, browser):
    """A logged-in page.

    Logs in through the **real API-key form** rather than injecting a cookie, so
    the break-glass auth path is covered by every test that needs a session.
    """
    from playwright.sync_api import Error as PlaywrightError

    try:
        context = browser.new_context(viewport={"width": 1280, "height": 900})
    except PlaywrightError as exc:  # browsers not installed
        pytest.skip(f"playwright browser unavailable: {exc}")
    page = context.new_page()
    page.goto(relay_server)
    key_input = page.locator("#apiKeyInput")
    key_input.wait_for(state="visible", timeout=10_000)
    key_input.fill(API_KEY)
    page.locator("#connectForm button[type=submit], #connectForm button").first.click()
    page.locator("#newPostBtn").wait_for(state="visible", timeout=10_000)
    yield page
    context.close()
