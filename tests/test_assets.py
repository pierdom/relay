"""Versioned asset URLs.

Splitting the UI out of index.html introduced a version skew that could not exist
when everything was inline: `/` always revalidates, but a proxy in front of relay
may cache /static hard (bespin's adds ~4h of `max-age`), so a deploy could hand a
browser the new markup with the previous release's script — a button present with
no handler behind it. That happened in production.

The version lives in the URL *path* rather than a query string because `main.js`
does `import './status.js'`: a `?v=` on the entry point does not reach its
imports, a path segment does.
"""
from __future__ import annotations

import os
import re

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay.main import app, asset_version


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _shell(client) -> str:
    r = await client.get("/")
    assert r.status_code == 200
    return r.text


@pytest.mark.asyncio
async def test_shell_stamps_the_asset_version_into_its_urls(client):
    html = await _shell(client)
    version = asset_version()
    assert "__ASSETS__" not in html, "placeholder was not substituted"
    assert f"/static/{version}/app.css" in html
    assert f"/static/{version}/js/main.js" in html


@pytest.mark.asyncio
async def test_shell_is_not_cacheable(client):
    """It carries the current asset version, so a cached copy would pin the old one."""
    r = await client.get("/")
    assert r.headers.get("cache-control") == "no-cache"


@pytest.mark.asyncio
async def test_versioned_assets_are_served_immutable(client):
    version = asset_version()
    for path in ("app.css", "js/main.js", "js/post-history.js"):
        r = await client.get(f"/static/{version}/{path}")
        assert r.status_code == 200, path
        assert "immutable" in r.headers.get("cache-control", ""), path


@pytest.mark.asyncio
async def test_every_module_resolves_under_the_versioned_path(client):
    """Relative imports resolve against the versioned directory, so each module
    must be reachable there — that is the whole reason the version is a path
    segment and not a query string."""
    version = asset_version()
    main = (await client.get(f"/static/{version}/js/main.js")).text
    imports = set(re.findall(r"from '\./([\w-]+\.js)'", main)) | set(
        re.findall(r"import '\./([\w-]+\.js)'", main)
    )
    assert imports, "no relative imports found — has main.js changed shape?"
    for module in sorted(imports):
        r = await client.get(f"/static/{version}/js/{module}")
        assert r.status_code == 200, f"{module} unreachable under the versioned path"


@pytest.mark.asyncio
async def test_unversioned_urls_still_work(client):
    """A browser holding a cached index.html still asks for these; 404ing them
    would break its UI outright until that cache expired."""
    for path in ("app.css", "js/main.js", "js/status.js"):
        r = await client.get(f"/static/{path}")
        assert r.status_code == 200, path
        assert "immutable" not in r.headers.get("cache-control", ""), path


@pytest.mark.asyncio
async def test_the_version_changes_when_an_asset_changes(client, tmp_path, monkeypatch):
    from relay import main as main_module

    before = asset_version()
    ui = tmp_path / "ui"
    (ui / "js").mkdir(parents=True)
    (ui / "app.css").write_text("body{}")
    monkeypatch.setattr(main_module, "_UI_DIR", ui)
    asset_version.cache_clear()
    try:
        changed = asset_version()
        assert changed != before
        (ui / "app.css").write_text("body{color:red}")
        asset_version.cache_clear()
        assert asset_version() != changed, "content change did not move the version"
    finally:
        asset_version.cache_clear()


@pytest.mark.asyncio
async def test_traversal_outside_the_ui_dir_is_refused(client):
    version = asset_version()
    for attack in ("%2e%2e%2f%2e%2e%2fmain.py", "..%2f..%2fconfig.py"):
        r = await client.get(f"/static/{version}/{attack}")
        assert r.status_code == 404, attack
