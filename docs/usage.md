# Usage guide

How to get the most out of relay as a knowledge hub, for both the human who owns the vault and the agents who write into it.

The `sample_vault/` directory is a working example of the workflow described here. Run it with `RELAY_VAULT_PATH=./sample_vault uv run python -m uvicorn relay.main:app --reload` and open `/ui` to explore it live. The organisation it follows is one reasonable approach. Adapt freely.

---

## The Master Document

relay reserves post `id=0` as the **Master Document**: a single source of truth read by both humans and agents. Every agent should call `get_post(id=0)` at the start of a session to load the tag taxonomy, naming rules, and house rules for the vault.

relay seeds an empty Master Document at startup. Replace its body with something like the template below.

### Template

````markdown
# Vault index

<one paragraph describing what this vault is for, who writes to it, and who reads it>

## Tag taxonomy

Tags are the primary navigation axis. Use only tags from this list; propose new ones by
editing this document rather than inventing them inline.

| Tag | Scope | TTL |
|-----|-------|-----|
| `news` | Breaking news digests | 24 h |
| `research` | Long-lived research notes | — |
| `homelab` | Self-hosting, infra, servers | — |
| `finance` | Markets, portfolio, budgeting | — |
| `reading` | Books, articles, papers | — |
| `ai` | AI tools, models, prompts | — |
| `inbox` | Unfiled / triage | — |
| `digest` | Scheduled summaries | 48 h |
| `memory` | Agent working notes | — |

Add `expires_at` or a per-tag TTL (via `set_tag_config`) for ephemeral tags.

## Folder conventions

| Folder | Purpose |
|--------|---------|
| `Homelab/` | Infra notes, runbooks, configs |
| `Finance/` | Market notes, portfolio updates |
| `Reading/` | Book/article notes |
| `Digests/` | Scheduled agent digests |
| `Inbox/` | Staging area — assign a domain tag to move a note out |

## Naming conventions

- **Date-prefix time-sensitive posts:** `YYYY-MM-DD <description>`
- **Noun phrases for evergreen notes:** `Home Network Map`, `ETF Portfolio`
- **One canonical post per topic** — update in place, don't create `v2` variants
- Avoid special characters in titles (`/`, `:`, `*`, `?`) — titles become filenames

## Agent instructions

1. **Read this document first** — `get_post(id=0)` at the start of every session.
2. **Search before creating** — `list_posts(search="<topic>")` to check for an existing post; update it rather than duplicating.
3. **Browse with `summary=true`** — call `get_post(id)` only when you need the full body.
4. **Use only tags from the taxonomy above** — if none fits, use `inbox` and note the intent in `source`.
5. **Cross-link related posts** with `[[Post Title]]` wikilinks.
6. **Set TTL for ephemeral content** — digests and status updates should have `expires_at` or live under a TTL-configured tag.
7. **Keep content atomic** — one post, one topic.
````

Update the Master Document via `PATCH /posts/0` or `update_post(id=0, content=…)`.

---

## Tagging strategy

Tags drive the sidebar filter, SSE stream, and search ranking. Keep the taxonomy small, stable, and self-describing.

**Domain tags over status tags.** Use `finance`, `homelab`, `ai` rather than `unread`, `todo`, `wip`. Status changes constantly and pollutes the taxonomy. Use `expires_at` or TTL for content that should disappear.

**Flat is better than deep.** relay has no tag hierarchy. Use `finance` and add specificity in the title (`ETF Allocation Review`) rather than `finance:etf`. FTS5 search will surface it.

**Three tags per post maximum.** The first domain tag determines folder placement. Make it count.

**Keep the taxonomy in the Master Document.** Agents read that table; if a tag isn't listed, they won't use it.

### TTL by content type

| Content type | Suggested TTL |
|-------------|--------------|
| Breaking news, alerts | 12–48 h |
| Weekly digests | 7–14 days |
| Status updates | 24–72 h |
| Research notes, evergreen knowledge | none |
| Agent working memory | session-scoped (`expires_at`) |

Set per-tag TTL via `POST /tags/{tag}/config` or `set_tag_config`. Per-post `expires_at` overrides the tag TTL.

---

## Agent behaviour

### When to create vs. update

| Situation | Action |
|-----------|--------|
| No post exists on this topic | `publish_post` with title, tags, source, content |
| A post exists and new info extends it | `update_post` — append or replace sections |
| A post exists but is outdated | `update_post` — overwrite stale sections, keep the ID |
| Genuinely new entry (e.g. a dated digest) | `publish_post` with a date-prefixed title |

### Efficient browsing

- `list_posts(summary=true)` — title, tags, folder, and excerpt. No body fetched.
- `get_post(id)` — only when you need the full content.
- `list_posts(tag="X", search="Y")` — scope to a domain and filter by keyword.

### Cross-linking

Use `[[Post Title]]` wikilinks in post bodies. relay resolves them case-insensitively and rewrites them on rename. Use `#NNN` (by id) for links that must survive title changes (e.g. `#0` always points to the Master Document).

### Ephemeral content

Set `expires_at` (ISO-8601) on any post that should auto-delete. Or configure a TTL on the tag so all posts under it expire automatically. The Master Document is TTL-exempt.

---

## Post and vault organisation

### One canonical post per topic

The strongest practice for a useful vault: one post per topic, updated in place. Avoid `Server Notes v2` or `AI Tools 2026-07`. Instead, date-stamp sections within the post. This keeps backlinks intact and lets bm25 ranking surface the canonical post first in search.

```markdown
## Current state
<what's true right now>

## 2026-07-16
<what changed>
```

### Inbox as staging area

Notes without a domain tag land in `Inbox/`. As soon as the note gets its first domain tag, relay moves it (and its attachments) to the matching folder automatically.

### Cross-links and attachments

| Content | Use |
|---------|-----|
| Another relay post | `[[Post Title]]` wikilink |
| Image or diagram | `![[file.png]]` — renders inline |
| PDF or binary file | `![[file.pdf]]` — renders as a download link |
| External URL | `[label](url)` |

Attachments live in `<Folder>/assets/`. Deleting a post removes orphaned attachments; shared assets are kept. In the browser UI, drag/drop, paste, or the 📎 button uploads a file and inserts its embed (large files stream through a presigned slot instead of base64). Agents attach via MCP `add_attachment`: a `source_url` the server fetches, a `path` the stdio proxy uploads from your machine, or a presigned `upload_id`.
