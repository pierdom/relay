"""Scope-aware UI gating (relay #198, B-8 follow-up).

A read-only key hides `+ New Post` entirely; a write-restricted-to-tags key's
compose Publish button disables/enables live against the Tags field,
mirroring the server's own ALL-of semantics (identity.Actor.can_write_tags).

Runs its own relay subprocess (distinct from `tests/ui/conftest.py`'s shared
session-scoped `relay_server`, which only ever configures the plain,
unscoped `API_KEY`) since these tests need `RELAY_API_KEY_SCOPES` set at
startup.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.ui

RO_KEY = "sk-ui-readonly"
NEWS_KEY = "sk-ui-news"


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


@pytest.fixture(scope="module")
def scoped_relay_server(tmp_path_factory):
    """A relay instance with a read-only key and a tag-restricted key
    configured, distinct from the shared unscoped `relay_server` fixture."""
    vault = tmp_path_factory.mktemp("ui-scope-vault")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "API_KEY": "sk-ui-primary",
        "RELAY_API_KEYS": f"ro-agent:{RO_KEY},news-agent:{NEWS_KEY}",
        "RELAY_API_KEY_SCOPES": "ro-agent:read,news-agent:write:news",
        "RELAY_VAULT_PATH": str(vault),
        "SECURE_COOKIES": "false",
        "RELAY_HISTORY_ENABLED": "false",
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


def _login_as(scoped_relay_server, browser, key: str, **context_args):
    from playwright.sync_api import Error as PlaywrightError

    try:
        context = browser.new_context(**context_args)
    except PlaywrightError as exc:  # browsers not installed
        pytest.skip(f"playwright browser unavailable: {exc}")
    page = context.new_page()
    page.goto(scoped_relay_server)
    key_input = page.locator("#apiKeyInput")
    key_input.wait_for(state="visible", timeout=10_000)
    key_input.fill(key)
    page.locator("#connectForm button[type=submit], #connectForm button").first.click()
    # Not #newPostBtn — a read-only key never shows it. #statusBtn is visible
    # for every authenticated key regardless of scope, so it's the one signal
    # every test below can wait on the same way.
    page.locator("#statusBtn").wait_for(state="visible", timeout=10_000)
    return context, page


def test_read_only_key_hides_new_post(scoped_relay_server, browser):
    context, page = _login_as(scoped_relay_server, browser, RO_KEY)
    try:
        # Login only guarantees the synchronous part of init() has run;
        # scope resolves asynchronously via fetchInitStatus(). Wait on the
        # actual value this test reads (the button's own hidden state), not
        # a proxy for it like "some time has passed since login".
        page.locator("#newPostBtn").wait_for(state="hidden", timeout=10_000)
    finally:
        context.close()


def test_read_only_key_status_panel_shows_read_only(scoped_relay_server, browser):
    context, page = _login_as(scoped_relay_server, browser, RO_KEY)
    try:
        page.locator("#statusBtn").click()
        # Wait on the text this test actually asserts, not a "panel opened"
        # proxy — the /status fetch inside openStatusModal() is itself async,
        # so #smBody briefly shows "loading…" after the modal is visible.
        page.get_by_text("Read-only").wait_for(timeout=10_000)
    finally:
        context.close()


def test_tag_scoped_key_gates_compose_publish_live(scoped_relay_server, browser):
    context, page = _login_as(scoped_relay_server, browser, NEWS_KEY)
    try:
        assert page.locator("#newPostBtn").is_visible()
        page.locator("#newPostBtn").click()
        publish = page.locator("#cpPublish")
        gate_msg = page.locator(".ef-gate-msg")
        # Empty Tags never satisfies a write-restricted key — Publish starts
        # disabled with an explanatory message the moment compose opens.
        publish.wait_for(state="visible", timeout=10_000)
        assert publish.is_disabled()
        assert gate_msg.inner_text().strip() != ""

        page.locator("#cpTags").fill("finance")
        assert publish.is_disabled()

        page.locator("#cpTags").fill("news")
        page.wait_for_function(
            "document.getElementById('cpPublish').disabled === false", timeout=5_000
        )
        assert gate_msg.inner_text().strip() == ""
    finally:
        context.close()
