"""Shared helpers for reading transactions and shaping them into ESB report rows."""
from typing import Any

from app import database as db
from app.database import ITEM_COLUMNS, TABLE_ITEMS
from app.utils import to_json_value

ITEMS_CHUNK = 5000

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
EXCEL_COLUMN_WIDTHS = [
    20, 20, 15, 12, 15, 20, 12, 20, 20, 25,
    15, 15, 15, 15, 15, 20, 15, 15, 20, 20,
    15, 15, 15, 15, 15, 15, 15, 15, 15, 12,
    8, 8, 10, 20, 25, 25, 15, 20, 10, 12,
    12, 12, 12, 20,
]
EXCEL_ITEM_CELLS = 12


def fetch_items(sales_nums: list[str]) -> list[dict]:
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


def group_items(items: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item["sales_num"], []).append(item)
    return grouped


def _fmt_date(value: Any) -> str:
    return str(to_json_value(value))[:10] if value else ""


def _fmt_datetime(value: Any) -> str:
    return str(to_json_value(value))[:19].replace("T", " ") if value else ""


def _num(value: Any) -> float:
    return float(value or 0)


def header_cells(row: dict) -> list[Any]:
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


def item_cells(item: dict) -> list[Any]:
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


def report_rows(headers: list[dict], items_by_sales: dict[str, list[dict]]):
    """Yield one row per item (header columns repeated); headers without items get one row."""
    for header in headers:
        cells = header_cells(header)
        header_items = items_by_sales.get(header["sales_num"], [])
        if not header_items:
            yield cells + [""] * EXCEL_ITEM_CELLS, False
            continue
        for item in header_items:
            yield cells + item_cells(item), True
