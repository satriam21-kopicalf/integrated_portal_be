"""Transactions endpoints.

Rows are built from transactions_pos_sales.raw_data with the same rules as the
ESB "Sales Recapitulation Detail Report" (app/esb_report.py), so the dashboard,
the export and ESB show identical figures. Excel exports live in
app/routes/exports.py.

Common filters:
  type    sales (default, = ESB report) | void | other_cost | all
  branch  branch_code (names change over time, codes do not)
"""
import time
from typing import Any, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app import database as db
from app.config import get_settings
from app.database import HEADER_COLUMNS, ITEM_COLUMNS, TABLE_ITEMS, TABLE_TRANSACTIONS
from app.esb_report import REPORT_HEADERS, TYPE_CASE_SQL, TYPE_CONDITIONS, load_masters, report_rows
from app.utils import TTLCache, escape_like, jsonable, resolve_date_range, to_json_value

router = APIRouter(prefix="/api/transactions", tags=["transactions"])
summary_router = APIRouter(prefix="/api/summary", tags=["summary"])

_cache = TTLCache()
CURSOR_SEP = "|||"
COL = {name: i for i, name in enumerate(REPORT_HEADERS)}


def resolve_type(value: Optional[str]) -> str:
    return value if value in TYPE_CONDITIONS else "sales"


def header_filters(date_from: str, date_to: str, branch: Optional[str], search: Optional[str],
                   tx_type: str) -> tuple[list[str], list[Any]]:
    """WHERE parts for transactions_pos_sales aliased as h."""
    where: list[str] = [TYPE_CONDITIONS[tx_type]]
    params: list[Any] = []
    if date_from:
        where.append("h.sales_date >= %s")
        params.append(date_from)
    if date_to:
        # sales_date is midnight of the sales day; include the whole end day
        where.append("h.sales_date < %s::date + 1")
        params.append(date_to)
    if branch:
        where.append("h.branch_code = %s")
        params.append(branch)
    if search:
        term = f"%{escape_like(search)}%"
        where.append("(h.sales_num ILIKE %s OR h.bill_num ILIKE %s OR h.branch_name ILIKE %s)")
        params.extend([term, term, term])
    return where, params


def _h_columns() -> str:
    return ", ".join(f"h.{c.strip()}" for c in HEADER_COLUMNS.split(","))


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def dashboard_rows(header: dict, masters: tuple) -> list[dict]:
    """Header columns + one entry per ESB report row (menu / package / extra)."""
    raw = header.get("raw_data") or {}
    base = jsonable({k: v for k, v in header.items() if k != "raw_data"})
    rows = report_rows(raw, *masters)
    if not rows:
        return [{**base, "line_number": None, "menu_name": None, "quantity": None,
                 "unit_price": None, "total_item": None}]
    out = []
    for i, r in enumerate(rows, start=1):
        out.append({
            **base,
            # ESB values (current branch name, local WIB times, ESB payment text)
            "branch_name": r[COL["Branch"]],
            "brand": r[COL["Brand"]],
            "city": r[COL["City"]],
            "area": r[COL["Area"]],
            "payment_method": r[COL["Payment Method"]],
            "customer_name": r[COL["Customer Name"]],
            "sales_date": _iso(r[COL["Sales Date"]]),
            "sales_date_in": _iso(r[COL["Sales Date In"]]),
            "sales_date_out": _iso(r[COL["Sales Date Out"]]),
            "line_number": i,
            "batch_order": r[COL["Batch Order"]],
            "menu_category": r[COL["Menu Category"]],
            "menu_category_detail": r[COL["Menu Category Detail"]],
            "menu_name": r[COL["Menu"]],
            "menu_code": r[COL["Menu Code"]],
            "menu_notes": r[COL["Menu Notes"]],
            "order_mode": r[COL["Order Mode"]],
            "quantity": r[COL["Qty"]],
            "unit_price": r[COL["Price"]],
            "subtotal_item": r[COL["Subtotal"]],
            "discount_item": r[COL["Discount"]],
            "service_charge_item": r[COL["Service Charge"]],
            "tax_item": r[COL["Tax"]],
            "vat_item": r[COL["VAT"]],
            "total_item": r[COL["Total"]],
            "nett_sales_item": round(r[COL["Nett Sales"]], 4),
            "bill_discount_item": round(r[COL["Bill Discount"]], 4),
            "total_after_bill_discount": round(r[COL["Total After Bill Discount"]], 4),
            "waiter": r[COL["Waiter"]],
            "order_time": _iso(r[COL["Order Time"]]),
        })
    return out


