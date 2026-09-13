# REST API

All endpoints require `Authorization: Bearer <API_KEY>`. Browser-UI requests may authenticate with the `relay_session` cookie instead of the bearer token; both are checked by the same dependency (cookie-authenticated writes additionally reject cross-site requests). Interactive docs (Swagger UI) at `/docs`.

**Public, no auth:** `/health` (container healthcheck), the UI shell and its static files (`/`, `/ui`, `/id/{id}`, `/static/*`, `/assets/*`, `/favicon.ico`), the API schema (`/docs`, `/redoc`, `/openapi.json` — it carries no secrets and this repository is public), the login bootstrap (`/auth/*`, `POST /session` which itself takes the key) and, when MCP OAuth is enabled, the OAuth AS metadata, `/register` and `/mcp/oauth/callback`. Every other path — including any unmatched one — answers 401. `/id/{id}` is a redirect only (`/?post={id}`), not a lookup — it never touches the vault, so it needs no auth of its own; the UI fetches the post itself, authenticated, once it lands on `/`.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/posts` | Publish a post |
| GET | `/posts` | List posts (`tag`, `folder`, `search`, `summary`, `limit`, `offset`, `sort`, `order`, `mode`; master doc pinned on home feed). `mode` = `keyword` (default) / `semantic` / `hybrid` ranks a `search` and combines with `tag`/`folder`; 503 if embeddings are off. `sort` = `updated` (default, last-modified) or `created`; `order` = `desc` (default) or `asc`. A `search` ranks by relevance first and uses `sort`/`order` only as a tiebreak. A bare id or `#id` as `search` (e.g. `42`, `#42`) — the same `#NNN` convention `/posts/{id}/backlinks` resolves — is a lookup, not a ranked search: answers with just that post as `pinned` (`items` empty), ignoring `mode`/`tag`/`folder`, whether or not embeddings are enabled |
| GET | `/id/{id}` | Redirects to `/?post={id}` — the UI opens that post on load. A convenience for pasting a post id somewhere and landing directly on it, not an API endpoint (no auth, no body, `422` on a non-numeric or negative id) |
| GET | `/posts/{id}` | Get a single post |
| PATCH | `/posts/{id}` | Partial update — omitted fields unchanged; `null` or `""` clears `expires_at`/`source`. Optional `if_match` (body field or `If-Match` header) rejects the write with `409` if the post changed since |
| POST | `/posts/{id}/edit` | `str_replace`-style partial edit: `{"old_str": …, "new_str": …}` — `old_str` must match exactly once in the current content and differ from `new_str`, or `422` (naming the match count if ambiguous) |
| POST | `/posts/{id}/append` | Append `{"content": …}` to the post instead of resending the whole body; a blank line separates it from existing content |
| DELETE | `/posts/{id}` | Delete a post |
| GET | `/posts/{id}/backlinks` | Posts linking here via `[[title]]` or `#id` |
| GET | `/status` | Runtime diagnostics: version, uptime, vault path + counts, effective feature state, embedding model/coverage/backfill diagnostics |
| PATCH | `/embeddings` | Turn semantic/hybrid search on or off at runtime, without a restart (`{"enabled": bool}`). In-memory only. 503 if sqlite-vec/model unavailable, 409 on a dimension mismatch against the schema already on disk |
| POST | `/embeddings/backfill` | Re-run the embedding backfill without a restart (`force=true` wipes the cache first). 503 if embeddings aren't enabled, 409 if a backfill is already running |
| GET | `/metrics` | Prometheus/OpenMetrics text exposition (bearer-gated — relay sits behind a public proxy, and an open `/metrics` would leak vault size and activity) |
| GET | `/health` | Liveness probe — **no auth**, trivial by design; probed every 30 s by the Docker healthcheck |
| GET | `/posts/deleted` | Posts whose file is gone but which history can restore — id, title, the **restorable** sha, and why it went (`deleted`/`external`/`expiry`). TTL expiries excluded unless `include_expiry=true`. ⚠️ Declared before `/{id}`, or FastAPI parses `deleted` as an int and answers 422 |
| GET | `/posts/{id}/history` | Revisions of a post from vault history, newest first; answers for a **deleted** post too (`exists:false`) |
| GET | `/posts/{id}/history/{sha}` | The post as it was at that revision (preview before restoring) |
| POST | `/posts/{id}/restore` | Roll a post back to a revision (`{"sha": …}`), recreating it if deleted |
| GET | `/links` | `(id, title)` index for resolving `[[Title]]` wikilinks |
| GET | `/folders` | First-level vault folders with post counts |
| POST | `/attachments` | Store an attachment; bytes via `data` (base64), `source_url` (server fetches), or `upload_id` (filled slot). With `post_id`, appends `![[file]]` to that post |
| POST | `/attachments/uploads` | Mint a presigned upload slot (`upload_id` + `upload_url`) |
| PUT | `/attachments/uploads/{upload_id}` | Stream raw bytes into a slot (single, capped body) |
| GET | `/attachments` | List attachments (`folder`/`post_id` scope) |
| GET | `/attachments/{path}` | Serve a vault attachment |
| DELETE | `/attachments/{path}` | Delete an attachment; reports posts still referencing it |
| GET | `/tags` | List tags with post counts |
| POST | `/tags/{tag}/config` | Set per-tag TTL (`ttl_hours` and/or `expires_at`); an empty body `{}` removes the tag's config |
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

