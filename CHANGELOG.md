# Changelog

All notable changes to relay are documented here. Releases follow [semantic versioning](https://semver.org): minor bumps add capability, patches fix bugs or correct docs within a minor line.

---

## [Unreleased]

---

## [1.1.1] — 2026-09-02

Four fixes found deploying v1.1.0 to production for the first time — this path (real vault, `RELAY_EMBEDDING_ENABLED=true`, actual internet traffic) had never been exercised before. All four are internal/operational; no API or MCP surface changed.

### Fixed
- App failed to boot at all: `sqlite-vec`/`fastembed` were added to `pyproject.toml`'s dependencies (v1.1.0's own POC commit) but `uv.lock` was never regenerated to match. The Dockerfile installs via `uv sync --frozen`, which trusts the lockfile literally rather than re-resolving, so every image built from it installed a lockfile that never had those packages — `relay/vectors.py`'s unconditional `import sqlite_vec` crashed on startup regardless of `RELAY_EMBEDDING_ENABLED`. `uv lock` regenerated and committed
- Model download failed with a permission error: relay's container runs as an arbitrary host UID with no matching `/etc/passwd` entry (`docker-compose.yml`'s `user:`, deliberate — keeps vault writes host-owned), so `$HOME` is unset and `huggingface_hub`'s default cache path resolves to an unwritable location under `/`. `FastEmbedBackend` now passes an explicit `cache_dir` (`<vault>/.relay/models`) — same pattern as `uploads_dir`/`database_path`, rides the existing vault volume
- Same download still failed after that fix: `huggingface_hub`'s xet fast-transfer backend has its *own* cache/log path (`HF_XET_CACHE`), derived from `HF_HOME` as a plain module-level constant computed once at that module's import time — it never reads `cache_dir` at all, a completely separate mechanism. `HF_HOME` and `HF_HUB_DISABLE_XET=1` are now set at `embedding.py` module load, before anything can import `huggingface_hub` for the first time
- The real outage: once the model actually downloaded, the whole site 502'd — including `/health` — with CPU and memory maxed, for as long as the initial embedding backlog took. `rebuild_index`'s bulk pass called `vectors.sync_post_chunks` (real, sequential, CPU-bound inference) for every post, and it ran inline in the ASGI lifespan's `await init_db()`, before the server accepts any connection. Embedding sync is now deferred to `vault.backfill_embeddings`, a background task scheduled after startup — committed per post (a crash mid-run doesn't lose progress) and logging periodic `N/M posts checked` progress

### Known issue
- Memory footprint under a small VPS climbs during the initial backfill and doesn't come back down afterward — expected in shape (the model stays resident by design, to avoid reloading per search) but the steady-state cost on constrained hardware isn't characterized yet. Tracked as the next priority; not fixed in this release. Candidate levers: capping fastembed/onnxruntime's thread pool (`TextEmbedding(threads=...)`, never set today), confirming actual MB via `docker stats` rather than reading %, unloading the model between uses at the cost of reload latency

---

## [1.1.0] — 2026-09-02

Relay #253 phases 2-5: sqlite-vec semantic/hybrid search moves from an internal proof of concept (Python-only, reachable solely from `tests/eval`) to something reachable from REST, MCP, and the browser UI. Still off by default everywhere (`RELAY_EMBEDDING_ENABLED=false`) — this ships the surface, not a changed default.

### Added
- Semantic and hybrid search modes alongside the existing keyword/FTS5 mode: chunk-level embeddings (H2/H3-aware chunking, runt-merge, giant-split with overlap), a content-addressed embedding cache, sqlite-vec KNN search, and reciprocal rank fusion for hybrid. Measured against a real vault (21 golden queries): keyword recall@5=0.540/MRR=0.418, semantic 0.659/0.667, hybrid 0.687/0.667 — hybrid's per-query confidence gate (RRF weighted 8:1 toward semantic only when semantic's own top-1 distance is close) beats both keyword and semantic-only on recall, and ties semantic on MRR
- `mode=keyword|semantic|hybrid` is now a real parameter on `GET /posts` and the `list_posts` MCP tool (both the in-process and stdio proxy servers, kept in parity) — previously only reachable as a Python kwarg the eval harness called directly
- `GET /status` reports `features.search.embeddings`: whether semantic/hybrid ranking is actually usable on this relay, not just configured
- Browser UI: a ranking-mode select in the search bar, hidden until the client confirms via `/status` that embeddings are enabled; mutually exclusive with tag/folder filters, matching the server's own rule

