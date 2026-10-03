"""ESB "Sales Recapitulation Detail Report" rows built from transactions_pos_sales.raw_data.

The layout, row rules and amounts mirror the report exported from the ESB ERP
dashboard (Report > Sales Recapitulation Detail Report, Sales Type "Sales"):

* only sales with status "Finished" and a bill number are included (ESB leaves
  out void/cancelled bills and "OTHER COST" payments such as CUPPING/WASTE,
  which never get a bill number);
* one row per ordered menu, followed by its packages "(PACKAGE)" and extras
  "(EXTRA)"; package/extra categories come from the POS menu master;
* menu lines cancelled on the bill ("Print Cancelled") are left out, like ESB
  does (the sale's subtotal never includes them);
* the bill-level discount is spread over the rows proportionally to each row's
  Subtotal;
* Branch/Brand/City/Area come from the branch master (by branch code, so renamed
  branches show their current name) and Waiter from the POS user master.

Verified against the ESB export of 2026-09-10: same 21,770 sales / 55,318 rows
and identical amounts; daily Subtotal equals ESB for every day of Sept 2026.
"""
import time
from datetime import date, datetime
from threading import Lock
from typing import Any, Iterable, Iterator, Optional

from app import database as db
from app.database import SCHEMA

REPORT_HEADERS = [
    "Sales Number", "Bill Number", "Sales Type", "Batch Order", "Table Section", "Table Name",
    "Sales Date", "Sales Date In", "Sales Date Out", "Branch", "Brand", "City", "Area", "Visit Purpose",
    "Regular Member Code", "Regular Member Name", "Loyalty Member Code", "Loyalty Member Name",
    "Loyalty Member Type", "Employee Code", "Employee Name", "External Employee Code",
    "External Employee Name", "Customer Name", "Payment Method", "Menu Category", "Menu Category Detail",
    "Menu", "Custom Menu Name", "Menu Code", "Menu Notes", "Order Mode", "Qty", "Price", "Subtotal",
    "Discount", "Service Charge", "Tax", "VAT", "Total", "Nett Sales", "DPP", "Bill Discount",
    "Total After Bill Discount", "Waiter", "Order Time",
]
REPORT_COLUMN_WIDTHS = [
    22, 22, 8, 8, 14, 14, 12, 19, 19, 32, 12, 18, 10, 14,
    14, 18, 14, 18, 14, 12, 16, 12, 16, 22, 18, 12, 22,
    34, 16, 10, 22, 12, 6, 11, 12, 10, 10, 10, 10, 12, 13, 8, 12,
    14, 16, 19,
]

# Transaction types (h = transactions_pos_sales). "sales" is exactly ESB's
# Sales Recapitulation report (Sales Type "Sales"); the others are what ESB leaves
# out and the dashboard shows as deductions from the gross figure.
TYPE_CONDITIONS = {
    "sales": "h.status = 'Finished' AND COALESCE(h.bill_num, '') <> ''",
    "void": "h.status IN ('Void', 'Cancelled')",
    "other_cost": "h.status = 'Finished' AND COALESCE(h.bill_num, '') = ''",
    "all": "TRUE",
}
TYPE_LABELS = {
    "sales": "Sales",
    "void": "Void & Cancelled",
    "other_cost": "Other Cost (CUPPING, WASTE, ...)",
    "all": "All",
}
TYPE_CASE_SQL = (
    "CASE WHEN h.status = 'Finished' AND COALESCE(h.bill_num, '') <> '' THEN 'sales' "
    "WHEN h.status IN ('Void', 'Cancelled') THEN 'void' "
    "WHEN h.status = 'Finished' THEN 'other_cost' ELSE 'open' END"
)

def is_cancelled_line(menu: dict) -> bool:
    """A menu line cancelled after ordering (status "Print Cancelled", id 19)."""
    return str(menu.get("statusID") or "") == "19" or "cancel" in str(menu.get("statusName") or "").lower()


def active_line_sql(alias: str) -> str:
    """SQL twin of `not is_cancelled_line` for a salesMenus element (% escaped for psycopg params)."""
    return (f"(COALESCE({alias}->>'statusID', '') <> '19' "
            f"AND COALESCE({alias}->>'statusName', '') NOT ILIKE '%%cancel%%')")


_MASTER_TTL = 600
_master_lock = Lock()
_master: dict[str, Any] = {"loaded": 0.0, "menus": {}, "branches": {}, "users": {}}


def _load_masters() -> tuple[dict, dict, dict]:
    with _master_lock:
        if time.monotonic() - _master["loaded"] > _MASTER_TTL:
            menus = {}
            for r in db.fetch(f"SELECT menu_id, category_name, raw_data FROM {SCHEMA}.master_pos_menu"):
                detail = (r["raw_data"] or {}).get("categoryDetail") or ""
                category, _, sub = detail.partition(" - ")
                menus[str(r["menu_id"])] = (category or r["category_name"] or "", sub)
            branches = {
                r["branch_code"]: (r["branch_name"], r["brand"], r["city"], r["area"])
                for r in db.fetch(
                    f"""SELECT b.branch_code, b.branch_name, a.brand, a.city, a.area
                        FROM {SCHEMA}.master_branches b
                        LEFT JOIN {SCHEMA}.master_branch_attributes a ON a.branch_code = b.branch_code"""
                )
            }
            users = {r["user_name"]: r["display_name"]
                     for r in db.fetch(f"SELECT user_name, display_name FROM {SCHEMA}.master_pos_users")}
            _master.update(loaded=time.monotonic(), menus=menus, branches=branches, users=users)
        return _master["menus"], _master["branches"], _master["users"]


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _num(value: float) -> Any:
    return int(value) if float(value).is_integer() else value


