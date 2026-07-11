from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from relay import vault
from relay.auth import require_api_key
from relay.config import settings
from relay.database import init_db
from relay.main import app

AUTH = {"Authorization": "Bearer test-key"}


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", str(vault_root))
    await init_db()

    # Seed an attachment under a domain folder's assets/ dir.
    assets = vault_root / "Homelab" / "assets"
    assets.mkdir(parents=True)
    (assets / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (assets / "notes.pdf").write_bytes(b"%PDF-1.4 fake")

    async def override_auth():
        return None

    app.dependency_overrides[require_api_key] = override_auth
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ── pure resolver ─────────────────────────────────────────────────────────────


def test_resolve_bare_filename_under_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    assets = tmp_path / "Radio" / "assets"
    assets.mkdir(parents=True)
    f = assets / "antenna.jpg"
    f.write_bytes(b"x")
    assert vault.resolve_attachment("antenna.jpg") == f.resolve()


def test_resolve_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    assert vault.resolve_attachment("../secret.txt") is None


def test_resolve_rejects_relay_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    relay = tmp_path / "vault" / ".relay"
    relay.mkdir(parents=True)
    (relay / "index.db").write_text("db")
    assert vault.resolve_attachment(".relay/index.db") is None


def test_resolve_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    assert vault.resolve_attachment("ghost.png") is None


def test_resolve_glob_metachar_is_literal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    assets = tmp_path / "Homelab" / "assets"
    assets.mkdir(parents=True)
    (assets / "a.png").write_bytes(b"x")
    (assets / "b.png").write_bytes(b"x")
    # "*.png" must be a literal filename, never a glob pattern → no match.
    assert vault.resolve_attachment("*.png") is None


def test_resolve_absolute_path_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    assert vault.resolve_attachment("/etc/hostname") is None


# ── endpoint ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_attachment_image(client):
    r = await client.get("/attachments/diagram.png", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.content.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_get_attachment_pdf(client):
    r = await client.get("/attachments/notes.pdf", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"


@pytest.mark.asyncio
async def test_get_attachment_missing_404(client):
    r = await client.get("/attachments/nope.png", headers=AUTH)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_attachment_traversal_404(client):
    r = await client.get("/attachments/../../secret.txt", headers=AUTH)
    assert r.status_code in (400, 404)
