# Authentication and per-key scopes

Every request needs a bearer key or a browser session cookie. This page covers
the two things most setups eventually need: giving different agents their own
identity, and — once they have one — restricting what each identity can do.

## The two channels

Every endpoint requires `Authorization: Bearer <key>`, **or** the
`relay_session` cookie the browser UI sets after OIDC login or an API-key
paste. Both are checked by the same dependency; cookie-authenticated writes
additionally reject cross-site requests. See [docs/api.md](api.md#authentication)
for the full public/private endpoint breakdown and [docs/setup.md](setup.md#oidc-login-for-the-browser-ui-and-mcp)
for OIDC.

## Named keys (`RELAY_API_KEYS`)

A single `API_KEY` works fine for one person or one script. Once more than one
agent writes to the same vault — a news digest, a finance summary, you in the
browser — it's worth knowing which one made which change. `RELAY_API_KEYS`
gives each agent its own bearer key, resolving to its own identity:

```bash
# .env
RELAY_API_KEYS=news-agent:sk-a-long-random-secret,finance-agent:sk-another-one
```

Each named key attributes its own writes:

- The git commit's real `--author` field (`git log --format='%an <%ae>'` shows
  `news-agent <news-agent@relay.local>`; the *committer* stays the pinned
  `relay <relay@localhost>` identity, so `%cn <%ce>` is unchanged).
- The `changes` table's `author` column, filterable via `?author=` on
  `GET /changes` and `GET /posts` (REST) or the `author` parameter on
  `list_changes`/`list_posts` (MCP).
- A post's `updated_by` front-matter field — set on create, and overwritten on
  every subsequent write by whichever identity made it. A mechanical
  side-effect write that isn't really "by" whoever triggered it (a tag
  rename's bulk rewrite, an inbound-wikilink retarget on someone else's post)
  leaves the existing `updated_by` alone instead of misattributing it.

The primary `API_KEY` always keeps working too, under the reserved identity
`apikey`. A malformed entry, a name colliding with `apikey`, or a duplicate
name is skipped with a warning at startup — not a startup failure — so a
typo'd extra key just won't authenticate anything until you fix it.

`updated_by` and the `changes.author` column are **attribution, not a security
control**: anyone who can edit vault files directly (Obsidian, a text editor,
filesystem access) can hand-write any value into a post's front-matter, same
as every other field. Trust it for "who published this", not as an
authorization input.

## Per-key scopes (`RELAY_API_KEY_SCOPES`)

Named keys answer *who* wrote something. `RELAY_API_KEY_SCOPES` answers *what
they're allowed to write* — read-only keys, and keys restricted to specific
vault tags:

```bash
# .env
RELAY_API_KEYS=news-agent:sk-a-long-random-secret,readonly-dashboard:sk-yet-another
RELAY_API_KEY_SCOPES=news-agent:write:news+briefing,readonly-dashboard:read
```

Format: `name:mode[:tags]`, comma-separated, keyed by the same names as
`RELAY_API_KEYS` (deliberately a separate variable — see the note at the
bottom). Three modes:

| Mode | Example | Effect |
|------|---------|--------|
| `read` | `agent:read` | Every write route/tool 403s or errors for this key. Reads are unaffected. |
| `write:tags` | `agent:write:news+briefing` | Can write only posts/attachments whose tags are **all** within this set (`+`-joined). See "Tag semantics" below. |
| `full` | `agent:full` | Explicit full access — the same as no entry at all, for when you want it written down. |

**A name with no entry in `RELAY_API_KEY_SCOPES` is full access.** This is
what makes the feature safe to adopt incrementally: every key you already have
deployed keeps behaving exactly as it did before you ever set this variable.

A malformed entry, an unknown mode, a `write` entry with no (or an invalid)
tag, a duplicate name, or the reserved `apikey` name is skipped with a
warning at startup, never a startup failure — the primary `API_KEY` cannot be
scoped through this variable, matching its exemption everywhere else (the
OIDC allowlist, for instance).

### Tag semantics: ALL-of, not ANY-of

A `write`-restricted key may write a post or attachment only if **every** tag
on it — existing tags on an update, and any new tags being set — is in the
key's allowed set. A key scoped to `write:news+briefing` can write a post
tagged `["news"]` or `["news", "briefing"]` (both within its allowed set),
but not `["news", "finance"]` — the second tag being outside the allowed set
denies the whole write, even though `news` alone would have been fine. This
is deliberate: the alternative (any one matching tag is enough) would let a
`news`-scoped
key introduce content under a tag it was never granted, just by pairing it
with an allowed one.

