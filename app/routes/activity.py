"""Activity log (see app/activity.py).

    GET  /api/activity            superadmin: entries, newest first
         ?dateFrom&dateTo (WIB days, default last 7) &user=<id> &category=export,auth &status=ok|failed|denied
         &role=user|superadmin &search &limit (<=200) &offset
    GET  /api/activity/summary    superadmin: totals, per category, most active users (same filters)
    DELETE /api/activity          superadmin: reset the log now {"confirm": "RESET", "before": "YYYY-MM-DD" | null}
                                  (null = everything); the reset itself is logged as the first new entry
    POST /api/activity/events     any signed-in user: what the dashboard reports itself
         {"action": "page.view" | "filter.change", "page": "/sales", "details": {...}}

Everything else (sign-in, exports, transaction details, user changes, refused access) is
recorded by the backend itself, so it cannot be skipped from the browser.
"""
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app import activity
from app.config import get_settings
from app.routes.auth import current_user, require_superadmin
from app.utils import to_json_value

router = APIRouter(prefix="/api/activity", tags=["activity"])

CLIENT_ACTIONS = {"page.view", "filter.change"}


class ResetRequest(BaseModel):
    confirm: str = Field(max_length=10)
    before: Optional[str] = None  # WIB day: entries before it are removed; None = all


class ClientEvent(BaseModel):
    action: str = Field(max_length=40)
    page: Optional[str] = Field(default=None, max_length=200)
    summary: Optional[str] = Field(default=None, max_length=300)
    details: Optional[dict[str, Any]] = None


def _range(date_from: Optional[str], date_to: Optional[str]) -> tuple[date, date]:
    start, end = activity.default_range()
    try:
        end = date.fromisoformat(date_to) if date_to else end
        start = date.fromisoformat(date_from) if date_from else min(start, end)
    except ValueError as exc:
        raise ValueError("Format tanggal harus YYYY-MM-DD") from exc
    if start > end:
        raise ValueError("Tanggal mulai harus sebelum tanggal akhir")
    return start, end


def _entry(row: dict) -> dict:
    avatar = row.get("avatar_updated_at")
    return {
        "id": row["id"], "at": to_json_value(row["created_at"]),
        "user": {"id": str(row["user_id"]) if row["user_id"] else None, "username": row["username"],
                 "fullName": row.get("full_name"), "role": row["role"],
                 "avatarUrl": f"/api/avatars/{row['user_id']}?v={int(avatar.timestamp())}" if avatar else None},
        "category": row["category"], "action": row["action"], "status": row["status"], "page": row["page"],
        "summary": row["summary"], "details": row["details"] or {}, "ip": row["ip"], "userAgent": row["user_agent"],
    }


@router.get("", dependencies=[Depends(require_superadmin)])
def list_activity(dateFrom: Optional[str] = None, dateTo: Optional[str] = None, user: Optional[str] = None,
                  category: Optional[str] = None, status: Optional[str] = None, role: Optional[str] = None,
                  search: Optional[str] = Query(default=None, max_length=100),
                  limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    try:
        start, end = _range(dateFrom, dateTo)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    rows, total = activity.list_entries(start, end, user_id=user or None, category=category or None,
                                        status=status or None, role=role or None, search=(search or "").strip() or None,
                                        limit=limit, offset=offset)
    return JSONResponse({"data": [_entry(r) for r in rows], "total": total,
                         "dateFrom": start.isoformat(), "dateTo": end.isoformat()})


@router.get("/summary", dependencies=[Depends(require_superadmin)])
def activity_summary(dateFrom: Optional[str] = None, dateTo: Optional[str] = None, user: Optional[str] = None,
                     role: Optional[str] = None):
    try:
        start, end = _range(dateFrom, dateTo)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    s = activity.summarize(start, end, user_id=user or None, role=role or None)
    t = s["totals"]
    return JSONResponse({
        "dateFrom": start.isoformat(), "dateTo": end.isoformat(),
        "totals": {"total": t.get("total", 0), "users": t.get("users", 0), "exports": t.get("exports", 0),
                   "downloads": t.get("downloads", 0), "failedLogins": t.get("failed_logins", 0), "denied": t.get("denied", 0)},
        "byCategory": s["byCategory"],
        "topUsers": [{"id": str(r["user_id"]), "username": r["username"], "role": r["role"], "count": r["n"],
                      "exports": r["exports"], "lastAt": to_json_value(r["last_at"])} for r in s["topUsers"]],
        "byDay": [{"day": r["day"].isoformat(), "count": r["n"], "issues": r["issues"], "exports": r["exports"],
                   "users": r["users"]} for r in s["byDay"]],
        "byHour": sorted(({"hour": r["hour"], "count": r["n"]} for r in s["byHour"]), key=lambda h: h["hour"]),
        "byStatus": s["byStatus"],
        "topPages": [{"page": r["page"], "count": r["n"], "users": r["users"]} for r in s["topPages"]],
        "storage": {k: to_json_value(v) for k, v in activity.storage().items()},
        "retentionDays": get_settings().activity_retention_days,
    })


@router.delete("")
def reset_activity(body: ResetRequest, request: Request, user: dict = Depends(require_superadmin)):
    """Superadmin: remove the log now instead of waiting for the daily clean-up."""
    if body.confirm != "RESET":
        return JSONResponse({"error": 'Ketik "RESET" untuk mengonfirmasi'}, status_code=422)
    try:
        before = date.fromisoformat(body.before) if body.before else None
    except ValueError:
        return JSONResponse({"error": "Format tanggal harus YYYY-MM-DD"}, status_code=422)
    removed = activity.reset(before)
    activity.record("system.logs_reset", user=user, request=request, page="/activity",
                    summary=(f"Reset activity log: {removed:,} entri dihapus"
                             + (f" (sebelum {before:%d-%m-%Y})" if before else " (semua)")),
                    details={"removed": removed, "before": before.isoformat() if before else None})
    return JSONResponse({"removed": removed, "before": before.isoformat() if before else None})


@router.post("/events", status_code=204)
def client_event(event: ClientEvent, request: Request, user: dict = Depends(current_user)):
    if event.action not in CLIENT_ACTIONS:
        return JSONResponse({"error": "Unknown action"}, status_code=422)
    activity.record(event.action, user=user, request=request, page=event.page, summary=event.summary,
                    details=event.details)
    return Response(status_code=204)
