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
    # browsers allowed to open the dashboard WebSocket (/ws); shell-style wildcards
    ws_allowed_origins: str = (
        "https://portal.kopicalf.co.id,https://integrated-portal*.vercel.app,"
        "http://localhost:3002,http://127.0.0.1:3002"
    )
    timezone: str = "Asia/Jakarta"

    # Business defaults (mirrors the old Next.js API routes)
    default_days: int = 65
    transactions_cache_ttl: int = 60
    branches_cache_ttl: int = 300
    # Sign-in sessions (HttpOnly cookie); "remember me" keeps the longer one
    session_hours: int = 12
    session_remember_days: int = 30
    # Overview: first date of complete history (ESB roll-out reached all branches end of July 2025)
    overview_data_from: str = "2025-08-01"

    # Excel export jobs
    export_dir: str = os.path.join(tempfile.gettempdir(), "portal-exports")
    export_ttl_hours: int = 24
    export_max_concurrent: int = 2
    # e.g. https://portal-api.kopicalf.co.id -> absolute download links that bypass the
    # Vercel proxy. Empty = relative /api/exports/{id}/download (served via the proxy).
    public_base_url: str = ""
    # Export to Google Sheets (app/gsheets.py); empty = option disabled
    google_drive_folder_id: str = ""
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_refresh_token: str = ""
    google_service_account_json_b64: str = ""
    google_share_role: str = "writer"  # access of the exporting user: writer | reader

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