### Fixed
- Semantic search ran its embedding call — and, on the very first call ever, the model load — directly on the event loop. relay is single-worker, so one semantic query would stall every other in-flight request (other API/MCP calls, SSE delivery) for the duration. Now offloaded via `asyncio.to_thread`
- The ranked-search candidate pool was a flat 50 regardless of the caller's actual `limit`/`offset`, so pagination silently dead-ended past it and the reported `total` undercounted real matches. The pool is now sized from `offset + limit`, capped at 200 to bound sqlite-vec's KNN cost
- `mode=semantic`/`hybrid` combined with `tag`/`folder` now 400s instead of silently ignoring the filter — the ranked path doesn't apply SQL filters. An invalid `mode` value now errors instead of silently falling back to keyword when called through the in-process MCP server, which has no query-parameter validation layer in front of it (REST already validated this)

---

## [1.0.0] — 2026-08-31

Baseline for post #253's "Relay 2.0 — Semantic Layer" idea: no semantics shipped here, just the fix and the measurement the sequencing plan calls for before deciding whether to build it.

### Fixed
- Search: `_fts_query` implicitly AND-joined every token including stopwords, so natural-language queries ("what did we decide about the notes backend") almost always matched nothing — and worse, a short/common token's prefix match (e.g. Italian `"e"*`) could make AND accidentally "succeed" against an irrelevant giant post that merely contained it somewhere, returning garbage instead of nothing. Now OR-joined, still bm25-ranked, so a post matching more terms still outranks one matching fewer

### Added
- `tests/eval/` — a golden-query recall@5/MRR harness, scored against a real vault snapshot pulled read-only over REST (gated behind `RELAY_EVAL_URL`/`RELAY_EVAL_KEY`, skipped otherwise; `golden.yaml` itself is gitignored — real recall queries and post ids are personal, this repo is public; `golden.example.yaml` documents the shape). Measured FTS5-only baseline: recall@5=0.540, MRR=0.414 — the number future semantic-search work is measured against

---

## [0.10.1] — 2026-08-29

### Fixed
- The 0.10.0 fix for crushed modal tables didn't actually work: `min-width` on `th`/`td` is silently ignored by `table-layout: fixed` (verified against a real Chromium render, not just the spec — the columns stayed exactly as crushed). Moved the floor to the `<table>` element itself, set inline by `main.js` from the column count when it wraps the table in `.table-scroll` — that *is* honored as a hard minimum under fixed layout, so an over-wide table now genuinely overflows into the scroll wrapper instead of imploding to unreadable columns

---

## [0.10.0] — 2026-08-29

### Fixed
- Post detail modal: many-column tables (9-10 cols) no longer crush cell text to one syllable per line on narrow/mobile widths — cells get a `min-width` floor and overflow into the existing horizontal scroll wrapper instead

---

## [0.9.6] — 2026-08-22

### Fixed
- Mobile post-header: title now stacks above its tags instead of sharing a row, so several tags no longer crush a long title down to a couple visible characters
- Mobile search bar: the search input can now actually shrink (it had no `min-width`), and the sort control and view toggle are trimmed to fit alongside it — previously they were pushed off-screen entirely
- `relay/__init__.py`'s `__version__` had drifted from `pyproject.toml` since the 0.9.5 release (dependency-bump commit missed it); both now read 0.9.6

---

## [0.9.5] — 2026-08-20

### Changed
- Dependency bumps: `fastapi` ≥0.141.1, `aiosqlite` ≥0.22.1, `textual` ≥8.2.8, `pydantic-settings` ≥2.15.0, `pytest` (dev) ≥9.1.1

---

## [0.9.4] — 2026-08-20

### Added
- `docs/stability.md` — stability policy: what the version number promises (19 MCP tools + REST surface), what is explicitly out of scope (browser UI internals, SQLite index schema, `.relay/` layout), and the versioning rules for breaking vs. additive changes (closes 1.0-D)
- Pre-1.0 surface freeze review documented: all 19 tool names and REST paths confirmed as-is; no renames needed

---

## [0.9.3] — 2026-08-20

### Added
- `aria-live="polite"` on the connection indicator (`#liveLabel`) so screen readers announce state changes; hidden `#a11yAnnouncer` region announces new SSE-inserted posts (`"New post: <title>"`); edits to existing cards are not announced (closes 1.0 should-do: `aria-live` gap)
- Theme picker now groups by family with labelled separators — Relay · ANSI · Catppuccin · Gruvbox · Solarized · ungrouped — making seventeen themes scannable without scrolling

### Fixed
- `resolve_attachment` and `list_attachments` no longer `rglob` the vault on every call; assets-directory listing is now cached (keyed by vault path) and invalidated on write/delete (closes 1.0 should-do: O(vault) rglob on hot paths)

---

## [0.9.2] — 2026-08-20

### Added
- Watcher ignores Syncthing conflict copies (`*.sync-conflict-*`) and versioned files (`.stversions/`) — both carry stale `id:` front-matter that would silently corrupt the index (closes 1.0-A)
- `CONTRIBUTING.md` — setup, test/lint commands, MCP-parity and CSS-token invariants, PR conventions
- Issue templates (bug report, feature request) and Dependabot config for pip and GitHub Actions

