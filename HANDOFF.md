# HANDOFF — `explore/fs-storage` branch

_Last worked: 2026-06-26. Picking back up after ~2 weeks._

## What we were attempting

Spike the **relay-native** path to a file-backed PKM: instead of adopting an
off-the-shelf tool (SilverBullet etc. — see relay post **#123**), keep the
**entire relay stack** (FastAPI + MCP + browser UI + TUI + SSE) and swap **only**
the storage layer from SQLite → an **Obsidian-style Markdown vault**.

This is the head-to-head counterpart to the SilverBullet leaning in #123. The
open strategic question for when you return:

> **Does this relay-native vault backend make adopting SilverBullet unnecessary?**
> (I keep my own UI/TUI/MCP and still get plain-md-on-disk + Obsidian/nvim
> editability — vs SB's live web PWA + Space Lua queries.)

## What's done (working prototype)

Branch `explore/fs-storage`, pushed to `origin`. 13 tests pass. Commits:

- `036f1f6` feat(storage): Obsidian-style Markdown vault backend
- `cf43d92` fix(watcher,ui,tui): handle streamed edits; kill reconcile feedback loop
- `b9a5ea3` feat(sse): live delete events; document replay limitations
- `83ba3c7` fix(ui,sse): robust handling when an open post is edited externally

Design:
- **Files are canonical.** One `.md` per post; **title = filename** (Obsidian-native);
  metadata in YAML front-matter (`id`, `tags`, `source`, `created_at`, `updated_at`,
  `expires_at`). `id` is authoritative and survives renames. No `title` in front-matter.
- **SQLite demoted to a disposable index** at `<vault>/.relay/index.db`, rebuilt from
  the files at startup. Delete it → it regenerates. New modules: `relay/frontmatter.py`,
  `relay/vault.py`, `relay/watcher.py`.
- **Live `watchdog` watcher**: external edits (nvim/Obsidian) re-index + push over SSE;
  hand-dropped `.md` files get an `id` stamped in; deletes broadcast a `delete` SSE event.
- **Dropped the `format` enum** (markdown only). **`title` is now required.**
- Per-tag TTL config lives in `<vault>/.relay/tags.yml` (portable).
- `scripts/migrate_to_vault.py` pulls a running relay over REST → a vault.
  Already used to pull all 46 prod posts into `./vault` (gitignored). Caveat:
  per-tag TTL config can't be pulled over REST (no GET endpoint) — re-apply manually.

## ⛔ WHERE WE LEFT OFF — the open bug

**Browser UI freezes fully for several seconds when a `.md` is edited externally
while that post is currently open in the UI.** Reported with **vim/neovim** + the
**browser UI**.

Could **NOT** reproduce headlessly — drove the real UI with puppeteer/chromium on
the 46-post data across every scenario (detail-modal open + edit, inline-edit form
open + edit, oldest/largest-post edit, forced SSE reconnect): max main-thread block
**50 ms**, every post renders in ≤13 ms (marked@14 + DOMPurify@3), server emits
exactly one clean SSE event per save and stays responsive (no reconcile loop, even
with real neovim `writebackup`/`backupcopy=auto`). **Conclusion: it's specific to
the real browser environment**, not the server or the render path.

Applied the likely-contributor fixes anyway (`83ba3c7`):
- detail modal live-refreshes when its post is edited externally (was going stale);
- an open inline-edit form is no longer clobbered by an incoming stream event;
- `loadTags()` in SSE handlers is debounced (250 ms) — no fetch storm on a burst;
- SSE `id:` is forward-only, so a streamed edit of an older post can't rewind
  `Last-Event-ID` and cause a reconnect replay storm.

### To resume debugging
1. Restart the local server (old process was still running pre-fix) and
   **hard-refresh** the browser (`Ctrl+Shift+R`) — `index.html` changed.
2. If it still freezes, capture:
   - **which browser** (Firefox vs Chrome behave differently on SSE/EventSource);
   - whether the **server terminal spams reconcile lines** during the freeze
     (loop in your env) or stays quiet (pure client);
   - a **DevTools → Performance profile** of the freeze — the long task's call
     stack names the exact blocking function. (Plus any red console errors.)

## How to run

```bash
git checkout explore/fs-storage
uv run python scripts/migrate_to_vault.py --vault ./vault   # pulls from relay.geon.im (.env)
env RELAY_VAULT_PATH=./vault SECURE_COOKIES=false uv run uvicorn relay.main:app --reload
# browser UI: http://localhost:8000/ui  (paste API_KEY from .env)
# TUI (must override base URL or it hits prod):
env RELAY_BASE_URL=http://localhost:8000 uv run relay-tui
# Obsidian / nvim: open ./vault directly (obsidian.nvim — see relay #123)
```

## Known limitation (by design, for now)

Catch-up replay is append-only (`id > Last-Event-ID`), so edits/deletes to
already-seen posts made while a client was **offline** aren't replayed on
reconnect until a manual refresh.

## Plan of record

`~/.claude/plans/tingly-weaving-lampson.md` (the approved implementation plan) and
relay post **#123** (the broader file-backed-PKM evaluation: SilverBullet vs this).
