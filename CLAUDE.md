# relay — Claude Code guide

Personal knowledge base: plain-Markdown, **Obsidian-compatible vault** with an AI-integration layer. AI agents publish/query/subscribe over MCP, REST, and SSE; humans edit the *same* files in Obsidian/nvim or the browser/TUI. Posts are tagged, filed into first-level folders, and cross-linked (`[[wikilinks]]`/`#id`).

**Storage** (`RELAY_VAULT_PATH`): one `.md` file per post — the title *is* the filename, metadata in YAML front-matter (`id`, `tags`, `source`, timestamps, `expires_at`; **no `title`** field). Files are canonical; a disposable **SQLite index** at `<vault>/.relay/index.db` is rebuilt at startup. `id` is authoritative and survives renames. **Ids are monotonic and never reused** — a high-water mark in `<vault>/.relay/last_id` prevents a deleted id from being handed to a new post and silently repointing `#id` cross-links.

**Folders** (`relay/folders.py`): one folder per domain (`Homelab/`, `Finance/`, … + `Meta/`, `Digests/`, `Inbox/`). Placement is derived from the **first domain tag** at creation. A tag-less note in `Inbox` is moved to a domain folder when it gains its first tag; real folders are human-owned and never auto-moved. Scans are recursive; nesting is one level.

## Running

```bash
cp .env.example .env
uv run uvicorn relay.main:app --reload      # http://localhost:8000, docs at /docs
docker compose up -d                        # or Docker
docker compose pull && docker compose up -d # update

uv run pytest -q                            # tests (incl. 116 browser smokes)
uv run ruff check .                         # lint (config in pyproject.toml)
uv run playwright install chromium          # once, for browser smokes
RELAY_EVAL_URL=... RELAY_EVAL_KEY=... uv run pytest -m eval -s  # search-quality recall/MRR baseline (tests/eval), skipped otherwise
```

Uses `uv` — never `pip`. Add deps with `uv add <package>` — never hand-edit `pyproject.toml`'s `dependencies` and skip `uv lock`. This caused a real production outage (v1.1.0 → v1.1.1): `sqlite-vec`/`fastembed` were added to `pyproject.toml` without a matching `uv lock`, and the Dockerfile installs via `uv sync --frozen`, which trusts the lockfile literally rather than re-resolving — so every image built from it installed a lockfile that never had those packages, and `relay/vectors.py`'s unconditional `import sqlite_vec` crashed the app on boot. `uv run` silently self-heals a stale lock locally; don't mistake that diff for noise and discard it — recognize it as the lockfile catching up to something pyproject.toml already promised, and commit it.

**Tests always run against a throwaway vault.** `tests/conftest.py` has an autouse `isolated_vault` fixture that repoints `settings.vault_path` under `tmp_path`. Never patch `vault_path` outside `tmp_path` — the real `.env` points at a live Obsidian vault and `rebuild_index` stamps ids into every id-less file it finds there. Put throwaway scripts in `tests/`. **Exception:** `tests/eval` reads real vault content — read-only, over REST, via `scripts/export_vault.py` into a snapshot under `tmp_path` — because the golden query set's expected ids are real posts; it never touches `vault_path` directly and is gated behind `RELAY_EVAL_URL`/`RELAY_EVAL_KEY` so it can't fire by accident. **`tests/eval/golden.yaml` is gitignored, not committed** — real recall queries and real post ids are personal, and this repo is public; copy `golden.example.yaml` to get started, same as `.env.example` → `.env`.

**CI** runs `ruff check` + `pytest` on every push/PR. Ruff: `E,F,I,UP,B,C4,SIM` at line-length 120. `docker.yml` publishes the image on pushes to `main`.

## API

All endpoints need `Authorization: Bearer <API_KEY>`.

