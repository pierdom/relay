# relay — Claude Code guide

Personal knowledge base kept as a plain-Markdown, **Obsidian-compatible vault** with an AI-integration layer on top. AI agents publish/query/subscribe over MCP, REST, and SSE; humans edit the *same* files in Obsidian/nvim or the browser/terminal UIs. Posts are tagged, filed into first-level folders, cross-linked (`[[wikilinks]]`/`#id`), and can expire via configurable TTL.

**Storage** (`RELAY_VAULT_PATH`): one `.md` file per post — the title *is* the filename, metadata in YAML front-matter (`id`, `tags`, `source`, timestamps, `expires_at`; **no `title`**). Files are canonical; a disposable **SQLite index** at `<vault>/.relay/index.db` mirrors them for fast queries and is rebuilt from files at startup. A `watchdog` watcher live-reindexes external edits (Obsidian/nvim) and pushes them via SSE. `id` in front-matter is authoritative and survives renames; `title` is required; everything is Markdown (no `format` field).

**Folders** (`relay/folders.py`): one folder per domain (`Homelab/`, `Radio/`, `Finance/`, … plus `Meta/`, `Digests/`, `Inbox/`); master doc (#0) at the root. Folders are a browse aid — **tags stay primary for navigation**. Placement is *derived* from the **first domain tag** at creation, not stored, and never auto-moved on retag — **except** a tag-less note in `Inbox` (the unfiled bucket): when it gains its first domain tag, relay moves it (and its own attachments) into that domain folder. Moves only ever go *out of* Inbox; real folders stay human-owned (move a file in Obsidian and relay preserves it). Scans are recursive; nesting is one level.

## Running

```bash
cp .env.example .env            # set API_KEY
uv run uvicorn relay.main:app --reload      # local, http://localhost:8000 (docs at /docs)
docker compose up -d            # or Docker; update with: docker compose pull && docker compose up -d
```

`GET /health` (no auth) is probed every 30s by the Dockerfile HEALTHCHECK and compose. Uses `uv` — never `pip`; add deps with `uv add <package>`.

## API

All endpoints need `Authorization: Bearer <API_KEY>`.

| Method | Path | Description |
|--------|------|-------------|
| POST/GET | /posts | Publish / list posts (`tag`, `folder`, `limit`, `offset`, `search`, `summary`, `sort`, `order`; master pinned on home feed). `sort` = `updated` (default, last-modified via `COALESCE(updated_at, created_at)`) or `created`; `order` = `desc` (default) or `asc`; an FTS `search` ranks by bm25 first, then `sort`/`order` as tiebreak. `summary=true` → metadata-only items (`PostSummary`: id/title/tags/folder + plain-text `excerpt`, no `content`); REST default `false` (UI feed renders content inline), MCP `list_posts` default `true` |
| GET/PATCH/DELETE | /posts/{id} | Get / update (partial) / delete a post |
| GET | /posts/{id}/backlinks | Posts linking here via `[[title]]` or `#id` |
| GET | /links | (id, title) index — clients resolve `[[Title]]` wikilinks with this |
| GET | /folders | First-level folders with post counts |
| POST/GET | /attachments | Upload / list attachments — bytes via `data` (base64), `source_url` (server fetches), or `upload_id` (filled slot); see [Attachments](#attachments) |
| POST | /attachments/uploads | Mint a presigned upload slot (`upload_id` + `upload_url`) for out-of-band bytes |
| PUT | /attachments/uploads/{upload_id} | Stream raw bytes into a slot (single, capped body) |
| GET/DELETE | /attachments/{path} | Serve / delete an attachment file |
| GET | /tags | Tags with counts (incl. 0-count from tag_config) |
| POST/PATCH | /tags/{tag}[/config] | Set per-tag expiry / rename a tag across all posts |
| GET | /events | SSE stream (`?tag=` filter) |
| GET | /metrics | Prometheus/OpenMetrics text exposition (bearer-gated); see [Metrics](#metrics) |
| POST/GET | /mcp | Streamable HTTP MCP endpoint (bearer auth) |

## Attachments

Non-`.md` files (images, PDFs, …) live in a per-folder `<Folder>/assets/` subdir, embedded Obsidian-style with `![[file.png]]`. The index ignores them, so they never appear as posts/folders.

- **Serving:** `GET /attachments/{path}` (auth-gated, `nosniff`, path-traversal-protected in `vault.resolve_attachment`). Same-origin, so the UI session cookie authenticates `<img>`.
- **Names are vault-globally unique** (`vault.write_attachment` suffixes ` N` across *all* `assets/` dirs), so a bare `![[name]]` always resolves to exactly one file.
- **Rendering:** `![[img]]` → inline image (Obsidian `|WxH` sizing); `![[file]]`/`[[file.ext]]` → 📎 link. An `![[…]]` embed is always a file (any extension); a plain `[[…]]` uses a curated extension list so dotted note titles (`[[Section 2.1]]`) aren't misread. `![[Note]]` (no extension) → note transclusion, rendered as a link.
- **Placement:** with `post_id` → the post's folder (auto-embeds `![[file]]` unless `embed=false`); else by `folder`; else derived from `tags` (`folders.folder_for`); else `Inbox`.
- **Byte transport (`relay/ingest.py`):** an upload provides its bytes exactly one of three ways — `data` (inline base64; only viable for tiny files, since an MCP client must *emit the whole blob* as model tokens), `source_url` (an http(s) URL the **server** fetches — SSRF-guarded on every hop incl. redirects, streamed, size-capped; filename derived from Content-Disposition/URL when omitted; note the guard resolves DNS once so it's not rebind-proof — fine given callers are authenticated), or `upload_id` (a presigned slot: `POST /attachments/uploads` → PUT raw bytes out-of-band → finalize with the id). The model validator enforces exactly-one. Slots are in-memory + disk-staged under `.relay/uploads/`, single-use, TTL'd (`ATTACHMENT_UPLOAD_TTL_SECONDS`), swept by the cleanup loop, and wiped at startup — **single-worker assumption** (PUT + finalize must hit the same process). A failed `source_url`/unknown `upload_id` → **400** (loud); over-cap → **413**.
- **Presigned consumers:** the browser UI streams files ≥4 MB through a slot instead of base64; the **stdio proxy's** `add_attachment(path=…)` reads a local file on the client machine and drives the same create→PUT→finalize flow (see the parity exception in [MCP](#mcp)).
- **Lifecycle:** deleting a post removes attachments in its folder that no other post references (shared assets kept). Deleting an attachment reports the post ids still referencing it (now dangling).
- `ATTACHMENT_MAX_MB` (25) caps uploads → 413 (enforced on all three transports). `get_attachment` returns images as inline image content, size-guarded.

## Metrics

`GET /metrics` exposes Prometheus text format 0.0.4 (`relay/metrics.py` — a zero-dep counter registry + renderer, no `prometheus_client`; Telegraf/Prometheus both scrape it). **Gated behind the same `require_api_key`** as the rest of the API (scraper sends `Authorization: Bearer <API_KEY>`) — relay is behind a public proxy, so an open `/metrics` would leak vault size/activity; the bearer gate needs no new config. On a trusted-network deploy you could bind it loopback/tailnet-only instead.

- **Counters** (process-lifetime, reset on restart): `relay_http_requests_total{method,path,status}` (a raw-ASGI middleware — *not* `BaseHTTPMiddleware`, so it never buffers the SSE/MCP streams; `path` is the matched route template or a bucketed first segment, so cardinality stays bounded), `relay_mcp_tool_calls_total{tool}` (in-process `/mcp` tools only — the stdio proxy runs on the client and its calls land as REST `http_requests`; **not** a parity concern — internal instrumentation, not part of the tool contract), `relay_search_queries_total`, `relay_cleanup_deletions_total`, `relay_upload_slots_purged_total`.
- **Gauges** (sampled from the DB/state at scrape time, always exact): `relay_posts_total`, `relay_tags_total`, `relay_sse_clients`, `relay_build_info{version}` (from `relay.__version__`, the single version source, also FastAPI's `version=`).

## SSE / real-time

`GET /events` for live push. On reconnect, send `Last-Event-ID` with the last post id — the server replays missed posts (`id > Last-Event-ID`) before the live stream.

Event types: `post` (new **or edited** — the watcher streams external edits) and `delete` (`data: {"id": N}`). A `keepalive` fires every 30s. Both edits and deletes are sent **without** an SSE `id:` so they can't rewind the client's cursor (no replay storm). Clients treat a `post` for a known id as an in-place update and never clobber an inline edit-in-progress.

Both `create_post` and `update_post` publish SSE, so API/MCP edits (incl. Inbox→domain moves) propagate live; clients refresh the active sidebar counts (Tags or Tree) on any streamed change. **Known limitation:** offline edits/deletes to already-seen posts aren't replayed on reconnect (catch-up is append-only).

## Cross-links

- **`[[Title]]` / `[[Title|alias]]`** — resolved by filename, case-insensitive; renaming a post rewrites inbound `[[…]]` across the vault (`links.rewrite_wikilink_targets`). Unresolved → dimmed.
- **`#NNN`** — link by post id; stable across renames (renders as a tag in Obsidian — prefer `[[Title]]` there).

Links are stored verbatim and resolved at **display time** (never rewritten except on rename). UI/TUI fetch `GET /links` once and cache it; detail views show **Linked mentions** via `GET /posts/{id}/backlinks`. Code spans/blocks are skipped.

## Configuration (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | required | Bearer token for all endpoints |
| `RELAY_BASE_URL` | `http://localhost:8000` | Relay URL used by the stdio MCP proxy |
| `DEFAULT_TTL_HOURS` | 0 | Global expiry window; `0` disables (per-tag TTLs still apply) |
| `CLEANUP_INTERVAL_MINUTES` | 60 | Cleanup loop interval |
| `RELAY_VAULT_PATH` | /data/vault | Vault dir; index at `<vault>/.relay/index.db` |
| `RELAY_WATCH_ENABLED` | true | Live-reindex + SSE on external edits |
| `SECURE_COOKIES` | true | `Secure` on the UI session cookie; `false` for plain HTTP |
| `ATTACHMENT_MAX_MB` | 25 | Max attachment upload size → 413 (all transports) |
| `ATTACHMENT_UPLOAD_TTL_SECONDS` | 3600 | How long a presigned upload slot stays open before purge |
| `ATTACHMENT_FETCH_TIMEOUT_SECONDS` | 20 | Timeout for a server-side `source_url` fetch |
| `OIDC_ISSUER` | "" | PocketID base URL. Set (with client id/secret) to enable OIDC login for `/ui`; blank = key-paste only |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | "" | Confidential OIDC client registered in PocketID (redirect URI `<RELAY_BASE_URL>/auth/callback`) |
| `SESSION_SECRET` | "" | Signs the session cookie; falls back to `API_KEY` if unset |
| `SESSION_MAX_AGE_HOURS` | 720 | Signed session-cookie lifetime (default 30d) |
| `OIDC_ALLOWED_SUBS` | "" | Comma-separated allowlist of OIDC `sub`s (immutable user id — preferred) |
| `OIDC_ALLOWED_EMAILS` | "" | Comma-separated allowlist; matches **verified** emails only. Both allowlists empty = any PocketID user |
| `MCP_OAUTH_ENABLED` | false | Turn `/mcp` into an OAuth 2.1 AS+RS (DCR + PKCE, tokens brokered to PocketID). Needs `OIDC_*`; add `<RELAY_BASE_URL>/mcp/oauth/callback` to that PocketID client. Off = static-bearer only |
| `MCP_REQUIRED_SCOPES` | relay | Scopes required on `/mcp`; single scope = full tool access |
| `MCP_ALLOWED_REDIRECT_HOSTS` | claude.ai,claude.com,chatgpt.com | DCR https redirect-URI host allowlist (blocks a rogue client pointing an auth code at its own host); exact host match; blank = any https. http stays loopback-only. Add other clients as needed (Perplexity `www.perplexity.ai`, Mistral `console.mistral.ai`, …) |
| `MCP_AUTH_CODE_TTL_SECONDS` / `MCP_ACCESS_TOKEN_TTL_SECONDS` / `MCP_REFRESH_TOKEN_TTL_SECONDS` | 60 / 3600 / 2592000 | OAuth code / access / refresh lifetimes |

## Authentication

Two credential channels, both checked by the shared `require_api_key` dependency (`relay/auth.py`):

- **Bearer `API_KEY`** — machine-to-machine (REST, MCP, agents). Unchanged.
- **`relay_session` cookie** — human web-UI sessions. A **signed, expiring** token (`itsdangerous`) carrying `{sub, email}`; verified by signature + `SESSION_MAX_AGE_HOURS`.

The cookie is minted two ways: **OIDC login** via PocketID (`GET /auth/login` → PocketID authorize with PKCE/S256 → `GET /auth/callback` validates the ID token, enforces the allowlist — immutable `OIDC_ALLOWED_SUBS`, or `OIDC_ALLOWED_EMAILS` on **verified** emails only — sets the cookie), or the **API-key paste** break-glass (`POST /session`, synthetic `sub=apikey`). `GET /auth/me` reports session state + whether OIDC is configured (drives the UI login control); `GET /auth/logout` / `DELETE /session` clear it. Transient OAuth state (state/nonce/PKCE verifier) rides a short-lived `relay_oauth` cookie via Starlette `SessionMiddleware`.

**Remote MCP OAuth (`MCP_OAUTH_ENABLED`, off by default).** PocketID lacks Dynamic Client Registration and Claude's remote connector requires it, so relay acts as its **own** OAuth 2.1 Authorization Server and brokers the human login upstream to PocketID (reusing the Phase-1 OIDC client). When enabled, FastMCP (`mcp==1.27.1`) mounts `/authorize` `/token` `/register`(DCR) `/revoke` + RFC 8414/9728 metadata and wraps `/mcp` in `RequireAuthMiddleware`; a broker callback `/mcp/oauth/callback` (`relay/mcp_oauth/broker.py`) validates the PocketID id-token, enforces the **same `_authorized()` sub allowlist** as the web UI, and mints a relay auth code. Tokens are opaque, **hashed at rest**, audience-bound to `<RELAY_BASE_URL>/mcp` (RFC 8707), and stored in `<vault>/.relay/oauth.db` — a **separate** SQLite file the index rebuild never touches. The verifier also accepts the static `API_KEY` (synthetic full-scope bearer), so Claude Code CLI keeps working and flipping the flag is backward-compatible. Off = the minimal static-bearer gate (`BearerAuthASGI`), unchanged. **DCR is open but `https` redirect URIs are restricted to `MCP_ALLOWED_REDIRECT_HOSTS` (`http` loopback-only); auth codes + refresh tokens are single-use with atomic claims; the sub allowlist is re-checked on the refresh grant; and revoking a token — or detecting refresh reuse — cascades to the whole `(client_id, sub)` token family.** Note: FastMCP's default localhost DNS-rebinding protection is disabled (`TransportSecuritySettings(enable_dns_rebinding_protection=False)`) so a real `Host`/`Origin` reaches `/mcp` behind the reverse proxy. Design: relay post #201. **Setup:** add `<RELAY_BASE_URL>/mcp/oauth/callback` to the PocketID client's redirect-URI allowlist before enabling; keep `OIDC_ALLOWED_SUBS` non-empty.

## MCP

Two surfaces with **identical tools**, server `instructions`, and the `relay://master-document` resource (post 0 as `text/markdown`):

- **`relay/mcp_server.py`** — in-process, served over Streamable HTTP at `/mcp`; tools call `relay.service` directly. Remote-capable, **recommended**.
- **`relay_mcp/server.py`** — legacy stdio proxy; runs on the client, proxies to REST over `RELAY_BASE_URL`. For clients that can't speak remote MCP (e.g. Claude Desktop). Full parity (same twelve tools); `git pull` + restart the client to update.

**Feature parity rule:** every tool added, removed, or changed in `relay/mcp_server.py` must be reflected in `relay_mcp/server.py` and vice versa. Tool names, parameters (names, types, defaults), and descriptions must match exactly across both files. Whenever you touch either MCP server file, update the other one in the same change.

> **Documented parity exception:** `add_attachment`'s **`path`** parameter is **stdio-proxy-only**. Only the stdio proxy runs on the client's machine, so only it can read a local file and stream it to relay (via the presigned slot flow). The in-process HTTP server must **never** gain `path` — reading a server-host path over an authenticated call would be an arbitrary file-read on the relay host. This is the single intentional divergence; everything else stays at exact parity.

The in-process server advertises relay's logo + website in the initialize `serverInfo` (`icons`/`websiteUrl`, MCP SEP-973, built from `RELAY_BASE_URL` → public `/assets/` marks). Clients that read `serverInfo.icons` show the brand mark instead of the generic globe; Claude's remote connectors don't render it yet ([claude-ai-mcp#152](https://github.com/anthropics/claude-ai-mcp/issues/152)) but light up automatically when they do.

| Tool | Description |
|------|-------------|
| `publish_post` / `update_post` / `get_post` / `delete_post` | CRUD posts (partial update; `id=0` = master doc, delete blocked) |
| `list_posts` | List (tag/search/limit/offset; `summary` defaults **true** = metadata + excerpt, no bodies — call `get_post` for a full body) |
| `add_attachment` / `create_upload` / `get_attachment` / `list_attachments` / `delete_attachment` | Attachment CRUD; `add_attachment` bytes via `data`/`source_url`/`upload_id`, `create_upload` mints a presigned slot (see [Attachments](#attachments)) |
| `list_tags` / `set_tag_config` | Tags with counts / per-tag expiry |

```bash
# Remote (recommended):
claude mcp add --transport http relay https://your-relay.example.com/mcp \
  --header "Authorization: Bearer <your-api-key>"
```

```jsonc
// Local stdio (Claude Desktop) — claude_desktop_config.json:
{ "mcpServers": { "relay": {
  "command": "uv",
  "args": ["run", "--project", "/path/to/relay", "relay-mcp"],
  "env": { "API_KEY": "<key>", "RELAY_BASE_URL": "https://your-relay.example.com" }
} } }
```

## Browser UI (`GET /ui`)

Single-page app on the REST API + SSE.

- **Posts:** compose panel, inline edit, delete-with-confirm, `expires_at` picker; live SSE feed (new posts flash + prepend).
- **Attachments:** 📎 button / drag-drop / paste (screenshots) in compose + edit forms → uploads and inserts `![[embed]]` at the cursor. Edit form lists the post-folder's files with delete (×).
- **Sidebar tabs — Tags / Tree / Files:** Tags filters by tag (create/rename/⚙ expiry); Tree filters the feed by folder (`GET /folders`); **Files** swaps the feed for an attachment gallery (thumbnails/chips, folder filter, click-to-enlarge lightbox, delete). Tag and folder filters are mutually exclusive.
- **Search:** debounced bar over the feed (title/content/source), combinable with a tag filter. The same bar holds the **sort control** (Updated/Created field + ↓/↑ direction toggle) and the list/grid view toggle; sort + view are persisted in `localStorage` (default: updated · desc).
- **Responsive:** sidebar → slide-in drawer on mobile (≤768px).

## Terminal UI (`uv run relay-tui`)

Textual two-panel split: TOPICS sidebar + FEED. `RELAY_PALETTE=<name>` picks a theme (`default`, `dracula`, `nord`, `gruvbox`, `solarized`, `molokai`, `candy`, `earthy`, `pastel`, `tango`); `RELAY_TRANSPARENT=1` lets the terminal background show through (editing modals stay opaque).

| Key | Action | | Key | Action |
|-----|--------|-|-----|--------|
| `n`/`e`/`d` | New / edit / delete post | | `a` | Browse attachments (open externally / delete) |
| `/` | Search (title/content/source) | | `t` | Toggle TOPICS Tags ⇄ Tree |
| `c`/`R` | Tag expiry / rename (TOPICS) | | `Enter` | View full post |
| `f` | Follow-link picker (in detail view) | | `r`/`Tab`/`q` | Refresh / switch panel / quit |
| `s`/`o` | Sort field (updated⇄created) / order (desc⇄asc) | | | (default: updated · desc) |

SSE runs in a background thread (`● live`/`○ offline`); reconnect replays via `Last-Event-ID`. Feed paginates 50/page, auto-loading on scroll.

## Tags · master doc · TTL

- **Tags:** front-matter list (`tags: [news, ai]`); in the index stored with sentinel commas (`,news,ai,`) for `LIKE '%,tag,%'` matching. Per-tag TTL canonical in `<vault>/.relay/tags.yml`, mirrored to the index. `PATCH /tags/{tag}` rewrites the tag across all posts atomically (SQL `REPLACE()`).
- **Search (`search=`):** SQLite **FTS5** full-text over title/content/source/tags — porter-stemmed, multi-term (implicit AND), prefix-matched, **bm25-ranked** (title/tags weighted above body, so the canonical post surfaces first). An external-content `posts_fts` vtable kept in sync by AFTER INSERT/UPDATE/DELETE triggers on `posts`, so it tracks every write path (service, MCP, watcher reindex, TTL cleanup) and is `'rebuild'`-populated at startup after the index rebuild. Free-text is sanitized to bare word-tokens before hitting FTS5 (operators like `"` `*` `:` `-` `()` can't cause a syntax error). Falls back to `LIKE` substring if the SQLite build lacks FTS5 (`database.FTS_ENABLED`).
- **Master doc (`id=0`)** — reserved `Master Document.md`, seeded at startup if absent; the index + instruction set for agents. `DELETE` is blocked (403), TTL-exempt, and the watcher recreates it if deleted externally. Update via `update_post(id=0, …)`.
- **TTL:** off by default (`DEFAULT_TTL_HOURS=0`). Precedence: per-post `expires_at` > per-tag config (`POST /tags/{tag}/config`) > global. For multi-tag posts, the shortest applicable TTL wins. Cleanup sleeps before its first run; `id=0` is exempt; errors are logged, never fatal.

## Project layout

```
relay/
├── main.py        # FastAPI app + lifespan (index init, cleanup loop, watcher, MCP session); mounts /mcp
├── config.py · auth.py · models.py · database.py   # settings · bearer auth · pydantic models · aiosqlite index (+ FTS5 search)
├── frontmatter.py # YAML front-matter + Obsidian filename rules (sanitize, collision suffix)
├── folders.py     # Folder placement policy (primary domain tag → folder)
├── links.py       # Wikilink/#id resolver + rename rewrite
├── vault.py       # Canonical file layer: posts + attachments, id allocation, index rebuild, tags.yml
├── watcher.py     # watchdog: external edits → reindex + SSE (self-write suppressed)
├── service.py     # Shared post/tag/attachment logic — file-first via vault, then mirror to index
├── ingest.py      # Attachment byte transports: source_url fetch (SSRF-guarded) + presigned upload slots
├── mcp_server.py  # In-process FastMCP server (/mcp); static-bearer or OAuth (MCP_OAUTH_ENABLED)
├── mcp_oauth/     # Remote MCP OAuth AS: store.py (hashed oauth.db) · provider.py · pocketid.py (broker) · broker.py (callback)
├── events.py · cleanup.py   # SSE broadcast hub · TTL cleanup loop
├── metrics.py     # Zero-dep Prometheus counter registry + text renderer (/metrics)
└── routes/        # posts · tags · attachments · folders · links · events · metrics (thin — delegate to service)
relay_mcp/server.py            # Legacy stdio MCP proxy (REST client)
relay/static/index.html        # Browser UI (/ui)
relay_tui/                      # Textual TUI — app.py · api.py · sse.py · theme.py · palettes/ · widgets/
```
