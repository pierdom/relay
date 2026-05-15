# relay — Claude Code guide

Personal HTTP content feed. AI agents POST structured content (news digests, etc.); clients subscribe via SSE and receive it in real time. Posts are tagged, paginated, and expire via configurable TTL.

## Running locally

```bash
cp .env.example .env   # set API_KEY
uv run uvicorn relay.main:app --reload
```

## Docker

```bash
docker compose up -d
# to update to the latest image:
docker compose pull && docker compose up -d
```

Service on http://localhost:8000 — interactive docs at http://localhost:8000/docs

## API

All endpoints require `Authorization: Bearer <API_KEY>`.

| Method | Path | Description |
|--------|------|-------------|
| POST | /posts | Publish a post |
| GET | /posts | List posts (`tag`, `limit`, `offset`, `format` filters) |
| GET | /posts/{id} | Get single post |
| DELETE | /posts/{id} | Delete post |
| GET | /tags | List tags with post counts |
| POST | /tags/{tag}/config | Set per-tag TTL override |
| GET | /events | SSE stream of new posts (`?tag=` filter) |

## SSE / real-time

Clients subscribe to `GET /events` for live push. On reconnect after being offline, send the `Last-Event-ID` header with the last received post ID — the server replays missed posts before entering the live stream.

```
GET /events?tag=news
Authorization: Bearer <key>
Last-Event-ID: 42        ← omit on first connect
```

Events have type `post`; a `keepalive` ping fires every 30 s to hold the connection through proxies.

## Configuration (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | required | Bearer token for all endpoints |
| `DEFAULT_TTL_HOURS` | 72 | Global post expiry window |
| `CLEANUP_INTERVAL_MINUTES` | 60 | How often the cleanup loop runs |
| `DATABASE_PATH` | /data/relay.db | SQLite file path |

## Project layout

```
relay/
├── main.py        # FastAPI app + lifespan (init DB, start cleanup loop)
├── config.py      # pydantic-settings Settings (reads .env)
├── database.py    # aiosqlite connection, schema, get_db dependency
├── models.py      # Pydantic request/response models
├── auth.py        # require_api_key dependency (all endpoints)
├── events.py      # In-memory SSE broadcast hub
├── cleanup.py     # Background TTL cleanup loop (asyncio)
└── routes/
    ├── posts.py   # POST/GET/DELETE /posts
    ├── tags.py    # GET /tags, POST /tags/{tag}/config
    └── events.py  # GET /events (SSE)
```

## Tags

Tags are stored with sentinel commas (`,news,ai,`) for unambiguous `LIKE '%,tag,%'` matching. Stripped transparently in responses.

## TTL / cleanup

- Posts expire after `DEFAULT_TTL_HOURS` unless overridden per tag via `POST /tags/{tag}/config`.
- For multi-tag posts, the shortest applicable TTL wins.
- Cleanup loop sleeps before its first run — no deletions at startup.
- Errors are logged, never crash the service.

## Package manager

Uses `uv` — never `pip`. To add a dependency: `uv add <package>`.
