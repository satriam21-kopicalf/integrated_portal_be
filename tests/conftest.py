"""Shared fixtures: the database layer is replaced by an in-memory fake."""
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import database as db
from app import exports
from app.config import get_settings
from app.main import app
from app.routes import branches as branches_module
from app.routes import transactions as tx_module

HEADERS = [
    {"sales_num": "S-002", "bill_num": "B-2", "sales_date": datetime(2026, 9, 30), "branch_name": "Calf A",
     "total_amount": Decimal("50000.00"), "sales_date_in": datetime(2026, 9, 30, 10, 0, 0), "status": "Finished"},
    {"sales_num": "S-001", "bill_num": "B-1", "sales_date": datetime(2026, 9, 30), "branch_name": "Calf B",
     "total_amount": Decimal("25000.00"), "sales_date_in": None, "status": "Void"},
    {"sales_num": "S-000", "bill_num": "B-0", "sales_date": datetime(2026, 9, 29), "branch_name": "Calf A",
     "total_amount": Decimal("10000.00"), "sales_date_in": None, "status": "Finished"},
]
ITEMS = [
    {"sales_num": "S-002", "line_number": 1, "menu_name": "Latte", "menu_category": "Coffee",
     "quantity": Decimal("2"), "unit_price": Decimal("25000"), "subtotal": Decimal("50000"),
     "discount_amount": Decimal("0"), "total": Decimal("50000"), "order_time": None},
    {"sales_num": "S-000", "line_number": 1, "menu_name": "Tea", "menu_category": "Non Coffee",
     "quantity": Decimal("1"), "unit_price": Decimal("10000"), "subtotal": Decimal("10000"),
     "discount_amount": Decimal("0"), "total": Decimal("10000"), "order_time": None},
    {"sales_num": "S-000", "line_number": 2, "menu_name": "Cookie", "menu_category": "Food",
     "quantity": Decimal("1"), "unit_price": Decimal("0"), "subtotal": Decimal("0"),
     "discount_amount": Decimal("0"), "total": Decimal("0"), "order_time": None},
]


class FakeDB:
    def __init__(self):
        self.queries: list[tuple[str, object]] = []
        self.fail = False

    def fetch(self, query, params=None):
        self.queries.append((query, params))
        if self.fail:
            raise RuntimeError("database is down")
        if "COUNT(*)" in query:
            return [{"branch_name": "Calf A", "count": 10}, {"branch_name": "Calf B", "count": 3}]
        if "transactions_pos_sales_items" in query:
            wanted = params[0]
            wanted = wanted if isinstance(wanted, list) else [wanted]
            return [i for i in ITEMS if i["sales_num"] in wanted]
        if "WHERE sales_num = %s" in query:
            return [h for h in HEADERS if h["sales_num"] == params[0]]
        if "sales_date < %s" in query:  # export: one day at a time
            day = date.fromisoformat(params[0])
            rows = [h for h in HEADERS if h["sales_date"].date() == day]
            if len(params) > 2:
                rows = [h for h in rows if h["branch_name"] == params[2]]
            return rows
        limit = params[-1]
        return [h for h in HEADERS if h["sales_date"].date() == date(2026, 9, 30)][:limit]

    def fetchrow(self, query, params=None):
        rows = self.fetch(query, params)
        return rows[0] if rows else None


class SyncThread:
    """Runs export jobs inline so tests can assert on the finished file."""

    def __init__(self, target, args=(), **_):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


@pytest.fixture
def fake_db(monkeypatch, tmp_path):
    fake = FakeDB()
    monkeypatch.setattr(db, "fetch", fake.fetch)
    monkeypatch.setattr(db, "fetchrow", fake.fetchrow)
    monkeypatch.setattr(db, "open_pool", lambda: None)
    monkeypatch.setattr(db, "close_pool", lambda: None)
    monkeypatch.setattr(exports.threading, "Thread", SyncThread)
    monkeypatch.setattr(get_settings(), "export_dir", str(tmp_path / "exports"))
    monkeypatch.setattr(get_settings(), "public_base_url", "")
    tx_module._cache = tx_module.TTLCache()
    branches_module._cache = branches_module.TTLCache()
    return fake


@pytest.fixture
def client(fake_db):
    with TestClient(app) as c:
        yield c
