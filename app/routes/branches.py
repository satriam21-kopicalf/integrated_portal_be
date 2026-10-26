"""Branches endpoint: ESB branch master (current names) with recent sales counts, per company (brand)."""
from datetime import timedelta

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import database as db
from app.config import get_settings
from app.database import SCHEMA, TABLE_TRANSACTIONS
from app.esb_report import TYPE_CONDITIONS
from app.scope import COMPANY_NAMES, MAIN_COMPANY, allowed_branches, branch_scope
from app.utils import TTLCache, today

router = APIRouter(prefix="/api/branches", tags=["branches"])

_cache = TTLCache()


def _all_branches() -> list[dict]:
    """Every branch with sales in the last N days (and the Kopi Calf outlets without sales), all companies."""
    cached = _cache.get("branches")
    if cached is not None:
        return cached
    settings = get_settings()
    since = (today() - timedelta(days=settings.default_days)).isoformat()
    rows = db.fetch(
        f"""
        WITH c AS (
            SELECT h.branch_code, COUNT(*)::int AS n FROM {TABLE_TRANSACTIONS} h
            WHERE h.sales_date >= %s AND {TYPE_CONDITIONS['sales']}
            GROUP BY h.branch_code
        )
        SELECT b.branch_code, b.branch_name, COALESCE(b.company_code, %s) AS company_code, a.brand, COALESCE(c.n, 0) AS count
        FROM {SCHEMA}.master_branches b
        LEFT JOIN c ON c.branch_code = b.branch_code
        LEFT JOIN {SCHEMA}.master_branch_attributes a ON a.branch_code = b.branch_code
        WHERE COALESCE(b.branch_code, '') <> '' AND COALESCE(b.is_deleted, false) = false
          AND (c.n > 0 OR b.branch_name ILIKE 'Kopi Calf%%')
        ORDER BY COALESCE(c.n, 0) DESC, b.branch_name
        """,
        (since, MAIN_COMPANY),
    )
    branches = [{"branch_code": r["branch_code"], "branch_name": r["branch_name"], "company_code": r["company_code"],
                 "brand": r["brand"], "count": r["count"]} for r in rows]
    _cache.set("branches", branches, settings.branches_cache_ttl)
    return branches


@router.get("")
def list_branches():
    """[{branch_code, branch_name, company_code, brand, count}] of the company of the request (`?company=`, default
    the Kopi Calf outlets; "all" = every company) - count = ESB sales in the last N days.

    Branches with sales come first (by count), then the rest of the master by name.
    """
    return JSONResponse(_only(_all_branches(), allowed_branches()))


@router.get("/companies")
def list_companies():
    """The companies (brands) the user sees branches of: [{code, name, branches}], the main company first. One entry =
    no brand switch needed."""
    mine = branch_scope.get()   # the user's branches, whatever company the request names
    counts: dict[str, int] = {}
    brands: dict[str, str] = {}
    for b in _all_branches():
        if mine is not None and b["branch_code"] not in mine:
            continue
        counts[b["company_code"]] = counts.get(b["company_code"], 0) + 1
        if b["brand"]:
            brands.setdefault(b["company_code"], b["brand"])
    codes = sorted(counts, key=lambda c: (c != MAIN_COMPANY, c))
    return [{"code": c, "name": COMPANY_NAMES.get(c) or brands.get(c) or c, "branches": counts[c]} for c in codes]


def _only(branches: list[dict], allowed) -> list[dict]:
    """Role "user" / a company: only the branches assigned to the account, of that company."""
    return branches if allowed is None else [b for b in branches if b["branch_code"] in allowed]
