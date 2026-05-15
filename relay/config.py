from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    api_key: str
    default_ttl_hours: int = 72
    cleanup_interval_minutes: int = 60
    database_path: str = "/data/relay.db"


settings = Settings()
