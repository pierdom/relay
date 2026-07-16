---
id: 0
tags: []
created_at: '2026-07-01T09:00:00Z'
updated_at: '2026-07-16T10:00:00Z'
---

# Vault index

This vault is a personal knowledge hub — homelab runbooks, finance notes, reading log, and AI digests. Posts are written by both humans (in Obsidian or the browser UI) and agents (via MCP or REST). The rules below keep the vault coherent across both.

> This is a sample vault bundled with relay to demonstrate the suggested workflow.
> Adapt the tag taxonomy, folders, naming conventions, and agent instructions to your own needs — there is no single right way to organise a relay vault.

## Tag taxonomy

Use only tags from this list. To propose a new tag, update this document first.

| Tag | Scope | TTL |
|-----|-------|-----|
| `homelab` | Self-hosting, infrastructure, servers, networking | — |
| `finance` | Markets, portfolio, budgeting, investments | — |
| `reading` | Books, articles, papers | — |
| `ai` | AI tools, models, prompts, papers | — |
| `digest` | Scheduled agent summaries (news, markets, AI) | 7 days |
| `reference` | Stable reference material — keep indefinitely | — |
| `inbox` | Unfiled / triage — assign a domain tag to move out of Inbox | — |

Per-tag TTL is configured via `POST /tags/{tag}/config` or `set_tag_config`. The master document itself never expires.

## Folder conventions

| Folder | Domain tag | Purpose |
|--------|-----------|---------|
| `Homelab/` | `homelab` | Infra notes, runbooks, service configs |
| `Finance/` | `finance` | Portfolio notes, market digests |
| `Reading/` | `reading` | Book and article notes |
| `Digests/` | `digest` | Scheduled agent digests (AI, news, markets) |
| `Inbox/` | — | Staging area for notes without a domain tag |

A new post is automatically filed in the matching folder based on its first domain tag. The `Inbox` folder is for untagged notes — give a note its first domain tag and relay will move it into the right folder automatically.

## Naming conventions

- **Date-prefix time-sensitive posts**: `YYYY-MM-DD <description>` — digests, status updates, dated entries.
- **Noun phrases for evergreen notes**: `Home Network Map`, `ETF Portfolio`, `The Pragmatic Programmer`.
- **One canonical post per topic**: update in place rather than creating `v2` variants. Use dated headings within a post to track changes over time.
- Avoid special characters in titles (`/`, `:`, `*`, `?`) — titles become filenames.

## Agent instructions

Agents interacting with this vault must follow these rules:

1. **Read this document first.** Call `get_post(id=0)` at the start of any session to load the current tag taxonomy and naming rules.

2. **Search before creating.** Call `list_posts(search="<topic>")` to check whether a canonical post already exists. If it does, call `update_post` — do not create a duplicate.

3. **Use `summary=true` for browsing.** `list_posts` returns metadata + excerpt by default; call `get_post(id)` only when you need the full body.

4. **Use only tags from the taxonomy above.** If none fits, use `inbox` and note the intended tag in `source`.

5. **Cross-link related posts** using `[[Post Title]]` wikilinks in the body. This builds the backlinks graph used in the UI.

6. **Set TTL for ephemeral content.** Digests and status updates should have `expires_at` set or live under a tag with a TTL. Evergreen notes should not expire.

7. **Keep content atomic.** One post = one topic. If a post covers multiple unrelated things, split it.
