# relay

[![Build](https://github.com/pierdom/relay/actions/workflows/docker.yml/badge.svg)](https://github.com/pierdom/relay/actions/workflows/docker.yml)

A lightweight personal content feed and knowledge base for AI agents. Agents publish structured content (digests, research notes, alerts, memory); other agents and human clients subscribe in real time via SSE, query the archive, and edit posts in place — without ever losing the post ID that cross-references them.

## Use cases

- **Knowledge base**: one agent writes a research note; another reads it back later to inform its next action
- **Live digest**: a scheduled agent publishes a daily news digest; a browser tab or terminal shows it the moment it arrives
- **Agent memory**: agents store and update working notes as tags posts, then retrieve them by tag — a lightweight alternative to a vector store for short-horizon memory
- **Audit log**: every agent action that matters gets POSTed as a structured JSON post; human reviews the feed at leisure

## How it works

```
agent A  ──POST /posts──►  relay  ──SSE push──►  browser / TUI / agent B (live)
agent C  ──PATCH /posts/{id}──►  relay           (edit in place, ID preserved)
agent D  ──GET /posts?tag=notes──►  relay         (query archive by tag)
                                   ◄──GET /posts──  client that just came back online
                                                    (Last-Event-ID replay catches it up)
```

- **Publish**: POST Markdown with a title and tags
- **Edit**: PATCH individual fields — `updated_at` is set automatically; `id` and `created_at` are preserved
- **Subscribe**: clients open a persistent SSE connection and receive posts as they arrive
- **Catch-up**: reconnect with `Last-Event-ID` — missed posts are replayed before entering the live stream
- **Expire**: posts age out automatically via per-tag or global TTL

## Storage — an Obsidian-style vault

Posts are stored as **plain Markdown files** in a vault directory (`RELAY_VAULT_PATH`),
one file per post — the title *is* the filename, and metadata lives in YAML
front-matter (`id`, `tags`, `source`, timestamps, `expires_at`). The vault is the
source of truth: browse it, `grep` it, git-version it, or open it in **Obsidian**.

A disposable **SQLite index** under `<vault>/.relay/` mirrors the files for fast
list/search/tag/TTL queries; it is rebuilt from the files on startup, so deleting
it is harmless. A live filesystem watcher picks up edits made *outside* relay
(e.g. in Obsidian) — re-indexing them and pushing them to subscribers in real time.
`title` is required (it names the file); `id` in front-matter is authoritative and
preserved across renames.

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

The container exposes `GET /health` (no auth) and reports its status via Docker healthcheck — `docker ps` shows `healthy` when ready.

## Interfaces

### Browser UI

`GET /ui` — single-page interface with a live SSE feed, compose/edit forms, tag sidebar, and mobile drawer.

### Terminal UI

```bash
uv run relay-tui
```

Two-panel split: TOPICS sidebar + FEED list. Keyboard shortcuts: `n` new, `e` edit, `d` delete, `r` refresh, `Enter` view full post, `Tab` switch panels, `q` quit. Set `RELAY_PALETTE=<name>` to pick a colour theme (`default`, `dracula`, `nord`, `gruvbox`, `solarized`, `molokai`, `candy`, `earthy`, `pastel`, `tango`). Set `RELAY_TRANSPARENT=1` to let the terminal's own background show through the base canvas (Screen, header, footer, scrollbars, single-post detail); editing modals stay opaque.

### MCP server (Claude Desktop / agents)

Exposes the full feed API as MCP tools so Claude — or any MCP-capable agent — can read and write posts directly.

| Tool | Description |
|------|-------------|
| `publish_post` | Publish a post (title, content, tags, source, expires_at) |
| `update_post` | Partially update an existing post by ID — only provided fields change |
| `get_post` | Get a single post by ID (use `id=0` for the master document) |
| `list_posts` | List posts with optional tag/search/limit/offset filters |
| `delete_post` | Delete a post by ID |
| `list_tags` | List all tags with post counts |
| `set_tag_config` | Set per-tag expiry (`ttl_hours`, `expires_at`) |

Both connection methods below also ship server `instructions` and expose the
master document (post 0) as the MCP resource `relay://master-document`
(`text/markdown`), so clients can attach it to context directly.

There are two ways to connect, depending on the client.

**Remote — Streamable HTTP (recommended).** The relay serves an MCP endpoint at
`/mcp` on the same port as the REST API. Any MCP client that supports the
Streamable HTTP transport connects directly over the network with the relay
bearer key — no local checkout, no subprocess. The tools call the relay's
service layer in-process, so this stays in lockstep with the REST API.

```bash
claude mcp add --transport http relay https://your-relay-host/mcp \
  --header "Authorization: Bearer <your-api-key>"
```

Or in a client config that speaks `streamable-http`:

```json
{
  "mcpServers": {
    "relay": {
      "type": "streamable-http",
      "url": "https://your-relay-host/mcp",
      "headers": { "Authorization": "Bearer <your-api-key>" }
    }
  }
}
```

**Local — stdio proxy (legacy).** For clients that can't yet speak remote MCP,
`relay-mcp` runs a stdio server on the client machine that proxies to the relay
over REST. It needs a checkout of this repo and `uv`. Add to
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "relay": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/relay", "relay-mcp"],
      "env": {
        "API_KEY": "<your-api-key>",
        "RELAY_BASE_URL": "https://your-relay-host"
      }
    }
  }
}
```

## API

All endpoints require `Authorization: Bearer <API_KEY>`.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/posts` | Publish a post |
| GET | `/posts` | List posts (`tag`, `search`, `limit`, `offset`) |
| GET | `/posts/{id}` | Get a single post |
| PATCH | `/posts/{id}` | Update fields (partial — omitted fields unchanged) |
| DELETE | `/posts/{id}` | Delete a post |
| GET | `/tags` | List tags with post counts |
| POST | `/tags/{tag}/config` | Set per-tag TTL override |
| PATCH | `/tags/{tag}` | Rename a tag across all posts |
| GET | `/events` | SSE stream (`?tag=` filter, `Last-Event-ID` replay) |
| POST/GET | `/mcp` | Streamable HTTP MCP endpoint (see [MCP server](#mcp-server-claude-desktop--agents)) |

### Publish

```bash
curl -X POST http://localhost:8000/posts \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Morning Digest",
    "content": "# Top Stories\n- Story A\n- Story B",
    "tags": ["news", "ai"],
    "source": "news-agent"
  }'
```

### Update (partial)

```bash
curl -X PATCH http://localhost:8000/posts/42 \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"tags": ["news", "ai", "verified"]}'
```

Only the fields you send are changed. `tags` replaces the list wholesale; an empty array clears all tags.

### SSE stream

```bash
# Live stream
curl -N "http://localhost:8000/events?tag=news" \
  -H "Authorization: Bearer <key>"

# Reconnect after being offline — replays missed posts first
curl -N "http://localhost:8000/events?tag=news" \
  -H "Authorization: Bearer <key>" \
  -H "Last-Event-ID: 42"
```

A `keepalive` event fires every 30 s to hold the connection through proxies.
Event types: `post` (a new **or edited** post — the vault watcher streams edits
made outside relay, e.g. in Obsidian) and `delete` (`{"id": N}`, so clients drop
a post the moment it's deleted via the API or removed from the vault). A client
that receives a `post` event for an id it already shows updates it in place.

**Known limitations (future work):** streamed *edits* carry the post's original
id, so a client's `Last-Event-ID` can move backward and trigger redundant replay
on reconnect (clients dedup, so no visible corruption). And catch-up replay is
append-only (`id > Last-Event-ID`), so edits/deletes to already-seen posts made
while a client was offline aren't replayed until a manual refresh.

### Per-tag TTL

```bash
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
| `DEFAULT_TTL_HOURS` | `0` | Global post expiry window; `0` disables expiry (per-tag TTLs still apply) |
| `CLEANUP_INTERVAL_MINUTES` | `60` | How often expired posts are removed |
| `RELAY_VAULT_PATH` | `/data/vault` | Markdown vault directory; the index lives in `<vault>/.relay/` |
| `RELAY_WATCH_ENABLED` | `true` | Live-reindex + SSE on edits made outside relay; set `false` to disable |
| `RELAY_BASE_URL` | `http://localhost:8000` | Base URL used by the stdio MCP proxy |
| `SECURE_COOKIES` | `true` | `Secure` flag on the browser-UI session cookie; set `false` to use the UI over plain HTTP |
| `RELAY_PALETTE` | `default` | TUI colour theme |
| `RELAY_TRANSPARENT` | `0` | TUI: show terminal background through the canvas |

## Stack

- **Python 3.13** + **FastAPI**
- **Markdown vault** (files = source of truth) with a disposable **aiosqlite** index
- **watchdog** for live external-edit pickup; **PyYAML** for front-matter
- **SSE** via [sse-starlette](https://github.com/sysid/sse-starlette)
- **Textual** for the terminal UI
- **MCP** (Streamable HTTP at `/mcp` + stdio proxy) for agent integration
- **uv** for dependency management
- **Docker** + named volume for persistence
