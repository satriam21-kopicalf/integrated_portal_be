"""Small helpers shared by the routes."""
import time
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from threading import Lock, Thread
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from app.config import get_settings


# Data version the browser saw over the WebSocket (query parameter "v"). It is part
# of every response-cache key, so a fetch triggered by an update never gets a
# response cached before that update. Set per request by middleware in app/main.py.
data_version: ContextVar[str] = ContextVar("data_version", default="")


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


MAX_BRANCHES = 200


def parse_branches(value: Optional[str]) -> list[str]:
    """Branch filter "CCI04,CCI01" -> ["CCI01", "CCI04"] (trimmed, deduplicated, sorted); "" -> []."""
    codes = sorted({c.strip() for c in (value or "").split(",") if c.strip()})
    return codes[:MAX_BRANCHES]


def normalize_branch(value: Optional[str]) -> Optional[str]:
    """Canonical form of a branch filter (stable cache keys), None when empty."""
    return ",".join(parse_branches(value)) or None


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

    def clear(self) -> None:
        with self._lock:
            self._data = {}

    def set(self, key: str, value: Any, ttl: int) -> None:
        with self._lock:
            now = time.monotonic()
            self._data = {k: v for k, v in self._data.items() if v[0] > now}
            self._data[key] = (now + ttl, value)


class Fresh:
    """One value kept warm (stale-while-revalidate, 2026-11-09): computed once, then served from memory; after `ttl`
    seconds, or once marked stale, the next reader starts a refresh in the background and still gets the previous value
    at once. Only the very first reader of a worker waits for the computation."""

    def __init__(self, compute: Callable[[], Any], ttl: float) -> None:
        self._compute, self._ttl = compute, ttl
        self._value: Any = None
        self._at = 0.0          # monotonic time of the value; 0 = none yet
        self._stale = False
        self._lock = Lock()     # one computation at a time
        self._busy = False

    def _refresh(self) -> Any:
        with self._lock:
            stale = self._stale
            self._stale = False
            try:
                value = self._compute()
            except Exception:
                self._stale = self._stale or stale
                raise
            finally:
                self._busy = False
            self._value, self._at = value, time.monotonic()
            return value

    def _background(self) -> None:
        try:
            self._refresh()
        except Exception:  # noqa: BLE001 - the previous value stays served; the next reader tries again
            pass

    def get(self) -> Any:
        if not self._at:
            with self._lock:
                if self._at:  # computed while this reader waited
                    return self._value
            return self._refresh()
        if (self._stale or time.monotonic() - self._at >= self._ttl) and not self._busy:
            self._busy = True
            Thread(target=self._background, daemon=True).start()
        return self._value

    def stale(self) -> None:
        """Something changed: the next reader starts a refresh (and gets the previous value meanwhile)."""
        self._stale = True

    def reset(self) -> None:
        """Drop the value: the next reader computes it again (tests)."""
        with self._lock:
            self._value, self._at, self._stale = None, 0.0, False

    def warm(self) -> None:
        """Compute it in the background now (the start of the worker)."""
        if not self._at and not self._busy:
            self._busy = True
            Thread(target=self._background, daemon=True).start()