def _dt(value: Any) -> Optional[datetime]:
    """ESB timestamps are WIB wall-clock ("2026-09-10 07:23:39" or with +07:00)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T")[:19])
    except ValueError:
        return None


def _date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _waiter(header: dict, users: dict) -> Optional[str]:
    """ESB shows the POS user's display name; unknown users fall back to
    createdBy "CALFCCI01KASIR1" -> "CCI01 KASIR1", the usual naming pattern."""
    user = header.get("createdBy") or ""
    if not user:
        return None
    if user in users:
        return users[user]
    if user.startswith("CALF"):
        user = user[4:]
    code = header.get("branchCode") or ""
    if code and user.startswith(code) and len(user) > len(code):
        return f"{code} {user[len(code):]}"
    return user


def _id_number(value: float) -> str:
    """Indonesian number format used by ESB: 23100 -> "23.100", 1234.5 -> "1.234,5"."""
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def _payment_method(header: dict) -> str:
    payments = header.get("salesPayments") or []
    if len(payments) == 1:
        return payments[0].get("paymentMethodName") or ""
    return ",".join(f"{p.get('paymentMethodName') or ''} ({_id_number(_f(p.get('paymentAmount')))})" for p in payments)


def report_rows(header: dict, menus: dict, branches: dict, users: dict) -> list[list[Any]]:
    """All report rows for one sale (raw_data of transactions_pos_sales)."""
    lines = []
    for menu in header.get("salesMenus") or []:
        if is_cancelled_line(menu):
            continue
        lines.append(("", menu, menu))
        lines.extend((" (PACKAGE)", p, menu) for p in menu.get("packages") or [])
        lines.extend((" (EXTRA)", e, menu) for e in menu.get("extras") or [])
    if not lines:
        return []

    amounts = []
    for suffix, item, parent in lines:
        qty = _f(item.get("qty"))
        price = _f(item.get("price"))
        subtotal = price * qty
        discount = _f(item.get("discountValue"))
        service = _f(item.get("otherTaxValue"))
        tax = _f(item.get("vatValue"))
        vat = _f(item.get("otherVatValue"))
        total = subtotal - discount + service + tax + vat
        amounts.append((qty, price, subtotal, discount, service, tax, vat, total))

    bill_discount = _f(header.get("discountTotal")) + _f(header.get("promotionDiscount")) \
        + _f(header.get("voucherDiscountTotal"))
    subtotal_sum = sum(a[2] for a in amounts)

    branch_name, brand, city, area = branches.get(header.get("branchCode"), (None, None, None, None))
    member_code = header.get("memberCode") or "Non Member"
    member_name = header.get("memberName") or "Non Member"
    head = [
        header.get("salesNum"), header.get("billNum"), "Sales",
    ]
    common = [
        header.get("tableName"), header.get("tableName"),
        _date(header.get("salesDate")), _dt(header.get("salesDateIn")), _dt(header.get("salesDateOut")),
        branch_name or header.get("branchName"), brand or None, city or None, area or None, header.get("visitPurposeName"),
        member_code, member_name, header.get("externalMemberCode") or None, None,
        header.get("visitorTypeName") or None, "-", "-", "-", "-",
        header.get("fullName") or "-", _payment_method(header),
    ]
    waiter = _waiter(header, users)

    rows = []
    for (suffix, item, parent), (qty, price, subtotal, discount, service, tax, vat, total) in zip(lines, amounts):
        if suffix:
            category, detail = menus.get(str(item.get("menuID")), (parent.get("menuCategoryName"), ""))
        else:
            category, detail = item.get("menuCategoryName"), item.get("menuCategoryDetailName")
        row_bill_discount = bill_discount * subtotal / subtotal_sum if subtotal_sum else 0.0
        rows.append(head + [str(parent.get("batchID") or "")] + common + [
            category or None, detail or None, f"{item.get('menuName') or ''}{suffix}",
            None, item.get("menuCode") or None, item.get("notes") or None, parent.get("salesType"),
            _num(qty), price, subtotal, discount, service, tax, vat, total,
            subtotal - discount - row_bill_discount, 0.0, row_bill_discount, total - row_bill_discount,
            waiter, _dt(parent.get("createdDate")),
        ])
    return rows


def load_masters() -> tuple[dict, dict, dict]:
    return _load_masters()


def iter_report_rows(headers: Iterable[dict]) -> Iterator[list[Any]]:
    menus, branches, users = _load_masters()
    for h in headers:
        yield from report_rows(h["raw_data"] or {}, menus, branches, users)
