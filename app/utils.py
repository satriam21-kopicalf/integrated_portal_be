"""Small helpers shared by the routes."""
import time
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from threading import Lock
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.config import get_settings


def today() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


def resolve_date_range(date_from: Optional[str], date_to: Optional[str]) -> tuple[str, str]:
    """Default to the last N days when no dates are given (same as the old Next.js routes)."""
    if not date_from and not date_to:
        end = today()
        start = end - timedelta(days=get_settings().default_days)
        return start.isoformat(), end.isoformat()
    return date_from or "", date_to or ""


def to_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, dt_time)):
        return value.isoformat()
    if isinstance(value, (list, dict)):
        return value
    return str(value)  # UUID, timedelta, ...


def jsonable(row: dict) -> dict:
    return {k: to_json_value(v) for k, v in row.items()}


def escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class TTLCache:
    """Tiny thread-safe in-memory cache."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = Lock()

    def get(self, key: str) -> Any:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expiry, value = entry
            if time.monotonic() >= expiry:
                del self._data[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        with self._lock:
            now = time.monotonic()
            self._data = {k: v for k, v in self._data.items() if v[0] > now}
            self._data[key] = (now + ttl, value)
