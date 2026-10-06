"""Activity log: who did what in the dashboard (integration_portal.activity_log, migration 009).

`record()` never raises: a failing log write is logged and the request carries on.
Only superadmins can read the log (app/routes/activity.py). Entries older than
ACTIVITY_RETENTION_DAYS (default 90) are deleted daily by app/maintenance.py.
"""
import json
import logging
from datetime import date, timedelta
from typing import Any, Optional

from fastapi import Request

from app import database as db

logger = logging.getLogger("activity")

T = "integration_portal.activity_log"
CATEGORIES = ("auth", "page", "filter", "transaction", "export", "user", "profile", "access")
MAX_DETAILS = 8000  # characters of JSON per entry


def client_ip(request: Optional[Request]) -> Optional[str]:
    """The browser's address: the dashboard proxy (Vercel) forwards it in X-Forwarded-For."""
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() or request.headers.get("x-real-ip", "").strip()
    return (ip or (request.client.host if request.client else None) or None)


def _details(details: Optional[dict]) -> str:
    text = json.dumps(details or {}, default=str, ensure_ascii=False)
    if len(text) > MAX_DETAILS:
        text = json.dumps({"truncated": True, "preview": text[:MAX_DETAILS]}, ensure_ascii=False)
    return text


def record(action: str, *, user: Optional[dict] = None, request: Optional[Request] = None,
           summary: Optional[str] = None, details: Optional[dict] = None, status: str = "ok",
           page: Optional[str] = None, username: Optional[str] = None) -> None:
    category = action.split(".", 1)[0]
    if category not in CATEGORIES:
        category = "access"
    try:
        with db.transaction() as conn:
            conn.execute(
                f"INSERT INTO {T} (user_id, username, role, category, action, status, page, summary, details, ip, user_agent) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                (str(user["id"]) if user else None, (user or {}).get("username") or username,
                 (user or {}).get("role"), category, action, status, (page or "")[:200] or None,
                 (summary or "")[:500] or None, _details(details), client_ip(request),
                 (request.headers.get("user-agent", "")[:300] or None) if request else None))
    except Exception:  # noqa: BLE001 - the log must never break the request
        logger.exception("activity log write failed (%s)", action)


def _where(date_from: date, date_to: date, user_id: Optional[str], category: Optional[str],
           status: Optional[str], role: Optional[str], search: Optional[str]) -> tuple[str, list]:
    # whole days in WIB, whatever the session time zone
    where = ["a.created_at >= (%s::date)::timestamp AT TIME ZONE 'Asia/Jakarta'",
             "a.created_at < (%s::date + 1)::timestamp AT TIME ZONE 'Asia/Jakarta'"]
    params: list[Any] = [date_from.isoformat(), date_to.isoformat()]
    if user_id:
        where.append("a.user_id = %s::uuid")
        params.append(user_id)
    if category:
        where.append("a.category = ANY(%s)")
        params.append([c for c in category.split(",") if c in CATEGORIES] or ["-"])
    if status:
        where.append("a.status = %s")
        params.append(status)
    if role:
        where.append("a.role = %s")
        params.append(role)
    if search:
        like = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(a.summary ILIKE %s OR a.username ILIKE %s OR a.action ILIKE %s OR a.page ILIKE %s OR a.details::text ILIKE %s)")
        params += [like] * 5
    return " AND ".join(where), params


def list_entries(date_from: date, date_to: date, *, user_id=None, category=None, status=None, role=None,
                 search=None, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    where, params = _where(date_from, date_to, user_id, category, status, role, search)
    rows = db.fetch(
        f"SELECT a.id, a.created_at, a.user_id, a.username, a.role, a.category, a.action, a.status, a.page, "
        f"a.summary, a.details, a.ip, a.user_agent, u.full_name, u.avatar_updated_at "
        f"FROM {T} a LEFT JOIN integration_portal.user_account u ON u.id = a.user_id "
        f"WHERE {where} ORDER BY a.created_at DESC, a.id DESC LIMIT %s OFFSET %s", (*params, limit, offset))
    total = db.fetchrow(f"SELECT count(*)::int AS n FROM {T} a WHERE {where}", params)
    return rows, (total or {}).get("n", 0)


def summarize(date_from: date, date_to: date, **filters) -> dict:
    where, params = _where(date_from, date_to, filters.get("user_id"), None, None, filters.get("role"), None)
    by_category = db.fetch(f"SELECT a.category, count(*)::int AS n FROM {T} a WHERE {where} GROUP BY 1", params)
    totals = db.fetchrow(
        f"SELECT count(*)::int AS total, count(DISTINCT a.user_id)::int AS users, "
        f"count(*) FILTER (WHERE a.action = 'export.create')::int AS exports, "
        f"count(*) FILTER (WHERE a.action = 'export.download')::int AS downloads, "
        f"count(*) FILTER (WHERE a.action = 'auth.login_failed')::int AS failed_logins, "
        f"count(*) FILTER (WHERE a.status = 'denied')::int AS denied "
        f"FROM {T} a WHERE {where}", params) or {}
    top_users = db.fetch(
        f"SELECT a.user_id, max(a.username) AS username, max(a.role) AS role, count(*)::int AS n, "
        f"count(*) FILTER (WHERE a.category = 'export')::int AS exports, max(a.created_at) AS last_at "
        f"FROM {T} a WHERE {where} AND a.user_id IS NOT NULL GROUP BY a.user_id ORDER BY n DESC LIMIT 10", params)
    return {"totals": totals, "byCategory": {r["category"]: r["n"] for r in by_category}, "topUsers": top_users}


def default_range() -> tuple[date, date]:
    from app.utils import today
    end = today()
    return end - timedelta(days=6), end
