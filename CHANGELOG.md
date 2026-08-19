# Changelog

All notable changes to relay are documented here. Releases follow [semantic versioning](https://semver.org): minor bumps add capability, patches fix bugs or correct docs within a minor line.

---

## [Unreleased]

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

[Unreleased]: https://github.com/pierdom/relay/compare/v0.9.1...HEAD
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
