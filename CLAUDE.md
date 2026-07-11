# relay — Claude Code guide

Personal knowledge base kept as a plain-Markdown, **Obsidian-compatible vault**, with an AI-integration layer on top. AI agents publish/query/subscribe over MCP, REST, and SSE; humans edit the *same* files in Obsidian/nvim or the browser/terminal UIs. Posts are tagged, filed into first-level folders, cross-linked (`[[wikilinks]]`/`#id`), paginated, and can expire via configurable TTL.

**Storage is an Obsidian-style Markdown vault** (`RELAY_VAULT_PATH`): one `.md` file per post, the title *is* the filename, metadata in YAML front-matter (`id`, `tags`, `source`, timestamps, `expires_at` — **no `title`**, that's the filename). Files are canonical. A disposable **SQLite index** under `<vault>/.relay/index.db` mirrors them for fast queries and is rebuilt from the files at startup. A `watchdog` watcher live-reindexes external edits (e.g. Obsidian) and pushes them via SSE. `title` is required; `format` no longer exists (everything is Markdown); `id` in front-matter is authoritative and survives renames.

**First-level folders** (see `relay/folders.py`): the vault is organised into one folder per domain (`Homelab/`, `Radio/`, `Finance/`, `Reading/`, … plus `Meta/`, `Digests/`, `Inbox/`); the master document (#0) stays at the root. Folders are a browse aid — **tags remain primary for navigation**. A new post is filed by its **first domain tag** (placement is *derived* from tags, not stored). The folder is set once at creation and never auto-moved on retag — **except** a tag-less note in `Inbox` (the unfiled bucket): when it gains its first domain tag, relay moves it (and its own attachments) into that domain folder. Moves only ever go *out of* Inbox; real folders stay human-owned (move a file yourself in Obsidian and relay preserves it — `id` is authoritative). Scans are recursive; nesting is one level only.

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

`GET /health` (no auth required) is probed every 30s by both the Dockerfile `HEALTHCHECK` and docker-compose. Check status with `docker ps` or `docker inspect`.

Service on http://localhost:8000 — interactive docs at http://localhost:8000/docs

## API

All endpoints require `Authorization: Bearer <API_KEY>`.

| Method | Path | Description |
|--------|------|-------------|
| POST | /posts | Publish a post |
| GET | /posts | List posts (`tag`, `folder`, `limit`, `offset`, `search` filters; `pinned` master on home feed) |
| GET | /posts/{id} | Get single post |
| GET | /posts/{id}/backlinks | Posts that link to this one (via `[[title]]` or `#id`) |
| PATCH | /posts/{id} | Update post fields (title, content, tags, source, expires_at) |
| DELETE | /posts/{id} | Delete post |
| GET | /links | (id, title) index of all posts — clients resolve `[[Title]]` wikilinks with this |
| GET | /folders | First-level vault folders with post counts (sidebar tree view) |
| POST | /attachments | Store a base64 attachment in a folder's `assets/`; with `post_id`, append the `![[file]]` embed to that post |
| GET | /attachments | List attachments (`folder`/`post_id` scope) — filename, folder, size, `![[file]]` ref |
| DELETE | /attachments/{path} | Delete an attachment file; reports post ids still referencing it |
| GET | /attachments/{path} | Serve a vault attachment (image/PDF/…) embedded via `![[file]]` (auth-gated; same-origin so the UI session cookie works on `<img>`) |
| GET | /tags | List tags with post counts (includes 0-count tags from tag_config) |
| POST | /tags/{tag}/config | Set per-tag expiry (`ttl_hours`, `expires_at`, or both) |
| PATCH | /tags/{tag} | Rename a tag across all posts and tag_config |
| GET | /events | SSE stream of new posts (`?tag=` filter) |
| POST/GET | /mcp | Streamable HTTP MCP endpoint (in-process MCP server; bearer auth) |

## SSE / real-time

Clients subscribe to `GET /events` for live push. On reconnect after being offline, send the `Last-Event-ID` header with the last received post ID — the server replays missed posts before entering the live stream.

```
GET /events?tag=news
Authorization: Bearer <key>
Last-Event-ID: 42        ← omit on first connect
```

Events have type `post` (new **or edited** — the vault watcher streams external edits too) or `delete` (`data: {"id": N}`, emitted on API and external/Obsidian deletes so clients drop the post live). A `keepalive` ping fires every 30 s to hold the connection through proxies. `delete` events deliberately carry no SSE `id:` field so a delete of an old post can't rewind the client's `Last-Event-ID` cursor. Clients treat a `post` event for an id they already show as an in-place update.

The SSE `id:` field only moves forward — a streamed *edit* of an older post is sent without an `id:` so it can't rewind the client's `Last-Event-ID` (no reconnect replay storm). Clients also update an open post in place: editing a post that's shown in the detail modal refreshes it, and an inline edit-in-progress on a card is never clobbered by an incoming stream event.

### Known limitation (future work)

- **Offline edits/deletes aren't replayed.** Catch-up replays only `id > Last-Event-ID` (append-only assumption). Edits or deletes to already-seen posts made while a client was offline won't propagate on reconnect until a manual refresh.

## Cross-links (wikilinks)

Posts link to each other two ways, both rendered clickable in the browser UI and TUI:

- **`[[Title]]` / `[[Title|alias]]`** — Obsidian wikilinks, resolved by title (filename), case-insensitive. Work natively in Obsidian/neovim. Renaming a post rewrites inbound `[[…]]` across the vault (`service.update_post` → `links.rewrite_wikilink_targets`). Unresolved links render dimmed/broken.
- **`#NNN`** — link by post id; stable across renames. (In Obsidian these render as tags, not links — prefer `[[Title]]` there.)

Files store links verbatim; relay resolves them at **display time** and never rewrites the stored form (except the rename case). Resolution is client-side: the UI/TUI fetch `GET /links` (id↔title) once and cache it. Post detail views show **Linked mentions** via `GET /posts/{id}/backlinks`. Code spans/blocks are skipped by the renderers.

## Configuration (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | required | Bearer token for all endpoints |
| `RELAY_BASE_URL` | `http://localhost:8000` | Relay base URL used by the stdio MCP proxy |
| `DEFAULT_TTL_HOURS` | 0 | Global post expiry window; `0` disables expiry (per-tag TTLs still apply) |
| `CLEANUP_INTERVAL_MINUTES` | 60 | How often the cleanup loop runs |
| `RELAY_VAULT_PATH` | /data/vault | Markdown vault dir; disposable index at `<vault>/.relay/index.db` |
| `RELAY_WATCH_ENABLED` | true | Live-reindex + SSE on edits made outside relay; `false` disables the watcher |
| `SECURE_COOKIES` | true | `Secure` flag on the browser-UI session cookie; set `false` to use the UI over plain HTTP |
| `ATTACHMENT_MAX_MB` | 25 | Max size of a single uploaded attachment (base64-decoded); larger uploads are rejected with 413 |

## Project layout

```
relay/
├── main.py        # FastAPI app + lifespan (init index, cleanup loop, vault watcher, MCP session); mounts /mcp
├── config.py      # pydantic-settings Settings (reads .env); vault_path + derived index/tags paths
├── frontmatter.py # YAML front-matter parse/serialize + Obsidian filename rules (sanitize, collision suffix)
├── folders.py     # Folder placement policy — maps a post's primary domain tag to its first-level folder
├── links.py       # Wikilink resolver: [[Title]]/[[Title|alias]] + #NNN → post ids; rename rewrite
├── vault.py       # Canonical file layer: write/read/delete/rename, id allocation, startup index rebuild, tags.yml
├── watcher.py     # watchdog observer — external edits → re-index + SSE (self-write suppressed)
├── database.py    # aiosqlite *index* (disposable mirror), schema, get_db dependency
├── models.py      # Pydantic request/response models (title required, no format)
├── auth.py        # require_api_key dependency (all endpoints)
├── service.py     # Shared post/tag logic — writes go file-first via vault, then mirror to the index
├── mcp_server.py  # In-process FastMCP server (Streamable HTTP at /mcp), bearer-gated
├── events.py      # In-memory SSE broadcast hub
├── cleanup.py     # Background TTL cleanup loop (asyncio) — unlinks files + index rows
└── routes/
    ├── posts.py   # POST/GET/DELETE /posts (thin — delegate to service)
    ├── tags.py    # GET /tags, POST /tags/{tag}/config (thin — delegate to service)
    ├── attachments.py # GET /attachments/{path} — serve vault attachments (![[file]])
    └── events.py  # GET /events (SSE)
```

```
relay_mcp/
└── server.py      # Legacy stdio MCP proxy — runs on the client machine, talks to a (possibly remote) relay over REST
```

Two MCP surfaces exist:

- **`relay/mcp_server.py`** — the in-process server, served over Streamable HTTP at `/mcp` by the main app. Tools call `relay.service` directly (no network hop, no schema duplication). This is the remote-capable, recommended path; any MCP client connects with the bearer key. See README for `claude mcp add --transport http`. Also exposes the master document (post 0) as the MCP resource `relay://master-document` (text/markdown) so clients can attach it to context structurally, and ships server `instructions` pointing at it.
- **`relay_mcp/server.py`** — the legacy stdio proxy. Still useful for clients that can't speak remote MCP (e.g. Claude Desktop); it spawns locally and proxies to the relay's REST API over `RELAY_BASE_URL`. At full parity with the in-process server: same eleven tools, the same server `instructions`, and the `relay://master-document` resource (read via REST `GET /posts/0`). Kept for transition; prefer `/mcp`.

```
relay/static/
└── index.html     # Browser UI served at /ui
```

## Browser UI

`GET /ui` serves a single-page interface backed by the REST API and SSE.

- **Posts**: create (compose panel), edit inline, delete with confirmation; `expires_at` datetime picker in both compose and edit forms; posts with an expiry show "expires in X" in the footer
- **Attachments**: compose + edit forms take a file via the 📎 Attach button, drag-drop onto the content box, or paste (e.g. a screenshot). Files upload to `POST /attachments`, and an `![[file]]` embed is inserted at the cursor (images render inline; other files as a 📎 link). Edit-mode uploads file under the post's folder (`embed=false` — the UI places the ref) and the edit form lists that folder's attachments with a delete (×) button; compose uploads pass the form's tags so the file lands in the same folder the note will file under (`folders.folder_for`), e.g. an `audio`-tagged note's image → `Audio/assets/` (falls back to `Inbox/assets/` when no domain tag). Attachment filenames are **vault-globally unique** so a bare `![[name]]` never resolves ambiguously. Deleting a post also removes any attachment in its folder that no other post references (shared assets are kept)
- **Search**: search bar above the feed (visible after connect); 300 ms debounce; filters by title, content, or source; combinable with tag filter; `×` button or Escape to clear
- **Sidebar Tags / Tree / Files tabs**: three tabs at the top of the sidebar. **Tags** filters the feed by tag (create a new tag, rename inline, ⚙ per-tag expiry form). **Tree** lists the first-level vault folders (`GET /folders`) with counts; clicking one filters the feed to that folder (`GET /posts?folder=`). **Files** swaps the feed for an **attachment gallery** (`GET /attachments`): folder-grouped cards with image thumbnails / file-type chips, a folder filter in the sidebar, a click-to-enlarge lightbox for images, and a per-file delete (×). Tag and folder filters are mutually exclusive.
- **Live feed**: SSE connection with amber dot + "live/offline/error" label; new posts flash and prepend automatically
- **Responsive**: sidebar collapses to a slide-in drawer on mobile (≤ 768 px) with hamburger toggle; post action buttons always visible on touch devices

## Tags

In post files, tags are a YAML front-matter list (`tags: [news, ai]`). In the SQLite **index** they're stored with sentinel commas (`,news,ai,`) for unambiguous `LIKE '%,tag,%'` matching, stripped transparently in responses. Per-tag TTL config is canonical in `<vault>/.relay/tags.yml` and mirrored into the index's `tag_config` table.

`GET /tags` returns tags derived from posts plus any tags registered in `tag_config` (shown with count 0 until posts carry them). `PATCH /tags/{tag}` uses SQL `REPLACE()` to rewrite the tag string across all matching posts atomically.

## Master document (id=0)

Post `id=0` is a reserved, permanent document, stored as `Master Document.md` (front-matter `id: 0`) and seeded at startup if absent. It is intended as an index and instruction set for AI agents: naming conventions, tag taxonomy, content guidelines, etc.

- `GET /posts/0` — read the master document
- `PATCH /posts/0` — update it (title, content, tags, source all work normally)
- `DELETE /posts/0` — **blocked** (returns `403 Forbidden`)
- TTL cleanup **never** touches id=0 regardless of tag config
- Deleting the file externally is reversed by the watcher (it recreates `Master Document.md` from the index)

Update it via MCP: `update_post(id=0, content="...")`.

## TTL / cleanup

- Posts never expire by default (`DEFAULT_TTL_HOURS=0`). Set it to a positive integer to enable global expiry.
- A per-post `expires_at` ISO datetime field overrides tag/global TTL for that post. Set it via `POST /posts` or `PATCH /posts/{id}`; clear it by patching with `expires_at: null`.
- Per-tag expiry is configurable via `POST /tags/{tag}/config` with `ttl_hours` (relative), `expires_at` (absolute), or both. Omitting both just registers the tag with no expiry. Tag-level expiry only applies to posts without an explicit `expires_at`. Only tags with actual config are excluded from the global TTL sweep.
- For multi-tag posts without an explicit `expires_at`, the shortest applicable TTL wins.
- Cleanup loop sleeps before its first run — no deletions at startup.
- Post `id=0` (master document) is exempt from cleanup regardless of TTL settings.
- Errors are logged, never crash the service.

## MCP server

Relay speaks MCP through two surfaces that offer identical tools, server
`instructions`, and the `relay://master-document` resource (the master document,
post 0, as `text/markdown`). See [Project layout](#project-layout) for how they
differ internally.

| Tool | Description |
|------|-------------|
| `publish_post` | Publish a post (title, content, tags, source, expires_at) |
| `update_post` | Partially update an existing post by ID (only provided fields change, including expires_at) |
| `list_posts` | List posts (tag/search/limit/offset; the stdio proxy omits search) |
| `get_post` | Get a single post by ID (use `id=0` for the master document) |
| `delete_post` | Delete a post by ID (id=0 is blocked) |
| `add_attachment` | Store a base64 file in a folder's `assets/`; with `post_id` appends the `![[file]]` embed to that post |
| `get_attachment` | Retrieve an attachment by filename; images are returned as inline image content |
| `list_attachments` | List attachments (filename, folder, size, ref); scope by `post_id` or `folder` |
| `delete_attachment` | Delete an attachment by filename; reports post ids that still reference it |
| `list_tags` | List all tags with post counts |
| `set_tag_config` | Set per-tag expiry (ttl_hours, expires_at, or both) |

**Remote — Streamable HTTP (recommended).** Served in-process at `/mcp` by the
main app; tools call `relay.service` directly. Any MCP client connects with the
bearer key, no checkout:

```bash
claude mcp add --transport http relay https://your-relay.example.com/mcp \
  --header "Authorization: Bearer <your-api-key>"
```

**Local — stdio proxy (legacy).** `relay-mcp` runs `relay_mcp/server.py` on the
client machine and proxies to the relay over REST. Needs a checkout of this repo
and `uv`; used by clients that can't speak remote MCP (e.g. Claude Desktop). Add
to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "relay": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/relay", "relay-mcp"],
      "env": {
        "API_KEY": "<your-api-key>",
        "RELAY_BASE_URL": "https://your-relay.example.com"
      }
    }
  }
}
```

Replace `/path/to/relay` with the absolute path to this repo on the client
machine. Because the proxy runs the local checkout, `git pull` there to pick up
changes, then fully restart the client.

## Terminal UI (TUI)

`relay_tui/` is a [Textual](https://github.com/Textualize/textual)-based terminal interface.

```bash
uv run relay-tui
```

Set `RELAY_PALETTE=<name>` to pick a colour theme (same palettes as tuidash). Available: `default` (amber), `dracula`, `nord`, `gruvbox`, `solarized`, `molokai`, `candy`, `earthy`, `pastel`, `tango`.

Set `RELAY_TRANSPARENT=1` to let the terminal's own background (transparency / background image) show through the base canvas (Screen, header, footer, scrollbars). The single-post detail popup follows the same transparency; the editing modals (compose/edit/confirm/etc.) stay opaque for readability. Same mechanism as tuidash: the canvas uses `ansi_default` (SGR 49) and a custom `ANSIToTruecolor` line filter preserves it instead of baking in a solid colour.

**Layout:** two-panel split — TOPICS sidebar on the left, FEED on the right.

| Key | Action |
|-----|--------|
| `n` | Compose new post |
| `e` | Edit selected post |
| `d` | Delete selected post (with confirmation) |
| `/` | Search posts (by title, content, source) |
| `a` | Browse attachments (open externally / delete) |
| `t` | Toggle the TOPICS panel between Tags and Tree (folders) |
| `c` | Configure expiry for selected tag (TOPICS panel, tags mode) |
| `R` | Rename selected tag (TOPICS panel) |
| `r` | Refresh |
| `Enter` | View full post |
| `f` | (in a post's detail view) Follow a link — opens a filterable picker of the post's `[[wikilinks]]` / `#NNN` refs + backlinks; ↑↓/type to select, Enter to jump |
| `Tab` | Switch between TOPICS / FEED panels |
| `q` | Quit |

SSE live feed runs in a background thread; the header shows `● live` / `○ offline`. New posts arriving via SSE prepend automatically. On reconnect the `Last-Event-ID` replay catches up missed posts. The feed loads 50 posts per page and fetches the next page automatically as you scroll toward the bottom.

```
relay_tui/
├── app.py          # RelayTuiApp + main()
├── theme.py        # RELAY_PALETTE loader + build_textual_theme()
├── api.py          # Sync requests-based HTTP client
├── sse.py          # SSE background thread with reconnect/backoff
├── palettes/       # 10 TOML palette files (default, dracula, nord, …)
└── widgets/
    ├── tag_panel.py    # TOPICS sidebar
    ├── post_panel.py   # FEED list
    └── modals.py       # Compose / Edit / Confirm / PostDetail / TagConfig modals
```

## Package manager

Uses `uv` — never `pip`. To add a dependency: `uv add <package>`.
