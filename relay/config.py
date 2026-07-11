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
    relay_palette: str = "default"
    relay_transparent: bool = False
    secure_cookies: bool = True
    attachment_max_mb: int = 25  # reject uploads larger than this (base64-decoded)

    # --- Web UI OIDC (PocketID). All optional; absent => OIDC login disabled,
    # the API-key paste + bearer paths keep working unchanged. ---
    oidc_issuer: str = ""  # PocketID base URL (OIDC discovery at /.well-known/openid-configuration)
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    # Signs the relay_session cookie. Falls back to api_key if unset, so sessions
    # still carry identity/expiry even without a dedicated secret.
    session_secret: str = ""
    session_max_age_hours: int = 24 * 30  # signed-cookie lifetime
    # Comma-separated allowlist of emails permitted to log in via OIDC. Empty =>
    # any user PocketID authenticates is allowed.
    oidc_allowed_emails: str = ""

    # --- Phase 2 scaffold: remote MCP OAuth (relay as AS brokering to PocketID).
    # Not yet wired to a broker; the metadata endpoint advertises the surface. ---
    mcp_oauth_enabled: bool = False
    mcp_required_scopes: str = ""  # comma-separated

    @property
    def attachment_max_bytes(self) -> int:
        return self.attachment_max_mb * 1024 * 1024

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret)

    @property
    def session_signing_key(self) -> str:
        """Key for signing the session cookie; falls back to the API key."""
        return self.session_secret or self.api_key

    @property
    def allowed_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.oidc_allowed_emails.split(",") if e.strip()}

    @property
    def mcp_scopes(self) -> list[str]:
        return [s.strip() for s in self.mcp_required_scopes.split(",") if s.strip()]

    @property
    def relay_dir(self) -> str:
        """Hidden control folder inside the vault (index DB + tag config)."""
        return str(Path(self.vault_path) / ".relay")

    @property
    def database_path(self) -> str:
        """Derived index DB path. The index is disposable — files are canonical."""
        return str(Path(self.relay_dir) / "index.db")

    @property
    def tags_config_path(self) -> str:
        return str(Path(self.relay_dir) / "tags.yml")


settings = Settings()
