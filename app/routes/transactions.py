"""Transactions endpoints.

The request/response shapes intentionally match the former Next.js API routes
(`src/app/api/transactions/*` in integrated_portal) so the frontend components
work unchanged.
"""
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import database as db
from app.config import get_settings
from app.database import HEADER_COLUMNS, ITEM_COLUMNS, TABLE_ITEMS, TABLE_TRANSACTIONS
from app.utils import TTLCache, escape_like, jsonable, resolve_date_range, to_json_value

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/transactions", tags=["transactions"])

_cache = TTLCache()
CURSOR_SEP = "|||"
ITEMS_CHUNK = 5000


def _build_header_filters(
    date_from: str, date_to: str, branch: Optional[str], search: Optional[str], alias: str = ""
) -> tuple[list[str], list[Any]]:
    p = f"{alias}." if alias else ""
    where: list[str] = []
    params: list[Any] = []
    if date_from:
        where.append(f"{p}sales_date >= %s")
        params.append(date_from)
    if date_to:
        where.append(f"{p}sales_date <= %s")
        params.append(date_to)
    if branch:
        where.append(f"{p}branch_name = %s")
        params.append(branch)
    if search:
        term = f"%{escape_like(search)}%"
        where.append(f"({p}sales_num ILIKE %s OR {p}bill_num ILIKE %s OR {p}branch_name ILIKE %s)")
        params.extend([term, term, term])
    return where, params


def _fetch_items(sales_nums: list[str]) -> list[dict]:
    items: list[dict] = []
    for i in range(0, len(sales_nums), ITEMS_CHUNK):
        chunk = sales_nums[i : i + ITEMS_CHUNK]
        items.extend(
            db.fetch(
                f"SELECT {ITEM_COLUMNS} FROM {TABLE_ITEMS} WHERE sales_num = ANY(%s) ORDER BY sales_num, line_number",
                (chunk,),
            )
        )
    return items


def _group_items(items: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item["sales_num"], []).append(item)
    return grouped


@router.get("")
def list_transactions(
    cursor: Optional[str] = None,
    limit: int = Query(100, ge=1),
    search: Optional[str] = None,
    dateFrom: Optional[str] = None,
    dateTo: Optional[str] = None,
    branch: Optional[str] = None,
    cache: Optional[str] = None,
):
    start = time.perf_counter()
    limit = min(limit, 100)
    use_cache = cache != "false"
    date_from, date_to = resolve_date_range(dateFrom, dateTo)

    cache_key = f"transactions:{date_from}:{date_to}:{limit}"
    cacheable = use_cache and not cursor and not search and not branch
    if cacheable:
        cached = _cache.get(cache_key)
        if cached is not None:
            return JSONResponse(cached, headers={"X-Cache": "HIT"})

    where, params = _build_header_filters(date_from, date_to, branch, search)
    if cursor and CURSOR_SEP in cursor:
        cursor_date, cursor_num = cursor.split(CURSOR_SEP, 1)
        where.append("(sales_date, sales_num) < (%s, %s)")
        params.extend([cursor_date, cursor_num])

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    headers = db.fetch(
        f"SELECT {HEADER_COLUMNS} FROM {TABLE_TRANSACTIONS} {where_sql} "
        f"ORDER BY sales_date DESC, sales_num DESC LIMIT %s",
        (*params, limit + 1),
    )
    has_more = len(headers) > limit
    headers = headers[:limit]

    if not headers:
        body = {
            "data": [],
            "pagination": {"cursor": None, "hasMore": False, "limit": limit},
            "summary": {
                "totalRows": 0, "totalHeaders": 0, "totalItems": 0,
                "totalRevenue": 0, "totalTransactions": 0, "avgTransactionValue": 0,
            },
            "dateRange": {"from": date_from, "to": date_to},
        }
        return JSONResponse(body)

    items = _fetch_items([h["sales_num"] for h in headers])
    items_by_sales = _group_items(items)

    combined: list[dict] = []
    for header in headers:
        h = jsonable(header)
        header_items = items_by_sales.get(header["sales_num"], [])
        if not header_items:
            combined.append({**h, "line_number": None, "menu_name": None, "quantity": None,
                             "unit_price": None, "total_item": None})
            continue
        for item in header_items:
            combined.append({
                **h,
                "line_number": item.get("line_number"),
                "menu_category": item.get("menu_category"),
                "menu_category_detail": item.get("menu_category_detail"),
                "menu_name": item.get("menu_name"),
                "menu_code": item.get("menu_code"),
                "menu_notes": item.get("menu_notes"),
                "quantity": to_json_value(item.get("quantity")),
                "unit_price": to_json_value(item.get("unit_price")),
                "subtotal_item": to_json_value(item.get("subtotal")),
                "discount_item": to_json_value(item.get("discount_amount")),
                "total_item": to_json_value(item.get("total")),
                "order_time": to_json_value(item.get("order_time")),
            })

    total_revenue = sum(float(h.get("total_amount") or 0) for h in headers)
    total_transactions = len({h["sales_num"] for h in headers})
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
            "totalItems": len(items),
            "totalRevenue": round(total_revenue, 2),
            "totalTransactions": total_transactions,
            "avgTransactionValue": round(total_revenue / total_transactions, 2) if total_transactions else 0,
        },
        "dateRange": {"from": date_from, "to": date_to},
    }
    if cacheable:
        _cache.set(cache_key, body, get_settings().transactions_cache_ttl)

    elapsed = int((time.perf_counter() - start) * 1000)
    return JSONResponse(body, headers={"X-Cache": "MISS", "X-Response-Time": f"{elapsed}ms"})


