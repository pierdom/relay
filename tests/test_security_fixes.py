"""Regression tests for the security audit fixes (AUDIT.md, Phase 1).

One focused test per finding: attachments that browsers execute are forced to
download (S-01/S-14), `folder` cannot escape the vault (S-02), the UI ships its
own sanitiser under a CSP (S-03), a non-ASCII bearer is a 401 not a 500 (S-04),
MCP paging is bounded (S-07), cookie-authenticated writes reject cross-site
requests (S-11), and `%`/`_` in tag/folder filters are literal (S-16).
"""
from __future__ import annotations

import base64
import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay import database
from relay.auth import create_session, require_api_key
from relay.config import settings
from relay.main import app

AUTH = {"Authorization": "Bearer test-key"}


@pytest_asyncio.fixture
async def vault_dir(tmp_path, monkeypatch):
    vp = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", str(vp))
    await database.init_db()
    return vp


@pytest_asyncio.fixture
async def client(vault_dir):
    """Real auth, no dependency override — these tests are about the gate."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(require_api_key, None)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def _upload(client, name: str, data: bytes = b"x", **extra) -> dict:
    r = await client.post("/attachments", json={"filename": name, "data": _b64(data), **extra}, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()


# ── S-01 / S-14: active document types download, by MIME not by suffix ───────


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["evil.xht", "evil.svgz", "evil.shtml", "evil.xsl", "evil.svg", "evil.html"])
async def test_active_document_attachments_are_forced_to_download(client, name):
    await _upload(client, name, b"<script>alert(1)</script>")
    r = await client.get(f"/attachments/{name}", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_images_and_pdfs_still_render_inline(client):
    await _upload(client, "pic.png", b"\x89PNG")
    r = await client.get("/attachments/pic.png", headers=AUTH)
    assert r.status_code == 200
    assert "content-disposition" not in r.headers
    await _upload(client, "doc.pdf", b"%PDF-1.4")
    r = await client.get("/attachments/doc.pdf", headers=AUTH)
    assert "content-disposition" not in r.headers


@pytest.mark.asyncio
async def test_forced_download_survives_a_non_ascii_filename(client):
    stored = (await _upload(client, "gràfic — 1.svg", b"<svg/>"))["filename"]
    r = await client.get(f"/attachments/{stored}", headers=AUTH)
    assert r.status_code == 200
    assert "filename*=utf-8''" in r.headers["content-disposition"]


# ── S-02: folder cannot traverse ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", ["..", "../..", ".", ".relay", ".obsidian", "a/../.."])
async def test_attachment_folder_cannot_escape_the_vault(client, vault_dir, folder):
    r = await client.post(
        "/attachments", json={"filename": "x.txt", "data": _b64(b"hi"), "folder": folder}, headers=AUTH
    )
    assert r.status_code in (400, 422), r.text
    assert not (vault_dir.parent / "assets").exists()
    assert not (vault_dir / ".relay" / "assets").exists()
    r = await client.get("/attachments", params={"folder": folder}, headers=AUTH)
    assert r.status_code in (400, 422)


# ── S-03: vendored sanitiser + CSP ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_ui_shell_ships_its_own_libraries_under_a_csp(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "cdn.jsdelivr.net" not in r.text
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    # The one inline script (theme bootstrap) is allowed by hash, not by 'unsafe-inline'.
    assert "'sha256-" in csp
    for path in ("vendor/marked.min.js", "vendor/purify.min.js"):
        v = await client.get(f"/static/v/{path}")
        assert v.status_code == 200, path


# ── S-04: bearer comparison never raises ─────────────────────────────────────


async def _asgi_status(method: str, path: str, headers: list[tuple[bytes, bytes]], body: bytes = b"") -> list[int]:
    """Drive the ASGI app directly: httpx refuses non-ASCII header values, but a
    real client can put the latin-1 bytes on the wire."""
    scope = {
        "type": "http", "method": method, "path": path, "query_string": b"", "scheme": "http",
        "server": ("test", 80), "client": ("c", 1), "headers": headers,
    }
    statuses: list[int] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            statuses.append(msg["status"])

    await app(scope, receive, send)
    return statuses


@pytest.mark.asyncio
async def test_non_ascii_bearer_is_401_not_500(client):
    bad = [(b"authorization", "Bearer caf\xe9".encode("latin-1")), (b"content-type", b"application/json")]
    assert await _asgi_status("GET", "/posts", bad) == [401]
    assert await _asgi_status("GET", "/events", bad) == [401]
    assert await _asgi_status("POST", "/mcp", bad, b"{}") == [401]
    assert await _asgi_status("POST", "/session", bad, b"{}") == [401]


# ── S-07: MCP paging is bounded like REST ────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_list_posts_clamps_limit_and_offset(client):
    from relay.mcp_server import list_posts

    for _ in range(3):
        await client.post("/posts", json={"title": f"p{_}", "content": "b"}, headers=AUTH)
    out = await list_posts(limit=100000, offset=-5)
    assert out["limit"] == 100
    assert out["offset"] == 0
    out = await list_posts(limit=-1)
    assert out["limit"] == 1
    assert len(out["items"]) == 1


# ── S-11: cookie-authenticated writes reject cross-site requests ─────────────


@pytest.mark.asyncio
async def test_cross_site_cookie_write_is_rejected(client):
    cookies = {"relay_session": create_session()}
    body = {"title": "csrf", "content": "b"}
    r = await client.post("/posts", json=body, cookies=cookies, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = await client.post("/posts", json=body, cookies=cookies, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    # Same-origin browser traffic and reads are untouched.
    r = await client.post("/posts", json=body, cookies=cookies, headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://test"})
    assert r.status_code == 201, r.text
    r = await client.get("/posts", cookies=cookies, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200
    # A bearer is not a browser credential; no CSRF check applies to it.
    r = await client.post("/posts", json=body, headers={**AUTH, "Origin": "https://evil.example"})
    assert r.status_code == 201


# ── S-16: LIKE wildcards in filters are literal ──────────────────────────────


@pytest.mark.asyncio
async def test_like_wildcards_in_tag_and_folder_filters_are_literal(client):
    await client.post("/posts", json={"title": "a", "content": "b", "tags": ["homelab"]}, headers=AUTH)
    await client.post("/posts", json={"title": "b", "content": "b", "tags": ["finance"]}, headers=AUTH)
    r = await client.get("/posts", params={"folder": "%"}, headers=AUTH)
    assert r.json()["total"] == 0
    r = await client.get("/posts", params={"tag": "%"}, headers=AUTH)
    assert r.json()["total"] == 0
    r = await client.get("/posts", params={"tag": "home_ab"}, headers=AUTH)
    assert r.json()["total"] == 0
    r = await client.get("/posts", params={"folder": "Homelab"}, headers=AUTH)
    assert r.json()["total"] == 1
