# relay — stability policy

This document defines what relay's version number promises and what it does not.

## Versioning

relay follows [semantic versioning](https://semver.org):

| Increment | When |
|-----------|------|
| **patch** (0.9.x) | bug fixes, documentation, internal refactors — no visible behaviour change |
| **minor** (0.x.0) | new stable endpoints or MCP tools added; all existing ones remain compatible |
| **major** (x.0.0) | a breaking change to the stable surface (renamed endpoint, removed parameter, changed response shape) |

**Pre-1.0 (0.x.x):** the stable surface below was already stable in practice; this document makes it official as of v1.0.0. The 0.x.x line is considered stable from v0.9.x onwards.

---

## Stable surface

The following are stable from v1.0.0. Breaking changes require a major version bump.

### REST API

All endpoints listed in [`docs/api.md`](api.md), excluding `/links` (see below):

| Endpoint | Methods |
|----------|---------|
| `/posts` | `POST`, `GET` |
| `/posts/{id}` | `GET`, `PATCH`, `DELETE` |
| `/posts/deleted` | `GET` |
| `/posts/{id}/backlinks` | `GET` |
| `/posts/{id}/history` | `GET`, `POST` |
| `/posts/{id}/history/{sha}` | `GET` |
| `/posts/{id}/restore` | `POST` |
| `/attachments` | `POST`, `GET` |
| `/attachments/uploads` | `POST` |
| `/attachments/uploads/{upload_id}` | `PUT` |
| `/attachments/{path}` | `GET`, `DELETE` |
| `/folders` | `GET` |
| `/tags` | `GET` |
| `/tags/{tag}` | `POST` |
| `/tags/{tag}/config` | `PATCH` |
| `/events` | `GET` |
| `/status` | `GET` |
| `/metrics` | `GET` |
| `/mcp` | `POST`, `GET` |

Stability covers: the HTTP methods, path shapes, query parameters, and response field names documented in `docs/api.md`. New optional query parameters and new fields in responses do not count as breaking changes.

### MCP tools (19)

All tool names and their documented parameters are stable:

`publish_post` · `update_post` · `get_post` · `delete_post` · `list_posts` · `get_post_history` · `get_post_revision` · `list_deleted_posts` · `restore_post` · `get_backlinks` · `add_attachment` · `create_upload` · `get_attachment` · `list_attachments` · `delete_attachment` · `list_tags` · `set_tag_config` · `rename_tag` · `get_status`

Adding new optional parameters to existing tools is not a breaking change. Adding new tools is a minor bump.

### Environment variables

The variables listed in `docs/setup.md` under "Configuration" are stable. New optional variables are minor; removing or renaming an existing variable is major.

---

## Out of scope — not covered by this promise

The following change freely within any release:

| What | Why |
|------|-----|
| **Browser UI** — DOM structure, CSS class names, JS module APIs, theme token names | Client-side; no external API contract |
| **SQLite index schema** — table and column layout of `index.db` | The index is disposable; rebuilt from vault files at startup. Never query it directly |
| **`.relay/` internal layout** — `history.git` format, `last_id`, `oauth.db` schema, `tags.yml` format, `uploads/` staging | Implementation details of history, auth, and cleanup subsystems |
| **`/links` endpoint** — wikilink resolution index | Internal to the browser UI; not intended for external callers |
| **`/mcp/oauth/**` paths** — the OAuth 2.1 AS endpoints | The OAuth protocol itself is stable; these paths are relay-internal plumbing |
| **MCP tool descriptions** — the human-readable strings passed to AI clients | Improved for clarity without a version bump |

---

## Surface freeze review (pre-1.0)

Before tagging v1.0.0, a deliberate pass was made over all 19 MCP tools and every REST endpoint asking "would I regret this name or shape in a year?" Conclusion: **no renames or reshapes are needed**. Notes:

- `add_attachment` — "add" is slightly unusual (vs. "upload" or "create") but captures the intent (the bytes may come from `source_url`, presigned slot, or base64, not only from an upload). Consistent with the REST `POST /attachments`. Keep.
- `create_upload` — names the operation correctly (creating a presigned upload slot, not the attachment itself). Keep.
- `set_tag_config` — verbose, but mirrors `PATCH /tags/{tag}/config` and is unambiguous. Keep.
- `/posts/deleted` — declared before `/{id}` for FastAPI path resolution; the shape is deliberate and documented. Keep.
- `/links` — marked out of scope above; not worth versioning as a stable endpoint.

All other names and shapes are unambiguous and consistent. The 19 tool names and REST paths above are locked as of v1.0.0.
