# relay — Claude Code guide

Personal knowledge base kept as a plain-Markdown, **Obsidian-compatible vault** with an AI-integration layer on top. AI agents publish/query/subscribe over MCP, REST, and SSE; humans edit the *same* files in Obsidian/nvim or the browser/terminal UIs. Posts are tagged, filed into first-level folders, cross-linked (`[[wikilinks]]`/`#id`), and can expire via configurable TTL.

**Storage** (`RELAY_VAULT_PATH`): one `.md` file per post — the title *is* the filename, metadata in YAML front-matter (`id`, `tags`, `source`, timestamps, `expires_at`; **no `title`**). Files are canonical; a disposable **SQLite index** at `<vault>/.relay/index.db` mirrors them for fast queries and is rebuilt from files at startup. A `watchdog` watcher live-reindexes external edits (Obsidian/nvim) and pushes them via SSE. `id` in front-matter is authoritative and survives renames; `title` is required; everything is Markdown (no `format` field). **Ids are monotonic and never reused:** a high-water mark in `<vault>/.relay/last_id` (durable, like `oauth.db`/`history.git`) is maxed with the live table on every allocation. Without it `MAX(id)+1` handed a deleted post's id straight to the next one created, silently repointing every `#id` cross-link at unrelated content and making a post's history ambiguous. Seeded from the files present when the counter is absent, so existing vaults upgrade cleanly; a failed create burns an id rather than risking reuse, so ids are not contiguous.