---

## [0.9.1] — 2026-08-19

### Added
- Single-worker startup guard: relay now refuses to start if `WEB_CONCURRENCY > 1` (see `docs/setup.md`)
- `SECURITY.md` — private disclosure path for security issues
- `CHANGELOG.md` — full release history from v0.1.0

---

## [0.9.0] — 2026-08-19

### Added
- **Keyboard navigation** in the feed (`j`/`k` to move, `Enter` to open, `Esc` to dismiss) and a shortcuts panel (`?`)
- **Post-modal navigation stack** — `[[wikilink]]` links open in-place and `←` walks back; the full chain is preserved
- **`[[wikilink]]` hover preview** — hover a link in rendered Markdown to peek at the target post without opening it
- **Code-block copy button** — one click copies the raw block contents
- **Solarized Dark / Solarized Light** themes — seventeen themes total
- TUI: post history panel, full palette overhaul aligned with web tokens (Tokyo Night, Molokai, Solarized), background-transparency toggle at runtime (`Ctrl+P` → *Draw theme background*)

### Changed
- Tokyo Night and Molokai accent colours punched up from over-pastel originals
- Catppuccin decorative chip contrasts documented and verified in `app.css`

### Fixed
- Brand mark centred in the logo; iOS given its own app icon (previously shipped as v0.8.1 in code but never tagged — reached a published image here)

---

## [0.8.0] — 2026-08-17

### Added
- **Deleted-post recovery end-to-end** — `GET /posts/deleted` lists restorable posts; `POST /posts/{id}/restore` recreates a deleted post keeping its original id; the status panel has a **Recovery** section with preview-and-restore
- **History diff view** — the history panel now diffs any revision against the current post body before restoring
- Four new MCP tools: `list_deleted_posts`, `get_post_revision`, `get_backlinks`, `rename_tag`
- `list_posts` gains `folder`, `sort`, and `order` parameters (REST and MCP)

---

## [0.7.2] — 2026-08-17

### Added
- **Theme system — fifteen themes**: Relay Dark, Relay Light, ANSI Dark, ANSI Light, Catppuccin Frappé/Macchiato/Mocha/Latte, Dracula, Everforest Dark, Gruvbox, Gruvbox Light, Molokai, Nord, Tokyo Night
- Theme picker with live colour swatches
- Reading column width for the post modal on wide displays

---

## [0.7.1] — 2026-08-16

### Fixed
- Corrected stale claims in README, `docs/api.md`, and `docs/setup.md` (`RELAY_HISTORY_ENABLED` documented; "content feed" / `sqlite` references removed from MCP instructions)
- Cut as a patch to publish the updated MCP `instructions` to a tagged image (`:0.7.0` predated the merge)

---

## [0.7.0] — 2026-08-16

### Added
- **Themeable colour-token layer** — all 37 colour literals routed through a semantic token layer in `app.css`; a theme is now a ~25-line `:root` override block, no component rules change per theme
- **`--accent-2` / `--accent-3`** secondary accent tokens
- Contrast floor enforced in CI: `--text` ≥ 10:1, `--body` ≥ 9.5:1 against `--surface`; `--on-accent` ≥ 4.5:1 against `--accent`

### Changed
- Desktop modals enlarged for readability
- Mobile modals replaced with real bottom sheets (grab handle, drag-to-dismiss, snap physics)

---

## [0.6.2] — 2026-08-15

### Fixed
- History panel no longer resizes when navigating between revisions; given additional vertical space to read long diffs

---

## [0.6.1] — 2026-08-15

### Fixed
- Static asset URLs versioned by content hash — a deploy can no longer serve a stale cached script
- `core.quotePath=false` pinned on every git call — non-ASCII post titles previously had no revision history

---

## [0.6.0] — 2026-08-15

### Added
- **Post history panel in the browser UI** — list a post's revisions, preview any revision's body, and restore to it with one click

---

## [0.5.1] — 2026-08-15

### Changed
- Feed shared state given an explicit owner (`api.js`) — imported ES bindings are read-only; shared mutable state now uses exported objects and private setters

---

## [0.5.0] — 2026-08-15

### Added
- **Browser smoke suite** — 11 Playwright tests covering the core read/write/navigation paths

### Changed
- `index.html` split from a 2,564-line monolith into a 185-line shell + ES modules under `relay/static/ui/js/`

---

## [0.4.0] — 2026-08-15

### Added
- `GET /status` — version, uptime, vault path + counts, and **effective** feature state (history off if git is missing, FTS5 fallback, watcher stopped)
- `get_status` MCP tool (both in-process and stdio-proxy servers)

---

## [0.3.0] — 2026-08-15

