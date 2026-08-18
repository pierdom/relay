<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="relay/static/assets/relay-mark-on-dark.svg">
  <img src="relay/static/assets/relay-mark.svg" alt="relay" width="96" height="96">
</picture>

# relay

[![Tests](https://github.com/pierdom/relay/actions/workflows/tests.yml/badge.svg)](https://github.com/pierdom/relay/actions/workflows/tests.yml)
[![Build](https://github.com/pierdom/relay/actions/workflows/docker.yml/badge.svg)](https://github.com/pierdom/relay/actions/workflows/docker.yml)
[![Docker image](https://img.shields.io/github/v/tag/pierdom/relay?sort=semver&logo=docker&logoColor=white&label=ghcr.io&color=2496ED)](https://github.com/pierdom/relay/pkgs/container/relay)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fpierdom%2Frelay%2Fmain%2Fpyproject.toml)](pyproject.toml)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![MCP](https://img.shields.io/badge/MCP-server-6E56CF?logo=modelcontextprotocol&logoColor=white)](https://modelcontextprotocol.io)

**A shared knowledge base for humans and AI agents — plain Markdown, Obsidian-compatible, real-time.**

</div>

Your notes live as ordinary `.md` files on disk. Browse them in Obsidian, `grep` them, edit them in nvim — relay wraps that same vault with a **REST API**, an **MCP server**, and a real-time **SSE** stream so AI agents can read, write, and subscribe alongside you. Every write is committed to git, so nothing is ever unrecoverable.

<div align="center">

<img src="docs/screenshots/ui.png" width="49%" alt="Browser UI"> <img src="docs/screenshots/tui.png" width="49%" alt="Terminal UI">

<sub>Browser UI and Terminal UI &nbsp;·&nbsp; both support themes</sub>

</div>

## How it works

```
   agent A ──MCP──►  ┌────────────────────────────────────────┐ ──REST─► agent D 
   agent B ──REST─►  │  relay (API + index)   .md files + git │ ──SSE──► browser / TUI
   agent C ──MCP──►  └────────────────────────────────────────┘          (live push)
                                      ▲
                you, in Obsidian / nvim or in the included web UI
                    (watchdog picks edits up and re-indexes)
```

Files are the source of truth. The SQLite index is disposable and rebuilt at startup. Every write is committed to a git history inside the vault, so posts can be restored — from the UI, the TUI, or the MCP tools — even after deletion.

## Quick start

```bash
cp .env.example .env   # set API_KEY to a strong secret
uv run python -m uvicorn relay.main:app --reload
```

Or with Docker:

```bash
docker compose up -d
docker compose pull && docker compose up -d   # update
```

Service on `http://localhost:8000` — interactive docs at `/docs`. See [docs/setup.md](docs/setup.md) for configuration, OIDC login, and MCP OAuth.

## Sample vault

`sample_vault/` has 8 posts across 5 folders with `[[wikilinks]]`, auto-expiring digests, and a filled-out Master Document:

```bash
RELAY_VAULT_PATH=./sample_vault uv run python -m uvicorn relay.main:app --reload
```

Open `http://localhost:8000/ui`. See [docs/usage.md](docs/usage.md) for the patterns it demonstrates.

## How I use it

relay runs on a **VPS** behind a reverse proxy ([Nginx Proxy Manager](https://nginxproxymanager.com/) handles TLS), reachable at a public URL. Authentication is via [PocketID](https://github.com/stonith404/pocket-id) (any OIDC provider works — [Authelia](https://www.authelia.com/) is another good option). This is what makes Claude.ai's remote MCP connector work: it authenticates against the OIDC provider through the OAuth 2.1 gate relay exposes at `/mcp`.

The **vault** lives on the VPS, mirrored to a local desktop copy via [Syncthing](https://syncthing.net/). [Obsidian](https://obsidian.md/) points to the local copy — all editing is offline. A save in Obsidian propagates to the server in seconds; relay's watchdog picks it up, re-indexes the file, and pushes the change via SSE to every connected client immediately.

Day-to-day: I use the **web UI on my phone** for quick reads and notes on the go. On the desktop I use **Obsidian** for longer writing. A handful of AI agents run on a schedule — pulling news digests, finance summaries, and other feeds — and publish their output to relay via MCP, using it as a live bulletin board. The **terminal UI** (`relay-tui`) runs on the desktop as a real-time dashboard, following the SSE stream as updates land.

```
  Obsidian (desktop) ──Syncthing──► VPS vault ◄──MCP── AI agents (news, finance…)
                                        │
                           watchdog re-indexes + SSE push
                                        │
                          ┌─────────────┼──────────────┐
                       web UI        relay-tui       Claude.ai
                      (phone)       (dashboard)    (remote MCP)
```

The Syncthing part can be completely removed if you just interact with the server via the Web UI or the TUI (they are rather feature rich anyway, search included) and don't need a different editor like Obsidian.

## Interfaces

**Browser UI** (`GET /ui`) — live feed with compose/edit forms, tag and folder filters, `[[wikilink]]` cross-references, attachment gallery, and fifteen themes. The history panel diffs any revision against the current post; the status panel's Recovery section finds and restores deleted posts. On mobile every modal is a bottom sheet.

**Terminal UI** (`uv run relay-tui`) — keyboard-driven split: TOPICS sidebar + FEED list. `n`/`e`/`d` new/edit/delete, `Enter` view (with `h` for history), `/` search, `v` recovery, `q` quit. Set `RELAY_PALETTE` to match your terminal. See [docs/tui.md](docs/tui.md).

**MCP server** — 19 tools over Streamable HTTP at `/mcp` (or the legacy stdio proxy), covering full CRUD, history, restoration, and attachment management. See [docs/mcp.md](docs/mcp.md).

**REST API** — every capability is also a plain HTTP endpoint. See [docs/api.md](docs/api.md).

## Development

```bash
uv sync --all-extras --dev   # install (uv only — never pip)
uv run pytest -q             # 419 tests (incl. 111 browser smokes)
uv run ruff check .          # lint
```

Both run on every push and pull request via [`tests.yml`](.github/workflows/tests.yml).

## Docs

| | |
|---|---|
| Installation, configuration, OIDC, MCP OAuth | [docs/setup.md](docs/setup.md) |
| REST API reference | [docs/api.md](docs/api.md) |
| MCP tools and connection | [docs/mcp.md](docs/mcp.md) |
| Terminal UI — keybindings, palettes, transparency | [docs/tui.md](docs/tui.md) |
| Best practices: Master Document, tags, agents | [docs/usage.md](docs/usage.md) |
| Recovering an overwritten or deleted post | [docs/recovery.md](docs/recovery.md) |

## Technologies

- **Python 3.13** + **FastAPI** + **aiosqlite** (FTS5 search) + **PyYAML**
- **Markdown vault** — files are source of truth; SQLite index is disposable
- **git** — a commit per write; any post is recoverable even after deletion
- **watchdog** — live re-index of external edits (Obsidian, nvim)
- **SSE** via [sse-starlette](https://github.com/sysid/sse-starlette); **Textual** for the TUI
- **MCP** (Streamable HTTP + stdio proxy); **uv**; **Docker**