**Folders** (`relay/folders.py`): one folder per domain (`Homelab/`, `Radio/`, `Finance/`, … plus `Meta/`, `Digests/`, `Inbox/`); master doc (#0) at the root. Folders are a browse aid — **tags stay primary for navigation**. Placement is *derived* from the **first domain tag** at creation, not stored, and never auto-moved on retag — **except** a tag-less note in `Inbox` (the unfiled bucket): when it gains its first domain tag, relay moves it (and its own attachments) into that domain folder. Moves only ever go *out of* Inbox; real folders stay human-owned (move a file in Obsidian and relay preserves it). Scans are recursive; nesting is one level.

## Running

```bash
cp .env.example .env            # set API_KEY
uv run uvicorn relay.main:app --reload      # local, http://localhost:8000 (docs at /docs)
docker compose up -d            # or Docker; update with: docker compose pull && docker compose up -d

uv run pytest -q                # 297 tests (incl. 24 browser smokes)
uv run ruff check .             # lint — config in pyproject.toml
uv run playwright install chromium           # once, for the tests/ui browser smokes
```

`GET /health` (no auth) is probed every 30s by the Dockerfile HEALTHCHECK and compose. Uses `uv` — never `pip`; add deps with `uv add <package>`.

**Browser smokes (`tests/ui/`).** 9 Playwright tests drive the real UI in Chromium against a real uvicorn on a throwaway vault, logging in through the actual API-key form. They exist because `relay/static/index.html` is the largest file in the repo, has the most `fix(ui)` commits, and had no automated coverage — they are the safety net that has to be in place *before* it gets split into modules. **Every one was mutation-checked**: each was confirmed to fail when the behaviour it covers is deliberately broken. That caught a vacuous test — the grid-overflow smoke passed even with `min-width: 0` removed, because the seeded posts were too tame to overflow; the fixture now seeds a hostile card (long `nowrap` source, wide table, unbreakable token) so the invariant is genuinely pinned. Without the browser installed they **skip**, not error (guarded at collection time). CI installs Chromium explicitly so the coverage can't silently vanish.

**Tests always run against a throwaway vault.** `tests/conftest.py` has an **autouse** `isolated_vault` fixture that repoints `settings.vault_path` under `tmp_path` for every test, and asserts on teardown that nothing moved it back out. This is not optional hygiene: `Settings` loads the developer's real `.env`, so an unpatched `settings.vault_path` resolves to their live Obsidian vault — a test that forgets to patch it *writes real notes* (and `rebuild_index` will stamp ids into any id-less file it finds there). Never patch `vault_path` to anything outside `tmp_path`, and never disable this fixture to "test the real thing". **conftest only covers files under `tests/`** — an ad-hoc script run from anywhere else resolves `vault_path` from the real `.env`, which has caused real writes twice — so `vault.vault_dir()` carries a backstop that raises under `PYTEST_CURRENT_TEST` when the vault isn't under the temp dir. Put throwaway test scripts in `tests/`.

**CI (`.github/workflows/tests.yml`) runs `ruff check` + `pytest` on every push and PR**, so both must pass before a change lands. Ruff selects `E,F,I,UP,B,C4,SIM` at line-length 120; each ignore is documented inline in `pyproject.toml` (notably `B008` for FastAPI's `Depends()` idiom, and a per-file `E501` exemption for `relay_mcp/server.py` whose tool-description strings must stay byte-identical to `relay/mcp_server.py` under the parity rule below). `docker.yml` builds and publishes the image on pushes to `main`. Workflow actions are pinned to majors except `astral-sh/setup-uv`, which stopped publishing major tags at v8 — it needs a full release tag (`@v10.0.1`).

## API

All endpoints need `Authorization: Bearer <API_KEY>`.

| Method | Path | Description |
|--------|------|-------------|
| POST/GET | /posts | Publish / list posts (`tag`, `folder`, `limit`, `offset`, `search`, `summary`, `sort`, `order`; master pinned on home feed). `sort` = `updated` (default, last-modified via `COALESCE(updated_at, created_at)`) or `created`; an **externally-edited** note (Obsidian/nvim leaves front-matter alone) takes its `updated_at` from the file's **mtime** — `vault.effective_updated_at`, applied by both the watcher and the startup rebuild, with a 2s slack so a fresh write never reads as an edit; `order` = `desc` (default) or `asc`; an FTS `search` ranks by bm25 first, then `sort`/`order` as tiebreak. `summary=true` → metadata-only items (`PostSummary`: id/title/tags/folder + plain-text `excerpt`, no `content`); REST default `false` (UI feed renders content inline), MCP `list_posts` default `true` |
| GET/PATCH/DELETE | /posts/{id} | Get / update (partial) / delete a post |
| GET | /posts/{id}/backlinks | Posts linking here via `[[title]]` or `#id` |
| GET | /posts/{id}/history/{sha} | The post **as it was** at one revision (title/content/tags) so a restore can be previewed; works for a deleted post; short sha accepted |
| GET/POST | /posts/{id}/history · /posts/{id}/restore | Revisions from vault history (works for a **deleted** post — `exists:false`) / roll back to a `sha`, recreating if deleted. 503 when history is off |
| GET | /links | (id, title) index — clients resolve `[[Title]]` wikilinks with this |
| GET | /folders | First-level folders with post counts |
| POST/GET | /attachments | Upload / list attachments — bytes via `data` (base64), `source_url` (server fetches), or `upload_id` (filled slot); see [Attachments](#attachments) |
| POST | /attachments/uploads | Mint a presigned upload slot (`upload_id` + `upload_url`) for out-of-band bytes |
| PUT | /attachments/uploads/{upload_id} | Stream raw bytes into a slot (single, capped body) |
| GET/DELETE | /attachments/{path} | Serve / delete an attachment file |
| GET | /tags | Tags with counts (incl. 0-count from tag_config) |
| POST/PATCH | /tags/{tag}[/config] | Set per-tag expiry / rename a tag across all posts |
| GET | /events | SSE stream (`?tag=` filter) |
| GET | /status | Runtime diagnostics as JSON (bearer-gated): version, uptime, vault path + counts, and **effective** feature state — see [Status](#status) |
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
- **Lifecycle:** deleting a post removes the attachments **that post embedded** which no other post references (shared assets kept). Scoped to its own `![[…]]` refs on purpose — a folder's `assets/` also holds files a human dropped in from Obsidian but hasn't linked yet, and sweeping every unreferenced file in the folder would delete those bystanders. Deleting an attachment reports the post ids still referencing it (now dangling).
- `ATTACHMENT_MAX_MB` (25) caps uploads → 413 (enforced on all three transports). `get_attachment` returns images as inline image content, size-guarded.

## Status

`GET /status` (bearer-gated, same reasoning as `/metrics` — it reports the vault path and size) and the `get_status` MCP tool return JSON: `version`, `uptime_seconds`, `started_at`, `sse_clients`, a `vault` block (path, posts, tags, folders, attachments, attachment_bytes) and a `features` block.

**The counts are the bonus; effective feature state is the point.** Relay degrades silently in ways visible only in a startup log line, and `features` reports what is *working*, not what is configured:

| Field | Why it matters |
|---|---|
| `history.effective` | `enabled` is intent; this is false when `git` is missing, meaning writes are **not** recoverable. `history.git` carries the version or `null`. An image shipped without git once already |
| `search.fts5` | false = search silently fell back to `LIKE` substring matching |
| `watcher.running` | false = external Obsidian/nvim edits are never re-indexed |
| `auth.mcp_oauth` | true only when the flag **and** an OIDC client are set — the flag alone can't broker a login |

`vault.path` answers "which vault am I actually talking to", which is not obvious when a local checkout and a remote deployment are both in play. Post/tag counts come from the same helpers `/metrics` uses (`relay/status.py`), so the two surfaces can't disagree. `/health` is untouched and stays public and trivial — it is probed every 30s by the Dockerfile HEALTHCHECK and compose.

## Metrics

`GET /metrics` exposes Prometheus text format 0.0.4 (`relay/metrics.py` — a zero-dep counter registry + renderer, no `prometheus_client`; Telegraf/Prometheus both scrape it). **Gated behind the same `require_api_key`** as the rest of the API (scraper sends `Authorization: Bearer <API_KEY>`) — relay is behind a public proxy, so an open `/metrics` would leak vault size/activity; the bearer gate needs no new config. On a trusted-network deploy you could bind it loopback/tailnet-only instead.

- **Counters** (process-lifetime, reset on restart): `relay_http_requests_total{method,path,status}` (a raw-ASGI middleware — *not* `BaseHTTPMiddleware`, so it never buffers the SSE/MCP streams; `path` is the matched route template or a bucketed first segment, so cardinality stays bounded), `relay_mcp_tool_calls_total{tool}` (in-process `/mcp` tools only — the stdio proxy runs on the client and its calls land as REST `http_requests`; **not** a parity concern — internal instrumentation, not part of the tool contract), `relay_search_queries_total`, `relay_cleanup_deletions_total`, `relay_upload_slots_purged_total`.
- **Gauges** (sampled from the DB/state at scrape time, always exact): `relay_posts_total`, `relay_tags_total`, `relay_sse_clients`, `relay_build_info{version}` (from `relay.__version__`, the single version source, also FastAPI's `version=`).

## SSE / real-time

`GET /events` for live push. On reconnect, send `Last-Event-ID` with the last post id — the server replays missed posts (`id > Last-Event-ID`) before the live stream.

Event types: `post` (new **or edited** — the watcher streams external edits) and `delete` (`data: {"id": N}`). A `keepalive` fires every 30s. **TTL expiry emits its own `delete`** — the cleanup loop's file unlink is self-delete-suppressed, so the watcher never sees it and the post would otherwise linger in every connected client until reload. Both edits and deletes are sent **without** an SSE `id:` so they can't rewind the client's cursor (no replay storm). Clients treat a `post` for a known id as an in-place update and never clobber an inline edit-in-progress.

Both `create_post` and `update_post` publish SSE, so API/MCP edits (incl. Inbox→domain moves) propagate live; clients refresh the active sidebar counts (Tags or Tree) on any streamed change. **Known limitation:** offline edits/deletes to already-seen posts aren't replayed on reconnect (catch-up is append-only).

## History

Every write commits the vault to a git repo, so a clobbered post is recoverable. `update_post` is a full-body replace and `delete_post` unlinks — before this, a bad reconstruction by one agent overwrote a canonical post with no way back.

- **Layout:** the repo is at `<vault>/.relay/history.git` with the **vault as work-tree**, every command passing `--git-dir`/`--work-tree`. There is deliberately **no `.git` in the vault root**: the vault is typically a Syncthing folder, and syncing a live object store between machines corrupts repos. `.relay/` is already Syncthing-ignored and invisible to Obsidian. The ignore rule for `.relay/` lives in the repo's `info/exclude`, so no `.gitignore` appears in the vault either.
- **Durable, unlike its neighbour.** `.relay/index.db` is disposable and rebuilt at startup; `history.git` must never be wiped (same standing as `oauth.db`).
- **Coverage:** every service write path (create/update/delete, attachment add/delete, tag rename), TTL expiry, **and external edits** — the watcher commits once per debounced batch, so Obsidian/nvim edits relay never saw through its API are captured too. Attachments are tracked, so the note and the assets deleted with it land in one commit and revert together.
- **Messages:** `post <id> <verb>: <title>`, `attachment add|delete: <name>`, `tag rename: a -> b (N post(s))`, `external edit: <file>`, `ttl expiry: N post(s)`, `vault: initial import`.
- **`core.quotePath=false` is pinned on every git call.** With git's default, a path containing any non-ASCII byte is printed quoted and octal-escaped (`"Digests/Digest mattutino \342\200\224 …"`). `--name-only` output is how the module learns a post's path, so titles with an em dash, arrow, accent or «» parsed to a path that does not exist: those posts reported **no history at all** and could not be restored. Titles are filenames, so that was most of a real vault. Test data must include non-ASCII titles — ASCII-only fixtures are exactly why this shipped.
- **Never a gate.** Every call swallows its errors and logs; a missing `git` binary disables history after one warning and writes proceed untouched. Commits run in a worker thread (never blocking the loop) under a lock, since each stages the whole tree.
- **Recovery, in-band:** `GET /posts/{id}/history` + `POST /posts/{id}/restore` (and the `get_post_history` / `restore_post` MCP tools) — both answer for a **deleted** post, and a restore keeps the original id so `[[links]]` and `#id` resolve again. A restore is an ordinary write, so it is itself committed and can be undone. Every revision is verified to carry the right front-matter `id` before it is listed or restored, because titles are filenames and a deleted note's path can be taken over by a different post. Only restorable revisions are listed — the delete commit itself is not, since the file has no blob there.
- **Recovery, by hand** — plain git, no relay involved; full runbook in [docs/recovery.md](docs/recovery.md):
  ```bash
  cd /path/to/vault && export GIT_DIR=.relay/history.git GIT_WORK_TREE=.
  git log --oneline --follow -- "Dev/Some Note.md"   # history of one note
  git show HEAD~2:"Dev/Some Note.md"                 # read a prior body
  git revert <sha>                                   # undo a bad write
  ```
- **Residual:** attachments are committed too, so the repo grows with binary uploads (bounded by `ATTACHMENT_MAX_MB`). Tracking them is deliberate — attachment deletion was a real data-loss path. Run `git gc` on the history repo if it ever matters.

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
| `RELAY_HISTORY_ENABLED` | true | Commit the vault to git after every write (see [History](#history)); no-ops with a warning if `git` is missing |
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
- **`relay_session` cookie** — human web-UI sessions. A **signed, expiring** token (`itsdangerous`) carrying `{sub, email}`; verified by signature + `SESSION_MAX_AGE_HOURS` + a **live allowlist re-check** (`auth.still_authorized`). The re-check runs on every request, so dropping a sub from `OIDC_ALLOWED_SUBS` revokes sessions already in the wild instead of letting them coast the full 30d — the same guarantee the MCP OAuth refresh grant gives (`mcp_oauth/provider.py`). Sub-allowlist only (the session stores `email` but not `email_verified`, so email allowlists stay login-time-only), and `sub=apikey` from the paste path is exempt. **Note:** there is still no *per-token* revocation — a captured cookie stays valid until it expires; `revoke_session` is a documented no-op.

The cookie is minted two ways: **OIDC login** via PocketID (`GET /auth/login` → PocketID authorize with PKCE/S256 → `GET /auth/callback` validates the ID token, enforces the allowlist — immutable `OIDC_ALLOWED_SUBS`, or `OIDC_ALLOWED_EMAILS` on **verified** emails only — sets the cookie), or the **API-key paste** break-glass (`POST /session`, synthetic `sub=apikey`). `GET /auth/me` reports session state + whether OIDC is configured (drives the UI login control); `GET /auth/logout` / `DELETE /session` clear it. Transient OAuth state (state/nonce/PKCE verifier) rides a short-lived `relay_oauth` cookie via Starlette `SessionMiddleware`.

**Remote MCP OAuth (`MCP_OAUTH_ENABLED`, off by default).** PocketID lacks Dynamic Client Registration and Claude's remote connector requires it, so relay acts as its **own** OAuth 2.1 Authorization Server and brokers the human login upstream to PocketID (reusing the Phase-1 OIDC client). When enabled, FastMCP (`mcp==1.27.1`) mounts `/authorize` `/token` `/register`(DCR) `/revoke` + RFC 8414/9728 metadata and wraps `/mcp` in `RequireAuthMiddleware`; a broker callback `/mcp/oauth/callback` (`relay/mcp_oauth/broker.py`) validates the PocketID id-token, enforces the **same `_authorized()` sub allowlist** as the web UI, and mints a relay auth code. Tokens are opaque, **hashed at rest**, audience-bound to `<RELAY_BASE_URL>/mcp` (RFC 8707), and stored in `<vault>/.relay/oauth.db` — a **separate** SQLite file the index rebuild never touches. The verifier also accepts the static `API_KEY` (synthetic full-scope bearer), so Claude Code CLI keeps working and flipping the flag is backward-compatible. Off = the minimal static-bearer gate (`BearerAuthASGI`), unchanged. **DCR is open but `https` redirect URIs are restricted to `MCP_ALLOWED_REDIRECT_HOSTS` (`http` loopback-only); auth codes + refresh tokens are single-use with atomic claims; the sub allowlist is re-checked on the refresh grant; and revoking a token — or detecting refresh reuse — cascades to the whole `(client_id, sub)` token family.** Note: FastMCP's default localhost DNS-rebinding protection is disabled (`TransportSecuritySettings(enable_dns_rebinding_protection=False)`) so a real `Host`/`Origin` reaches `/mcp` behind the reverse proxy. Design: relay post #201. **Setup:** add `<RELAY_BASE_URL>/mcp/oauth/callback` to the PocketID client's redirect-URI allowlist before enabling; keep `OIDC_ALLOWED_SUBS` non-empty.

## MCP

Two surfaces with **identical tools**, server `instructions`, and the `relay://master-document` resource (post 0 as `text/markdown`):

- **`relay/mcp_server.py`** — in-process, served over Streamable HTTP at `/mcp`; tools call `relay.service` directly. Remote-capable, **recommended**.
- **`relay_mcp/server.py`** — legacy stdio proxy; runs on the client, proxies to REST over `RELAY_BASE_URL`. For clients that can't speak remote MCP (e.g. Claude Desktop). Full parity (same twelve tools); `git pull` + restart the client to update.

**Feature parity rule:** every tool added, removed, or changed in `relay/mcp_server.py` must be reflected in `relay_mcp/server.py` and vice versa. Tool names, parameters, and descriptions must match exactly across both files. Whenever you touch either MCP server file, update the other one in the same change. **`tests/test_mcp_parity.py` enforces this in CI** — it ast-parses both files and diffs names, parameters, and descriptions. The rule previously relied on a `PostToolUse` hook nudging the agent, and the descriptions had silently drifted in 9 of 12 tools; a reminder is not a gate. (Parameter *types and defaults* aren't compared: one side is Python annotations, the other JSON Schema, and stdio documents defaults in prose.)

> **Documented parity exception** (encoded in the test as `PROXY_ONLY_PARAMS` / `DESCRIPTION_EXEMPT`)**:** `add_attachment`'s **`path`** parameter is **stdio-proxy-only**, and its description differs because it has to document that parameter. Only the stdio proxy runs on the client's machine, so only it can read a local file and stream it to relay (via the presigned slot flow). The in-process HTTP server must **never** gain `path` — reading a server-host path over an authenticated call would be an arbitrary file-read on the relay host. This is the single intentional divergence; everything else stays at exact parity.

The in-process server advertises relay's logo + website in the initialize `serverInfo` (`icons`/`websiteUrl`, MCP SEP-973, built from `RELAY_BASE_URL` → public `/assets/` marks). Clients that read `serverInfo.icons` show the brand mark instead of the generic globe; Claude's remote connectors don't render it yet ([claude-ai-mcp#152](https://github.com/anthropics/claude-ai-mcp/issues/152)) but light up automatically when they do.

| Tool | Description |
|------|-------------|
| `publish_post` / `update_post` / `get_post` / `delete_post` | CRUD posts (partial update; `id=0` = master doc, delete blocked) |
| `list_posts` | List (tag/search/limit/offset; `summary` defaults **true** = metadata + excerpt, no bodies — call `get_post` for a full body) |
| `add_attachment` / `create_upload` / `get_attachment` / `list_attachments` / `delete_attachment` | Attachment CRUD; `add_attachment` bytes via `data`/`source_url`/`upload_id`, `create_upload` mints a presigned slot (see [Attachments](#attachments)) |
| `get_post_history` / `restore_post` | Read a post's revisions / roll it back to a sha (recreates a deleted post, keeping its id) |
| `get_status` | Version, uptime, vault path + counts, and which features actually work |
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

- **Editing happens in its own modal** (`#editModal`), not inside the card. The form is unchanged; it just gets the room the reading modal already had, with the content field flexing to fill the panel. Inside a card it inherited the card's width — in grid view a ~200px column, where the textarea was a few words wide and a long note unusable. The card is looked up by `data-id` after saving rather than held across the edit, since the feed may have re-rendered meanwhile; a dirty body confirms before discarding.
- **Posts:** compose panel, delete-with-confirm, `expires_at` picker; live SSE feed (new posts flash + prepend). Clicking a card opens the detail modal — **except the master doc (`#0`)**, which is an inline accordion instead: a one-line peek that expands in place (`max-height` animated from JS, released to `none` after the transition so late reflow isn't clipped).
- **Attachments:** 📎 button / drag-drop / paste (screenshots) in compose + edit forms → uploads and inserts `![[embed]]` at the cursor. Edit form lists the post-folder's files with delete (×).
- **Tag row controls are inline SVG** (`ICON_PENCIL`, `ICON_CLOCK`), not glyphs. `✏︎` is U+270F plus a text-presentation selector and renders as a thin *horizontal* stroke at this size — read as a minus, so the rename button looked like delete. A gear was tried and rejected too: at 13px its spokes read as a brightness control, and since the button sets TTL a **clock** says what it actually does. Icons also become visible on touch (`@media (hover: none)`), where hover-only controls were unreachable.
- **Tag editors (rename / expiry):** only **one** may be open at a time — a module-level registry closes the previous one, and the form is dismissed by Save/Cancel, Escape, or clicking away. The form replaces the row's contents, so the gear is not there to click again; the row is restored intact on close. Its inputs need `min-width: 0` (and the row drops out of flex via `.tag-editing`): `datetime-local` has a wide min-content width and a flex child's automatic minimum is min-content, so without it the form cannot shrink and spills out of the sidebar, carrying its controls off-screen — the same automatic-minimum trap as the grid tiles. **How wide that widget renders depends on browser, locale and zoom**, so the smoke forces a 150px sidebar rather than trusting the CI browser to reproduce it.
- **Sidebar tabs — Tags / Tree / Files:** Tags filters by tag (create/rename/⚙ expiry); Tree filters the feed by folder (`GET /folders`); **Files** swaps the feed for an attachment gallery (thumbnails/chips, folder filter, click-to-enlarge lightbox, delete). Tag and folder filters are mutually exclusive.
- **Search:** debounced bar over the feed (title/content/source), combinable with a tag filter. The same bar holds the **sort control** (Updated/Created field + ↓/↑ direction toggle) and the list/grid view toggle; sort + view are persisted in `localStorage` (default: updated · desc).
- **Grid tiles are the tight constraint.** A tile is fixed-height with a `1fr` inner track, and a `1fr` track's automatic minimum is its items' *min-content* width — so any child that can't shrink (a `nowrap` source, a `nowrap` table) widens the track past the card border and everything inside then paints outside the frame. Every card grid area therefore carries `min-width: 0`; the source ellipsizes; feed tables use `table-layout: fixed` with wrapping cells (the modal keeps `nowrap` + `.table-scroll`). The footer drops what a tile has no room for — the created stamp when an edit stamp is present, the button captions, the body's `Last updated:` chunk — via CSS only, since the view toggle swaps a class on `.feed` and never re-renders. **Check any new card element against a narrow tile.**
- **Assets are versioned by path**, not query string: `/static/<version>/js/main.js`, where `<version>` is `relay.__version__` + a short digest of every file under `relay/static/ui/` (so it also moves during development). `GET /` substitutes `__ASSETS__` in the markup and is served `no-cache`; versioned assets are `immutable` with a one-year max-age. **Why the path and not `?v=`:** `main.js` does `import './status.js'`, and a query string on the entry point does not propagate to its imports — a path segment does, because the browser resolves relative imports against the versioned directory. This exists because a proxy caching `/static` handed a browser the new markup with the previous release's script (button present, no handler). The plain `/static/js/main.js` form still resolves, for browsers holding a cached shell.
- **File layout.** `index.html` is now **185 lines of markup**; everything else lives under `relay/static/ui/`, mounted at `/static` (public, like `/assets`, which stays brand-marks-only). The only inline script left is the **before-paint theme script**, which sets `data-theme` before the first paint and cannot move without a flash.
- **ES modules, no build step.** `main.js` is loaded with `<script type="module">` and imports `./util.js` (pure helpers), `./api.js`, `./status.js`. Native ESM keeps the zero-dependency, no-bundler posture — there is no `package.json` for the app itself. Two consequences to respect: **nothing is on `window` any more** (safe here only because the markup carries no inline `on*` handlers — check before adding one), and **an imported binding is read-only**, so shared mutable state cannot be an `export let`. `api.js` owns `apiKey` privately behind `setApiKey`/`clearApiKey` for exactly that reason; the remaining cross-section state (`authed`, `activeTag`, `offset`, …) is why `main.js` is still ~1,300 lines and is the next thing to untangle.
- **Shared state is owned, not global.** `feed-query.js` exports a `query` object (`tag`, `folder`, `search`, `offset`, `total`) plus `resetPaging()`; `view-prefs.js` owns the list⇄grid mode and sort field/order together with their `localStorage` keys, and takes a reload callback so it knows *when* to reload without knowing *how*. These were six top-level `let`s reachable from every section — naming the concept (the query the feed is showing) is what removed the coupling. State rides on an exported **object** because an imported binding is read-only; `api.js` uses private state + setters for the same reason.
- **Splitting further:** extract along the section markers in `main.js`, one module per PR, and run `tests/ui` after each. A module that owns DOM should wire its own controls and export only what another module genuinely calls (`status.js` exports `closeStatusModal`/`isStatusOpen` purely so the single Escape handler keeps its original priority over the post modal). **Renaming a global into a property is not safe as a blind find-and-replace** — a local `const total` shadowed one and became `const query.total`, and an object shorthand `{ limit, offset }` became invalid syntax. Reverse the renames and diff against the original to prove nothing else moved. Still un-owned in `main.js`: `authed`, `sidebarMode`, `attachFolder`, `es`.
- **History panel:** a 🕑 History button in the post modal opens a **two-pane** panel over `GET /posts/{id}/history` — revision list on the left, preview on the right — at a **fixed height** (`min(82vh, 860px)`), stacking on ≤860px. Both are load-bearing: the panel was previously sized by its contents, so selecting a revision collapsed it to the height of the loading line and re-inflated when the body arrived, jumping on every click. The panes are built once and only their *contents* swap; each scrolls internally. A smoke measures the shell across every revision, mid-load included, and fails on any change. **Preview then restore** — selecting a revision fetches its body via `/history/{sha}` and only then offers Restore, because the listing is metadata-only and picking a sha blind is a poor way to undo something. Restore confirms, then reloads the feed. Bodies render with `textContent`, never `innerHTML`. **Limitation:** the entry point is the post modal, so the UI can only reach history for a post that still *exists* — recovering a **deleted** post remains a REST/MCP job (or `docs/recovery.md`).
- **Status panel:** a header `i` button (shown once authed, alongside `+ New Post`) opens a narrow read-only modal over `GET /status` — a **Health** block with coloured dots (history `bad` when git is missing, since writes are then unrecoverable; search and watcher `warn` when degraded), then Vault and Server details. Built with `textContent`/`createElement` throughout, never `innerHTML`, since it renders server-provided strings like the vault path. Reuses `.pm-backdrop` and the `modalIn` keyframes; `fmtBytes` is shared with the attachments gallery.
- **Responsive:** sidebar → slide-in drawer on mobile (≤768px); on desktop a header toggle collapses it to zero width, persisted in `localStorage`. The status modal becomes a bottom sheet at ≤768px like the post modal.

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
├── vault.py       # Canonical file layer: posts + attachments, monotonic id allocation, index rebuild, tags.yml
├── watcher.py     # watchdog: external edits → reindex + SSE (self-write suppressed)
├── history.py     # git commit per write → <vault>/.relay/history.git (detached git-dir, vault as work-tree)
├── service.py     # Shared post/tag/attachment logic — file-first via vault, then mirror to index
├── ingest.py      # Attachment byte transports: source_url fetch (SSRF-guarded) + presigned upload slots
├── mcp_server.py  # In-process FastMCP server (/mcp); static-bearer or OAuth (MCP_OAUTH_ENABLED)
├── mcp_oauth/     # Remote MCP OAuth AS: store.py (hashed oauth.db) · provider.py · pocketid.py (broker) · broker.py (callback)
├── events.py · cleanup.py   # SSE broadcast hub · TTL cleanup loop
├── metrics.py     # Zero-dep Prometheus counter registry + text renderer (/metrics)
├── status.py      # Runtime diagnostics for /status + get_status (shared counts with /metrics)
└── routes/        # posts · tags · attachments · folders · links · events · metrics (thin — delegate to service)
relay_mcp/server.py            # Legacy stdio MCP proxy (REST client)
relay/static/index.html        # Browser UI — markup only (185 lines)
relay/static/ui/app.css        # UI stylesheet          → /static/app.css
relay/static/ui/js/main.js     # App entry point (ES module) → /static/js/main.js
relay/static/ui/js/{util,api,status,feed-query,view-prefs,post-history}.js   # Extracted modules
relay_tui/                      # Textual TUI — app.py · api.py · sse.py · theme.py · palettes/ · widgets/
scripts/export_vault.py        # Operator tool: pull a live relay into a fresh vault (see below)
```

## Exporting a vault

```bash
uv run python scripts/export_vault.py --source https://your-relay.example.com --vault ./snapshot
```

Pulls every post (incl. `#0`, which `GET /posts` omits) over REST, writes the Markdown files with front-matter and folder placement, then builds the index — so the output is a vault relay can serve as-is. Use it to snapshot a **remote** instance to local disk or to seed a second one. It's a standalone client: nothing imports it and no test covers it, so **re-run it after changing `vault.write_file`** — ruff won't catch signature drift.

Two caveats: per-tag TTL config isn't exported (no REST read endpoint — re-apply with `set_tag_config`), and `--vault` must be a new or empty directory, since writing into a populated vault suffixes collisions rather than merging.
