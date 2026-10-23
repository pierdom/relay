# Authentication and per-key scopes

Every request needs `Authorization: Bearer <key>` **or** the `relay_session` cookie the browser UI sets after OIDC login or a pasted key. Both go through the same check; cookie-authenticated writes also refuse cross-site requests. Public endpoints: [api.md](api.md#authentication). OIDC: [setup.md](setup.md#oidc-login-for-the-browser-ui-and-mcp).

This page covers giving each agent its own identity, and then restricting what each identity may write.

## Named keys (`RELAY_API_KEYS`)

Once several agents write to one vault, you want to know which made which change. `RELAY_API_KEYS` gives each its own key and identity:

```bash
RELAY_API_KEYS=news-agent:sk-a-long-random-secret,finance-agent:sk-another-one
```

A named key's writes are attributed in three places:

- **The git commit's `--author`** (`news-agent <news-agent@relay.local>`); the committer stays `relay <relay@localhost>`.
- **`changes.author`**, filterable with `?author=` on `GET /changes` and `GET /posts`, or `author` on MCP `list_changes`/`list_posts`.
- **The post's `updated_by`** front-matter, rewritten by every write — except mechanical side-effect writes (a tag rename, an inbound-link retarget on someone else's post), which leave it alone.

`API_KEY` keeps working as the reserved identity `apikey`. A malformed entry, a name colliding with `apikey`, or a duplicate name is skipped with a startup warning, never a failure — that key just won't authenticate until fixed.

**Attribution is not a security control.** Anyone who can edit the vault files can write any `updated_by`. Trust it for "who published this", never as an authorization input.

## Per-key scopes (`RELAY_API_KEY_SCOPES`)

Named keys say *who* wrote; scopes say *what they may write*:

```bash
RELAY_API_KEYS=news-agent:sk-a-long-random-secret,readonly-dashboard:sk-yet-another
RELAY_API_KEY_SCOPES=news-agent:write:news+briefing,readonly-dashboard:read
```

`name:mode[:tags]`, comma-separated, keyed by the `RELAY_API_KEYS` names:

| Mode | Example | Effect |
|------|---------|--------|
| `read` | `agent:read` | Every write is refused; reads unaffected |
| `write:tags` | `agent:write:news+briefing` | May write only posts/attachments whose tags are **all** in the `+`-joined set |
| `full` | `agent:full` | Full access, written down — same as no entry |

**No entry means full access**, so adopting scopes never changes a key you already run. A malformed entry, unknown mode, `write` without a valid tag, duplicate name, or `apikey` (never scopable, like its OIDC-allowlist exemption) is skipped with a startup warning.

### Tag rules

- **ALL-of, not ANY-of.** Every tag on the post — existing ones on an update, plus any being set — must be allowed. `write:news+briefing` may write `["news"]` or `["news", "briefing"]`, not `["news", "finance"]`: otherwise pairing an allowed tag would smuggle content under one never granted.
- **Untagged is denied** — an empty set is trivially a subset of anything.
- **Attachments use their owning post's tags.** One filed by `folder=` alone (no `post_id` or `tags`) has no owning tag and is denied to any non-full key.
- **Edit and append check scope before matching content**, so a refusal can't reveal what a post contains.
- **Full access only:** `rename_tag`, `set_tag_config`, and the embeddings toggle/backfill (`PATCH /embeddings`, `POST /embeddings/backfill`) — none has a single owning tag to check.

### REST and MCP

One shared check, so permissions don't depend on the surface:

- **REST** — a read-only key gets `403` on every non-`GET` route; a tag-restricted key gets `403` `"This API key's scope does not permit this write"`.
- **MCP** — a read-only key's `tools/list` omits every write tool (calling one anyway from a cached list returns `insufficient_scope`). A tag-restricted key sees every tool and gets the same scope error where it doesn't apply.

```bash
curl -s -H "Authorization: Bearer sk-yet-another" $RELAY/posts                   # 200
curl -s -X POST -H "Authorization: Bearer sk-yet-another" -H 'Content-Type: application/json' \
     -d '{"title":"x","content":"x","tags":[]}' $RELAY/posts                     # 403: read-only

curl -s -X POST -H "Authorization: Bearer sk-a-long-random-secret" -H 'Content-Type: application/json' \
     -d '{"title":"Overnight roundup","content":"…","tags":["news"]}' $RELAY/posts   # 201
curl -s -X POST -H "Authorization: Bearer sk-a-long-random-secret" -H 'Content-Type: application/json' \
     -d '{"title":"Market close","content":"…","tags":["finance"]}' $RELAY/posts    # 403
```

### Browser sessions keep the key's scope

Pasting a key into the browser login mints a session for that key's identity and scope — the browser is no way around a restriction. The session lives only as long as the key: remove or rename it in `RELAY_API_KEYS` and its sessions stop working on the next request (the OIDC allowlist doesn't apply to them).

### How this shows up in the UI and TUI

`GET /status` → `caller` (`{mode, tags}`) tells a client its own scope up front:

- **Browser UI** — the status panel's Access section shows it. `+ New Post` is hidden for a read-only key; for a tag-restricted key, Publish and Save check the Tags field live with the server's own rule and explain when they're disabled. That's convenience only — the server stays the enforcement point.
- **TUI** — the header shows `read-only` or `write:tag1,tag2` (nothing for full access), and a read-only key's `n`/`e`/`d` stop locally with a toast. Tag restrictions aren't checked client-side; the server's error comes back as a toast.

---

*Why a separate variable:* `RELAY_API_KEYS` splits each entry on its first colon only, so a key may contain colons. A scope suffix on the same grammar would be ambiguous and could corrupt deployed secrets — and a separate variable lets you change a key's scope without rotating it.
