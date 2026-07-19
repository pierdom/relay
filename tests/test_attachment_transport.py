"""source_url fetch + presigned upload-slot transports for add_attachment."""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, MockTransport

from relay import ingest, service
from relay.auth import require_api_key
from relay.config import settings
from relay.database import init_db
from relay.main import app

AUTH = {"Authorization": "Bearer test-key"}


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    await init_db()
    ingest.registry.reset()  # clear the process-global slot registry between tests

    async def override_auth():
        return None

    app.dependency_overrides[require_api_key] = override_auth
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    ingest.registry.reset()


# ── fetch_url unit (mocked transport, guard bypassed) ─────────────────────────


def _mock_client(handler, monkeypatch):
    """Point ingest.fetch_url at an in-memory transport, no DNS, no guard."""
    def factory(timeout):
        return httpx.AsyncClient(transport=MockTransport(handler), timeout=timeout,
                                 follow_redirects=True)
    monkeypatch.setattr(ingest, "_make_client", factory)


@pytest.mark.asyncio
async def test_fetch_url_content_disposition_filename(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"\x89PNGbody",
                              headers={"content-disposition": 'attachment; filename="pic.png"'})
    _mock_client(handler, monkeypatch)
    data, name = await ingest.fetch_url("http://host.test/x", max_bytes=1024)
    assert data == b"\x89PNGbody"
    assert name == "pic.png"


@pytest.mark.asyncio
async def test_fetch_url_derives_name_from_path(monkeypatch):
    _mock_client(lambda r: httpx.Response(200, content=b"data"), monkeypatch)
    _data, name = await ingest.fetch_url("http://host.test/dir/photo.jpg", max_bytes=1024)
    assert name == "photo.jpg"


@pytest.mark.asyncio
async def test_fetch_url_rejects_non_http_scheme():
    with pytest.raises(ingest.FetchError):
        await ingest.fetch_url("ftp://host/x", max_bytes=1024)
    with pytest.raises(ingest.FetchError):
        await ingest.fetch_url("file:///etc/passwd", max_bytes=1024)


@pytest.mark.asyncio
async def test_fetch_url_streamed_overflow(monkeypatch):
    _mock_client(lambda r: httpx.Response(200, content=b"0123456789"), monkeypatch)
    with pytest.raises(ingest.FetchError):
        await ingest.fetch_url("http://host.test/big", max_bytes=4)


@pytest.mark.asyncio
async def test_fetch_url_declared_length_overflow(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"x", headers={"content-length": "999999999"})
    _mock_client(handler, monkeypatch)
    with pytest.raises(ingest.FetchError):
        await ingest.fetch_url("http://host.test/big", max_bytes=4)


@pytest.mark.asyncio
async def test_fetch_url_http_error(monkeypatch):
    _mock_client(lambda r: httpx.Response(404), monkeypatch)
    with pytest.raises(ingest.FetchError):
        await ingest.fetch_url("http://host.test/missing", max_bytes=1024)


def test_guard_host_blocks_metadata_and_loopback():
    # IP literals resolve without DNS, so these run offline.
    for blocked in ("169.254.169.254", "127.0.0.1", "0.0.0.0"):
        with pytest.raises(ingest.FetchError):
            ingest._guard_host(blocked)
    # a public IP passes the guard
    ingest._guard_host("93.184.216.34")


# ── source_url through the REST endpoint (fetch mocked at the seam) ───────────


@pytest.mark.asyncio
async def test_add_attachment_via_source_url(client, monkeypatch):
    async def fake_fetch(url, *, max_bytes, timeout=None):
        assert url == "http://files.test/rack.png"
        return b"\x89PNGfetched", "rack.png"
    monkeypatch.setattr(ingest, "fetch_url", fake_fetch)

    post = (await client.post("/posts", json={"title": "Rack", "content": "Body.",
                                              "tags": ["homelab"]}, headers=AUTH)).json()
    r = await client.post("/attachments",
                          json={"source_url": "http://files.test/rack.png", "post_id": post["id"]},
                          headers=AUTH)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["filename"] == "rack.png"       # derived from the fetch
    assert body["folder"] == "Homelab"
    got = await client.get("/attachments/Homelab/assets/rack.png", headers=AUTH)
    assert got.status_code == 200 and got.content == b"\x89PNGfetched"
    updated = (await client.get(f"/posts/{post['id']}", headers=AUTH)).json()
    assert "![[rack.png]]" in updated["content"]


@pytest.mark.asyncio
async def test_source_url_fetch_failure_is_400(client, monkeypatch):
    async def boom(url, *, max_bytes, timeout=None):
        raise ingest.FetchError("could not fetch source_url: boom")
    monkeypatch.setattr(ingest, "fetch_url", boom)
    r = await client.post("/attachments",
                          json={"source_url": "http://files.test/x.png", "filename": "x.png"},
                          headers=AUTH)
    assert r.status_code == 400
    assert "boom" in r.json()["detail"]


# ── model validation: exactly one byte source ────────────────────────────────


@pytest.mark.asyncio
async def test_no_source_is_rejected(client):
    r = await client.post("/attachments", json={"filename": "x.png"}, headers=AUTH)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_two_sources_is_rejected(client):
    import base64
    b64 = base64.b64encode(b"x").decode()
    r = await client.post("/attachments",
                          json={"filename": "x.png", "data": b64, "source_url": "http://h/x"},
                          headers=AUTH)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_inline_data_requires_filename(client):
    import base64
    b64 = base64.b64encode(b"x").decode()
    r = await client.post("/attachments", json={"data": b64}, headers=AUTH)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_upload_id_requires_filename(client):
    # upload_id carries no name to derive → filename is mandatory (clean 422, not
    # a later resolve-time 400).
    slot = (await client.post("/attachments/uploads", headers=AUTH)).json()
    await client.put(f"/attachments/uploads/{slot['upload_id']}", content=b"x", headers=AUTH)
    r = await client.post("/attachments", json={"upload_id": slot["upload_id"]}, headers=AUTH)
    assert r.status_code == 422


