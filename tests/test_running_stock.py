"""Running stock (app/running_stock.py): SO + moves − approved waste − system usage since the count."""
from datetime import date

from app import menu_costing as mc
from app import running_stock as rs
from app import stock_moves


def test_the_estimate_from_the_count_and_what_moved_since(monkeypatch):
    rs.clear()
    monkeypatch.setattr(rs, "latest_counts", lambda branches: {"A": {"date": "2026-11-01", "refs": ["SC-1"], "final": True, "statuses": ["APPROVED"],
                                                                      "items": {"RM-MILK": (1000.0, "GR")}, "esb": {}}})
    monkeypatch.setattr(stock_moves, "moves", lambda a, b, branches: {"A": {"received": {("RM-MILK", "GR"): 500.0}, "transferOut": {("RM-MILK", "GR"): -100.0}}})
    monkeypatch.setattr(rs, "_waste", lambda starts, d_to, cutoffs=None: {"A": {"RM-MILK": 50.0}})
    monkeypatch.setattr(rs, "system_usage", lambda starts, d_to, cutoffs=None: {"A": {"RM-MILK": {"code": "RM-MILK", "name": "Milk", "unit": "GR", "qty": 700.0},
                                                                         "RM-CUP": {"code": "RM-CUP", "name": "Cup", "unit": "PCS", "qty": 3.0}}})
    monkeypatch.setattr(mc, "item_units", lambda codes: {})
    out = rs.running(None)["A"]
    milk = out["items"]["RM-MILK"]
    assert milk["estimate"] == 1000 + 500 - 100 - 50 - 700 and milk["moves"] == {"received": 500.0, "transferOut": -100.0}
    assert out["items"]["RM-CUP"]["estimate"] == -3  # used but never counted: shows as missing stock
    assert out["so"]["final"] is True
    rs.clear()


def test_waste_entered_before_the_so_was_sent_is_inside_the_count(monkeypatch):
    """2026-11-08: the SO is the stock when it was sent; a waste of that day entered earlier is already in the count."""
    from datetime import date, datetime, timezone
    sent = datetime(2026, 11, 7, 17, 0, tzinfo=timezone.utc)
    rows = [{"branch_code": "A", "business_date": date(2026, 11, 7), "created_at": datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc),
             "scope": "waste", "item_code": "RM-MILK", "qty": 30, "unit": "GR", "exploded": None},
            {"branch_code": "A", "business_date": date(2026, 11, 7), "created_at": datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc),
             "scope": "waste", "item_code": "RM-MILK", "qty": 20, "unit": "GR", "exploded": None}]
    monkeypatch.setattr(rs.db, "fetch", lambda q, p=None: rows)
    monkeypatch.setattr(mc, "item_units", lambda codes: {"RM-MILK": ("GR", {})})
    assert rs._waste({"A": date(2026, 11, 7)}, date(2026, 11, 8), {"A": sent}) == {"A": {"RM-MILK": 20.0}}
    assert rs._waste({"A": date(2026, 11, 7)}, date(2026, 11, 8)) == {"A": {"RM-MILK": 50.0}}
