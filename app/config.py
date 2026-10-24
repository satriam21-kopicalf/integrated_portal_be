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
    # browsers allowed to open the dashboard WebSocket (/ws); shell-style wildcards. The portal and Kopi Calf
    # Operations (fe-operations: the PIC / outlet app with the Dashboard, Sales and the forms)
    ws_allowed_origins: str = (
        "https://portal.kopicalf.co.id,https://integrated-portal*.vercel.app,"
        "http://localhost:3002,http://127.0.0.1:3002,"
        "https://operations.kopicalf.co.id,https://fe-operations*.vercel.app,"
        "http://localhost:3003,http://127.0.0.1:3003"
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

    # Stock opname (Cost Control menu): kopicalf-platform gateway (warehouse-backend /portal, D-51) and the shared
    # HMAC secret (same value as KC_PORTAL_GATEWAY_SECRET there); empty secret = stock opname disabled
    so_gateway_url: str = "https://api-wms.kopicalf.co.id/portal"
    so_gateway_secret: str = ""
    so_gateway_timeout: int = 180

    # Excel export jobs
    export_dir: str = os.path.join(tempfile.gettempdir(), "portal-exports")
    export_ttl_hours: int = 24
    export_max_concurrent: int = 2
    # e.g. https://portal-api.kopicalf.co.id -> absolute download links that bypass the
    # Vercel proxy. Empty = relative /api/exports/{id}/download (served via the proxy).
    public_base_url: str = ""
    # Damaged Equipment (app/damaged_equipment.py): photos / videos on the backend volume; share links valid for N days
    media_dir: str = os.path.join(tempfile.gettempdir(), "portal-media")
    share_link_days: int = 30
    # the portal's address, for the share links printed on documents
    portal_url: str = "https://portal.kopicalf.co.id"
    # Daily housekeeping (app/maintenance.py); 0 = keep forever
    activity_retention_days: int = 90
    gsheet_retention_days: int = 30
    # Export to Google Sheets through an Apps Script web app (app/gsheets.py); empty = option disabled
    google_apps_script_url: str = ""
    google_apps_script_key: str = ""
    google_share_role: str = "writer"  # access of the exporting user: writer | reader
    google_share_link: str = "view"  # anyone with the link: view | edit | none (no request for access)
    # Rating & Review → Google Maps (app/gmaps.py): Places API (New) key, restricted to that API and the VPS IP;
    # empty = the page shows that the sync is not set up
    google_places_api_key: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
