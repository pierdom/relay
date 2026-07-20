"""The /metrics endpoint — Prometheus text exposition, auth-gated, and the
counters/gauges it exposes actually move with real activity."""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from relay import metrics
from relay.auth import require_api_key
from relay.config import settings
from relay.main import app

AUTH = {"Authorization": "Bearer test-key"}


@pytest_asyncio.fixture
async def vault_dir(tmp_path, monkeypatch):
    from relay import database

    vp = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", str(vp))
    await database.init_db()
    return vp


@pytest_asyncio.fixture
async def client(vault_dir):
    async def override_auth():
        return None

    app.dependency_overrides[require_api_key] = override_auth
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _sample(text: str, metric: str) -> float | None:
    """First value of a metric line (optionally with labels) in exposition text."""
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        if name == metric:
            return float(line.rsplit(" ", 1)[1])
    return None


@pytest.mark.asyncio
async def test_metrics_requires_auth():
    """No dependency override here — a bare request must be rejected."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/metrics")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_metrics_exposition_shape(client):
    r = await client.get("/metrics", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    body = r.text
    # HELP/TYPE headers present for the core families.
    assert "# TYPE relay_http_requests_total counter" in body
    assert "# TYPE relay_posts_total gauge" in body
    assert "# HELP relay_build_info" in body
    # build_info carries the version label and value 1.
    assert 'relay_build_info{version="' in body
    assert _sample(body, "relay_build_info") == 1


@pytest.mark.asyncio
async def test_posts_gauge_tracks_vault(client):
    before = _sample((await client.get("/metrics", headers=AUTH)).text, "relay_posts_total")
    # The master doc (id=0) is seeded at startup, so the count is already ≥1.
    await client.post("/posts", json={"title": "m1", "content": "x", "tags": ["dev"]}, headers=AUTH)
    await client.post("/posts", json={"title": "m2", "content": "y", "tags": ["dev"]}, headers=AUTH)
    after = _sample((await client.get("/metrics", headers=AUTH)).text, "relay_posts_total")
    assert after == before + 2


@pytest.mark.asyncio
async def test_tags_gauge_counts_distinct(client):
    await client.post("/posts", json={"title": "t1", "content": "x", "tags": ["alpha", "beta"]}, headers=AUTH)
    await client.post("/posts", json={"title": "t2", "content": "y", "tags": ["beta"]}, headers=AUTH)
    body = (await client.get("/metrics", headers=AUTH)).text
    # alpha + beta = 2 distinct, regardless of how many posts carry them.
    assert _sample(body, "relay_tags_total") == 2


@pytest.mark.asyncio
async def test_http_requests_counter_increments(client):
    metrics.http_requests.inc(method="GET", path="/probe", status="200")
    body = (await client.get("/metrics", headers=AUTH)).text
    assert 'relay_http_requests_total{method="GET",path="/probe",status="200"}' in body


@pytest.mark.asyncio
async def test_search_counter_increments(client):
    start = metrics.search_queries.family()[3]
    start_val = start[0][1] if start else 0
    await client.get("/posts", params={"search": "anything"}, headers=AUTH)
    end = metrics.search_queries.family()[3][0][1]
    assert end == start_val + 1


def test_label_escaping():
    # Quotes/backslashes/newlines in label values must be escaped so the line parses.
    fam = ("m", "help", "counter", [({"k": 'a"b\\c\nd'}, 1.0)])
    out = metrics.render([fam])
    assert 'k="a\\"b\\\\c\\nd"' in out


def test_render_integer_formatting():
    fam = metrics.gauge("g", "help", 5.0)
    assert "g 5\n" in metrics.render([fam])
