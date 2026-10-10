# relay — Claude Code guide

Personal knowledge base: a plain-Markdown, **Obsidian-compatible vault** with an AI-integration layer. Agents publish/query/subscribe over MCP, REST and SSE; humans edit the *same* files in Obsidian/nvim or the browser/TUI. User-facing docs are indexed in README.md's Docs section.

## Storage model

- **One `.md` file per post** under `RELAY_VAULT_PATH`. The title *is* the filename; front-matter holds `id`, `tags`, `source`, `created_at`, `updated_at`, `expires_at`, `updated_by` (no `title`). Files are canonical.
- **Any other front-matter key round-trips verbatim** (Obsidian Properties, custom fields) and is exposed read-only as `properties`; relay's seven keys win on a name collision.
- **`id` is authoritative, survives renames, and is never reused**: `<vault>/.relay/last_id` is a high-water mark, so a deleted id can't be handed out again and silently repoint `#id` links.
- **The SQLite index (`<vault>/.relay/index.db`) is disposable** — rebuilt from the files at startup. `database.connect()` is the only way to open it (Row factory, `busy_timeout`, sqlite-vec).
- **History (`<vault>/.relay/history.git`) and `<vault>/.relay/mcp_oauth/` are durable** — never wipe them. History is a detached git-dir with the vault as work-tree (no `.git` in the vault root, so Syncthing can't corrupt it).
- **Dot-directories are never vault content** (`vault.is_hidden_path`): `.relay`, `.obsidian`, `.trash` (Obsidian's trash keeps ids — indexing it resurrected deletes), `.stversions`.
- **Folders** (`folders.py`): a post is filed once, by its first domain tag, into a first-level folder (`Homelab/`, `Finance/`, … `Digests/`, `Inbox/`). Real folders are human-owned and never auto-moved; the one exception is a tag-less `Inbox` note moving to a domain folder when it gains its first domain tag.
- **Tags** are stored with sentinel commas (`,news,ai,`) and matched with `LIKE '%,tag,%'` — **always through `database.escape_like`** (`tag_folder_filters`, `cleanup._TAG_LIKE`): `_` is a legal tag character and a LIKE wildcard, and an unescaped TTL match once expired `my-notes` posts for a TTL on `my_notes`. One normal form: `models.clean_tag`.

## Running

```bash
cp .env.example .env
uv run uvicorn relay.main:app --reload       # http://localhost:8000, API docs at /docs
docker compose up -d                         # or Docker; update: docker compose pull && docker compose up -d
uv run pytest -q                             # tests (tests/ui = browser smokes; skip without Chromium)
uv run ruff check .                          # lint: E,F,I,UP,B,C4,SIM, line length 120
uvx mypy relay --ignore-missing-imports      # types
uv run playwright install chromium           # once, for the browser smokes
RELAY_EVAL_URL=... RELAY_EVAL_KEY=... uv run pytest -m eval -s   # search-quality baseline, skipped otherwise
```

CI (`tests.yml`) runs ruff, pip-audit and the full suite including the browser smokes; `docker.yml` publishes the image on pushes to `main` and version tags.

**Rules that each caused a production outage:**

- **Dependencies: `uv add`, never hand-edit `pyproject.toml`, always commit `uv.lock`.** The Dockerfile installs with `uv sync --frozen`, which trusts the lockfile literally; a dependency added without `uv lock` crashed every image on boot (v1.1.0). `uv run` self-heals a stale lock locally, so a lockfile diff is not noise — commit it. **Dependabot PRs never run `uv lock`**: after merging any, run `uv lock` and the full pytest/ruff/mypy pass before the next deploy.
- **A new index column's `CREATE INDEX` runs after its `ALTER TABLE`**, never inside `_SCHEMA`: `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so the index statement hit a column that didn't exist yet and crashed every upgraded deployment (v1.13.0). Add the column in the post-`_SCHEMA` migration block, its index after that, and a `tests/test_database.py` case that starts from the previous schema — the test fixtures always start from an empty index and cannot see this.

**Tests always run against a throwaway vault.** `tests/conftest.py`'s autouse `isolated_vault` repoints `settings.vault_path` under `tmp_path`; never point it elsewhere — `.env` names a live Obsidian vault and `rebuild_index` stamps ids into every file. Throwaway scripts go in `tests/`. The only exception is `tests/eval`, which reads a real vault read-only over REST behind `RELAY_EVAL_URL`/`RELAY_EVAL_KEY`; its `golden.yaml` is personal and gitignored (copy `golden.example.yaml`).

## Errors

Every caller-facing error is a `relay.errors.ServiceError` subclass carrying its HTTP status and message. REST maps them in one FastAPI exception handler (`main.py`) and MCP in one decorator (`mcp_server._tool`, which also records the tool metric) — **routes and tools never translate errors by hand**. `ConcurrentModification` carries the post's current state for the 409.

## API

All endpoints need `Authorization: Bearer <key>` (or the browser session cookie). Full reference: docs/api.md.

| Method | Path | Notes |
|---|---|---|
| POST/GET | /posts | Publish / list: `tag`, `folder`, `author`, `limit`, `offset`, `search`, `summary`, `sort`=updated\|created, `order`, `mode`=keyword\|semantic\|hybrid (503 without embeddings). A bare `42`/`#42` search is an id lookup answered as `pinned`. POST's response carries advisory `similar` |
| GET/PATCH/DELETE | /posts/{id} | `PATCH` accepts `if_match` (body or `If-Match` header, header wins) → 409 with the current post. Every single-post response carries `etag` + an `ETag` header |
| POST | /posts/{id}/edit · /append | `str_replace`-style edit (exactly one match, else 422) / append |
| GET | /posts/deleted | Restorable deleted posts — **declared before `/{id}`** or FastAPI parses `deleted` as an int |
| GET | /posts/{id}/backlinks · /related | Linking posts / similar-but-unlinked posts (503 without embeddings) |
| GET/POST | /posts/{id}/history[/{sha}] · /restore | Revisions, one revision's body (short sha ok), restore (recreates a deleted post, keeps its id). 503 when history is off |
| GET | /changes | Vault changelog `{seq,id,title,action,when,sha,author}`; `since` = seq or ISO time; `?author=` |
| GET | /lint · /links · /folders · /tags · /status · /metrics | Lint report, (id,title) index, folder counts, tag counts (+ each tag's `ttl_hours`/`expires_at`), diagnostics, Prometheus |
| POST/PATCH | /tags/{tag}/config · /tags/{tag} | Per-tag TTL / rename across all posts |
| POST/GET, PUT, GET/DELETE | /attachments, /attachments/uploads[/{id}], /attachments/{path} | Upload (base64 `data`, `source_url`, or presigned `upload_id`), slot mint/fill, serve/delete |
| GET | /events | SSE; `?tag=` filter; `Last-Event-ID` is a `changes.seq` and replays every change since |
| PATCH/POST | /embeddings · /embeddings/backfill | Toggle semantic search at runtime (in-memory) / re-run the backfill (`force=true` wipes the cache) |
| GET | /id/{id} | Redirect to `/?post={id}` — a UI deep link, no auth of its own |
| POST/GET | /mcp | Streamable HTTP MCP |

## Attachments

Non-`.md` files live in `<Folder>/assets/`, embedded as `![[file.png]]`; names are vault-globally unique.

- **`vault.resolve_attachment` is the security boundary for serving *and* deleting**: it never resolves a `.md` file, a dotfile, or anything under a dot-directory (a path-form `DELETE /attachments/Folder/Note.md` once unlinked posts around every post-level check).
- **Serving is an inline allowlist by resolved MIME** (`routes/attachments._renders_inline`); everything else downloads (`Content-Disposition: attachment`) so an upload can never run script in the UI's origin. Never go back to a suffix list.
- A caller-supplied `folder` must pass `folders.is_valid_name`. Exactly one byte transport per upload; `source_url` fetches are SSRF-guarded at connect time (`ingest._GuardedNetworkBackend`) and happen only **after** the scope check. Upload slots are in-memory — **single-worker assumption**.
- Deleting a post removes only the attachments it embedded that no other post references.

## History and the changelog

Every write path commits to `history.git` (`core.quotePath=false` pinned — test fixtures must include non-ASCII titles), including TTL expiry and the watcher's debounced external edits. The restorable sha of a deleted post is `log -1 <delete>^ -- <path>`. Manual recovery: docs/recovery.md.

- **Commit while still holding `vault.write_lock`** — `history.commit` stages the whole work-tree, so committing after releasing it can sweep another writer's file into this commit and misattribute it.
- `changes.py`'s table is a **materialized index over `history.git`**, not a second store: `_catch_up` ingests `{last sha}..HEAD` under `changes._lock`. It must stay a range under a lock — a `-1` fetch lost rows when two commits raced, and an unlocked read-then-insert duplicated them. A rename is a `D`+`A` pair in one commit (`--no-renames`), deduped per post.
- `watcher._reconcile` and the TTL sweep publish live SSE before their commit, so those frames carry no `seq`; reconnect catch-up still finds them.

## Lint (`relay/lint.py`)

Machine-checks #0's written rules: tag axes, stale Inbox, H1 missing/mismatched, broken links / links to deleted posts / broken embeds / wikilinks to filenames (one finding per distinct target, `occurrences` + `match`), stale hub/plan posts, zero backlinks (digest-type tags exempt), empty tag configs, zero-chunk posts, #0's stated post count. #0 is exempt from everything but the link checks. Link and H1 scanning ignore code (`markdown_scan`: `strip_code` for links, `strip_fences` for H1 location); bare `#N` below 10 or above the id high-water mark is never a link. A rule needing a disabled feature is skipped and named in `skipped_rules`, never a 503.

## Authentication and scopes

`auth.require_api_key` accepts a bearer key or the `relay_session` cookie and returns an `identity.Actor` (name, email, scope). **`identity.resolve_bearer` is the only place a bearer is compared** (constant-time, per key, on UTF-8 bytes).

- **Keys**: `API_KEY` is the reserved identity `apikey`; `RELAY_API_KEYS` (`name:key,…`) adds named keys. Each write's actor becomes the git commit `--author`, `changes.author` and the post's `updated_by` (a mechanical side-effect write — tag rename, link retarget — carries `updated_by` over).
- **Scopes** (`RELAY_API_KEY_SCOPES`, `name:read` / `name:write:tag1+tag2` / `name:full`; absent = full) — a separate variable because key material may contain `:`. `apikey` is never scopable. Enforcement is two layers: the coarse read-only gate inside `require_api_key` (every non-GET route) and, for tag restrictions, `service._common._require_write_scope`/`_require_full_access` in the service layer (ALL-of: every tag on the post, old and new, must be allowed; an untagged target is denied to a restricted key). Vault-wide operations (tag rename/config, embeddings control) need full access. Edit/append check scope before any content matching (no oracle).
- **MCP** mirrors this: write tools carry fastmcp's `tags={"write"}` with `AuthMiddleware(restrict_tag("write", …))` (read-only keys don't even see them), and tool bodies call the same service helpers. `_current_actor()` fails closed on a token with no identity.
- **Sessions**: signed stateless cookie, re-checked on every request (`auth.still_authorized`). OIDC sessions follow `OIDC_ALLOWED_SUBS`; a pasted-key session (`key: true`) stays valid only while that key is configured. Cookie-authenticated writes reject cross-site requests (`auth.is_cross_site`). No per-token revocation.
- **MCP OAuth** (`MCP_OAUTH_ENABLED`): `_build_auth()` wires fastmcp's `OIDCProxy` (subclassed as `_RelayOIDCProxy`) brokering to PocketID, plus `_StaticBearerAuth` as a fallback. Load-bearing details, each documented in its own docstring: `verify_id_token=True` (else every call 401s — regression-tested); PocketID's scope vocabulary is disjoint from relay's (`forward_resource=False`, overridden upstream `scope`, `_translate_scopes_from_idp`); the allowlist is re-enforced on every refresh (`_extract_upstream_claims`, `exp` essential); the refresh trigger is clamped to the id_token's `exp`; client storage is a **Fernet-encrypted** `FileTreeStore` under `.relay/mcp_oauth/` (it holds live upstream tokens); `redirect_path` is `/mcp/oauth/callback`. Keep PocketID ≥ 2.6.0 (GHSA-w6p7-2fxx-4f44). Known accepted gaps: no refresh-token-family revocation on reuse, no access→refresh revocation cascade.

## MCP

`relay/mcp_server.py` is the only place tools are declared (27 tools, `@_tool`). `relay_mcp/server.py` is a stdio ↔ Streamable HTTP bridge that forwards verbatim — its one addition is a `path` parameter on `add_attachment` (a file on the client machine); **the in-process server must never gain `path`** (arbitrary file read on the host). The MCP SDK can't distinguish an omitted argument from `null`, so `""` clears `expires_at`/`source`. Tool list and semantics: docs/mcp.md.

## Semantic search (opt-in, `RELAY_EMBEDDING_ENABLED`)

- H2/H3-aware chunking; embeddings cached by `(model_id, chunk_body)` hash, so unchanged content is never re-embedded and a model change needs no migration. The vector table's dimension comes from `EMBEDDING_MODEL` via fastembed's registry and is rebuilt at startup on a mismatch.
- **Startup never embeds inline** — the backfill is a background task (`vault.spawn_backfill`). Inline bulk embedding once blocked `/health` for minutes on every restart.
- **Every call that touches `embedding.get_backend()` goes through `asyncio.to_thread`** — the model is idle-unloaded after `EMBEDDING_IDLE_UNLOAD_SECONDS` (it costs ~570MB RSS; `malloc_trim` returns it), so any call may pay a cold reload.
- `vectors.sync_post_chunks` never raises and never commits — embeddings are derived data. Mode semantic/hybrid with embeddings *off* is a 503; a backend failure *at query time* answers keyword-ranked with `search_timing.degraded=true`.
- Digest-type posts (`folders.DISPOSABLE_TAGS`) are never embedded. `find_similar_posts` (the advisory `similar` field) never raises; `/related` does 503 when disabled. `/status`'s `embeddings` block is the diagnostic surface.

## Browser UI (`relay/static`)

Single-page app on REST + SSE: ES modules, no build step, `marked` and `DOMPurify` vendored; `/` carries a CSP (`main.ui_csp()`, script hashes computed from `index.html`) — no inline handlers or `javascript:` URLs, use `addEventListener`.

- **Themes** (docs/themes.md, regenerated by `scripts/theme_gallery.py`): a token block in `app.css` plus a `CATALOGUE` entry in `theme.js`, nothing else. **Colour lives in tokens only** (`tests/test_css_tokens.py` rejects literals outside `:root` blocks — it reads `#feed` as a hex colour, so use `.feed`). Tested floors: `--body` ≥ 7:1 on `--surface` (house themes ≥ 9.5, text ≥ 10; `REPRODUCTIONS` below that), `--on-accent` on `--accent` and chips ≥ 4.5, every small-text role (muted on card/sidebar/tag pill, accent, red, green, hovered primary) ≥ 4.5 (`test_small_text_roles_clear_aa`, palette limits in `TEXT_ROLE_EXCEPTIONS`). Fix a shortfall with another member of the same palette, never a mix; a palette whose text can't reach 7:1 doesn't ship.
- **Icons are inline SVG from `js/icons.js`**, never emoji or rare glyphs (blank boxes without an emoji font — `tests/test_ui_glyphs.py`).
- **Modals are wired once, by `wireModal` (`js/dialog.js`)**: ×, backdrop, swipe, Escape (always the top-most modal), an optional `confirmDiscard` on every user dismissal, `role="dialog"`, focus in/trap/return, and `closeAllModals()` on sign-out. Modules only toggle the `open` class. A new modal is wired with `wireModal` and added to `SHEETS` in `tests/ui/test_sheets.py` (`test_every_desktop_modal_shares_the_same_chrome`). The body never scrolls (the feed does), so nothing locks it.
- **Feed**: only the newest `loadPosts` response paints (`loadSeq`); paging is by offset, so a removed card decrements `query.offset`, a live insert increments it, and an appended page skips ids already shown. The SSE stream filters by tag only — `setFilter` reconnects it whenever the tag changes; with a search, folder or non-default sort, live posts go to the "new posts" pill. The "all" row counts posts (`postCount()`), never summed counts. Replace cards through `replaceCard` (keeps `.pinned`). Grid tiles are a fixed 340px (rows never ragged) except at ≤560px.
- **Session**: signed-in vs signed-out is the `body.signed-out` class in CSS. Any 401 (`api.js` fires `relay:unauthorized`) or a refused SSE stream calls `signOut()` back to the login card with a reason.
- **`api.js`**: `apiFetch` for JSON both ways, `apiSend` for a raw body or empty reply (uploads, 204 DELETEs); both throw with the server's `detail` (`err.status`, `err.detail`). The `/links` index lives once in `links.js` — refresh it after anything that adds, removes or renames a post. Edit saves send `if_match` (409 → "save again to overwrite").
- **Feedback is inline or a toast, never `alert()`**: form problems beside the form's buttons (`.ef-gate-msg`), the rest through `js/toast.js`. Deletes toast with Undo when history is on (restore via `/posts/deleted`'s sha); with history off they confirm by name. #0 has no Delete.
- **Keyboard and a11y**: sidebar labels are `<button class="tag-name">`, card titles `<a class="post-title" href="/id/N">` (clickable `<div>`s are mouse-only); clicks on other links inside a card are the link's, and `http(s)` links open in a new tab (DOMPurify hook). Single-key shortcuts `preventDefault` (`e` once typed itself into the Title it focused). One global reduced-motion rule shortens durations rather than removing them (`transitionend` listeners still fire); `[hidden]` is `display: none !important`.
- **Layout**: `min-width: 0` on every card grid area; tables size to content in a `.table-scroll` box (`main.wrapTables`: `<wbr>` after path joints, 10em floor for prose columns); the mobile override block lives at the **end** of `app.css` (earlier overrides lose on source order); draggables use `animation-fill-mode: backwards` with no `to` keyframe; a sheet's `touchmove` stays non-passive and calls `preventDefault` (else iOS Safari pull-to-refreshes); iOS input zoom is handled once (`@media (hover: none)`, 16px).
- **Header order is a safety property**: `+ New Post` · theme · status · disconnect. Recovery lives in the status panel; the lint modal hosts the real editor (`edit-form.buildEditForm`).

## Terminal UI

`relay_tui/` (Textual). `RELAY_PALETTE=<name>` picks a palette from `relay_tui/palettes/*.toml` (most mirror a browser theme); `RELAY_TRANSPARENT=1` lets the terminal background show. docs/tui.md.

## Tags · master doc · TTL

- **Master document (`id=0`)**: `Master Document.md` at the vault root, seeded at startup, undeletable, TTL-exempt; it holds the taxonomy and house rules agents read first.
- **TTL** is off by default. Precedence: per-post `expires_at` > per-tag (`<vault>/.relay/tags.yml`) > `DEFAULT_TTL_HOURS`; shortest wins across tags. `expires_at` is normalised to `YYYY-MM-DDTHH:MM:SSZ` because the sweep compares it lexically (`"1 week"` once sorted below every date and got swept); the sweep only considers values in that shape.
- **Search** is SQLite FTS5 (porter, bm25, title/tags weighted), falling back to `LIKE`.
- **The watcher re-stamps a duplicate**: an external file carrying an id already indexed at another *existing* path gets a fresh id; a rename (old path gone) is not a duplicate.
- **Cross-links**: `[[Title]]`/`[[Title|alias]]` (case-insensitive; renames rewrite inbound links vault-wide and stream the rewritten posts) and `#NNN` (stable across renames). Stored verbatim, resolved at display; code spans/blocks are skipped.

## Configuration (.env)

Full table in docs/setup.md. Most-used: `API_KEY` (required), `RELAY_API_KEYS`, `RELAY_API_KEY_SCOPES`, `RELAY_VAULT_PATH`, `RELAY_BASE_URL`, `RELAY_HISTORY_ENABLED`, `RELAY_WATCH_ENABLED`, `RELAY_EMBEDDING_ENABLED`/`EMBEDDING_MODEL`, `DEFAULT_TTL_HOURS`, `SECURE_COOKIES` (`false` for plain HTTP), `OIDC_*`, `SESSION_SECRET`, `MCP_OAUTH_ENABLED`, `MCP_ALLOWED_REDIRECT_HOSTS` (exact hosts or `*.`-prefixed subdomain suffixes, dot-bounded, apex excluded). Malformed `RELAY_API_KEYS`/scope entries are skipped with one warning, never a startup failure; both are parsed once per value.

## Layout

```
relay/
├── main.py            FastAPI app, lifespan, ServiceError handler, UI shell + CSP
├── config.py · auth.py · identity.py · errors.py · models.py · database.py
├── vault.py           file layer: posts, attachments, ids, rebuild, tags.yml
├── frontmatter.py · folders.py · links.py · markdown_scan.py
├── service/           posts · revisions · attachments · tags (+ _common); routes and MCP call this
├── routes/            thin REST handlers
├── mcp_server.py      in-process MCP server (tools + auth)
├── history.py · changes.py · lint.py · watcher.py · cleanup.py · events.py · ingest.py
├── chunking.py · embedding.py · vectors.py     semantic search
├── metrics.py · status.py
└── static/            index.html, ui/app.css, ui/js/{main,api,icons,util,dom,links,theme,status,edit-form,lint,post-history,deleted,sheet,dialog,toast,feed-query,view-prefs}.js
relay_mcp/server.py    stdio bridge (no tool definitions)
relay_tui/             Textual TUI + palettes/
scripts/               export_vault.py (pull a live relay into a vault), theme_gallery.py (docs/themes.md)
```