EXCEL_HEADERS = [
    "Sales Number", "Bill Number", "Sales Type", "Batch Order",
    "Table Section", "Table Name", "Sales Date", "Sales Date In", "Sales Date Out",
    "Branch", "Brand", "City", "Area", "Visit Purpose",
    "Member Code", "Member Name", "Visitor Type",
    "Employee Code", "Employee Name", "Customer Name",
    "Payment Method", "Subtotal", "Discount Total", "Service Charge",
    "Tax Total", "Grand Total", "Voucher Discount",
    "Cash Received", "Change Given", "Cashier", "Status", "Pax Total",
    "Line Number", "Menu Category", "Menu Category Detail", "Menu", "Menu Code", "Menu Notes",
    "Quantity", "Unit Price", "Subtotal Item", "Discount Item", "Total Item", "Order Time",
]
EXCEL_ITEM_CELLS = 12


def _fmt_date(value: Any) -> str:
    return str(to_json_value(value))[:10] if value else ""


def _fmt_datetime(value: Any) -> str:
    return str(to_json_value(value))[:19].replace("T", " ") if value else ""


def _num(value: Any) -> float:
    return float(value or 0)


def _header_cells(row: dict) -> list[Any]:
    return [
        row.get("sales_num") or "",
        row.get("bill_num") or "",
        row.get("sales_type") or "",
        row.get("batch_order") or 0,
        row.get("table_section") or "",
        row.get("table_name") or "",
        _fmt_date(row.get("sales_date")),
        _fmt_datetime(row.get("sales_date_in")),
        _fmt_datetime(row.get("sales_date_out")),
        row.get("branch_name") or "",
        row.get("brand") or "",
        row.get("city") or "",
        row.get("area") or "",
        row.get("visit_purpose") or "",
        row.get("regular_member_code") or "",
        row.get("regular_member_name") or "",
        row.get("loyalty_member_type") or "",
        row.get("employee_code") or "",
        row.get("employee_name") or "",
        row.get("customer_name") or "",
        row.get("payment_method") or "",
        _num(row.get("subtotal")),
        _num(row.get("discount_amount")),
        _num(row.get("service_charge")),
        _num(row.get("tax_amount")),
        _num(row.get("total_amount")),
        _num(row.get("bill_discount")),
        _num(row.get("cash_received")),
        _num(row.get("change_given")),
        row.get("cashier_id") or "",
        row.get("status") or "",
        row.get("pax_total") or 0,
    ]


def _item_cells(item: dict) -> list[Any]:
    def n(v: Any) -> Any:
        return to_json_value(v) if v is not None else ""

    return [
        n(item.get("line_number")),
        item.get("menu_category") or "",
        item.get("menu_category_detail") or "",
        item.get("menu_name") or "",
        item.get("menu_code") or "",
        item.get("menu_notes") or "",
        n(item.get("quantity")),
        n(item.get("unit_price")),
        n(item.get("subtotal")),
        n(item.get("discount_amount")),
        n(item.get("total")),
        _fmt_datetime(item.get("order_time")),
    ]


class ExportRequest(BaseModel):
    dateFrom: Optional[str] = None
    dateTo: Optional[str] = None
    branch: Optional[str] = None


@router.post("/export")
def export_transactions(req: ExportRequest):
    date_from, date_to = resolve_date_range(req.dateFrom, req.dateTo)
    where, params = _build_header_filters(date_from, date_to, req.branch, None)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    headers = db.fetch(
        f"SELECT {HEADER_COLUMNS} FROM {TABLE_TRANSACTIONS} {where_sql} "
        f"ORDER BY sales_date DESC, sales_num DESC LIMIT %s",
        (*params, get_settings().export_max_headers),
    )
    date_range = {"from": date_from, "to": date_to}
    if not headers:
        return JSONResponse({"data": [], "headers": EXCEL_HEADERS, "totalRows": 0,
                             "totalHeaders": 0, "totalItems": 0, "dateRange": date_range})

    items_by_sales = _group_items(_fetch_items([h["sales_num"] for h in headers]))

    data: list[list[Any]] = []
    items_count = 0
    for header in headers:
        cells = _header_cells(header)
        header_items = items_by_sales.get(header["sales_num"], [])
        if not header_items:
            data.append(cells + [""] * EXCEL_ITEM_CELLS)
            continue
        for item in header_items:
            items_count += 1
            data.append(cells + _item_cells(item))

    return JSONResponse({
        "data": data,
        "headers": EXCEL_HEADERS,
        "totalRows": len(data),
        "totalHeaders": len({h["sales_num"] for h in headers}),
        "totalItems": items_count,
        "dateRange": date_range,
    })


@router.get("/{sales_num:path}")
def get_transaction(sales_num: str):
    header = db.fetchrow(f"SELECT {HEADER_COLUMNS} FROM {TABLE_TRANSACTIONS} WHERE sales_num = %s", (sales_num,))
    if not header:
        return JSONResponse({"error": "Transaction not found"}, status_code=404)
    items = db.fetch(
        f"SELECT {ITEM_COLUMNS} FROM {TABLE_ITEMS} WHERE sales_num = %s ORDER BY line_number", (sales_num,)
    )
    return JSONResponse({**jsonable(header), "items": [jsonable(i) for i in items]})
