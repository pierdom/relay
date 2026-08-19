# Setup

## Which setup do you need?

The right path depends on where and how you plan to connect to relay.

**Local use (single machine)**
You want to use relay on your own computer — via Claude Code CLI, Claude Desktop, or the browser/TUI on the same machine. The static API key is all you need; OIDC and OAuth are not required.

→ [Install locally](#installation--local) and connect via the [stdio MCP proxy](mcp.md#connect-via-stdio-proxy-legacy).

**Remote use (mobile, Claude.ai, shared access)**
You want to connect from Claude on your phone, Claude.ai in a browser, or share relay with others. This requires a server or VPS with a public HTTPS URL, an OIDC identity provider so remote clients can authenticate, and `MCP_OAUTH_ENABLED=true` so Claude.ai can connect via its OAuth flow.

→ [Deploy with Docker](#installation--docker), then set up [OIDC](#oidc-login-for-the-browser-ui-and-mcp) and [MCP OAuth](#oauth-21-for-remote-mcp-clients).

---

## Prerequisites

- **Python 3.13** + [uv](https://docs.astral.sh/uv/) — local path
- **Docker** + **Docker Compose** — container path

## Deployment constraint: single worker

**relay must run as a single process.** Two things break above one worker: the in-memory upload-slot registry (a `PUT` and its finalise must reach the same process) and the id allocator (which relies on an in-process lock rather than an immediate database transaction). Running multiple workers silently corrupts upload slots and can produce duplicate post ids.

relay refuses to start if `WEB_CONCURRENCY` is greater than 1. If you need to handle more load, put a reverse proxy (nginx, Caddy, Traefik) in front of a single relay process rather than scaling workers.

The `--workers` flag for uvicorn and gunicorn is not supported. The Docker Compose file in this repo uses a single worker by default.

---

## Installation — local

```bash
cp .env.example .env        # set API_KEY at minimum
uv run python -m uvicorn relay.main:app --reload
```

Service on `http://localhost:8000`. Interactive API docs at `/docs`.

## Installation — Docker

```bash
docker compose up -d

# Update to latest:
docker compose pull && docker compose up -d
```

`GET /health` (no auth) is probed every 30 s by the Docker healthcheck — `docker ps` shows `healthy` when ready. Set `RELAY_BASE_URL` to your public URL (e.g. `https://relay.example.com`) before starting.

---

## Configuration reference

Copy `.env.example` to `.env`. Only `API_KEY` is required; everything else has a working default.

```bash
echo "API_KEY=$(openssl rand -hex 32)" >> .env
```

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | **required** | Bearer token for all endpoints |
| `RELAY_VAULT_PATH` | `/data/vault` | Markdown vault directory; SQLite index lives in `<vault>/.relay/` |
| `RELAY_BASE_URL` | `http://localhost:8000` | Public URL of this relay instance (used by the stdio proxy and OAuth redirects) |
| `DEFAULT_TTL_HOURS` | `0` | Global post expiry window; `0` disables expiry (per-tag TTLs still apply) |
| `CLEANUP_INTERVAL_MINUTES` | `60` | How often expired posts are removed |
| `RELAY_WATCH_ENABLED` | `true` | Live-reindex + SSE on edits made outside relay (e.g. in Obsidian) |
| `RELAY_HISTORY_ENABLED` | `true` | Commit the vault to git after every write, so a clobbered or deleted post can be restored. No-ops with a warning if the `git` binary is missing — check `features.history.effective` in `/status` |
| `SECURE_COOKIES` | `true` | `Secure` flag on the session cookie; set `false` for plain HTTP |
| `ATTACHMENT_MAX_MB` | `25` | Max upload size; larger uploads → 413 (all transports) |
| `ATTACHMENT_UPLOAD_TTL_SECONDS` | `3600` | Presigned upload slot lifetime before it's purged |
| `ATTACHMENT_FETCH_TIMEOUT_SECONDS` | `20` | Timeout for a server-side `source_url` fetch |
| `OIDC_ISSUER` | `""` | OIDC provider base URL. Set with the client credentials below to enable Login with OIDC; blank = API-key-paste login |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | `""` | Confidential OIDC client credentials |
| `SESSION_SECRET` | `""` | Signs the session cookie; falls back to `API_KEY` if unset |
| `SESSION_MAX_AGE_HOURS` | `720` | Session-cookie lifetime (default 30 days) |
| `OIDC_ALLOWED_SUBS` | `""` | Comma-separated OIDC `sub` allowlist (immutable user IDs — preferred over email) |
| `OIDC_ALLOWED_EMAILS` | `""` | Comma-separated email allowlist; matches **verified** emails only. Both empty = any authenticated IdP user |
| `MCP_OAUTH_ENABLED` | `false` | Turn `/mcp` into an OAuth 2.1 AS+RS for remote clients via Dynamic Client Registration; the static `API_KEY` still works |
| `MCP_REQUIRED_SCOPES` | `relay` | Scope required on `/mcp` |
| `MCP_ALLOWED_REDIRECT_HOSTS` | `claude.ai,claude.com,chatgpt.com` | DCR redirect-URI host allowlist (exact match, https only; blank = any https). Add other clients as needed |
| `MCP_AUTH_CODE_TTL_SECONDS` | `60` | OAuth authorization-code lifetime |
| `MCP_ACCESS_TOKEN_TTL_SECONDS` | `3600` | OAuth access-token lifetime |
| `MCP_REFRESH_TOKEN_TTL_SECONDS` | `2592000` | OAuth refresh-token lifetime (30 days) |
| `RELAY_PALETTE` | `default` | TUI colour theme (`default`, `dracula`, `nord`, `gruvbox`, `solarized`, `solarized-light`, `molokai`, `candy`, `earthy`, `pastel`, `tango`, `tokyo-night`, `catppuccin-latte`, `catppuccin-frappe`, `catppuccin-macchiato`, `catppuccin-mocha`) |
| `RELAY_TRANSPARENT` | `0` | TUI: let the terminal background show through the canvas |

**Docker-only**, read by `docker-compose.yml` rather than by relay itself:

| Variable | Default | Description |
|----------|---------|-------------|
| `RELAY_VAULT_DIR` | `./vault` | Host path bind-mounted to `/data/vault`. Set an absolute path in production |
| `RELAY_UID` / `RELAY_GID` | `1000` | Run as your host user so files relay writes stay user-owned — matters for Syncthing/Obsidian/backups |

---

## OIDC login for the browser UI and MCP

Required for remote use. relay delegates authentication to any standard OIDC provider.

**Recommended for self-hosters: [PocketID](https://github.com/pocket-id/pocket-id)** — a minimal, single-binary OIDC provider designed for personal use. No database setup, no admin overhead, runs as a Docker container alongside relay. Other providers (Authentik, Keycloak, Authelia, …) work equally well if you already run one.

**1. Register a confidential client at your IdP.** Set the redirect URI to `<RELAY_BASE_URL>/auth/callback`. Note the client ID and secret.

**2. Add to `.env`:**

```bash
OIDC_ISSUER=https://your-idp.example.com
OIDC_CLIENT_ID=relay
OIDC_CLIENT_SECRET=<secret>
```

**3. Restrict who can log in** (strongly recommended):

```bash
# Prefer sub — immutable even if the user changes their email:
OIDC_ALLOWED_SUBS=abc123,def456

# Or by verified email:
OIDC_ALLOWED_EMAILS=alice@example.com,bob@example.com
```

Leaving both empty lets **any** user authenticated by your IdP log in.

**4. Restart relay.** The Login button on `/ui` now shows "Login with OIDC".

---

## OAuth 2.1 for remote MCP clients

Lets remote MCP clients (Claude.ai, ChatGPT, etc.) authenticate via OAuth + Dynamic Client Registration instead of a pasted bearer key. Requires OIDC to be configured first.

**1.** Add `<RELAY_BASE_URL>/mcp/oauth/callback` to your IdP's redirect-URI allowlist.

**2.** Set `MCP_OAUTH_ENABLED=true` in `.env`.

**3.** Keep `OIDC_ALLOWED_SUBS` non-empty — an empty allowlist gives any IdP user full MCP access.

**4.** Add non-default clients to `MCP_ALLOWED_REDIRECT_HOSTS` if needed (e.g. `www.perplexity.ai`).

**5. Restart relay.**

In the client's connector dialog, fill only the **name** and **URL** (`<RELAY_BASE_URL>/mcp`) — Dynamic Client Registration self-registers the client. The static `API_KEY` keeps working alongside OAuth.

> Tokens are opaque and hashed at rest, audience-bound to `/mcp`, single-use on auth codes and refresh tokens, and revoking one cascades to the whole client+user token family.