Only the fields you send are changed. `tags` replaces the list wholesale; an empty array clears all tags. `id` and `created_at` are never modified. Any other front-matter key present in the file (Obsidian Properties like `aliases`/`cssclasses`, or a hand-added custom field) round-trips verbatim through every write and is returned read-only as `properties` — there is no way to set it over the API; edit it in Obsidian or by hand.

### Partial edits and optimistic concurrency

For editing part of a long post without resending the whole body:

```bash
# Replace one occurrence of a substring (422 if it's not found, or matches more than once)
curl -X POST http://localhost:8000/posts/42/edit \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"old_str": "## Status: draft", "new_str": "## Status: published"}'

# Append rather than replace
curl -X POST http://localhost:8000/posts/42/append \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"content": "## New section\nMore text."}'
```

Every single-post response (`GET`/`PATCH`/`POST /posts`/`/edit`/`/append`/`/restore`) carries an `etag` — an opaque token that changes whenever any mutable field of the post changes — both in the JSON body and as an `ETag` response header. Pass it back as `if_match` (a body field on `PATCH`/`/edit`/`/append`, or an `If-Match` request header — the header wins if both are given) to detect a concurrent change: if the post no longer matches, the write is rejected with `409` and the response carries the post's current state (`{"detail": {"error": "...", "current": {...}}}`) instead of silently overwriting it. Omitting `if_match` is today's exact behavior — last write wins, unchanged.

### Listing and search

Key query params for `GET /posts`:

| Param | Description |
|-------|-------------|
| `tag` | Filter by tag (exact) |
| `folder` | Filter by vault folder name |
| `search` | FTS5 full-text over title/content/source/tags; porter-stemmed, bm25-ranked |
| `summary` | `true` returns metadata + plain-text excerpt only (default in MCP) |
| `limit` / `offset` | Pagination |
| `mode` | Ranking for `search`: `keyword` (default), `semantic`, or `hybrid` (relay #253, proof of concept). `semantic`/`hybrid` 503 if this relay doesn't have embeddings enabled, and 400 if combined with `tag`/`folder` (the ranked path doesn't apply them) |

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

Edits to older posts are sent without an SSE `id:` so they don't rewind a client's `Last-Event-ID`. Catch-up replay on reconnect is append-only. Edits and deletes to already-seen posts require a manual refresh.

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

`expires_at` (on posts and tag configs) must be an ISO 8601 datetime; offsets and date-only values are accepted and normalised to `YYYY-MM-DDTHH:MM:SSZ` (UTC). Anything else is a 422 — the sweep compares timestamps lexically, so a relative value like `"1 week"` used to sort *before* every real date and delete the post at the next run. A non-ISO value hand-written into front-matter is skipped with a warning.

### Rename

```bash
curl -X PATCH http://localhost:8000/tags/news \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"new_name": "journalism"}'
```

---

## Attachments

Upload via `POST /attachments`, providing the bytes exactly one of three ways:

- **`data`** — base64-encoded body. Simple, but the whole blob rides in the request; fine for small files.
- **`source_url`** — an `http(s)` URL the **server** fetches (streamed, size-capped, SSRF-guarded; filename derived from the response when omitted). No bytes in the request.
- **`upload_id`** — for large files, mint a slot with `POST /attachments/uploads`, `PUT` the raw bytes to the returned `upload_url` (out-of-band, not base64), then finalize with `POST /attachments` carrying the `upload_id`. Slots are single-use and short-lived (`ATTACHMENT_UPLOAD_TTL_SECONDS`).

With `post_id`, the `![[file]]` embed is automatically appended to that post. Filenames are vault-globally unique, so `![[name]]` always resolves to exactly one file. Deleting a post removes orphaned attachments; shared assets are kept. A failed `source_url`/unknown `upload_id` → `400`; over the size cap → `413`.
