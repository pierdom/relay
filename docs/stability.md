# relay — stability policy

This document defines what relay's version number promises and what it does not.

## Versioning

relay follows [semantic versioning](https://semver.org):

| Increment | When |
|-----------|------|
| **patch** (1.2.x) | bug fixes, documentation, internal refactors — no visible behaviour change |
| **minor** (1.x.0) | new stable endpoints, MCP tools, parameters or variables; all existing ones remain compatible |
| **major** (x.0.0) | a breaking change to the stable surface (renamed endpoint, removed parameter, changed response shape) |

---

## Stable surface

The following are stable from v1.0.0. Breaking changes require a major version bump.

### REST API

All endpoints listed in [`docs/api.md`](api.md), excluding `/links` (see below):

| Endpoint | Methods |
|----------|---------|
| `/posts` | `POST`, `GET` |
| `/posts/{id}` | `GET`, `PATCH`, `DELETE` |
| `/posts/{id}/edit` | `POST` |
| `/posts/{id}/append` | `POST` |
| `/posts/deleted` | `GET` |
| `/posts/{id}/backlinks` | `GET` |
| `/posts/{id}/related` | `GET` |
| `/posts/{id}/history` | `GET` |
| `/posts/{id}/history/{sha}` | `GET` |
| `/posts/{id}/restore` | `POST` |
| `/changes` | `GET` |
| `/attachments` | `POST`, `GET` |
| `/attachments/uploads` | `POST` |
| `/attachments/uploads/{upload_id}` | `PUT` |
| `/attachments/{path}` | `GET`, `DELETE` |
| `/folders` | `GET` |
| `/lint` | `GET` |
| `/tags` | `GET` |
| `/tags/{tag}` | `PATCH` |
| `/tags/{tag}/config` | `POST` |
| `/events` | `GET` |
| `/status` | `GET` |
| `/embeddings` | `PATCH` |
| `/embeddings/backfill` | `POST` |
| `/metrics` | `GET` |
| `/health` | `GET` |
| `/id/{id}` | `GET` |
| `/mcp` | `POST`, `GET` |

Stability covers: the HTTP methods, path shapes, query parameters, and response field names documented in `docs/api.md`. New optional query parameters and new fields in responses do not count as breaking changes.

### MCP tools (27)

All tool names and their documented parameters are stable:

`publish_post` · `update_post` · `edit_post` · `append_post` · `get_post` · `delete_post` · `list_posts` · `get_post_history` · `get_post_revision` · `list_deleted_posts` · `restore_post` · `get_backlinks` · `get_related` · `list_changes` · `add_attachment` · `create_upload` · `get_attachment` · `list_attachments` · `delete_attachment` · `list_tags` · `set_tag_config` · `rename_tag` · `get_status` · `trigger_embedding_backfill` · `set_embeddings_enabled` · `list_folders` · `lint_vault`

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
| **`.relay/` internal layout** — `history.git` format, `last_id`, `mcp_oauth/` (fastmcp `DiskStore` directory, relay #313) contents, `tags.yml` format, `uploads/` staging | Implementation details of history, auth, and cleanup subsystems |
| **`/links` endpoint** — wikilink resolution index | Internal to the browser UI; not intended for external callers |
| **The OAuth 2.1 AS endpoints** — `/register`, `/authorize`, `/token`, `/consent`, `/.well-known/oauth-*`, `/mcp/oauth/callback` | The OAuth protocol itself is stable; these paths are fastmcp-internal plumbing, not a relay-designed surface |
| **MCP tool descriptions** — the human-readable strings passed to AI clients | Improved for clarity without a version bump |

---

## Surface freeze review

Before v1.0.0, every MCP tool and REST endpoint then shipped (19 tools) was reviewed with the question "would I regret this name or shape in a year?". None needed a rename. `add_attachment` (the bytes may come from a URL, a presigned slot or base64, not only an upload), `create_upload` (it creates the slot, not the attachment) and `set_tag_config` (mirrors `POST /tags/{tag}/config`) were kept deliberately.
