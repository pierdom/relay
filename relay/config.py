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