@router.get("")
def list_transactions(
    cursor: Optional[str] = None,
    limit: int = Query(100, ge=1),
    search: Optional[str] = None,
    dateFrom: Optional[str] = None,
    dateTo: Optional[str] = None,
    branch: Optional[str] = None,
    type: Optional[str] = None,  # noqa: A002 - public query parameter name
    cache: Optional[str] = None,
):
    start = time.perf_counter()
    limit = min(limit, 100)
    tx_type = resolve_type(type)
    date_from, date_to = resolve_date_range(dateFrom, dateTo)

    cache_key = f"transactions:{tx_type}:{date_from}:{date_to}:{limit}"
    cacheable = cache != "false" and not cursor and not search and not branch
    if cacheable:
        cached = _cache.get(cache_key)
        if cached is not None:
            return JSONResponse(cached, headers={"X-Cache": "HIT"})

    where, params = header_filters(date_from, date_to, branch, search, tx_type)
    if cursor and CURSOR_SEP in cursor:
        cursor_date, cursor_num = cursor.split(CURSOR_SEP, 1)
        where.append("(h.sales_date, h.sales_num) < (%s, %s)")
        params.extend([cursor_date, cursor_num])

    headers = db.fetch(
        f"SELECT {_h_columns()}, h.raw_data FROM {TABLE_TRANSACTIONS} h WHERE {' AND '.join(where)} "
        f"ORDER BY h.sales_date DESC, h.sales_num DESC LIMIT %s",
        (*params, limit + 1),
    )
    has_more = len(headers) > limit
    headers = headers[:limit]

    masters = load_masters()
    combined = [row for h in headers for row in dashboard_rows(h, masters)]

    total_revenue = sum(float(h.get("total_amount") or 0) for h in headers)
    next_cursor = None
    if has_more:
        last = headers[-1]
        next_cursor = f"{to_json_value(last['sales_date'])}{CURSOR_SEP}{last['sales_num']}"

    body = {
        "data": combined,
        "pagination": {"cursor": next_cursor, "hasMore": has_more, "limit": limit},
        "summary": {
            "totalRows": len(combined),
            "totalHeaders": len(headers),
            "totalItems": sum(1 for r in combined if r.get("menu_name")),
            "totalRevenue": round(total_revenue, 2),
            "totalTransactions": len(headers),
            "avgTransactionValue": round(total_revenue / len(headers), 2) if headers else 0,
        },
        "type": tx_type,
        "dateRange": {"from": date_from, "to": date_to},
    }
    if cacheable:
        _cache.set(cache_key, body, get_settings().transactions_cache_ttl)

    elapsed = int((time.perf_counter() - start) * 1000)
    return JSONResponse(body, headers={"X-Cache": "MISS", "X-Response-Time": f"{elapsed}ms"})


@router.get("/{sales_num:path}")
def get_transaction(sales_num: str):
    header = db.fetchrow(
        f"SELECT {_h_columns()}, h.raw_data FROM {TABLE_TRANSACTIONS} h WHERE h.sales_num = %s", (sales_num,))
    if not header:
        return JSONResponse({"error": "Transaction not found"}, status_code=404)
    items = db.fetch(f"SELECT {ITEM_COLUMNS} FROM {TABLE_ITEMS} WHERE sales_num = %s ORDER BY line_number", (sales_num,))
    rows = dashboard_rows(header, load_masters())
    body = {k: v for k, v in rows[0].items() if not k.endswith("_item") and k not in (
        "line_number", "batch_order", "menu_category", "menu_category_detail", "menu_name", "menu_code",
        "menu_notes", "order_mode", "quantity", "unit_price", "total_after_bill_discount", "order_time")}
    body["items"] = [jsonable(i) for i in items]
    body["report_rows"] = rows if rows[0].get("menu_name") else []
    return JSONResponse(body)


# ---------------------------------------------------------------- summary

def summarize(date_from: str, date_to: str, branch: Optional[str]) -> dict:
    """Gross figures split into ESB sales and the deductions ESB leaves out."""
    where, params = header_filters(date_from, date_to, branch, None, "all")
    rows = db.fetch(
        f"""SELECT to_char(h.sales_date AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day, {TYPE_CASE_SQL} AS kind,
                   CASE WHEN {TYPE_CONDITIONS['other_cost']} THEN COALESCE(NULLIF(h.payment_method, ''), '-') END AS method,
                   COUNT(*)::int AS n, COALESCE(SUM(h.subtotal), 0) AS subtotal,
                   COALESCE(SUM(h.nett_sales), 0) AS nett, COALESCE(SUM(h.total_amount), 0) AS total
            FROM {TABLE_TRANSACTIONS} h WHERE {' AND '.join(where)}
            GROUP BY 1, 2, 3 ORDER BY 1""",
        params,
    )

    def bucket() -> dict:
        return {"transactions": 0, "subtotal": 0.0, "nettSales": 0.0, "total": 0.0}

    def add(b: dict, r: dict) -> None:
        b["transactions"] += r["n"]
        b["subtotal"] += float(r["subtotal"])
        b["nettSales"] += float(r["nett"])
        b["total"] += float(r["total"])

    days: dict[str, dict] = {}
    totals = {k: bucket() for k in ("gross", "void", "other_cost", "open", "sales")}
    methods: dict[str, dict] = {}
    for r in rows:
        day = days.setdefault(r["day"], {"date": r["day"], **{k: bucket() for k in totals}})
        for target in (day, totals):
            add(target["gross"], r)
            add(target[r["kind"]], r)
        if r["kind"] == "other_cost":
            add(methods.setdefault(r["method"], bucket()), r)

    def rounded(b: dict) -> dict:
        return {k: round(v, 2) if isinstance(v, float) else v for k, v in b.items()}

    return {
        "dateRange": {"from": date_from, "to": date_to},
        "branch": branch,
        "totals": {k: rounded(v) for k, v in totals.items()},
        "otherCostByMethod": {k: rounded(v) for k, v in sorted(methods.items())},
        "days": [{"date": d["date"], **{k: rounded(d[k]) for k in totals}} for d in days.values()],
    }


@summary_router.get("")
def get_summary(dateFrom: Optional[str] = None, dateTo: Optional[str] = None, branch: Optional[str] = None):
    """Gross - Void/Cancelled - Other Cost (CUPPING, WASTE, ...) - open bills = Sales (ESB report)."""
    date_from, date_to = resolve_date_range(dateFrom, dateTo)
    key = f"summary:{date_from}:{date_to}:{branch}"
    cached = _cache.get(key)
    if cached is None:
        cached = summarize(date_from, date_to, branch)
        _cache.set(key, cached, get_settings().transactions_cache_ttl)
    return JSONResponse(cached)

