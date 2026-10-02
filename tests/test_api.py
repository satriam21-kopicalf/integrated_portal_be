"""API tests with the database layer stubbed out (no DB connection needed)."""
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import database as db
from app.main import app
from app.routes import branches as branches_module
from app.routes import transactions as tx_module

HEADERS = [
    {"sales_num": "S-002", "bill_num": "B-2", "sales_date": date(2026, 9, 30), "branch_name": "Calf A",
     "total_amount": Decimal("50000.00"), "sales_date_in": datetime(2026, 9, 30, 10, 0, 0), "status": "Finished"},
    {"sales_num": "S-001", "bill_num": "B-1", "sales_date": date(2026, 9, 30), "branch_name": "Calf B",
     "total_amount": Decimal("25000.00"), "sales_date_in": None, "status": "Void"},
]
ITEMS = [
    {"sales_num": "S-002", "line_number": 1, "menu_name": "Latte", "menu_category": "Coffee",
     "quantity": Decimal("2"), "unit_price": Decimal("25000"), "subtotal": Decimal("50000"),
     "discount_amount": Decimal("0"), "total": Decimal("50000"), "order_time": None},
]


class FakeDB:
    def __init__(self):
        self.queries: list[tuple[str, object]] = []

    def fetch(self, query, params=None):
        self.queries.append((query, params))
        if "COUNT(*)" in query:
            return [{"branch_name": "Calf A", "count": 10}, {"branch_name": "Calf B", "count": 3}]
        if "transactions_pos_sales_items" in query:
            wanted = params[0]
            wanted = wanted if isinstance(wanted, list) else [wanted]
            return [i for i in ITEMS if i["sales_num"] in wanted]
        if "WHERE sales_num = %s" in query:
            return [h for h in HEADERS if h["sales_num"] == params[0]]
        limit = params[-1]
        return HEADERS[:limit]

    def fetchrow(self, query, params=None):
        rows = self.fetch(query, params)
        return rows[0] if rows else None


@pytest.fixture
def fake_db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(db, "fetch", fake.fetch)
    monkeypatch.setattr(db, "fetchrow", fake.fetchrow)
    monkeypatch.setattr(db, "open_pool", lambda: None)
    monkeypatch.setattr(db, "close_pool", lambda: None)
    tx_module._cache = tx_module.TTLCache()
    branches_module._cache = branches_module.TTLCache()
    return fake


@pytest.fixture
def client(fake_db):
    with TestClient(app) as c:
        yield c


def test_root(client):
    assert client.get("/").json()["status"] == "ok"


def test_transactions_shape_matches_frontend_contract(client):
    res = client.get("/api/transactions", params={"dateFrom": "2026-09-01", "dateTo": "2026-09-30"})
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"data", "pagination", "summary", "dateRange"}
    assert body["pagination"] == {"cursor": None, "hasMore": False, "limit": 100}
    assert body["dateRange"] == {"from": "2026-09-01", "to": "2026-09-30"}
    assert body["summary"]["totalHeaders"] == 2
    assert body["summary"]["totalItems"] == 1
    assert body["summary"]["totalRevenue"] == 75000
    first = body["data"][0]
    assert first["sales_num"] == "S-002"
    assert first["sales_date"] == "2026-09-30"
    assert first["menu_name"] == "Latte"
    assert first["total_item"] == 50000
    # header without items still produces one row
    assert body["data"][1]["sales_num"] == "S-001"
    assert body["data"][1]["menu_name"] is None


def test_transactions_cursor_pagination(client, fake_db):
    res = client.get("/api/transactions", params={"limit": 1, "dateFrom": "2026-09-01"})
    body = res.json()
    assert body["pagination"]["hasMore"] is True
    assert body["pagination"]["cursor"] == "2026-09-30|||S-002"

    client.get("/api/transactions", params={"limit": 1, "cursor": body["pagination"]["cursor"]})
    header_queries = [q for q in fake_db.queries if "(sales_date, sales_num) < (%s, %s)" in q[0]]
    assert len(header_queries) == 1
    params = header_queries[0][1]
    assert "2026-09-30" in params and "S-002" in params


def test_transactions_search_escapes_like(client, fake_db):
    client.get("/api/transactions", params={"search": "50%_off"})
    query, params = fake_db.queries[0]
    assert "ILIKE" in query
    assert "%50\\%\\_off%" in params


def test_default_date_range_applied(client):
    body = client.get("/api/transactions").json()
    assert body["dateRange"]["from"] and body["dateRange"]["to"]


def test_branches(client):
    res = client.get("/api/branches")
    assert res.json() == [{"branch_name": "Calf A", "count": 10}, {"branch_name": "Calf B", "count": 3}]


def test_transaction_detail_and_404(client):
    body = client.get("/api/transactions/S-002").json()
    assert body["sales_num"] == "S-002"
    assert body["items"][0]["menu_name"] == "Latte"
    res = client.get("/api/transactions/NOPE")
    assert res.status_code == 404
    assert res.json() == {"error": "Transaction not found"}


def test_transaction_detail_with_slash_in_id(client, fake_db):
    client.get("/api/transactions/ABC%2F123")
    assert fake_db.queries[0][1] == ("ABC/123",)


def test_export(client):
    res = client.post("/api/transactions/export", json={"dateFrom": "2026-09-01", "dateTo": "2026-09-30"})
    body = res.json()
    assert body["totalHeaders"] == 2
    assert body["totalItems"] == 1
    assert body["totalRows"] == 2
    assert len(body["headers"]) == 44
    assert all(len(row) == 44 for row in body["data"])
    assert body["data"][0][35] == "Latte"
    assert body["dateRange"] == {"from": "2026-09-01", "to": "2026-09-30"}