### Added
- `GET /posts/{id}/history` — list all revisions of a post (works for deleted posts; returns `exists: false`)
- `GET /posts/{id}/history/{sha}` — fetch the full body at any revision (short sha accepted)
- `POST /posts/{id}/restore` — roll back to any revision; recreates a deleted post keeping its original id
- `get_post_history`, `restore_post` MCP tools on both servers

### Fixed
- Post ids are never reused — a high-water mark in `.relay/last_id` prevents a deleted id from being handed to a new post and silently repointing `#id` cross-links

---

## [0.2.1] — 2026-08-15

### Fixed
- Watcher failed to re-index a deleted note restored byte-identically (self-write suppression swallowed the external edit)

---

## [0.2.0] — 2026-08-15

### Added
- **Vault history** — every write (including TTL expiry and external Obsidian/nvim edits) is committed to a detached git repo at `<vault>/.relay/history.git`. The vault is the work-tree; no `.git` in the root so Syncthing stays safe. Controlled by `RELAY_HISTORY_ENABLED` (default `true`; no-ops with a warning if git is missing)

---

## [0.1.1] — 2026-08-15

### Added
- UI: collapsible sidebar, master-doc accordion, card-overflow fix
- Feed sort toggle (created/updated · ascending/descending) in the browser UI and TUI
- CI: `ruff check` + `pytest` run on every push/PR

### Fixed
- Session allowlist (`OIDC_ALLOWED_SUBS`) now re-checked on **every** request — a revoked sub's in-wild session is invalidated immediately
- Orphan asset cleanup scoped correctly on delete (previously could remove attachments still referenced by other posts)
- External edits (Obsidian/nvim) now stamp the correct `updated_at`
- TTL deletions are now streamed to SSE subscribers

---

## [0.1.0] — 2026-07-20

Initial release.

### Added
- **Posts** — Markdown files in `RELAY_VAULT_PATH`; YAML front-matter (`id`, `tags`, `source`, timestamps, `expires_at`); id is authoritative and survives renames; `[[wikilink]]` / `#id` cross-links resolved and rewritten on rename
- **Folders** — one folder per domain; placement derived from the first domain tag; `Inbox/` holding area for tag-less posts
- **Full-text search** — SQLite FTS5 with bm25 ranking (title and tags weighted above body); `LIKE` fallback when FTS5 is unavailable
- **REST API** — `POST/GET /posts`, `GET/PATCH/DELETE /posts/{id}`, backlinks, history, folders, tags, links index, attachments, events (SSE), status, metrics
- **MCP** — in-process Streamable HTTP server at `/mcp` (19 tools); legacy stdio proxy for Claude Desktop
- **Browser UI** — single-page app over REST + SSE; no build step; ES modules
- **Terminal UI** — Textual-based TUI with post list, editor, and status display
- **Attachments** — `data` (base64), `source_url` (server-side fetch, SSRF-guarded), presigned upload slots; stored under `<Folder>/assets/`; embedded as `![[file.png]]`
- **OIDC login** — PocketID or any standard OIDC provider; signed `relay_session` cookie with live sub-allowlist re-check; PKCE/S256
- **MCP OAuth 2.1** — relay as its own authorization server brokering to PocketID; Dynamic Client Registration + PKCE; opaque tokens hashed at rest; `MCP_OAUTH_ENABLED` (default `false`)
- **TTL** — per-post `expires_at`, per-tag TTL config, global `DEFAULT_TTL_HOURS`; shortest TTL wins for multi-tag posts
- **Prometheus metrics** — `GET /metrics`, zero-dependency registry, bearer-gated
- **`GET /status`** — version, uptime, vault counts, effective feature state
- **Live watcher** — external edits (Obsidian, nvim) trigger reindex + SSE push; controlled by `RELAY_WATCH_ENABLED`
- **Export script** — `scripts/export_vault.py` pulls a live relay into a fresh vault over REST

[Unreleased]: https://github.com/pierdom/relay/compare/v0.9.5...HEAD
[0.9.5]: https://github.com/pierdom/relay/compare/v0.9.4...v0.9.5
[0.9.4]: https://github.com/pierdom/relay/compare/v0.9.3...v0.9.4
[0.9.3]: https://github.com/pierdom/relay/compare/v0.9.2...v0.9.3
[0.9.2]: https://github.com/pierdom/relay/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/pierdom/relay/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/pierdom/relay/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/pierdom/relay/compare/v0.7.2...v0.8.0
[0.7.2]: https://github.com/pierdom/relay/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/pierdom/relay/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/pierdom/relay/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/pierdom/relay/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/pierdom/relay/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/pierdom/relay/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/pierdom/relay/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/pierdom/relay/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/pierdom/relay/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/pierdom/relay/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/pierdom/relay/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/pierdom/relay/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/pierdom/relay/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/pierdom/relay/releases/tag/v0.1.0
