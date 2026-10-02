"""Application configuration loaded from environment variables / .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Supabase PostgreSQL (same credentials as the ESB sync engine on the VPS)
    db_host: str = "aws-0-ap-southeast-1.pooler.supabase.com"
    db_port: int = 5432
    db_name: str = "postgres"
    db_user: str = ""
    db_password: str = ""
    db_sslmode: str = "require"
    db_pool_min: int = 1
    db_pool_max: int = 5
    db_statement_timeout_ms: int = 120_000

    # Server
    port: int = 8002
    cors_origins: str = "*"  # comma separated list
    timezone: str = "Asia/Jakarta"

    # Business defaults (mirrors the old Next.js API routes)
    default_days: int = 65
    export_max_headers: int = 50_000
    transactions_cache_ttl: int = 60
    branches_cache_ttl: int = 300

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
