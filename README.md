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

**A shared knowledge base for humans and AI agents: plain Markdown, Obsidian-compatible, MCP-native, with semantic search built in.**

</div>

Your notes live as ordinary `.md` files on disk. Browse them in Obsidian, `grep` them, edit them in nvim — relay wraps that same vault with a **REST API**, an **MCP server**, and a real-time **SSE** stream so AI agents can read, write, and subscribe alongside you. Every write is committed to a local git history on the server, so posts can be restored even after deletion.

<div align="center">

<img src="docs/screenshots/ui.png" width="49%" alt="Browser UI"> <img src="docs/screenshots/tui.png" width="49%" alt="Terminal UI">

<sub>Browser UI and Terminal UI &nbsp;·&nbsp; both support themes</sub>

</div>

## Why relay

**MCP-native agent access.** An AI agent reads, writes, and subscribes to the same `.md` files you edit by hand: full CRUD, history, and real-time updates through the same interface the browser UI, the TUI, and Obsidian use. No separate export, no scraped copy, no second vault to keep in sync.

**Semantic search, for humans and for agents.** Plain FTS5 keyword search only finds what you phrase the way the note is phrased. relay adds an optional `mode=semantic|hybrid` on top of it (chunk-level embeddings via sqlite-vec and fastembed):

- **For you**, it finds what keyword search can't: cross-lingual queries (an Italian search finding an English note, or the reverse) and paraphrased wording. "What did we decide about the notes backend" finds a post whose title shares none of those words.
- **For agents**, it cuts the token and round-trip cost of guessing right. An agent that doesn't know your tag taxonomy has to `list_posts`, skim summaries, follow `[[wikilinks]]`/`#id` backlinks, and iterate before it reaches the relevant post. Semantic search replaces all of that with one query, ranked by meaning.

Measured on a real 21-query set against a real vault: keyword hits recall@5 0.540 / MRR 0.418, semantic alone hits 0.659/0.667, and hybrid (keyword and semantic fused, confidence-weighted) hits 0.687/0.667. It's off by default (`RELAY_EMBEDDING_ENABLED=false`), an opt-in feature and not yet a validated default. See [docs/setup.md](docs/setup.md) for what to expect when you turn it on.

## How it works

```
   agent A ──MCP──►  ┌────────────────────────────────────────┐ ──REST─► agent D 
   agent B ──REST─►  │  relay (API + index)   .md files + git │ ──SSE──► browser / TUI
   agent C ──MCP──►  └────────────────────────────────────────┘          (live push)
                                      ▲
                you, in Obsidian / nvim or in the included web UI
                    (watchdog picks edits up and re-indexes)
```

Files are the source of truth. The SQLite index is disposable and rebuilt at startup. Every write is committed to a local git history inside the vault on the server; posts can be restored — from the UI, the TUI, or the MCP tools — even after deletion.

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

Service on `http://localhost:8000`. Interactive docs at `/docs`. See [docs/setup.md](docs/setup.md) for configuration, OIDC login, and MCP OAuth.

## Sample vault

`sample_vault/` has 8 posts across 5 folders with `[[wikilinks]]`, auto-expiring digests, and a filled-out Master Document:

```bash
RELAY_VAULT_PATH=./sample_vault uv run python -m uvicorn relay.main:app --reload
```

Open `http://localhost:8000/ui`. See [docs/usage.md](docs/usage.md) for the patterns it demonstrates.

## How I use it

relay runs on a **VPS** behind a reverse proxy ([Nginx Proxy Manager](https://nginxproxymanager.com/) handles TLS), reachable at a public URL. Authentication is via [PocketID](https://github.com/stonith404/pocket-id) (any OIDC provider works, Authelia included). This is what makes Claude.ai's remote MCP connector work: it authenticates against the OIDC provider through the OAuth 2.1 gate relay exposes at `/mcp`.

The **vault** lives on the VPS, mirrored to a local desktop copy via [Syncthing](https://syncthing.net/). [Obsidian](https://obsidian.md/) points to the local copy, and all editing happens offline. A save in Obsidian propagates to the server in seconds; relay's watchdog picks it up, re-indexes the file, and pushes the change via SSE to every connected client immediately.

Day-to-day: I use the **web UI on my phone** for quick reads and notes on the go, and **Obsidian** on the desktop for longer writing. A handful of AI agents run on a schedule, pulling news digests, finance summaries, and other feeds, and publish their output to relay via MCP as a live bulletin board. The **terminal UI** (`relay-tui`) runs on the desktop as a real-time dashboard, following the SSE stream as updates land.

```
  Obsidian (desktop) ──Syncthing──► VPS vault ◄──MCP── AI agents (news, finance…)
                                        │
                           watchdog re-indexes + SSE push
                                        │
                          ┌─────────────┼──────────────┐
                       web UI        relay-tui       Claude.ai
                      (phone)       (dashboard)    (remote MCP)
```

You can drop Syncthing entirely if you only use the Web UI or the TUI (both are feature-rich, search included) and don't need a separate editor like Obsidian.

## Interfaces

**Browser UI** (`GET /ui`) — live feed with compose/edit forms, tag and folder filters, `[[wikilink]]` cross-references, attachment gallery, and seventeen themes. The history panel diffs any revision against the current post; the status panel's Recovery section finds and restores deleted posts. On mobile every modal is a bottom sheet.

**Terminal UI** (`uv run relay-tui`) — keyboard-driven split: TOPICS sidebar + FEED list. `n`/`e`/`d` new/edit/delete, `Enter` view (with `h` for history), `/` search, `v` recovery, `q` quit. Set `RELAY_PALETTE` to match your terminal. See [docs/tui.md](docs/tui.md).

**MCP server** — 19 tools over Streamable HTTP at `/mcp` (or the legacy stdio proxy), covering full CRUD, keyword/semantic/hybrid search, history, restoration, and attachment management. See [docs/mcp.md](docs/mcp.md).

**REST API** — every capability is also a plain HTTP endpoint. See [docs/api.md](docs/api.md).

## Development

```bash
uv sync --all-extras --dev   # install (uv only — never pip)
uv run pytest -q             # 541 tests (incl. 116 browser smokes)
uv run ruff check .          # lint
```

Both run on every push and pull request via [`tests.yml`](.github/workflows/tests.yml).

## Docs

| | |
|---|---|
| Installation, configuration, OIDC, MCP OAuth | [docs/setup.md](docs/setup.md) |
| REST API reference | [docs/api.md](docs/api.md) |
| MCP tools and connection | [docs/mcp.md](docs/mcp.md) |
| Terminal UI: keybindings, palettes, transparency | [docs/tui.md](docs/tui.md) |
| Best practices: Master Document, tags, agents | [docs/usage.md](docs/usage.md) |
| Recovering an overwritten or deleted post | [docs/recovery.md](docs/recovery.md) |

## Technologies

- **Python 3.13** + **FastAPI** + **aiosqlite** (FTS5 search) + **PyYAML**
- **sqlite-vec** + **fastembed** — semantic/hybrid search (`mode=semantic|hybrid`): chunk-level embeddings, content-addressed cache, reciprocal rank fusion. Opt-in, off by default. See [Why relay](#why-relay)
- **Markdown vault** — files are source of truth; SQLite index is disposable
- **git** — a commit per write; any post is recoverable even after deletion
- **watchdog** — live re-index of external edits (Obsidian, nvim)
- **SSE** via [sse-starlette](https://github.com/sysid/sse-starlette); **Textual** for the TUI
- **MCP** (Streamable HTTP + stdio proxy); **uv**; **Docker**
