# REST API

All endpoints require `Authorization: Bearer <API_KEY>`. Interactive docs (Swagger UI) at `/docs`.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/posts` | Publish a post |
| GET | `/posts` | List posts (`tag`, `folder`, `search`, `summary`, `limit`, `offset`; master doc pinned on home feed) |
| GET | `/posts/{id}` | Get a single post |
| PATCH | `/posts/{id}` | Partial update — omitted fields unchanged |
| DELETE | `/posts/{id}` | Delete a post |
| GET | `/posts/{id}/backlinks` | Posts linking here via `[[title]]` or `#id` |
| GET | `/links` | `(id, title)` index for resolving `[[Title]]` wikilinks |
| GET | `/folders` | First-level vault folders with post counts |
| POST | `/attachments` | Upload a base64 attachment; with `post_id`, appends `![[file]]` to that post |
| GET | `/attachments` | List attachments (`folder`/`post_id` scope) |
| GET | `/attachments/{path}` | Serve a vault attachment |
| DELETE | `/attachments/{path}` | Delete an attachment; reports posts still referencing it |
| GET | `/tags` | List tags with post counts |
| POST | `/tags/{tag}/config` | Set per-tag TTL |
| PATCH | `/tags/{tag}` | Rename a tag across all posts |
| GET | `/events` | SSE stream (`?tag=` filter, `Last-Event-ID` replay) |
| POST/GET | `/mcp` | Streamable HTTP MCP endpoint (see [mcp.md](mcp.md)) |

---

## Posts

### Publish

```bash
curl -X POST http://localhost:8000/posts \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Morning Digest",
    "content": "# Top Stories\n- Story A\n- Story B",
    "tags": ["news", "ai"],
    "source": "news-agent"
  }'
```

### Partial update

```bash
curl -X PATCH http://localhost:8000/posts/42 \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"tags": ["news", "ai", "verified"]}'
```

Only the fields you send are changed. `tags` replaces the list wholesale; an empty array clears all tags. `id` and `created_at` are never modified.

### Listing and search

Key query params for `GET /posts`:

| Param | Description |
|-------|-------------|
| `tag` | Filter by tag (exact) |
| `folder` | Filter by vault folder name |
| `search` | FTS5 full-text over title/content/source/tags; porter-stemmed, bm25-ranked |
| `summary` | `true` returns metadata + plain-text excerpt only (default in MCP) |
| `limit` / `offset` | Pagination |

---

## SSE stream

```bash
# Live stream
curl -N "http://localhost:8000/events?tag=news" -H "Authorization: Bearer <key>"

# Reconnect — replays posts with id > 42 before entering the live stream
curl -N "http://localhost:8000/events?tag=news" \
  -H "Authorization: Bearer <key>" \
  -H "Last-Event-ID: 42"
```

A `keepalive` fires every 30 s. Event types:

| Event | Data | When |
|-------|------|------|
| `post` | Full post object | On create or edit (API, MCP, or external vault edit via watcher) |
| `delete` | `{"id": N}` | On delete via API or vault |
| `keepalive` | — | Every 30 s |

Edits to older posts are sent without an SSE `id:` so they don't rewind a client's `Last-Event-ID`. Catch-up replay on reconnect is append-only — edits and deletes to already-seen posts require a manual refresh.

---

## Tags

### Per-tag TTL

```bash
curl -X POST http://localhost:8000/tags/news/config \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"ttl_hours": 24}'
```

TTL precedence: per-post `expires_at` > per-tag config > global `DEFAULT_TTL_HOURS`. For multi-tag posts, the shortest TTL wins.

### Rename

```bash
curl -X PATCH http://localhost:8000/tags/news \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"new_name": "journalism"}'
```

---

## Attachments

Upload via `POST /attachments` with a base64-encoded body. With `post_id`, the `![[file]]` embed is automatically appended to that post. Filenames are vault-globally unique, so `![[name]]` always resolves to exactly one file. Deleting a post removes orphaned attachments; shared assets are kept.
