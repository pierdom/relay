# REST API

## Authentication

Every endpoint needs `Authorization: Bearer <API_KEY>`; the browser UI may use its `relay_session` cookie instead (same check, plus cookie-authenticated writes refuse cross-site requests). Swagger UI at `/docs`. Named keys and per-key scopes: [auth.md](auth.md).

**Public, no auth:** `/health`; the UI shell and static files (`/`, `/ui`, `/id/{id}`, `/static/*`, `/assets/*`, `/favicon.ico`); the API schema (`/docs`, `/redoc`, `/openapi.json` — no secrets, the repo is public); the login bootstrap (`/auth/*`, and `POST /session`, which takes the key itself); and with MCP OAuth on, fastmcp's OAuth surface — metadata, `/register`, `/authorize`, `/token`, `/consent`, `/mcp/oauth/callback` (the IdP's return leg, before login by definition; a binding cookie ties it to the browser that saw the consent screen). Anything else, unmatched paths included, answers 401.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/posts` | Publish. The response carries advisory `similar` posts when embeddings are on (never for digest-type posts) |
| GET | `/posts` | List — see [Listing and search](#listing-and-search). The master doc is pinned on the home feed |
| GET | `/posts/{id}` | One post. `?section=Heading` (or `Parent > Heading`) returns only that section in `content`, echoed as `section`; `?outline=true` adds `outline` (every heading's `path`, `level`, `chars`) and, without a section, narrows `content` to the text before the first heading. An unknown or ambiguous section is 422 listing the paths. The `etag` stays the whole post's |
| PATCH | `/posts/{id}` | Partial update — omitted fields unchanged, `null`/`""` clears `expires_at`/`source`. Optional `if_match` → 409 if the post changed |
| POST | `/posts/{id}/edit` | `{"old_str", "new_str"}` — `old_str` must match exactly once and differ from `new_str`, else 422 (with the match count) |
| POST | `/posts/{id}/append` | `{"content"}` appended after a blank line |
| DELETE | `/posts/{id}` | Delete |
| GET | `/posts/{id}/backlinks` | Posts linking here via `[[title]]` or `#id` |
| GET | `/posts/{id}/related` | Similar posts not yet cross-linked either way. 503 without embeddings |
| GET | `/posts/deleted` | Gone but restorable: id, title, **restorable** sha, reason (`deleted`/`external`/`expiry`). Expiries hidden unless `include_expiry=true` |
| GET | `/posts/{id}/history` | Revisions, newest first, `{sha, short_sha, when, message, path, author}`; works for a deleted post (`exists:false`) |
| GET | `/posts/{id}/history/{sha}` | The post at that revision (short sha ok) |
| POST | `/posts/{id}/restore` | `{"sha"}` — roll back, recreating a deleted post with its id |
| GET | `/changes` | Vault changelog `{seq, id, title, action, when, sha, author}`, newest first; `since` = a `seq` or ISO time; `?author=` filters. `author` is `null` for writes without an identity (TTL sweep, external edits). 503 if history is off |
| GET | `/links` | `(id, title)` index for resolving `[[Title]]` |
| GET | `/lint` | Vault checked against #0's rules (tags, Inbox, links/embeds, H1, stale hub/plan, backlinks, tag configs, zero chunks, #0's post count). #0 is exempt from all but the link checks |
| GET | `/folders` | First-level folders with post counts |
| GET | `/tags` | Tags with post counts and each tag's `ttl_hours`/`expires_at`/`keep_last` (`null` when unset) |
| POST | `/tags/{tag}/config` | Per-tag expiry and retention — see [Tags](#tags) |
| PATCH | `/tags/{tag}` | `{"new_name"}` — rename across all posts |
| POST | `/attachments` | Store a file — see [Attachments](#attachments) |
| POST | `/attachments/uploads` | Mint an upload slot (`upload_id` + `upload_url`) |
| PUT | `/attachments/uploads/{upload_id}` | Stream raw bytes into a slot (one capped body) |
| GET | `/attachments` | List (`folder` / `post_id`) |
| GET / DELETE | `/attachments/{path}` | Serve / delete by bare name or vault-relative path — never a `.md`, a dotfile, or anything under a dot-directory. Delete reports posts still referencing it |
| GET | `/events` | SSE — see [SSE stream](#sse-stream) |
| GET | `/status` | Diagnostics: version, uptime, vault path and counts, what actually works, embedding state, and `caller` (your key's scope, `{mode, tags}` — [auth.md](auth.md#how-this-shows-up-in-the-ui-and-tui)) |
| PATCH | `/embeddings` | `{"enabled": bool}` until restart. 503 if sqlite-vec/model unavailable, 409 on a dimension mismatch with the schema on disk |
| POST | `/embeddings/backfill` | Re-run the backfill (`force=true` wipes the cache). 503 without embeddings, 409 while one runs |
| GET | `/metrics` | Prometheus text. Bearer-gated: it would leak vault size and activity |
| GET | `/health` | Liveness, **no auth** |
| GET | `/id/{id}` | Redirect to `/?post={id}` (UI deep link; no auth, no lookup; 422 on a bad id) |
| POST/GET | `/mcp` | Streamable HTTP MCP — [mcp.md](mcp.md) |

---

## Posts

```bash
curl -X POST http://localhost:8000/posts \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"title": "Morning Digest", "content": "# Top Stories\n- Story A", "tags": ["news", "ai"], "source": "news-agent"}'

curl -X PATCH http://localhost:8000/posts/42 \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"tags": ["news", "ai", "verified"]}'
```

`PATCH` changes only what you send; `tags` replaces the list (`[]` clears it); `id` and `created_at` never change. Other front-matter keys (Obsidian `aliases`, custom fields) round-trip verbatim and are returned read-only as `properties` — edit them in Obsidian or by hand.

### Partial edits and optimistic concurrency

```bash
curl -X POST http://localhost:8000/posts/42/edit \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"old_str": "## Status: draft", "new_str": "## Status: published"}'

curl -X POST http://localhost:8000/posts/42/append \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"content": "## New section\nMore text."}'
```

Every single-post response carries an `etag` (JSON field and `ETag` header) that changes with any mutable field. Send it back as `if_match` — body field on `PATCH`/`/edit`/`/append`, or an `If-Match` header (the header wins) — and a post changed in the meantime answers `409` with its current state (`{"detail": {"error": …, "current": {…}}}`). Without it, last write wins.

### Listing and search

| Param | Description |
|-------|-------------|
| `tag` · `folder` · `author` | Filter (one of each). `author` = the identity of the post's latest write |
| `search` | FTS5 over title/content/source/tags, porter-stemmed, bm25-ranked (title and tags weigh more). A bare `42`/`#42` is an id lookup: that post as `pinned`, `items` empty, every other filter ignored |
| `mode` | `keyword` (default), `semantic`, `hybrid` — ranks `search`, combines with the filters. 503 without embeddings |
| `sort` · `order` | `updated` (default) or `created`; `desc` (default) or `asc`. With `search`, only a tiebreak after relevance |
| `summary` | `true`: metadata + plain-text excerpt (MCP's default). With `search`, each item's `match` quotes the passage that hit: the keyword snippet from the body, or for `semantic` the closest section as `Heading: text` (`hybrid` prefers the keyword snippet). `null` when only the title or tags matched, or when the passage is the excerpt's own text |
| `limit` · `offset` | Paging |

---

## SSE stream

```bash
curl -N "http://localhost:8000/events?tag=news" -H "Authorization: Bearer <key>"

# Reconnect: replays every change after seq 1042, then goes live
curl -N "http://localhost:8000/events?tag=news" -H "Authorization: Bearer <key>" -H "Last-Event-ID: 1042"
```

| Event | Data | When |
|-------|------|------|
| `post` | Full post | Create or edit — API, MCP, or a file edited outside relay |
| `delete` | `{"id": N}` | Delete, via API or the vault |
| `keepalive` | — | Every 30 s |

Each frame's `id:` is its `changes` `seq`, so `Last-Event-ID` replays edits and deletes to posts you'd already seen, not only new ones. Frames from writes with no changelog row (history off, and the watcher's and TTL sweep's live frames, which publish before their commit) carry no `id:`; reconnect catch-up still finds those changes. A client too far behind is disconnected so it reconnects and replays.

---

## Tags

```bash
curl -X POST http://localhost:8000/tags/news/config \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" -d '{"ttl_hours": 24}'

# Keep only the 7 newest digests (by creation)
curl -X POST http://localhost:8000/tags/digest/config \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" -d '{"keep_last": 7}'
```

- **The body replaces the whole config** (`{}` removes it) — send every field you mean to keep; `GET /tags` shows the current one.
- **TTL precedence:** a post's own `expires_at` > per-tag > `DEFAULT_TTL_HOURS`; across tags the shortest wins.
- **`keep_last: N`** expires all but the tag's N newest posts at the next sweep, skipping #0 and posts with their own `expires_at`. Deletions are recorded as `expiry` and restorable while history is on.
- **`expires_at`** must be ISO 8601; offsets and dates are normalised to `YYYY-MM-DDTHH:MM:SSZ` UTC, anything else is a 422 (the sweep compares lexically — `"1 week"` once sorted before every date and was swept at once). A non-ISO value hand-written into front-matter is skipped with a warning.

---

## Attachments

`POST /attachments` takes the bytes exactly one way:

- **`data`** — base64 in the request; for small files.
- **`source_url`** — an `http(s)` URL the server fetches: streamed, size-capped, SSRF-guarded, filename from the response if omitted.
- **`upload_id`** — for large files: `POST /attachments/uploads`, `PUT` the raw bytes to `upload_url`, then `POST /attachments` with the `upload_id`. Slots are single-use and expire after `ATTACHMENT_UPLOAD_TTL_SECONDS`.

With `post_id` the `![[file]]` embed is appended to that post. Names are vault-globally unique, so an embed resolves to exactly one file. Deleting a post removes the attachments only it referenced. Errors: bad `source_url` / unknown `upload_id` / invalid base64 → 400 · over the cap → 413 · outside the key's scope → 403, checked before any fetch. Only an allowlist of safe types is served inline; everything else downloads.
