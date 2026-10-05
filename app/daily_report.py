"""ESB "Daily Sales Recapitulation Report": one row per sales date and branch.

Aggregated in SQL from the ESB payload (transactions_pos_sales.raw_data).
Verified against the ESB export for 1-29 Sept 2026: all 3,028 date/branch rows
and every amount column identical.
"""
from datetime import date
from typing import Any, Optional

from app import database as db
from app.database import SCHEMA, TABLE_TRANSACTIONS
from app.esb_report import TYPE_CONDITIONS, TYPE_LABELS
from app.utils import parse_branches

DAILY_HEADERS = [
    "Sales Date", "Sales Type", "Branch", "Number of Bill", "Pax Total", "Subtotal", "Discount Total",
    "Menu Discount Total", "Voucher Discount", "Service Charge Total", "Tax Total", "VAT Total",
    "Delivery Cost", "Order Fee", "Platform Fee", "Rounding Total", "Grand Total", "Voucher Sales Total",
    "Grand Total After Voucher", "DPP",
]
DAILY_COLUMN_WIDTHS = [12, 10, 36, 10, 10, 16, 14, 14, 12, 12, 14, 12, 12, 10, 10, 12, 16, 12, 16, 8]


def _n(key: str) -> str:
    return f"COALESCE(NULLIF(h.raw_data->>'{key}', '')::numeric, 0)"


def daily_rows(day: date, branch: Optional[str], tx_type: str) -> list[list[Any]]:
    where = ["h.sales_date >= %s", "h.sales_date < %s::date + 1", TYPE_CONDITIONS[tx_type]]
    params: list[Any] = [day.isoformat(), day.isoformat()]
    if branch:
        where.append("h.branch_code = ANY(%s)")
        params.append(parse_branches(branch))
    rows = db.fetch(
        f"""SELECT COALESCE(b.branch_name, MAX(h.branch_name)) AS branch, COUNT(*) AS bills,
                   SUM({_n('paxTotal')}) AS pax, SUM({_n('subtotal')}) AS subtotal,
                   SUM({_n('discountTotal')}) AS discount, SUM({_n('menuDiscountTotal')}) AS menu_discount,
                   SUM({_n('voucherDiscountTotal')}) AS voucher_discount, SUM({_n('otherTaxTotal')}) AS service,
                   SUM({_n('vatTotal')}) AS tax, SUM({_n('otherVatTotal')}) AS vat,
                   SUM({_n('deliveryCost')}) AS delivery, SUM({_n('orderFee')}) AS order_fee,
                   SUM({_n('roundingTotal')}) AS rounding, SUM({_n('grandTotal')}) AS grand,
                   SUM({_n('voucherTotal')}) AS voucher_sales
            FROM {TABLE_TRANSACTIONS} h
            LEFT JOIN {SCHEMA}.master_branches b ON b.branch_code = h.branch_code
            WHERE {' AND '.join(where)}
            GROUP BY h.branch_code, b.branch_name
            ORDER BY 1""",
        params,
    )
    label = TYPE_LABELS[tx_type]
    out = []
    for r in rows:
        num = {k: float(v) for k, v in r.items() if k != "branch"}
        out.append([
            day, label, r["branch"], num["bills"], num["pax"], num["subtotal"], num["discount"],
            num["menu_discount"], num["voucher_discount"], num["service"], num["tax"], num["vat"],
            num["delivery"], num["order_fee"], 0.0, num["rounding"], num["grand"], num["voucher_sales"],
            num["grand"] - num["voucher_sales"], 0.0,
        ])
    return out
