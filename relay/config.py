from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_key: str
    relay_base_url: str = "http://localhost:8000"
    default_ttl_hours: int = 0  # 0 = never expire
    cleanup_interval_minutes: int = 60
    database_path: str = "/data/relay.db"
    relay_palette: str = "default"


settings = Settings()
