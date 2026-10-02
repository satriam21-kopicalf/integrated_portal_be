"""Application configuration loaded from environment variables / .env."""
import os
import tempfile
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
    transactions_cache_ttl: int = 60
    branches_cache_ttl: int = 300

    # Excel export jobs
    export_dir: str = os.path.join(tempfile.gettempdir(), "portal-exports")
    export_ttl_hours: int = 24
    export_max_concurrent: int = 2
    # e.g. https://portal-api.kopicalf.co.id -> absolute download links that bypass the
    # Vercel proxy. Empty = relative /api/exports/{id}/download (served via the proxy).
    public_base_url: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
