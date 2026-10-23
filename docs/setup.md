# Setup

## Which setup do you need?

**Local (one machine)** — Claude Code, Claude Desktop, or the browser/TUI on the same computer. The static API key is all you need.
→ [Install locally](#installation--local), connect via the [stdio MCP proxy](mcp.md#connect-via-the-stdio-bridge).

**Remote (phone, Claude.ai, shared access)** — a server with a public HTTPS URL, an OIDC provider so remote clients can sign in, and `MCP_OAUTH_ENABLED=true` for Claude.ai's OAuth flow.
→ [Deploy with Docker](#installation--docker), then [OIDC](#oidc-login-for-the-browser-ui-and-mcp) and [MCP OAuth](#oauth-21-for-remote-mcp-clients).

**Prerequisites:** Python ≥ 3.13 + [uv](https://docs.astral.sh/uv/) (local), or Docker + Docker Compose.

## Single process only

relay must run as **one worker**: upload slots live in memory (a `PUT` and its finalise must reach the same process) and the id allocator relies on an in-process lock. More workers silently corrupt uploads and can duplicate post ids. relay refuses to start with `WEB_CONCURRENCY` > 1, and uvicorn/gunicorn `--workers` is unsupported. For more load, put a reverse proxy in front of the one process.

---

## Installation — local

```bash
cp .env.example .env        # set API_KEY at minimum
uv run python -m uvicorn relay.main:app --reload
```

Serves `http://localhost:8000`, interactive API docs at `/docs`.

## Installation — Docker

```bash
docker compose up -d
docker compose pull && docker compose up -d     # update
```

Set `RELAY_BASE_URL` to your public URL first. The healthcheck probes `GET /health` (no auth) every 30 s; `docker ps` shows `healthy` when ready. The reference `docker-compose.yml` also runs relay read-only with no capabilities, and sets `init: true` so a real init reaps orphaned child processes (without it they pile up as zombies under uvicorn) — keep it if you write your own compose file.

---

## Configuration reference

Copy `.env.example` to `.env`. Only `API_KEY` is required:

```bash
echo "API_KEY=$(openssl rand -hex 32)" >> .env
```

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | **required** | Bearer token for all endpoints |
| `RELAY_API_KEYS` | `""` | Named keys (`name:key,…`), each attributed separately (commit author, `changes.author`, `updated_by`) — [auth.md](auth.md) |
| `RELAY_API_KEY_SCOPES` | `""` | Restrict a named key: `name:read`, `name:write:tag1+tag2`, `name:full`. No entry = full access — [auth.md](auth.md) |
| `RELAY_VAULT_PATH` | `/data/vault` | The vault; relay's index and state live in `<vault>/.relay/` |
| `RELAY_BASE_URL` | `http://localhost:8000` | Public URL (stdio proxy, OAuth redirects) |
| `DEFAULT_TTL_HOURS` | `0` | Global expiry; `0` = off (per-tag rules still apply) |
| `CLEANUP_INTERVAL_MINUTES` | `60` | How often expired posts are swept |
| `RELAY_WATCH_ENABLED` | `true` | Reindex + SSE for edits made outside relay (Obsidian, nvim) |
| `RELAY_HISTORY_ENABLED` | `true` | Commit every write to git so posts can be restored. Needs the `git` binary — check `features.history.effective` in `/status` |
| `RELAY_EMBEDDING_ENABLED` | `false` | Semantic/hybrid search — read [Semantic search](#semantic-search-optional) first |
| `EMBEDDING_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | fastembed model id (no `RELAY_` prefix) |
| `EMBEDDING_THREADS` | `1` | onnxruntime threads. Embedding is sequential, so more threads mostly cost memory |
| `EMBEDDING_IDLE_UNLOAD_SECONDS` | `300` | Unload the model after this much idle time; `0` keeps it resident |
| `SECURE_COOKIES` | `true` | `Secure` session cookie; `false` for plain HTTP |
| `ATTACHMENT_MAX_MB` | `25` | Upload limit, all transports (413 above it) |
| `ATTACHMENT_UPLOAD_TTL_SECONDS` | `3600` | Presigned upload slot lifetime |
| `ATTACHMENT_FETCH_TIMEOUT_SECONDS` | `20` | Timeout for a server-side `source_url` fetch |
| `OIDC_ISSUER` | `""` | OIDC provider URL; with the client credentials below, enables Login with OIDC (blank = paste-a-key login) |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | `""` | Confidential client credentials |
| `SESSION_SECRET` | `""` | Signs the session cookie (falls back to `API_KEY`, with a startup warning when OIDC is on). `openssl rand -hex 32` |
| `SESSION_MAX_AGE_HOURS` | `720` | Session lifetime (30 days) |
| `OIDC_ALLOWED_SUBS` | `""` | `sub` allowlist — immutable ids, preferred over email |
| `OIDC_ALLOWED_EMAILS` | `""` | Verified-email allowlist. Both empty = any user your IdP authenticates |
| `MCP_OAUTH_ENABLED` | `false` | OAuth 2.1 + Dynamic Client Registration on `/mcp`; `API_KEY` keeps working |
| `MCP_REQUIRED_SCOPES` | `relay` | Scope required on `/mcp` |
| `MCP_ALLOWED_REDIRECT_HOSTS` | `claude.ai,claude.com,chatgpt.com,*.mistral.ai` | DCR redirect hosts, https only; blank = any. `*.x` matches subdomains, not the apex — list both if needed |
| `RELAY_PALETTE` | `default` | TUI palette — names in [tui.md](tui.md) |
| `RELAY_TRANSPARENT` | `0` | TUI: let the terminal background show through |

**Docker-only** (read by `docker-compose.yml`, not relay):

| Variable | Default | Description |
|----------|---------|-------------|
| `RELAY_VAULT_DIR` | `./vault` | Host path mounted at `/data/vault` — use an absolute path in production |
| `RELAY_UID` / `RELAY_GID` | `1000` | Run as your host user so vault files stay yours (Syncthing, Obsidian, backups) |

---

## Semantic search (optional)

`RELAY_EMBEDDING_ENABLED=true` adds chunk-level embeddings (sqlite-vec + fastembed) and `mode=semantic|hybrid` beside the default `keyword` on `GET /posts`, MCP `list_posts` and the browser search bar.

- **Model download.** On first use the model downloads into `<vault>/.relay/models` (plus `.relay/hf-home`), on the vault's volume so it survives restarts.
- **Startup stays fast.** Existing posts are embedded by a background task after the server is up. Follow it in `/status` → `embeddings.backfill` or the logs (`Embedding backfill starting` · `N/M posts checked` every 15 s · `complete`). The cache is content-addressed, so an interrupted backfill resumes.
- **Memory.** The loaded model costs ~550 MB. It loads only when something actually needs embedding — a semantic/hybrid search, or a write with new or changed text. Unchanged writes and digest-type posts (`digest`, `news`, `briefing`, …) never load it. After `EMBEDDING_IDLE_UNLOAD_SECONDS` idle it unloads and the memory returns to the OS; the next embed pays a few seconds' reload. `/status` → `embeddings.backend_loaded` says whether it's resident now.
- **Transparent huge pages.** relay opts its own process out of THP at startup (visible as `THP_enabled: 0` in `/proc/<pid>/status`). On a host with THP set to `always`, huge pages are the likely reason the idle floor crept up by hundreds of MB over days. Nothing to configure; other processes on the host are unaffected.
- **Verify** with `/status` → `features.search.embeddings` (usable, not just configured) or the status panel's Semantic search row. `/status` → `embeddings` has the model, dimension, size and coverage.
- **Changing `EMBEDDING_MODEL`** re-embeds the vault automatically. A different dimension rebuilds the vector schema at the next startup; either way the cache is keyed on the model id, so the backfill redoes every post.
- **At runtime:** `PATCH /embeddings` `{"enabled": bool}` toggles the feature until restart (disabling frees the model at once); `POST /embeddings/backfill` re-runs the catch-up (`?force=true` wipes the cache first). Enabling resumes against the model the on-disk schema was built for — a dimension change still needs a restart.

---

## OIDC login for the browser UI and MCP

Required for remote use; any standard OIDC provider works. For self-hosting, [PocketID](https://github.com/pocket-id/pocket-id) is a small single-container provider (keep it ≥ 2.6.0); Authentik, Keycloak or Authelia work too.

1. **Register a confidential client** with redirect URI `<RELAY_BASE_URL>/auth/callback`.
2. **Add to `.env`:**
   ```bash
   OIDC_ISSUER=https://your-idp.example.com
   OIDC_CLIENT_ID=relay
   OIDC_CLIENT_SECRET=<secret>
   ```
3. **Restrict who can sign in** — `OIDC_ALLOWED_SUBS=abc123,def456` (preferred: immutable) or `OIDC_ALLOWED_EMAILS=alice@example.com`. Leaving both empty admits **any** user of your IdP.
4. **Restart.** The login card now offers "Login with OIDC".

---

## OAuth 2.1 for remote MCP clients

Lets Claude.ai, ChatGPT and other remote clients sign in with OAuth + Dynamic Client Registration instead of a pasted key. Needs OIDC first.

1. Add `<RELAY_BASE_URL>/mcp/oauth/callback` to your IdP's redirect URIs.
2. Set `MCP_OAUTH_ENABLED=true`.
3. Keep `OIDC_ALLOWED_SUBS` non-empty — an empty allowlist gives any IdP user full MCP access.
4. Add other clients to `MCP_ALLOWED_REDIRECT_HOSTS` if needed (e.g. `www.perplexity.ai`).
5. Restart, then in the client's connector dialog give only a name and `<RELAY_BASE_URL>/mcp`.

**Every connection shows a consent screen first** — the client's self-reported name and redirect target, Approve/Deny — so a client can't silently ride an existing IdP session. There is no "remember this app": each attempt asks again.

> Tokens are opaque, hashed at rest, audience-bound to `/mcp`, and single-use for auth codes and refresh tokens. A replayed, already-rotated refresh token is rejected but does not revoke tokens issued after it — an accepted trade-off (relay #313). Lifetimes follow what your IdP issues (fastmcp's `OIDCProxy` owns them); `mcp_server.py` documents the overrides if you ever need to decouple.

**Rate-limit `/register` and `/authorize` at your reverse proxy.** OAuth requires them to be unauthenticated, relay has no limiter of its own, and client registrations never expire: on the reference deployment ~280 registrations/s were accepted at ~5.6 KB each — a disk-fill risk for the volume holding your notes and `history.git`. Clients register about once, so a tight limit is invisible in normal use. The proxy is also the only layer that sees the real client IP. nginx:

```nginx
limit_req_zone $binary_remote_addr zone=relay_oauth:10m rate=10r/m;

location ~ ^/(register|authorize)$ {
    limit_req zone=relay_oauth burst=20 nodelay;
    proxy_pass http://relay;
}
```

> **nginx-proxy-manager:** don't paste that as one block into a host's *Advanced* field — `limit_req_zone` is only valid in `http {}`, and NPM silently drops the whole block (the host then stopped serving TLS at all). Split it: put the `limit_req_zone` line alone in `/data/nginx/custom/http_top.conf` (included once in `http {}`; reload nginx), and only the `location` block in the host's Advanced field.
