# MCP server

relay exposes the full feed API as MCP tools so Claude — or any MCP-capable agent — can read and write posts directly. Both connection methods ship server `instructions` and expose the master document as the `relay://master-document` resource (`text/markdown`).

## Tools

| Tool | Description |
|------|-------------|
| `publish_post` | Publish a post (title, content, tags, source, expires_at) |
| `update_post` | Partially update a post by ID — only provided fields change |
| `get_post` | Get a post by ID (`id=0` for the master document) |
| `list_posts` | List posts with tag/search/limit/offset filters; returns metadata + excerpt by default |
| `delete_post` | Delete a post by ID |
| `add_attachment` | Upload a base64 file; with `post_id` appends `![[file]]` to that post |
| `get_attachment` | Retrieve an attachment; images return as inline image content |
| `list_attachments` | List attachments; scope by `post_id` or `folder` |
| `delete_attachment` | Delete an attachment; reports posts still referencing it |
| `list_tags` | List all tags with post counts |
| `set_tag_config` | Set per-tag expiry (`ttl_hours` or `expires_at`) |

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
