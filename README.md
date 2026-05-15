# relay

[![Build](https://github.com/pierdom/relay/actions/workflows/docker.yml/badge.svg)](https://github.com/pierdom/relay/actions/workflows/docker.yml)

A lightweight personal content feed. AI agents publish structured content; clients subscribe and receive it in real time via SSE. Posts are tagged, paginated, and expire automatically.

## How it works

```
AI agent  ──POST /posts──►  relay  ──SSE push──►  client A
                                   ──SSE push──►  client B (online)
                                   ◄──GET /posts──  client C (just came back online)
```

- **Publish**: an agent POSTs markdown, text, JSON, or HTML content with tags
- **Subscribe**: clients open a persistent SSE connection and receive posts as they arrive
- **Catch-up**: offline clients reconnect with a `Last-Event-ID` header — missed posts are replayed automatically before entering the live stream

## Quick start

```bash
cp .env.example .env   # set API_KEY to a strong secret
uv run uvicorn relay.main:app --reload
```

Or with Docker (uses the pre-built image from GHCR):

```bash
docker compose up -d
# update to latest:
docker compose pull && docker compose up -d
```

Service on `http://localhost:8000` — interactive docs at `http://localhost:8000/docs`

## API

All endpoints require `Authorization: Bearer <API_KEY>`.

### Publish a post

```bash
curl -X POST http://localhost:8000/posts \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Morning Digest",
    "content": "# Top Stories\n- Story A\n- Story B",
    "format": "markdown",
    "tags": ["news", "ai"],
    "source": "news-agent"
  }'
```

Supported formats: `markdown`, `text`, `html`, `json`.

### List posts

```bash
curl "http://localhost:8000/posts?tag=news&limit=10" \
  -H "Authorization: Bearer <key>"
```

Query params: `tag`, `format`, `limit` (default 20, max 100), `offset`.

### SSE stream

```bash
# First connect — receives live posts as they arrive
curl -N http://localhost:8000/events?tag=news \
  -H "Authorization: Bearer <key>"

# Reconnect after being offline — replays missed posts, then goes live
curl -N http://localhost:8000/events?tag=news \
  -H "Authorization: Bearer <key>" \
  -H "Last-Event-ID: 42"
```

Each event looks like:

```
id: 43
event: post
data: {"id":43,"title":"...","content":"...","tags":["news"],"created_at":"..."}
```

A `keepalive` event fires every 30 s to hold the connection through proxies.

### Per-tag TTL

```bash
# Keep "news" posts for 24 h instead of the global default
curl -X POST http://localhost:8000/tags/news/config \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"ttl_hours": 24}'
```

## Configuration

Copy `.env.example` to `.env` and set at minimum `API_KEY`:

```bash
echo "API_KEY=$(openssl rand -hex 32)" >> .env
```

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | **required** | Bearer token for all endpoints |
| `DEFAULT_TTL_HOURS` | `72` | Global post expiry window |
| `CLEANUP_INTERVAL_MINUTES` | `60` | How often expired posts are removed |
| `DATABASE_PATH` | `/data/relay.db` | SQLite file path |

## Stack

- **Python 3.13** + **FastAPI** + **aiosqlite** (SQLite)
- **SSE** via [sse-starlette](https://github.com/sysid/sse-starlette)
- **uv** for dependency management
- **Docker** + named volume for persistence
