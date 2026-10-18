"""Sales Recapitulation per Menu (app/menu_report.py): the detail rows summed per date × branch × channel × payment ×
menu; amounts add up to the detail report, bills are counted once per row."""
from datetime import date

from app import menu_report
from app.esb_report import REPORT_HEADERS as H

I = {h: i for i, h in enumerate(H)}


def _row(sales, branch, channel, menu, qty, subtotal, payment="QRIS"):
    r = [None] * len(H)
    r[I["Sales Number"]], r[I["Branch"]], r[I["City"]], r[I["Visit Purpose"]] = sales, branch, "Jakarta", channel
    r[I["Payment Method"]], r[I["Menu Category"]], r[I["Menu Category Detail"]], r[I["Menu"]] = payment, "Coffee", "Hot", menu
    r[I["Qty"]], r[I["Subtotal"]], r[I["Discount"]], r[I["Tax"]] = qty, subtotal, 0.0, subtotal * 0.1
    r[I["Total"]], r[I["Bill Discount"]] = subtotal * 1.1, 0.0
    r[I["Total After Bill Discount"]], r[I["Nett Sales"]] = subtotal * 1.1, subtotal
    return r


def test_rows_summed_per_menu_and_bills_counted_once(monkeypatch):
    detail = [
        _row("S1", "Kopi Calf A", "Dine In", "Latte", 1, 25000.0),
        _row("S1", "Kopi Calf A", "Dine In", "Latte", 1, 25000.0),       # same bill, second line
        _row("S1", "Kopi Calf A", "Dine In", "Oat Milk (PACKAGE)", 1, 5000.0),
        _row("S2", "Kopi Calf A", "Dine In", "Latte", 2, 50000.0),
        _row("S3", "Kopi Calf A", "GoFood", "Latte", 1, 28000.0),
    ]
    monkeypatch.setattr(menu_report, "iter_report_rows", lambda headers: iter(detail))
    rows = menu_report.menu_rows(date(2026, 10, 17), [])
    assert len(rows[0]) == len(menu_report.MENU_HEADERS)
    col = {h: i for i, h in enumerate(menu_report.MENU_HEADERS)}
    by = {(r[col["Channel"]], r[col["Menu"]]): r for r in rows}
    latte = by[("Dine In", "Latte")]
    assert latte[col["Bills"]] == 2 and latte[col["Qty"]] == 4 and latte[col["Subtotal"]] == 100000.0
    assert latte[col["Day"]] == "Sabtu" and latte[col["Channel Group"]] == "Offline" and latte[col["Item Type"]] == "Menu"
    assert by[("Dine In", "Oat Milk (PACKAGE)")][col["Item Type"]] == "Package"
    assert by[("GoFood", "Latte")][col["Channel Group"]] == "Online"
    # every amount adds up to the detail report
    assert sum(r[col["Subtotal"]] for r in rows) == sum(d[I["Subtotal"]] for d in detail)
    assert abs(sum(r[col["Total After Bill Discount"]] for r in rows) - sum(d[I["Total After Bill Discount"]] for d in detail)) < 0.01
