<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="relay/static/assets/relay-mark-on-dark.svg">
  <img src="relay/static/assets/relay-mark.svg" alt="relay" width="96" height="96">
</picture>

# relay

[![Build](https://github.com/pierdom/relay/actions/workflows/docker.yml/badge.svg)](https://github.com/pierdom/relay/actions/workflows/docker.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fpierdom%2Frelay%2Fmain%2Fpyproject.toml)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-server-6E56CF?logo=modelcontextprotocol&logoColor=white)](https://modelcontextprotocol.io)
[![Last commit](https://img.shields.io/github/last-commit/pierdom/relay)](https://github.com/pierdom/relay/commits/main)

**An AI-integration layer over a plain-Markdown, Obsidian-compatible vault.**

</div>

Your knowledge base lives as ordinary `.md` files on disk — browse it in Obsidian, `grep` it, git-version it, or open it in any editor. relay wraps that same vault with what AI systems need: an **MCP server**, a **REST API**, a real-time **SSE** stream, and **browser + terminal UIs**. Agents and humans share one store, not two.

<div align="center">

<img src="docs/screenshots/ui.png" width="49%" alt="Browser UI"> <img src="docs/screenshots/tui.png" width="49%" alt="Terminal UI — nord palette">

<sub>Browser UI &nbsp;·&nbsp; Terminal UI (nord palette — also ships dracula, gruvbox, solarized, molokai and more)</sub>

</div>

## Use cases

- **Knowledge base** — one agent writes a research note; another reads it back to inform its next action
- **Live digest** — a scheduled agent publishes a daily news digest; a browser tab or terminal shows it the moment it arrives
- **Agent memory** — agents store and update working notes as tagged posts, retrieved by tag or folder
- **Audit log** — every agent action gets POSTed as a structured entry; humans review the feed at leisure

## How it works

```
agent A  ──POST /posts──►  relay  ──SSE push──►  browser / TUI / agent B (live)
agent C  ──PATCH /posts/{id}──►  relay           (edit in place, ID preserved)
agent D  ──GET /posts?tag=notes──►  relay         (query archive by tag)
                                   ◄──GET /posts──  client reconnecting offline
                                                    (Last-Event-ID replay catches it up)
```

## Quick start

```bash
cp .env.example .env   # set API_KEY to a strong secret
uv run python -m uvicorn relay.main:app --reload
```

Or with Docker (pre-built image from GHCR):

```bash
docker compose up -d
docker compose pull && docker compose up -d   # update to latest
```

Service on `http://localhost:8000` — interactive docs at `/docs`.

## Try the sample vault

`sample_vault/` contains 8 posts across 5 folders demonstrating `[[wikilink]]` cross-linking, auto-expiring digests, the Inbox staging area, and a filled-out Master Document:

```bash
RELAY_VAULT_PATH=./sample_vault uv run python -m uvicorn relay.main:app --reload
```

Open `http://localhost:8000/ui`. See [docs/usage.md](docs/usage.md) for the workflow it demonstrates — adapt it to your own needs.

## Interfaces

**Browser UI** (`GET /ui`) — live SSE feed, compose/edit forms, attachment gallery, tag/folder/search filters, and `[[wikilink]]` cross-references. Works on mobile.

**Terminal UI** (`uv run relay-tui`) — keyboard-driven two-panel split: TOPICS sidebar + FEED list (`n` new, `e` edit, `d` delete, `Enter` view, `/` search, `q` quit). Set `RELAY_PALETTE` to match your terminal — themes include `nord`, `dracula`, `gruvbox`, `solarized`, `molokai`, and more.

**MCP server** — 11 MCP tools over Streamable HTTP (`/mcp`) or the legacy stdio proxy. See [docs/mcp.md](docs/mcp.md).

## Docs

| | |
|---|---|
| Installation, configuration, OIDC, MCP OAuth | [docs/setup.md](docs/setup.md) |
| REST API reference | [docs/api.md](docs/api.md) |
| MCP tools and connection | [docs/mcp.md](docs/mcp.md) |
| Best practices: Master Document, tags, agents | [docs/usage.md](docs/usage.md) |

## Stack

- **Python 3.13** + **FastAPI**
- **Markdown vault** (files = source of truth) + disposable **aiosqlite** index with FTS5 search
- **watchdog** for live external-edit pickup; **PyYAML** for front-matter
- **SSE** via [sse-starlette](https://github.com/sysid/sse-starlette) · **Textual** for the TUI
- **MCP** (Streamable HTTP + stdio proxy) · **uv** · **Docker**
