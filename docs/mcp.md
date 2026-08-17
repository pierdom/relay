# MCP server

relay exposes the full feed API as **16 MCP tools** so Claude — or any MCP-capable agent — can read and write posts directly. Both connection methods ship server `instructions` and expose the master document as the `relay://master-document` resource (`text/markdown`).

## Tools

| Tool | Description |
|------|-------------|
| `publish_post` | Publish a post (title, content, tags, source, expires_at) |
| `update_post` | Partially update a post by ID — only provided fields change |
| `get_post` | Get a post by ID (`id=0` for the master document) |
| `list_posts` | List posts with tag/search/limit/offset filters; returns metadata + excerpt by default |
| `delete_post` | Delete a post by ID |
| `get_post_history` | List a post's revisions from vault history; works for a deleted post (`exists:false`) |
| `restore_post` | Restore a post to a revision sha, recreating it if deleted; the restore is itself recorded |
| `list_deleted_posts` | Posts that are gone but restorable — id, title, restorable sha, and why they went |
| `add_attachment` | Attach a file; bytes via `source_url` (server fetches), `upload_id` (a filled presigned slot), or `data` (base64, tiny files only). With `post_id` appends `![[file]]` to that post. The stdio proxy also accepts `path` (a local file it uploads for you) |
| `create_upload` | Mint a presigned upload slot (`upload_id` + `upload_url`); PUT the raw bytes there, then finalize with `add_attachment(upload_id=…)` |
| `get_attachment` | Retrieve an attachment; images return as inline image content |
| `list_attachments` | List attachments; scope by `post_id` or `folder` |
| `delete_attachment` | Delete an attachment; reports posts still referencing it |
| `get_status` | Version, uptime, which vault is served, counts, and which features actually work |
| `list_tags` | List all tags with post counts |
| `set_tag_config` | Set per-tag expiry (`ttl_hours` or `expires_at`) |

## Recovering a post

Every write to the vault is committed to git, so a post that was overwritten or
deleted can be brought back without leaving the tool call.

```
list_deleted_posts()           → [{id, title, sha, when, reason, path}, …] newest first
get_post_history(id=54)        → [{sha, short_sha, when, message, path}, …] newest first
restore_post(id=54, sha="a8dcc37")
```

- **Start with `list_deleted_posts` when you do not know the id.** Everything else
  here needs one, and after a delete nobody has one — that is the whole reason the
  tool exists. The `sha` it reports is already a restorable revision, so it can go
  straight to `restore_post` without a `get_post_history` round trip.
- **It is not a trash can.** Nothing is moved on delete and there is nothing to
  purge; this reads the git history that already records every removal. TTL
  expiries are excluded unless you pass `include_expiry=true`, because a vault
  with per-tag TTLs sheds routine posts constantly and they would bury the
  deletion you are looking for.

- **Works for a deleted post.** `get_post_history` answers with `exists: false` and
  still lists restorable revisions — that is the case most worth recovering, so it
  is deliberately not a 404.
- **The delete commit itself is not listed.** Every revision returned is one you
  can restore *to*, and a file has no content at the commit that removed it.
  Restore from the newest listed revision to undo a deletion.
- **A restore keeps the post's id**, so inbound `[[wikilinks]]` and `#id`
  references resolve again — and it is itself committed, so it can be undone the
  same way.
- **Short shas are accepted.**
- Posts that predate vault history show a single `vault: initial import`
  revision — their state when history was first enabled.

To read a revision's *content* before restoring it, use the REST endpoint
`GET /posts/{id}/history/{sha}`; the browser UI's History panel is built on it.
There is no MCP tool for the preview.

## Notes for agents

- **`get_status` reports what is actually working**, not what is configured:
  `history.effective` is false when the git binary is missing (so writes are not
  recoverable), `search.fts5` false means search fell back to substring matching,
  and `watcher.running` false means edits made directly to the files are not being
  indexed. It also reports which vault path is being served, which settles "am I
  talking to the instance I think I am".
- **Post ids are never reused.** Deleting the newest post does not free its id, so
  an `#id` reference always means the same post — ids are simply not contiguous.
- **`list_posts` returns metadata + excerpt by default** (`summary=true`); call
  `get_post` for a full body.

---

## Connect via Streamable HTTP (recommended)

The MCP endpoint is at `/mcp`, on the same port as the REST API — no local checkout needed.

```bash
claude mcp add --transport http relay https://your-relay-host/mcp \
  --header "Authorization: Bearer <your-api-key>"
```

Or in a `streamable-http` client config:

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

## Connect via stdio proxy (legacy)

For clients that don't support remote MCP, `relay-mcp` runs locally and proxies to the relay over REST. Requires a checkout of this repo and `uv`.

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

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

Run `git pull` and restart the client to pick up updates.

---

## OAuth login (optional)

With `MCP_OAUTH_ENABLED=true`, relay acts as its own OAuth 2.1 Authorization Server. Remote clients authenticate via OAuth + Dynamic Client Registration instead of a pasted bearer key — in the connector dialog, fill only the **name** and **URL** and leave the OAuth fields blank. The static `API_KEY` keeps working alongside OAuth.

Setup: [docs/setup.md — OAuth 2.1 for remote MCP clients](setup.md#optional-oauth-21-for-remote-mcp-clients).
