"""Branches endpoint: ESB branch master (current names) with recent sales counts."""
from datetime import timedelta

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import database as db
from app.config import get_settings
from app.database import SCHEMA, TABLE_TRANSACTIONS
from app.esb_report import TYPE_CONDITIONS
from app.scope import allowed_branches
from app.utils import TTLCache, today

router = APIRouter(prefix="/api/branches", tags=["branches"])

_cache = TTLCache()


@router.get("")
def list_branches():
    """[{branch_code, branch_name, count}] - count = ESB sales in the last N days.

    Branches with sales come first (by count), then the rest of the master by name.
    """
    allowed = allowed_branches()
    cached = _cache.get("branches")
    if cached is not None:
        return JSONResponse(_only(cached, allowed))

    settings = get_settings()
    since = (today() - timedelta(days=settings.default_days)).isoformat()
    rows = db.fetch(
        f"""
        WITH c AS (
            SELECT h.branch_code, COUNT(*)::int AS n FROM {TABLE_TRANSACTIONS} h
            WHERE h.sales_date >= %s AND {TYPE_CONDITIONS['sales']}
            GROUP BY h.branch_code
        )
        SELECT b.branch_code, b.branch_name, COALESCE(c.n, 0) AS count
        FROM {SCHEMA}.master_branches b
        LEFT JOIN c ON c.branch_code = b.branch_code
        WHERE COALESCE(b.branch_code, '') <> '' AND COALESCE(b.is_deleted, false) = false
          AND (c.n > 0 OR b.branch_name ILIKE 'Kopi Calf%%')
        ORDER BY COALESCE(c.n, 0) DESC, b.branch_name
        """,
        (since,),
    )
    branches = [{"branch_code": r["branch_code"], "branch_name": r["branch_name"], "count": r["count"]} for r in rows]
    _cache.set("branches", branches, settings.branches_cache_ttl)
    return JSONResponse(_only(branches, allowed))


def _only(branches: list[dict], allowed) -> list[dict]:
    """Role "user": only the branches assigned to the account."""
    return branches if allowed is None else [b for b in branches if b["branch_code"] in allowed]
