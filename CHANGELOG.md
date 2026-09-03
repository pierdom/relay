# Changelog

All notable changes to relay are documented here. Releases follow [semantic versioning](https://semver.org): minor bumps add capability, patches fix bugs or correct docs within a minor line.

---

## [Unreleased]

---

## [1.5.0] — 2026-09-03

Three fixes from a real vault usage report on semantic/hybrid search (relay #253): the missing pieces were diagnosability and a filter gap, not the ranking math itself — a length-normalization theory the report also raised was investigated and refuted against real vault data before any ranking code was touched (see relay #253 for the writeup; no ranking change shipped here).

### Added
- `/status`'s `embeddings.posts_missing_ids` — up to 50 concrete post ids behind `posts_missing`, instead of just a count. The only way a post lands there while a backfill runs cleanly is `chunking.chunk_post` returning zero chunks for it (e.g. a body that's entirely a fenced code block).
- `mode=semantic`/`hybrid` on `GET /posts` and `list_posts` (both MCP surfaces) can now be combined with `tag`/`folder` — previously a 400. `vectors.semantic_search` and `service._keyword_ranked_ids` both apply the filter directly (same condition shape as the unranked path) rather than the fused list silently dropping it.
- Ranked search responses (`mode=semantic`/`hybrid`) carry a `search_timing` field: whether the query triggered a cold embedding-model load, and how long the embed call took — makes a session's first (cold) ranked query distinguishable from a genuinely slow vault.

### Removed
- `RankedSearchFilterUnsupported` / the 400 it produced — superseded by real filter support above.

---

## [1.4.0] — 2026-09-03

A batch of browser-UI fixes and one new capability: the semantic-search toggle and backfill trigger (v1.3.0's REST/MCP endpoints) are now reachable from the info panel, not just curl or MCP.

### Added
- Info panel ("Semantic search" section): model, dimension, on-disk size, whether the embedding backend is currently resident in memory, idle-unload timeout, thread count, coverage (posts embedded/missing, chunk count, cache entries), and backfill state (never run / running with live counts / last completed) — the full `/status` `embeddings` object, not just the on/off dot. Two buttons alongside it: turn semantic search on/off at runtime, and re-run the backfill on demand, both wired to v1.3.0's `PATCH /embeddings` and `POST /embeddings/backfill`.
- The search bar's ranking-mode select now defaults to `hybrid` instead of `keyword` once `/status` confirms embeddings are usable — it's the mode that already beats keyword-alone on this relay's own eval. Every reset point (fresh connect, clearing the search box) routes through the same default; tag/folder selection still forces `keyword` unconditionally, since the ranked path can't combine with either.
- `apiFetch` now surfaces the server's actual error `detail` ("A backfill is already running") instead of a generic status line ("409 Conflict") — a small change to shared plumbing that improves every existing caller, not just the two new buttons above.

### Fixed
- Mobile search bar: the input had no minimum width, so on a real phone it could be squeezed to a few unusable pixels by its four siblings (ranking mode, sort field, sort direction, list/grid toggle). Gave the input a real floor (72px), capped and ellipsis-truncated the two `<select>`s instead of relying on a smaller font-size that a separate iOS zoom-prevention rule was silently overriding anyway, tightened padding throughout, and added a `flex-wrap` safety net for the narrowest supported width combined with every optional control visible at once.
- The keyboard-shortcuts modal (`?`) reused the same shell as the other settings-style modals, so it displayed a grab handle on mobile — but was never wired to the shared `attachSheetDismiss` gesture, so dragging it did nothing. Fixed, and folded into `tests/ui/test_sheets.py`'s `SHEETS` list so it's covered by the same parity tests as the other four modals going forward (`CLAUDE.md`'s "four modals share chrome" is now five).

Caught in review before shipping: `searchClear`'s reset to the new default mode didn't check for an active tag/folder filter first, which could have set `mode=hybrid` while one was still active — a combination the server rejects. And the info panel's action buttons originally shared one try/catch between the action and its follow-up refresh, so a refresh failure after a successful action would have misreported as a failed one and re-enabled the button for a redundant, wrong-direction retry. Both fixed before merge.

Full non-UI suite green (457 passed), CSS token test passes (no color literals outside the token blocks). Browser-level verification wasn't possible this round — Playwright's Chromium can't launch in this environment (missing system libs); worth a manual pass on a phone.

---

## [1.3.0] — 2026-09-03

Runtime control over semantic search: pause/resume and re-trigger the embedding backfill without a restart, on both REST and MCP. New capability, not a diagnostics tweak — cut as a minor bump per the project's own versioning rule.

### Added
- `PATCH /embeddings` (`{"enabled": bool}`) and `POST /embeddings/backfill` (`?force=true` wipes the cache first), plus `set_embeddings_enabled`/`trigger_embedding_backfill` on both MCP surfaces (in-process and stdio proxy, parity maintained). Both REST endpoints and both MCP tools return the same object `GET /status`'s `embeddings` field already returns.
- The toggle mutates `settings.embedding_enabled` in memory only — every embedding call site already re-checks it per call rather than caching it, so the effect is immediate with no new state variable, and a restart reverts to whatever `.env` says.
- Enabling validates the configured model's dimension against whatever the on-disk `vec_chunks` schema was actually built for (`vectors.current_schema_dim`, mirroring `vectors.init_vec`'s own fallback) and 409s on a mismatch rather than attempting a live schema rebuild — that migration only runs at startup (v1.2.0). Enabling that passes the check auto-triggers a backfill; disabling force-unloads the embedding backend immediately (`embedding.force_unload`) instead of waiting for `EMBEDDING_IDLE_UNLOAD_SECONDS`.
- A backfill trigger 409s if one is already running (`vault.backfill_status()["running"]`) rather than racing two runs on the same progress counters. `vault.spawn_backfill()` marks `running` synchronously before scheduling the background task, and clears it via a `done_callback` if the task is cancelled before it ever gets a turn to run — closes a real edge case where a fast shutdown right after triggering could otherwise leave `/status` reporting a phantom backfill forever.

Refactored: `main.py`'s inline `_embedding_backfill` moved into `vault.run_backfill_task`/`spawn_backfill`, now shared by startup and the new endpoint instead of duplicated. `embedding.unload_if_idle` and the new `embedding.force_unload` share a `_do_unload` helper. `embedding.resolve_dim`/`resolve_size_mb` share a `_model_entry` registry lookup.

18 new tests (9 for the REST/MCP control surface, plus `vectors.reset`/`current_schema_dim` coverage); confirmed all fail without the implementation. Verified end-to-end against the real embedding backend, not just `FakeBackend`: enabled on a real vault, watched the real model download and the auto-triggered backfill complete, ran a real semantic query, disabled and confirmed the backend unloaded, force-retriggered and confirmed the wipe was immediate and synchronous. Full suite green, ruff clean, MCP parity intact.

---

## [1.2.1] — 2026-09-03

`GET /status` and the `get_status` MCP tool report real embedding diagnostics instead of just the on/off flag — model identity, coverage, and backfill progress a shell or a log tail used to be the only way to see.

### Added
- `StatusResponse.embeddings`: `enabled`/`available` (unchanged semantics, now also nested here), `model` and `dimension` and `model_size_mb` (resolved from fastembed's registry via `embedding.resolve_dim`/`resolve_size_mb`), `backend_loaded` (`embedding.is_loaded()` — is the ~570MB model actually resident right now, or idle-unloaded per `EMBEDDING_IDLE_UNLOAD_SECONDS`), `idle_unload_seconds`/`threads` (the configured knobs), `posts_total`/`posts_embedded`/`posts_missing`/`chunks_total`/`cache_entries` (`vectors.coverage`), and `backfill` (`vault.backfill_status()`: `running`/`checked`/`total`/`started_at`/`completed_at` for the current or most recent backfill run)
- `vault.backfill_status()` — a module-level snapshot `backfill_embeddings` now updates as it runs, so "is it still crunching, and how much is left" is one request instead of a log watch
- `embedding.resolve_size_mb`, `embedding.is_loaded` — small additions alongside the existing `resolve_dim`, sharing its registry lookup (refactored into `_model_entry`) rather than duplicating it
- `vectors.coverage(db)` — `(posts_with_chunks, chunks_total, cache_entries)`, purely additive to the existing schema (no migration)

Purely additive to `StatusResponse` — `features.search.embeddings` (the existing boolean gate) is unchanged, so this isn't a breaking change to the stable surface (`docs/stability.md`). 9 new tests cover the disabled default, the enabled/covered case through a real HTTP round trip, and a completed backfill's reflected state; confirmed all 9 fail without the implementation. Full suite green, ruff clean, MCP parity intact (both `get_status` tool descriptions updated identically).

---

## [1.2.0] — 2026-09-03

Makes the embedding model's vector dimension a real config axis instead of a hardcoded constant, so relay can move to a bigger multilingual model (or back) without a manual migration.

### Added
- `embedding.resolve_dim(model_id)` looks up a fastembed model's dimension from its static registry — no download, no ONNX session, safe to call in tests/CI. `FastEmbedBackend.dim` is now resolved per-instance from whichever model `EMBEDDING_MODEL` names, instead of a fixed `384`.
- `vectors.init_vec` tracks the dimension the on-disk `vec_chunks` table (a vec0 virtual table — no `ALTER` for its column width) was actually built at, in a new `embedding_state` table. A startup where the configured model's dimension has changed since the table was created drops and rebuilds `vec_chunks` (and clears `chunks`) at the new width, logging a warning. No separate re-embed step is needed: the embedding cache is already keyed on `(model_id, chunk_body)`, so any model change — same dimension or not — already makes every existing chunk a cache miss; the background backfill that already runs on every startup just re-fills whatever's empty.
- The check only runs while `embedding_enabled=true` — toggling the flag off leaves an existing `vec_chunks` table untouched rather than comparing it against the disabled-state placeholder dimension, which would otherwise misfire a rebuild on every disable/re-enable cycle.

Confirmed against fastembed's real registry: `paraphrase-multilingual-MiniLM-L12-v2` (current default) = 384d, `paraphrase-multilingual-mpnet-base-v2` = 768d, `multilingual-e5-large` = 1024d — both bigger options are now a pure `.env` change. 7 new tests cover the dimension-change rebuild, the same-dimension no-op case, the disabled-toggle no-op case, and `resolve_dim` itself (including an unknown-model error). Full suite green, ruff clean.

---

## [1.1.4] — 2026-09-03

Closes a gap v1.1.3's idle-unload made materially worse: the write path could block the event loop on a cold model reload, same class of bug already fixed on the search path.

### Fixed
- `vectors.sync_post_chunks` (the write path — `create_post`/`update_post` → `index_upsert`/`index_insert`) called `embedding.get_backend()` and `backend.embed_documents(...)` directly on the event loop, unlike `semantic_search`'s `_embed_query`, which already offloads via `asyncio.to_thread`. Before idle-unload this rarely mattered — the backend loaded once on the first write after startup and stayed resident, so a cold ~570MB/multi-second load only ever happened once per process lifetime. With idle-unload the model can go cold again after any quiet period, so an edit arriving after one would otherwise reconstruct it directly on the event loop, stalling every other in-flight request for the duration. Both calls now go through `asyncio.to_thread`, mirroring the search path exactly.

Regression test simulates slow inference with a blocking `time.sleep` in a `FakeBackend` subclass and asserts a concurrent asyncio task keeps making progress during `sync_post_chunks`; verified it actually catches the bug (fails without the fix, via `git stash`). Full suite (432 passed) + ruff clean, plus a real end-to-end check with the actual embedding backend (create + edit a post, confirm chunks re-embed correctly through the new threaded path).

---

## [1.1.3] — 2026-09-03

The real fix for v1.1.1's memory-footprint issue — v1.1.2's thread cap measured no improvement in production, confirmed by the numbers here to have been the wrong theory.

### Fixed
- Confirmed the actual cause: constructing `FastEmbedBackend` costs ~570MB of RSS by itself (onnxruntime session + model weights, measured locally: ~67MB → ~637MB before a single embed call), and that cost is flat across usage afterward — a single query and a 20-doc batch both left RSS unchanged. Not per-call, not per-thread — `EMBEDDING_THREADS` was addressing a mechanism that was never the bottleneck.

### Added
- `EMBEDDING_IDLE_UNLOAD_SECONDS` (default `300`) unloads the embedding model after that much idle time, polled every 60s from a background task. Trades a several-second reload on the next embed call for giving the memory back to the OS. `0` disables — keeps the model resident forever, the prior behavior.
- `gc.collect()` alone only reclaimed ~200MB of the ~570MB in testing — the rest sits in glibc's malloc arenas, freed but not returned to the OS (normal glibc behavior, not a leak). `malloc_trim(0)` after the `gc.collect()` is what actually returns it: verified end-to-end through the real code path, not just a probe script — 621MB → 105MB after unload, ~640MB again after reload. Best-effort (wrapped in a broad except) for platforms without glibc's `malloc_trim`.

---

## [1.1.2] — 2026-09-03

First mitigation for v1.1.1's known memory-footprint issue.

### Changed
- `EMBEDDING_THREADS` (default `1`) caps onnxruntime's intra-op thread pool. Previously unset, so onnxruntime picked its own default sized from the host's CPU count — real memory cost on a 2-CPU production VPS, since each thread carries its own tensor-buffer overhead. Embedding here is inherently sequential (one post's chunks at a time, `vault.backfill_embeddings`/`sync_post_chunks` never run concurrently), so there was no cross-request parallelism being bought by the extra threads. Raise it via env var, no code change, on a deployment with CPU to spare that wants faster embedding instead.

### Known issue
- Effect on actual steady-state memory not yet confirmed against production — this is the cheapest, most directly-targeted lever from the candidate list, not a verified fix. Next: measure `docker stats` after deploying this, before reaching for the remaining levers (checking for a configured `mem_limit`, unloading the model between uses).

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
