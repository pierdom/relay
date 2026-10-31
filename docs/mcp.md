# MCP server

relay exposes its whole API as **27 MCP tools**, so Claude or any MCP agent can read and write posts directly. Both connection methods ship server `instructions` and the master document as the `relay://master-document` resource (`text/markdown`).

## Tools

| Tool | Description |
|------|-------------|
| `publish_post` | Publish a post (title, content, tags, source, expires_at) |
| `update_post` | Change only the fields given; `""` clears `expires_at`/`source`. Optional `if_match` — see [Partial edits](#partial-edits-and-optimistic-concurrency) |
| `edit_post` | `str_replace` edit: `old_str` must match exactly once and differ from `new_str`; otherwise errors with the match count |
| `append_post` | Append to a post; a blank line separates it from existing content unless the post is empty |
| `get_post` | A post by id (`id=0` = master document). `outline=true` lists its headings with sizes; `section="Parent > Heading"` returns just that section. Other front-matter keys (Obsidian Properties, custom fields) come back read-only as `properties` |
| `list_posts` | Filter by tag/folder/search/author, page with limit/offset, sort/order; metadata + excerpt by default. `author` = whoever made the post's latest write ([auth.md](auth.md)). `mode=keyword\|semantic\|hybrid` ranks `search` and combines with tag/folder; semantic/hybrid error without embeddings. A bare `42`/`#42` search is an id lookup answered as `pinned`, ignoring every other filter |
| `delete_post` | Delete a post |
| `get_post_history` | A post's revisions; works for a deleted post (`exists:false`) |
| `get_post_revision` | A post exactly as it was at one revision — preview before restoring |
| `restore_post` | Restore to a revision sha, recreating a deleted post; the restore is itself recorded |
| `get_backlinks` | Posts linking here via `[[Title]]` or `#id` |
| `get_related` | Posts close by embedding that aren't cross-linked either way — a to-do list of missing links, not "more like this". Errors without embeddings |
| `list_deleted_posts` | Deleted but restorable posts: id, title, restorable sha, reason |
| `list_changes` | The vault changelog — see [below](#the-vault-changelog). Errors if history is off |
| `add_attachment` | Attach a file from `source_url` (server fetches), `upload_id` (a filled slot) or `data` (base64, tiny files). `post_id` appends `![[file]]` to that post. The stdio bridge also takes `path`, a local file |
| `create_upload` | Mint a presigned slot (`upload_id` + `upload_url`): PUT the bytes, then `add_attachment(upload_id=…)` |
| `get_attachment` | Fetch an attachment; images return as image content |
| `list_attachments` | List attachments, by `post_id` or `folder` |
| `delete_attachment` | Delete an attachment; reports posts still referencing it |
| `get_status` | Version, uptime, served vault, counts, which features actually work, embedding diagnostics, and `caller` — your key's effective scope |
| `trigger_embedding_backfill` | Re-run the embedding backfill (`force` wipes the cache); errors without embeddings or while one runs |
| `set_embeddings_enabled` | Toggle semantic/hybrid search until restart — see [Notes](#notes-for-agents) |
| `list_folders` | First-level folders with post counts — the values `folder` accepts |
| `list_tags` | Tags with post counts and each tag's `ttl_hours`/`expires_at`/`keep_last` (null when unset) |
| `set_tag_config` | Per-tag `ttl_hours`, `expires_at`, `keep_last` (keep the newest N). Replaces the whole config; none removes it |
| `rename_tag` | Rename a tag across every post, in one pass |
| `lint_vault` | Check the vault against #0's rules: tags, Inbox placement, broken links/embeds, H1, stale hub/plan, zero backlinks, unused tag config, zero-chunk posts, #0's post count. Ignores code. Read-only |

## Partial edits and optimistic concurrency

```
edit_post(id=54, old_str="## Status: draft", new_str="## Status: published")
append_post(id=54, content="## New section\nMore text.")
```

`edit_post` never guesses: zero matches or several both return `{"error": …}` — add context to `old_str` to disambiguate.

Every post response carries an `etag` that changes whenever any mutable field does (title, content, tags, source, expiry, a Property). Pass it as `if_match` on `update_post`/`edit_post`/`append_post` to refuse a write if someone else changed the post meanwhile:

```
post = get_post(id=54)
update_post(id=54, content="…", if_match=post["etag"])
→ {"error": "post #54 has changed since if_match was captured", "current": {…}}
```

`current` is the live post, so you can retry against it or ask a human. Without `if_match`, last write wins — except that `edit_post`/`append_post` always guard their own read-modify-write.

## The vault changelog

`list_changes` is a flat feed of every post-affecting write, newest first, read from the same git history (no second store):

```
list_changes(limit=20)                       → [{seq, id, title, action, when, sha, author}, …]
list_changes(since=1042)                     → everything after seq 1042
list_changes(since="2026-09-14T00:00:00Z")   → everything since then
```

`action` is `create`, `update`, `edit`, `append`, `delete`, `restore`, `tag_rename`, `external_edit`/`external_delete` (Obsidian, via the watcher) or `expiry`. `author` is the named key or OIDC user ([auth.md](auth.md)), `null` for older writes and for ones without an identity (TTL sweep, external edits); `author=` filters to one. SSE reconnects replay from this feed too, so edits and deletes made while disconnected aren't missed.

## Recovering a post

Every write is committed to git, so an overwritten or deleted post comes back without leaving the tool call:

```
list_deleted_posts()           → [{id, title, sha, when, reason, path}, …]
get_post_history(id=54)        → [{sha, short_sha, when, message, path, author}, …]
get_post_revision(id=54, sha="a8dcc37")   # preview
restore_post(id=54, sha="a8dcc37")
```

- **No id? Start with `list_deleted_posts`.** Its `sha` is already restorable. It reads history, so nothing moves on delete and there is nothing to purge. TTL expiries are hidden unless `include_expiry=true` — routine expiry would bury the delete you're after.
- **Deleted posts have history** (`exists: false`), deliberately not a 404. The delete commit itself isn't listed — every listed revision is one you can restore *to*; take the newest to undo a delete.
- **A restore keeps the id**, so `[[wikilinks]]` and `#id` references resolve again, and it is itself undoable. Short shas work.
- **Preview first** with `get_post_revision`: the history listing is metadata only.
- Posts older than history show one `vault: initial import` revision.

## Notes for agents

- **`get_status` reports what works, not what's configured.** `history.effective` false = git missing, writes not recoverable · `search.fts5` false = substring search · `search.embeddings` false = semantic/hybrid will error · `watcher.running` false = direct file edits aren't indexed. It also names the served vault — "am I talking to the instance I think I am".
- **Its `embeddings` block is the full picture**: model, dimension, size, coverage (`posts_embedded`/`posts_missing`/`chunks_total`), and `backfill.running` — check that before reading a silent semantic query on a fresh vault as "nothing matched".
- **Ids are never reused** and aren't contiguous; an `#id` always means the same post.
- **`list_posts` returns excerpts** (`summary=true`); `get_post` for the body. A search result's `match` is the passage that hit, often enough to answer without a `get_post`; it's `null` when the excerpt already shows it.
- **Read long posts by section.** `get_post(id, outline=true)` costs a few hundred bytes; then `get_post(id, section=…)` with a path from the outline, or the heading a semantic `match` starts with (`Heading > Sub: text`). A section's `content` is only that section — change it with `edit_post`, never `update_post`, which would replace the whole post with it.
- **`set_embeddings_enabled` lasts until restart.** Enabling resumes against the model the vector schema was built for (errors on a dimension mismatch rather than rebuilding live) and starts a backfill; disabling frees the model's memory at once.
- **`trigger_embedding_backfill` returns 409 while one runs** — including the one enabling just started. Check `embeddings.backfill.running` instead of retrying blind.
- **Scope errors are final.** A read-only key doesn't even see write tools; a tag-restricted key gets `"This API key's scope does not permit this write"` for posts outside its tags ([auth.md](auth.md)). Don't retry.

---

## Connect via Streamable HTTP (recommended)

`/mcp`, on the REST port. No checkout needed.

```bash
claude mcp add --transport http relay https://your-relay-host/mcp \
  --header "Authorization: Bearer <your-api-key>"
```

```json
{
  "mcpServers": {
    "relay": {
      "type": "streamable-http",
      "url": "https://your-relay-host/mcp",
      "headers": { "Authorization": "Bearer <your-api-key>" }
    }
  }
}
```

## Connect via the stdio bridge

For clients without remote MCP: `relay-mcp` bridges stdio to `/mcp` — same tools and results, plus a local `path` on `add_attachment`. Needs a checkout and `uv`. In `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "relay": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/relay", "relay-mcp"],
      "env": {
        "API_KEY": "<your-api-key>",
        "RELAY_BASE_URL": "https://your-relay-host"
      }
    }
  }
}
```

Update with `git pull` and a client restart.

---

## OAuth login (optional)

With `MCP_OAUTH_ENABLED=true` relay is its own OAuth 2.1 authorization server (fastmcp's `OIDCProxy`, brokering to your OIDC provider). In a client's connector dialog give only a name and URL; leave the OAuth fields blank. `API_KEY` keeps working. Setup and the consent screen: [setup.md](setup.md#oauth-21-for-remote-mcp-clients).
