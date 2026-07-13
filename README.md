<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="relay/static/assets/relay-mark-on-dark.svg">
  <img src="relay/static/assets/relay-mark.svg" alt="relay" width="96" height="96">
</picture>

# relay

[![Build](https://github.com/pierdom/relay/actions/workflows/docker.yml/badge.svg)](https://github.com/pierdom/relay/actions/workflows/docker.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fpierdom%2Frelay%2Fmain%2Fpyproject.toml)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-server-6E56CF?logo=modelcontextprotocol&logoColor=white)](https://modelcontextprotocol.io)
[![Last commit](https://img.shields.io/github/last-commit/pierdom/relay)](https://github.com/pierdom/relay/commits/main)

**An AI-integration layer over a plain-Markdown, Obsidian-compatible vault.**

</div>

Your knowledge base lives as ordinary `.md` files on disk — a first-class **Obsidian / filesystem vault** you can browse, `grep`, git-version, and edit in Obsidian, neovim, or any tool. relay wraps that *same* vault with the machine surface AI systems need: an **MCP server**, a **REST API**, a real-time **SSE** stream, and **browser + terminal UIs**. Agents publish structured content (digests, research notes, alerts, memory), query the archive, cross-reference posts by a stable `id` that survives renames, and subscribe live — all against files you can equally well open by hand.

The vault is the source of truth; relay is the integration surface on top. That split is deliberate: a filesystem-based knowledge base and your AI tooling share **one** store, not two — no separate database to sync, export, or lock you in.

## Use cases

- **Knowledge base**: one agent writes a research note; another reads it back later to inform its next action
- **Live digest**: a scheduled agent publishes a daily news digest; a browser tab or terminal shows it the moment it arrives
- **Agent memory**: agents store and update working notes as tagged posts, then retrieve them by tag or folder — a lightweight alternative to a vector store for short-horizon memory
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
source of truth: browse it, `grep` it, git-version it, or open it in **Obsidian**
(or any other filesystem-based notes / knowledge-management tool — nvim, VS Code, etc.).

A disposable **SQLite index** under `<vault>/.relay/` mirrors the files for fast
list/search/tag/TTL queries — including **FTS5 full-text search** (porter-stemmed,
multi-term, prefix, bm25-ranked); it is rebuilt from the files on startup, so
deleting it is harmless. A live filesystem watcher picks up edits made *outside* relay
(e.g. in Obsidian) — re-indexing them and pushing them to subscribers in real time.
`title` is required (it names the file); `id` in front-matter is authoritative and
preserved across renames.

The vault is organised into **first-level folders** — one per domain (`Homelab/`,
`Radio/`, `Finance/`, `Reading/`, …, plus `Digests/` and `Inbox/`), with the
master document at the root. Folders are a browse aid; **tags stay primary for
navigation**. A new post is filed automatically by its first domain tag (placement
is derived from tags, not stored). The folder is chosen once at creation and, apart
from one case, never moved on retag — a tag-less note filed in `Inbox` moves to its
domain folder (with its attachments) when it gains its first domain tag. Otherwise
reorganise freely in Obsidian and relay preserves it, since `id` is authoritative.

**Cross-links.** Posts link to each other with Obsidian **`[[Title]]`** / `[[Title|alias]]`
wikilinks (resolved by title) or by id with **`#NNN`** (stable across renames). Both
render clickable in the browser UI and TUI; `[[…]]` also works natively in Obsidian.
Renaming a post rewrites inbound `[[…]]` links across the vault, and each post's detail
view lists its **backlinks** ("linked mentions").

**Attachments.** Images, PDFs and other files live in a per-folder `<Folder>/assets/`
subdirectory, embedded Obsidian-style with `![[file.png]]` (images render inline; other
files as a link). Add them from the browser UI (📎 button, drag-drop, or paste a
screenshot) or over MCP/REST; filenames are made vault-globally unique so a bare
`![[name]]` never resolves ambiguously. Deleting a post also cleans up attachments in
its folder that no other post still references.

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

`GET /ui` — single-page interface with a live SSE feed, compose/edit forms, and a mobile drawer. The sidebar toggles between **Tags** (filter by tag), **Tree** (filter by vault folder), and **Files** (an attachment gallery with thumbnails, folder filter, click-to-enlarge lightbox, and delete). The master document is pinned on top of the home feed, and `[[wikilinks]]`/`#NNN` cross-references render clickable (with a "linked mentions" panel in the detail view). Attachments can be added to the compose/edit forms by the 📎 Attach button, drag-drop, or paste (screenshots) — the file uploads and its `![[embed]]` is inserted at the cursor.

### Terminal UI

```bash
uv run relay-tui
```

Two-panel split: TOPICS sidebar + FEED list. Keyboard shortcuts: `n` new, `e` edit, `d` delete, `r` refresh, `Enter` view full post, `a` browse attachments (open externally / delete), `t` toggle TOPICS between Tags and Tree (folders), `Tab` switch panels, `q` quit. In a post's detail view, `f` opens a filterable picker to follow its `[[wikilinks]]`/`#NNN` links and backlinks. The master document is pinned on top of the feed. Set `RELAY_PALETTE=<name>` to pick a colour theme (`default`, `dracula`, `nord`, `gruvbox`, `solarized`, `molokai`, `candy`, `earthy`, `pastel`, `tango`). Set `RELAY_TRANSPARENT=1` to let the terminal's own background show through the base canvas (Screen, header, footer, scrollbars, single-post detail); editing modals stay opaque.

### MCP server (Claude Desktop / agents)

Exposes the full feed API as MCP tools so Claude — or any MCP-capable agent — can read and write posts directly.

| Tool | Description |
|------|-------------|
| `publish_post` | Publish a post (title, content, tags, source, expires_at) |
| `update_post` | Partially update an existing post by ID — only provided fields change |
| `get_post` | Get a single post by ID (use `id=0` for the master document) |
| `list_posts` | List posts with optional tag/search/limit/offset filters |
| `delete_post` | Delete a post by ID |
| `add_attachment` | Store a base64 file in a folder's `assets/`; with `post_id` appends the `![[file]]` embed to that post |
| `get_attachment` | Retrieve an attachment by filename; images return as inline image content |
| `list_attachments` | List attachments (filename, folder, size, ref); scope by `post_id` or `folder` |
| `delete_attachment` | Delete an attachment by filename; reports post ids that still reference it |
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

**OAuth login (optional).** Set `MCP_OAUTH_ENABLED=true` (with OIDC configured)
and relay becomes its own OAuth 2.1 Authorization Server, brokering the login
upstream to your IdP — so a remote client like Claude Desktop / claude.ai can
connect to `/mcp` via the standard OAuth + Dynamic Client Registration flow
instead of a pasted bearer key. The static `API_KEY` keeps working either way.

In the client's connector dialog fill only the **name + URL** and leave the OAuth
client fields blank — DCR self-registers. Tokens are opaque, hashed at rest, and
audience-bound to `/mcp`; revoking one cascades to its pair, and the allowlist is
re-checked on refresh. Before enabling, add `<RELAY_BASE_URL>/mcp/oauth/callback`
to your IdP client, keep `OIDC_ALLOWED_SUBS` non-empty (empty = any IdP user gets
full tool access), and confirm the client's redirect host is in
`MCP_ALLOWED_REDIRECT_HOSTS`.

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
| GET | `/posts` | List posts (`tag`, `folder`, `search` (FTS5, bm25-ranked), `summary`, `limit`, `offset`; master doc returned as `pinned` on the home feed) |
| GET | `/posts/{id}` | Get a single post |
| GET | `/posts/{id}/backlinks` | Posts that link to this one (`[[title]]` or `#id`) |
| PATCH | `/posts/{id}` | Update fields (partial — omitted fields unchanged) |
| DELETE | `/posts/{id}` | Delete a post |
| GET | `/links` | (id, title) index — clients resolve `[[Title]]` wikilinks with this |
| GET | `/folders` | First-level vault folders with post counts |
| POST | `/attachments` | Store a base64 attachment; with `post_id`, append its `![[file]]` embed to that post |
| GET | `/attachments` | List attachments (`folder`/`post_id` scope) — filename, folder, size, ref |
| DELETE | `/attachments/{path}` | Delete an attachment file; reports posts still referencing it |
| GET | `/attachments/{path}` | Serve a vault attachment (image/PDF/…) embedded via `![[file]]` |
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
Event types: `post` (a new **or edited** post — both API/MCP edits and edits made
outside relay via the vault watcher, e.g. in Obsidian, stream live) and `delete`
(`{"id": N}`, so clients drop a post the moment it's deleted via the API or
removed from the vault). A client that receives a `post` event for an id it
already shows updates it in place (and refreshes the active sidebar counts, so an
Inbox→domain move reflows the Tree without a manual refresh).

The SSE `id:` field only ever moves forward (streamed edits to older posts are
sent without an `id:`), so an edit can't rewind a client's `Last-Event-ID` or
trigger a replay storm on reconnect.

**Known limitation (future work):** catch-up replay is append-only
(`id > Last-Event-ID`), so edits/deletes to already-seen posts made while a
client was offline aren't replayed until a manual refresh.

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
| `ATTACHMENT_MAX_MB` | `25` | Max size of a single uploaded attachment (base64-decoded); larger uploads are rejected with 413 |
| `OIDC_ISSUER` | `""` | OIDC provider (e.g. PocketID) base URL. Set with `OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET` to enable **Login with OIDC** for the web UI; blank keeps API-key-paste login |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | `""` | Confidential OIDC client; register redirect URI `<RELAY_BASE_URL>/auth/callback` at the provider |
| `SESSION_SECRET` | `""` | Signs the session cookie; falls back to `API_KEY` if unset |
| `SESSION_MAX_AGE_HOURS` | `720` | Signed session-cookie lifetime (default 30 days) |
| `OIDC_ALLOWED_SUBS` | `""` | Comma-separated allowlist of OIDC `sub`s (immutable IdP user ids — preferred over email) |
| `OIDC_ALLOWED_EMAILS` | `""` | Comma-separated allowlist; matches **verified** emails only. Both allowlists empty = any authenticated user |
| `MCP_OAUTH_ENABLED` | `false` | Turn `/mcp` into an OAuth 2.1 AS+RS so remote clients connect via OAuth + Dynamic Client Registration (needs `OIDC_*`); the static `API_KEY` still works. Off = static-bearer only |
| `MCP_REQUIRED_SCOPES` | `relay` | Scope required on `/mcp` (single scope = full tool access) |
| `MCP_ALLOWED_REDIRECT_HOSTS` | `claude.ai,claude.com,chatgpt.com` | Allowlist of DCR **https** redirect hosts (exact match) — blocks a rogue client pointing an auth code at its own host; blank = any https. `http` stays loopback-only. Add other clients (Perplexity `www.perplexity.ai`, Mistral `console.mistral.ai`) as needed |
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
