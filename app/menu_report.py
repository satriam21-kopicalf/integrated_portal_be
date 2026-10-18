"""Sales Recapitulation per Menu: the detail report summed up so one month of every branch fits in one Excel sheet.

One row per sales date × branch × channel (visit purpose) × payment method × menu (packages keep their own rows,
"(PACKAGE)", so add-on sales stay visible). Built from the same rows as the Sales Recapitulation Detail Report
(app/esb_report.py), so every amount column adds up to the detail report exactly; only the bill number, times,
table, member and employee columns are left out (one month of every branch: ~450,000 rows instead of ~2.4 million).

"Bills" counts the bills holding that menu in the group: a bill with three menus counts in three rows, so the
column is for "how many bills ordered this menu", not a bill total (Daily Sales Recapitulation has that).
"""
from datetime import date
from typing import Any, Iterable

from app.esb_report import REPORT_HEADERS, iter_report_rows

MENU_HEADERS = [
    "Sales Date", "Day", "Branch", "City", "Channel", "Channel Group", "Payment Method", "Menu Category",
    "Menu Category Detail", "Menu", "Item Type", "Bills", "Qty", "Subtotal", "Discount", "Tax", "Total",
    "Bill Discount", "Total After Bill Discount", "Nett Sales",
]
MENU_COLUMN_WIDTHS = [12, 10, 34, 16, 13, 13, 22, 18, 22, 38, 10, 8, 8, 14, 12, 12, 14, 12, 16, 14]

# the dashboard's channel groups (app/routes/overview.py)
ONLINE = ("GrabFood", "ShopeeFood", "GoFood", "Esb Order")
OFFLINE = ("Dine In", "Takeaway")
DAYS = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")

_I = {h: i for i, h in enumerate(REPORT_HEADERS)}
_KEY = [_I[h] for h in ("Branch", "City", "Visit Purpose", "Payment Method", "Menu Category", "Menu Category Detail", "Menu")]
_SUM = [_I[h] for h in ("Qty", "Subtotal", "Discount", "Tax", "Total", "Bill Discount", "Total After Bill Discount",
                        "Nett Sales")]
_SALES_NUM = _I["Sales Number"]


def _group(channel: str) -> str:
    return "Online" if channel in ONLINE else "Offline" if channel in OFFLINE else "Other"


def menu_rows(day: date, headers: Iterable[dict]) -> list[list[Any]]:
    """The rows of one sales date (headers = that day's sales, as for the detail report)."""
    groups: dict[tuple, list] = {}
    for r in iter_report_rows(headers):
        key = tuple(r[i] for i in _KEY)
        g = groups.get(key)
        if g is None:
            g = groups[key] = [set(), [0.0] * len(_SUM)]
        g[0].add(r[_SALES_NUM])
        sums = g[1]
        for n, i in enumerate(_SUM):
            sums[n] += float(r[i] or 0)
    out = []
    day_name = DAYS[day.weekday()]
    for (branch, city, channel, payment, category, detail, menu), (bills, sums) in groups.items():
        package = menu.endswith(" (PACKAGE)") or menu.endswith(" (EXTRA)")
        qty = sums[0]
        out.append([day, day_name, branch, city, channel, _group(channel or ""), payment, category, detail, menu,
                    "Package" if package else "Menu", len(bills), int(qty) if qty == int(qty) else qty,
                    *(round(v, 2) for v in sums[1:])])
    out.sort(key=lambda r: (r[2] or "", r[4] or "", r[6] or "", r[7] or "", r[9] or ""))
    return out