An **untagged** post or attachment always denies a `write`-restricted key —
an empty tag set trivially satisfies "subset of anything", so without this
rule an untagged write would silently escape the restriction.

An attachment inherits its owning post's tags for this check (there's no
tag concept of its own); one filed by `folder=` alone, with no `post_id` or
`tags`, has no derivable owning tag (folders are a many-to-one projection of
tags) and is denied outright for any non-full key rather than guessing.

### What always requires full access

`rename_tag`, `set_tag_config`, and the embeddings toggle/backfill
(`PATCH /embeddings`, `POST /embeddings/backfill`) require a `full`-access key
regardless of any tag restriction — none of them has a single owning tag a
restricted key could be checked against (a rename's blast radius is every
post carrying the old tag; the embeddings toggle is a global setting).

### REST vs. MCP

Both surfaces enforce the same rules, from the same shared check, so a key's
effective permissions don't depend on which surface it's used through:

- **REST**: a read-only key gets `403` on any non-`GET` route (`POST`/`PUT`/`PATCH`/`DELETE`),
  checked once at the auth layer rather than per route. A tag-restricted key
  gets `403` from whichever route it hit, with
  `"This API key's scope does not permit this write"`.
- **MCP**: a read-only key's `tools/list` silently omits every write tool —
  it never sees `publish_post`, `delete_post`, etc. in its own manifest.
  Calling one anyway (a client that cached an older list) gets an
  `insufficient_scope` error. A tag-restricted key sees every tool but gets
  the same `"scope does not permit this write"` error back from the ones it
  can't use for a given post.

### Example: a read-only monitoring key

```bash
curl -s -H "Authorization: Bearer sk-yet-another" $RELAY/posts   # 200
curl -s -X POST -H "Authorization: Bearer sk-yet-another" -H 'Content-Type: application/json' \
     -d '{"title":"x","content":"x","tags":[]}' $RELAY/posts     # 403
```

### Example: an agent restricted to one tag

```bash
curl -s -X POST -H "Authorization: Bearer sk-a-long-random-secret" -H 'Content-Type: application/json' \
     -d '{"title":"Overnight roundup","content":"…","tags":["news"]}' $RELAY/posts
# 201 — "news" is in scope

curl -s -X POST -H "Authorization: Bearer sk-a-long-random-secret" -H 'Content-Type: application/json' \
     -d '{"title":"Market close","content":"…","tags":["finance"]}' $RELAY/posts
# 403 — "finance" is not
```

### The web UI paste-login carries a key's scope through

Pasting a key into the browser's "paste your API key" login mints a session
cookie for that key's own identity and scope — a read-only or tag-restricted
key can't get full access just by going through the browser instead of
sending a bearer header.

### How this shows up in the UI and TUI

`GET /status`'s `caller` field (`{mode, tags}`) is how a client learns its
own scope, and both first-party clients use it so a restricted key finds out
up front instead of from a rejected write:

- **Browser UI** — the status panel's "Access" section always shows it
  (`Full access` / `Read-only` / `Write — restricted to tags` with the
  allowed list). `+ New Post` is hidden outright for a read-only key. For a
  write-restricted key, the compose Publish button and the Edit
  modal/vault-lint pane's Save button validate the Tags field live — the
  exact same ALL-of/non-empty rule the server enforces — and disable with an
  explanation when the current tags aren't fully in scope. This is UX
  polish only: the server remains the real enforcement point, and every
  existing error path still fires as a fallback if a client-side check ever
  disagrees with it (e.g. scope changed server-side mid-session).
- **TUI** — the header sub-title shows the scope next to the live/offline
  dot (`read-only`, or `write:tag1,tag2`; nothing at all for full access).
  A read-only key blocks `n`/`e`/`d` locally with a toast instead of
  attempting a write the server would reject anyway. A tag-restricted key
  is **not** validated client-side in the TUI — an out-of-scope write still
  reaches the server and surfaces via the existing error toast.

---

*Why a separate variable from `RELAY_API_KEYS` instead of a third field on
it:* `RELAY_API_KEYS` parses each entry on its first colon only, so key
material may itself legally contain a colon. Appending a scope suffix to that
same grammar would be ambiguous to parse and risks corrupting already-deployed
key secrets. Keeping the two independent also means you can add or change a
key's scope by name, with no need to rotate the key itself.
