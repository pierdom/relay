from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    api_key: str
    relay_base_url: str = "http://localhost:8000"
    default_ttl_hours: int = 0  # 0 = never expire
    cleanup_interval_minutes: int = 60
    vault_path: str = Field(
        default="/data/vault",
        validation_alias=AliasChoices("RELAY_VAULT_PATH", "VAULT_PATH"),
    )
    watch_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("RELAY_WATCH_ENABLED", "WATCH_ENABLED"),
    )
    # Commit the vault to a git repo after every write, so a clobbered post is
    # recoverable (`git log`/`revert`). Degrades to a no-op with one warning if
    # the git binary is missing — history never gates a write.
    history_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("RELAY_HISTORY_ENABLED", "HISTORY_ENABLED"),
    )
    # Proof-of-concept semantic search (relay post #253). Off by default
    # everywhere — including production — until phase 4's eval numbers say
    # it's worth surfacing; only opt-in tests and the eval harness turn it on.
    embedding_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("RELAY_EMBEDDING_ENABLED", "EMBEDDING_ENABLED"),
    )
    # Part of the cache key (relay.vectors._hash's model_id) — changing this
    # alone forces a full re-embed on next sync, no migration needed. Post
    # #253's pick, `intfloat/multilingual-e5-small`, isn't in fastembed 0.8.0's
    # model registry (only the 1024-dim `-large` variant is) — verified in this
    # environment. This is the same 384-dim, MIT-licensed, multilingual family
    # the post wanted (EN/IT/ES/CA coverage) without a schema change; exactly
    # the "cheap to change your mind about" swap the post's design allows for.
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    relay_palette: str = "default"
    relay_transparent: bool = False
    secure_cookies: bool = True
    attachment_max_mb: int = 25  # reject uploads larger than this (base64-decoded)
    # How long a presigned upload slot (POST /attachments/uploads) stays open for
    # its out-of-band PUT before it's purged. Short-lived like an OAuth code.
    attachment_upload_ttl_seconds: int = 3600  # 1h
    # Timeout (seconds) for a server-side source_url fetch on add_attachment.
    attachment_fetch_timeout_seconds: int = 20

    # --- Web UI OIDC (PocketID). All optional; absent => OIDC login disabled,
    # the API-key paste + bearer paths keep working unchanged. ---
    oidc_issuer: str = ""  # PocketID base URL (OIDC discovery at /.well-known/openid-configuration)
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    # Signs the relay_session cookie. Falls back to api_key if unset, so sessions
    # still carry identity/expiry even without a dedicated secret.
    session_secret: str = ""
    session_max_age_hours: int = 24 * 30  # signed-cookie lifetime
    # Allowlists of who may log in via OIDC (comma-separated). Prefer subs: the
    # OIDC `sub` is the IdP's immutable user id, so it can't be spoofed by a user
    # editing their own profile. Email matching additionally requires a verified
    # email. Both empty => any user PocketID authenticates is allowed.
    oidc_allowed_subs: str = ""
    oidc_allowed_emails: str = ""

    # --- Phase 2: remote MCP OAuth (relay as its own Authorization Server,
    # brokering the human login upstream to PocketID). When enabled, /mcp is an
    # OAuth 2.1 Resource Server; the SDK mounts /authorize /token /register
    # /revoke + metadata, and relay mints audience-bound tokens for Claude.
    # The upstream login reuses the Phase-1 OIDC client (oidc_*), so no separate
    # MCP client credentials — just add the /mcp/oauth/callback redirect URI to
    # that PocketID client. Absent/false keeps the static-bearer path unchanged. ---
    mcp_oauth_enabled: bool = False
    mcp_required_scopes: str = "relay"  # comma-separated; single scope = full tool access
    # DCR redirect-URI host allowlist (comma-separated) for https redirects. Blocks
    # an attacker from registering a client that points an auth code at their own
    # https endpoint. Defaults to Claude's known connector callback hosts; empty =
    # allow any https (opt-out). http redirects stay loopback-only regardless.
    mcp_allowed_redirect_hosts: str = "claude.ai,claude.com,chatgpt.com"
    # Token lifetimes (seconds). Auth codes are single-use and short-lived;
    # access tokens rotate via long-lived refresh tokens.
    mcp_auth_code_ttl_seconds: int = 60
    mcp_access_token_ttl_seconds: int = 60 * 60  # 1h
    mcp_refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30  # 30d, rotating

    @property
    def attachment_max_bytes(self) -> int:
        return self.attachment_max_mb * 1024 * 1024

    @property
    def uploads_dir(self) -> str:
        """Staging dir for presigned uploads (bytes land here before finalize).

        Under ``.relay/`` so it rides the vault dir, but wiped at startup — an
        unclaimed slot is disposable, like an OAuth auth code."""
        return str(Path(self.relay_dir) / "uploads")

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret)

    @property
    def mcp_oauth_active(self) -> bool:
        """Whether remote MCP OAuth is *actually* running. It needs the upstream
        OIDC client too — the flag alone can't broker a login. Single source of
        truth so wiring, store init, and cleanup never disagree."""
        return self.mcp_oauth_enabled and self.oidc_enabled

    @property
    def session_signing_key(self) -> str:
        """Key for signing the session cookie; falls back to the API key."""
        return self.session_secret or self.api_key

    @property
    def allowed_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.oidc_allowed_emails.split(",") if e.strip()}

    @property
    def allowed_subs(self) -> set[str]:
        return {s.strip() for s in self.oidc_allowed_subs.split(",") if s.strip()}

    @property
    def mcp_scopes(self) -> list[str]:
        return [s.strip() for s in self.mcp_required_scopes.split(",") if s.strip()]

    @property
    def mcp_redirect_hosts(self) -> set[str]:
        """Allowlisted https redirect hosts for DCR (lowercased). Empty = any."""
        return {h.strip().lower() for h in self.mcp_allowed_redirect_hosts.split(",") if h.strip()}

    @property
    def relay_dir(self) -> str:
        """Hidden control folder inside the vault (index DB + tag config)."""
        return str(Path(self.vault_path) / ".relay")

    @property
    def history_dir(self) -> str:
        """Git dir for the vault history.

        Inside ``.relay/`` on purpose: that path is already excluded from
        Syncthing and hidden from Obsidian, so the object store never syncs
        between machines (a reliable way to corrupt a repo) and never shows up as
        vault content. The work-tree is the vault itself, passed explicitly, so
        no ``.git`` entry is created in the vault root. Durable, unlike the index
        beside it — the startup rebuild must never touch this.
        """
        return str(Path(self.relay_dir) / "history.git")

    @property
    def database_path(self) -> str:
        """Derived index DB path. The index is disposable — files are canonical."""
        return str(Path(self.relay_dir) / "index.db")

    @property
    def tags_config_path(self) -> str:
        return str(Path(self.relay_dir) / "tags.yml")

    @property
    def mcp_oauth_db_path(self) -> str:
        """Persistent OAuth store (DCR clients, codes, tokens).

        Separate from the disposable ``index.db`` — the startup index rebuild must
        never touch it. Lives in ``.relay/`` so it rides the vault backup.
        """
        return str(Path(self.relay_dir) / "oauth.db")

    @property
    def mcp_resource_url(self) -> str:
        """RFC 8707 resource identifier for the MCP endpoint (token audience)."""
        return f"{self.relay_base_url.rstrip('/')}/mcp"


settings = Settings()
