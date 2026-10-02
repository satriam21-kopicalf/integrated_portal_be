"""Branches endpoint (same response shape as the former Next.js route)."""
from datetime import timedelta

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import database as db
from app.config import get_settings
from app.database import TABLE_TRANSACTIONS
from app.utils import TTLCache, today

router = APIRouter(prefix="/api/branches", tags=["branches"])

_cache = TTLCache()


@router.get("")
def list_branches():
    cached = _cache.get("branches")
    if cached is not None:
        return JSONResponse(cached)

    settings = get_settings()
    since = (today() - timedelta(days=settings.default_days)).isoformat()
    rows = db.fetch(
        f"""
        SELECT branch_name, COUNT(*)::int AS count
        FROM {TABLE_TRANSACTIONS}
        WHERE sales_date >= %s AND branch_name IS NOT NULL AND branch_name <> ''
        GROUP BY branch_name
        ORDER BY count DESC, branch_name ASC
        """,
        (since,),
    )
    branches = [{"branch_name": r["branch_name"], "count": r["count"]} for r in rows]
    _cache.set("branches", branches, settings.branches_cache_ttl)
    return JSONResponse(branches)