| Method | Path | Description |
|--------|------|-------------|
| POST/GET | /posts | Publish / list (`tag`, `folder`, `limit`, `offset`, `search`, `summary`, `sort`, `order`, `mode`). `sort`=`updated`(default)/`created`; `order`=`desc`(default)/`asc`; FTS `search` ranks by bm25. `summary=true` → metadata+excerpt only. `mode`=`keyword`(default)/`semantic`/`hybrid` (relay #253, proof of concept) — 503 if embeddings aren't enabled, 400 if combined with `tag`/`folder` |
| GET/PATCH/DELETE | /posts/{id} | Get / partial update / delete |
| GET | /posts/deleted | Gone-but-restorable posts (id, title, sha, reason). **Declared before `/{id}`** or FastAPI parses `deleted` as an int |
| GET | /posts/{id}/backlinks | Posts linking here via `[[title]]` or `#id` |
| GET/POST | /posts/{id}/history · /posts/{id}/restore | Revisions / roll back to sha (recreates if deleted). 503 when history is off |
| GET | /posts/{id}/history/{sha} | Full body at one revision — short sha accepted |
| GET | /links | (id, title) index for wikilink resolution |
| GET | /folders | First-level folders with post counts |
| POST/GET | /attachments | Upload / list — bytes via `data` (base64), `source_url`, or `upload_id` |
| POST | /attachments/uploads | Mint a presigned upload slot |
| PUT | /attachments/uploads/{upload_id} | Stream bytes into a slot |
| GET/DELETE | /attachments/{path} | Serve / delete an attachment |
| GET | /tags | Tags with counts |
| POST/PATCH | /tags/{tag}[/config] | Set per-tag expiry / rename across all posts |
| GET | /events | SSE stream (`?tag=` filter); replay via `Last-Event-ID` |
| GET | /status | Runtime diagnostics — effective feature state, vault path + counts |
| GET | /metrics | Prometheus text (bearer-gated) |
| POST/GET | /mcp | Streamable HTTP MCP endpoint |

## Attachments

Non-`.md` files live in `<Folder>/assets/`, embedded as `![[file.png]]`. Names are vault-globally unique. Three byte transports (enforced exactly-one): `data` (base64), `source_url` (server fetches, SSRF-guarded), `upload_id` (presigned slot). Slots are in-memory + disk-staged under `.relay/uploads/`, single-use, TTL'd — **single-worker assumption**. `ATTACHMENT_MAX_MB` (25) → 413. Deleting a post removes only the attachments it embedded that no other post references.

## History

Every write commits the vault to `<vault>/.relay/history.git` (detached git-dir, vault as work-tree — no `.git` in the root, so Syncthing doesn't corrupt it). **Durable**: never wipe `history.git` (same standing as `oauth.db`). The index is disposable; history is not.

**`core.quotePath=false` is pinned on every git call.** Without it, non-ASCII filenames are octal-escaped and the module can't find the file's history. Test fixtures must include non-ASCII titles.

Coverage: every write path + TTL expiry + external edits (watcher commits each debounced batch). Recovery: `GET /posts/{id}/history` + `POST /posts/{id}/restore` — both work for deleted posts; restore keeps the original id. The restorable sha is `log -1 <delete>^ -- <path>` (the last commit that *touched* the file, not the delete commit itself).

Manual recovery: `cd /vault && export GIT_DIR=.relay/history.git GIT_WORK_TREE=.` then plain git. Full runbook: [docs/recovery.md](docs/recovery.md).

## Cross-links

- **`[[Title]]` / `[[Title|alias]]`** — case-insensitive; renaming rewrites inbound links across the vault.
- **`#NNN`** — stable across renames; prefer `[[Title]]` in Obsidian.

Stored verbatim, resolved at display time. Code spans/blocks are skipped.

## Configuration (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | required | Bearer token |
| `RELAY_BASE_URL` | `http://localhost:8000` | Used by the stdio MCP proxy |
| `DEFAULT_TTL_HOURS` | 0 | Global expiry; 0 = off |
| `CLEANUP_INTERVAL_MINUTES` | 60 | |
| `RELAY_VAULT_PATH` | /data/vault | |
| `RELAY_WATCH_ENABLED` | true | Live-reindex + SSE on external edits |
| `RELAY_HISTORY_ENABLED` | true | git commit per write; no-ops with a warning if `git` missing |
| `RELAY_EMBEDDING_ENABLED` | false | Semantic/hybrid search (relay #253, proof of concept) — see "Semantic search" below |
| `EMBEDDING_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | fastembed model id — no `RELAY_` prefix |
| `EMBEDDING_THREADS` | 1 | onnxruntime intra-op thread cap — embedding is sequential, so extra threads only cost memory |
| `SECURE_COOKIES` | true | `false` for plain HTTP |
| `ATTACHMENT_MAX_MB` | 25 | |
| `ATTACHMENT_UPLOAD_TTL_SECONDS` | 3600 | |
| `ATTACHMENT_FETCH_TIMEOUT_SECONDS` | 20 | |
| `OIDC_ISSUER` / `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | "" | PocketID OIDC; blank = API-key paste only |
| `SESSION_SECRET` | "" | Falls back to `API_KEY` |
| `SESSION_MAX_AGE_HOURS` | 720 | |
| `OIDC_ALLOWED_SUBS` / `OIDC_ALLOWED_EMAILS` | "" | Allowlists; both empty = any PocketID user |
| `MCP_OAUTH_ENABLED` | false | OAuth 2.1 AS+RS for `/mcp` (DCR + PKCE, brokered to PocketID) |
| `MCP_REQUIRED_SCOPES` | relay | |
| `MCP_ALLOWED_REDIRECT_HOSTS` | claude.ai,claude.com,chatgpt.com | DCR redirect-URI allowlist |

## Authentication

Two channels checked by `require_api_key` (`relay/auth.py`): **Bearer `API_KEY`** (machine-to-machine) and **`relay_session` cookie** (web UI, signed `itsdangerous` token). The cookie is verified with a **live allowlist re-check on every request** — dropping a sub from `OIDC_ALLOWED_SUBS` revokes in-wild sessions immediately. No per-token revocation; a captured cookie stays valid until expiry.

**MCP OAuth** (`MCP_OAUTH_ENABLED`): relay acts as its own OAuth 2.1 AS, brokering to PocketID. Tokens are opaque, hashed at rest, stored in `<vault>/.relay/oauth.db` (separate from `index.db`). The static `API_KEY` always works as a synthetic bearer. Setup: add `<RELAY_BASE_URL>/mcp/oauth/callback` to the PocketID client before enabling.

## MCP

Two surfaces, **identical tools**:
- **`relay/mcp_server.py`** — in-process, Streamable HTTP at `/mcp`. Recommended.
- **`relay_mcp/server.py`** — legacy stdio proxy for clients that can't speak remote MCP (e.g. Claude Desktop).

**Parity rule:** every change to one file must be reflected in the other. Tool names, parameters, and descriptions must match exactly. **`tests/test_mcp_parity.py` enforces this in CI** — it ast-parses both files, diffs names/params/descriptions, and checks every advertised tool is actually dispatched. Always update both files in the same change.

**Documented exception** (`PROXY_ONLY_PARAMS` / `DESCRIPTION_EXEMPT`): `add_attachment`'s `path` parameter is stdio-proxy-only. The in-process server must never gain `path` — that would be an arbitrary file-read on the relay host.

| Tool | Description |
|------|-------------|
| `publish_post` / `update_post` / `get_post` / `delete_post` | CRUD (`id=0` = master doc, delete blocked) |
| `list_posts` | List with filters; `summary` defaults true (metadata + excerpt, no bodies). `mode`=`keyword`(default)/`semantic`/`hybrid` (relay #253, proof of concept) — errors if embeddings aren't enabled or if combined with `tag`/`folder` |
| `add_attachment` / `create_upload` / `get_attachment` / `list_attachments` / `delete_attachment` | Attachment CRUD |
| `get_post_history` / `get_post_revision` / `restore_post` | History browse / preview / restore |
| `list_deleted_posts` | Restorable deleted posts (discovery — you need an id to restore) |
| `get_status` | Version, uptime, vault path + counts, effective feature state |
| `list_tags` / `set_tag_config` / `rename_tag` | Tag management |
| `get_backlinks` | Posts linking here — check before rewriting or deleting |

```bash
# Remote (recommended):
claude mcp add --transport http relay https://your-relay.example.com/mcp \
  --header "Authorization: Bearer <your-api-key>"
```

```jsonc
// Local stdio — claude_desktop_config.json:
{ "mcpServers": { "relay": {
  "command": "uv",
  "args": ["run", "--project", "/path/to/relay", "relay-mcp"],
  "env": { "API_KEY": "<key>", "RELAY_BASE_URL": "https://your-relay.example.com" }
} } }
```

## Browser UI (`GET /ui`)

Single-page app on the REST API + SSE. ES modules, no build step — nothing is on `window`; imported bindings are read-only, so shared mutable state uses exported objects or private setters (`api.js` owns `apiKey` behind `setApiKey`/`clearApiKey`).

**Critical invariants:**

- **Colour lives in tokens only.** Every value declared in the two `:root` blocks of `app.css`; components use `var(--token)` or `color-mix()` from one. `tests/test_css_tokens.py` enforces this — fails on literals outside the blocks or a token declared in one theme but not the other.
- **Every theme clears a contrast floor** (`test_every_theme_clears_the_contrast_floor`): `--text` ≥ 10:1, `--body` ≥ 9.5:1 against `--surface`; `--on-accent` ≥ 4.5:1 against `--accent`. Faithful reproductions that can't hit the house target are named in `REPRODUCTIONS` with their measured value. Fix a shortfall by choosing a different palette member — never mix a new colour.
- **Catppuccin dark flavours have different accents by design**: Frappé=peach, Macchiato=blue, Mocha=mauve. Catppuccin Latte chips use `--text` (not an accent) — every Latte accent member falls under AA as chip text.
- **Mobile override block lives at the END of `app.css`.** A single-class override earlier in the file silently loses on source order. Check line numbers, not just specificity.
- **Draggable elements**: `animation-fill-mode: backwards` and no `to` keyframe. A `both` fill + explicit `to` outranks inline styles and swallows drag transforms.
- **Markdown content rules must go through `.post-body`** — a rule on `.pm-body` directly matches nothing (the rendered markdown is wrapped).
- **`min-width: 0` on every grid tile area.** A `1fr` track's automatic minimum is min-content; any non-shrinkable child overflows the card.
- **Four modals share chrome.** `test_every_desktop_modal_shares_the_same_chrome` asserts they agree as a set — change the look once.
- **`apiSend` for DELETEs only** (no `Content-Type`, never throws on non-ok). Use `apiFetch` for anything that sends a body and needs to know if it worked.
- **Deleted post recovery lives in the status panel**, not the sidebar. It's a read over `history.git`, not a trash can.
- **History panel is fixed height** — panes built once, only contents swap. `min(82vh, 860px)`.
- **Header control order is a safety property**: `+ New Post` · theme · status · disconnect. Primary action and session-kill must not be adjacent. `test_header_controls_are_one_visual_set` pins this.
- **Use inline SVG, not glyphs or emoji** for icons. Colour emoji ignores CSS `color`; Unicode glyphs render unpredictably at small sizes.
- **iOS input zoom**: handled globally by `@media (hover: none) { input, textarea, select { font-size: 16px !important } }` — not per-form.

## Terminal UI

See [docs/tui.md](docs/tui.md) for keybindings, palette names, and transparency toggle.

`RELAY_PALETTE=<name>` selects a palette; `RELAY_TRANSPARENT=1` lets the terminal background show. Toggle transparency at runtime via `Ctrl+P` → *Draw theme background*.

## Tags · master doc · TTL

- **Tags:** front-matter list; stored with sentinel commas (`,news,ai,`) for `LIKE '%,tag,%'` matching. Per-tag TTL in `<vault>/.relay/tags.yml`.
- **Search:** SQLite FTS5 over title/content/source/tags — porter-stemmed, bm25-ranked. Falls back to `LIKE` if FTS5 unavailable.
- **Master doc (`id=0`):** `Master Document.md` at vault root, seeded at startup, `DELETE` blocked, TTL-exempt. Update via `update_post(id=0, …)`.
- **TTL:** off by default. Precedence: per-post `expires_at` > per-tag > global. Shortest TTL wins for multi-tag posts.

## Semantic search (proof of concept, relay #253)

`RELAY_EMBEDDING_ENABLED` (off by default everywhere, including production). Adds `mode=keyword|semantic|hybrid` to `GET /posts` and `list_posts` (both MCP surfaces), and a ranking-mode select in the browser UI search bar (hidden unless `features.search.embeddings` in `/status` is true). `mode=semantic|hybrid` 503s if unavailable, 400s if combined with `tag`/`folder` (the ranked path — `service._list_posts_ranked` — doesn't apply SQL filters, and deliberately errors rather than silently ignoring them).

- **Chunking** (`chunking.py`): H2/H3-aware, runt-merge, giant-split with overlap. **Embedding cache** (`vectors.py`): content-addressed by `(model_id, chunk_body)` hash — unchanged content is a cache hit, never re-embedded, and the cache survives restarts (no cascade from `posts`).
- **`.relay/models`** — fastembed's downloaded ONNX model. **`.relay/hf-home`** — `huggingface_hub`'s cache root (xet fast-transfer disabled via `HF_HUB_DISABLE_XET=1`; its own cache/log path ignores `cache_dir` entirely and is derived from `HF_HOME` at import time, so both are set at `embedding.py` module load, before fastembed can be imported anywhere). Both under `.relay/` so they ride the vault volume in Docker and survive container restarts instead of re-downloading.
- **Startup never blocks on embedding.** `rebuild_index`'s bulk pass runs with `index_upsert(..., sync_embeddings=False)` — SQL-only, fast, safe to run inline in the ASGI lifespan. `vault.backfill_embeddings` (the deferred work) runs as a background `asyncio.create_task` from `main.py`'s lifespan, same pattern as `cleanup_loop`, logging `Embedding backfill starting` → periodic `N/M posts checked` (15s cadence) → `Embedding backfill complete`. **This split exists because of a real production outage**: embedding every post inline at startup blocked the ASGI server from accepting *any* connection — including `/health` — until the whole backlog finished; on a real vault that's minutes of total downtime on every restart. Never reintroduce an inline bulk-embed call.
- **Known open issue:** memory footprint under a small VPS is still being characterized — the model stays resident for the process lifetime by design (avoids reloading per request). First mitigation shipped: `EMBEDDING_THREADS` (default 1) caps onnxruntime's intra-op thread pool — embedding here is sequential, so extra threads never bought throughput, only memory. Effect on actual steady-state MB not yet confirmed against production. Remaining levers if it isn't enough: checking actual MB via `docker stats` rather than %, and possibly unloading the model between uses (`embedding._backend = None`) at the cost of reload latency on the next search.

## Project layout

```
relay/
├── main.py          # FastAPI app + lifespan
├── config.py · auth.py · models.py · database.py
├── frontmatter.py   # YAML front-matter + Obsidian filename rules
├── folders.py       # Folder placement policy
├── links.py         # Wikilink/#id resolver + rename rewrite
├── vault.py         # File layer: posts + attachments, id allocation, rebuild, tags.yml
├── watcher.py       # watchdog: external edits → reindex + SSE
├── history.py       # git commit per write → <vault>/.relay/history.git
├── service.py       # Shared post/tag/attachment logic
├── ingest.py        # Attachment byte transports
├── chunking.py      # H2/H3-aware post chunking (semantic search POC)
├── embedding.py     # Swappable embedding backend (FastEmbed / Fake for tests)
├── vectors.py       # sqlite-vec schema, embedding cache, KNN, RRF (semantic search POC)
├── mcp_server.py    # In-process FastMCP server (/mcp)
├── mcp_oauth/       # Remote MCP OAuth AS
├── events.py · cleanup.py
├── metrics.py       # Zero-dep Prometheus counter registry
├── status.py        # Runtime diagnostics
└── routes/          # Thin route handlers — delegate to service
relay_mcp/server.py              # Legacy stdio MCP proxy
relay/static/index.html          # Browser UI markup (210 lines)
relay/static/ui/app.css          # UI stylesheet
relay/static/ui/js/main.js       # App entry point (ES module)
relay/static/ui/js/{util,api,status,feed-query,view-prefs,post-history,sheet,theme}.js
relay_tui/                       # Textual TUI — app.py · api.py · sse.py · theme.py · palettes/ · widgets/
scripts/export_vault.py          # Pull a live relay into a fresh vault
```

## Exporting a vault

```bash
uv run python scripts/export_vault.py --source https://your-relay.example.com --vault ./snapshot
```

Pulls every post over REST and builds an index — output is a vault relay can serve as-is. Standalone script with no test coverage; re-run manually after changing `vault.write_file`. Caveats: per-tag TTL config isn't exported; `--vault` must be empty.
