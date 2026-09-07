# Contributing to relay

## Setup

```bash
cp .env.example .env          # edit RELAY_VAULT_PATH to a scratch dir, not your real vault
uv run uvicorn relay.main:app --reload
```

Point `RELAY_VAULT_PATH` at a throwaway directory (e.g. `./sample_vault`). The dev server boots a watcher and history repo against whatever path is in `.env` — if that's your live Obsidian vault, you'll have a second relay instance running against real data.

Install Playwright once for browser smoke tests:

```bash
uv run playwright install chromium
```

## Running tests and lint

```bash
uv run pytest -q          # full suite (includes 100+ browser smokes)
uv run ruff check .        # lint — E, F, I, UP, B, C4, SIM at line-length 120
```

Tests always run against a throwaway vault. The `isolated_vault` autouse fixture in `tests/conftest.py` repoints `settings.vault_path` at `tmp_path`; never patch `vault_path` to anything outside `tmp_path`.

## Key invariants

**One MCP tool definition.** Tools live only in `relay/mcp_server.py`. `relay_mcp/server.py` is a stdio bridge that forwards to `/mcp` and exposes whatever the server exposes (plus its own `path` parameter on `add_attachment`); never re-declare a tool there. `tests/test_mcp_bridge.py` drives the bridge against a real server.

**CSS tokens.** All colour values must be declared in the two `:root` blocks at the top of `relay/static/ui/app.css`; components reference `var(--token)` only. `tests/test_css_tokens.py` fails on literals outside those blocks or a token missing from one theme. A new theme is one override block plus one registry entry — no component rules change.

**Single worker.** The upload-slot registry and watcher's id allocator are in-process and not safe across multiple workers. `WEB_CONCURRENCY > 1` raises at startup.

## Submitting changes

- Open an issue before starting large or API-surface-changing work.
- Keep PRs focused; one logical change per PR.
- Add or update tests for any behaviour you change. Browser smokes live in `tests/ui/`.
- Run `ruff check .` and `pytest -q` locally before pushing — CI runs both.
- Minor version bumps move the Docker tag line (`:0.N`); patches do not. The version bump and the git tag are two separate acts: only the tag publishes a GHCR image.