# ── presigned upload slot round-trip ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_presigned_upload_roundtrip(client):
    slot = (await client.post("/attachments/uploads", headers=AUTH)).json()
    assert slot["upload_id"] and slot["upload_url"].endswith(slot["upload_id"])
    assert slot["method"] == "PUT"

    # PUT the raw bytes out-of-band
    put = await client.put(f"/attachments/uploads/{slot['upload_id']}",
                           content=b"\x89PNGuploaded", headers=AUTH)
    assert put.status_code == 200, put.text
    assert put.json()["ready"] is True and put.json()["bytes"] == len(b"\x89PNGuploaded")

    # finalize into a folder
    r = await client.post("/attachments",
                          json={"upload_id": slot["upload_id"], "filename": "up.png",
                                "folder": "Homelab"},
                          headers=AUTH)
    assert r.status_code == 201, r.text
    assert r.json()["filename"] == "up.png" and r.json()["folder"] == "Homelab"
    got = await client.get("/attachments/Homelab/assets/up.png", headers=AUTH)
    assert got.status_code == 200 and got.content == b"\x89PNGuploaded"


@pytest.mark.asyncio
async def test_upload_slot_is_single_use(client):
    slot = (await client.post("/attachments/uploads", headers=AUTH)).json()
    await client.put(f"/attachments/uploads/{slot['upload_id']}", content=b"bytes", headers=AUTH)
    first = await client.post("/attachments",
                              json={"upload_id": slot["upload_id"], "filename": "a.png"},
                              headers=AUTH)
    assert first.status_code == 201
    # slot consumed → second finalize fails loudly
    second = await client.post("/attachments",
                               json={"upload_id": slot["upload_id"], "filename": "b.png"},
                               headers=AUTH)
    assert second.status_code == 400


@pytest.mark.asyncio
async def test_put_to_unknown_slot_404(client):
    r = await client.put("/attachments/uploads/does-not-exist", content=b"x", headers=AUTH)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_add_attachment_unknown_upload_id_400(client):
    r = await client.post("/attachments",
                          json={"upload_id": "nope", "filename": "x.png"}, headers=AUTH)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_put_over_cap_is_413(client, monkeypatch):
    monkeypatch.setattr(settings, "attachment_max_mb", 0)  # 0 bytes → any body too big
    slot = (await client.post("/attachments/uploads", headers=AUTH)).json()
    r = await client.put(f"/attachments/uploads/{slot['upload_id']}", content=b"x", headers=AUTH)
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_purge_expired_drops_stale_slots(client, monkeypatch):
    import time as _time
    slot = (await client.post("/attachments/uploads", headers=AUTH)).json()
    await client.put(f"/attachments/uploads/{slot['upload_id']}", content=b"x", headers=AUTH)
    # force the slot past its TTL, then sweep
    reg = ingest.registry
    reg._slots[slot["upload_id"]].expires_at = _time.time() - 1
    staged = reg._slots[slot["upload_id"]].path
    assert staged.exists()
    assert reg.purge_expired() == 1
    assert not staged.exists()
    # finalize now fails — slot is gone
    r = await client.post("/attachments",
                          json={"upload_id": slot["upload_id"], "filename": "x.png"}, headers=AUTH)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_create_upload_slot_service_shape(client):
    resp = service.create_upload_slot()
    assert resp.upload_id
    assert resp.upload_url.endswith(resp.upload_id)
    assert resp.max_bytes == settings.attachment_max_bytes


# ── stdio proxy add_attachment(path=…) local-file upload ─────────────────────


@pytest.mark.asyncio
async def test_stdio_proxy_path_upload(client, tmp_path, monkeypatch):
    import relay_mcp.server as proxy

    # Route the proxy's httpx calls at the in-process ASGI app (same vault/registry).
    def factory(*a, **k):
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    monkeypatch.setattr(proxy.httpx, "AsyncClient", factory)

    f = tmp_path / "photo.png"
    f.write_bytes(b"\x89PNG-local-file")
    res = await proxy._upload_local_path({"path": str(f), "folder": "Homelab"})
    text = res[0].text
    assert "photo.png" in text and "Homelab/assets" in text
    got = await client.get("/attachments/Homelab/assets/photo.png", headers=AUTH)
    assert got.status_code == 200 and got.content == b"\x89PNG-local-file"


@pytest.mark.asyncio
async def test_stdio_proxy_path_filename_override(client, tmp_path, monkeypatch):
    import relay_mcp.server as proxy

    def factory(*a, **k):
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    monkeypatch.setattr(proxy.httpx, "AsyncClient", factory)

    f = tmp_path / "IMG_4021.HEIC.jpg"
    f.write_bytes(b"jpegbytes")
    res = await proxy._upload_local_path(
        {"path": str(f), "filename": "invoice.jpg", "folder": "Finance"}
    )
    assert "invoice.jpg" in res[0].text
    assert (await client.get("/attachments/Finance/assets/invoice.jpg", headers=AUTH)).status_code == 200


@pytest.mark.asyncio
async def test_stdio_proxy_path_missing_file():
    import relay_mcp.server as proxy

    res = await proxy._upload_local_path({"path": "/no/such/file.png"})
    assert "not found" in res[0].text.lower()
