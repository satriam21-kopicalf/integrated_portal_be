"""Transactions/branches API tests (database stubbed, see conftest.py)."""


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
    assert first["sales_date"] == "2026-09-30T00:00:00"
    assert first["menu_name"] == "Latte"
    assert first["total_item"] == 50000
    # header without items still produces one row
    assert body["data"][1]["sales_num"] == "S-001"
    assert body["data"][1]["menu_name"] is None


def test_transactions_cursor_pagination(client, fake_db):
    res = client.get("/api/transactions", params={"limit": 1, "dateFrom": "2026-09-01"})
    body = res.json()
    assert body["pagination"]["hasMore"] is True
    assert body["pagination"]["cursor"] == "2026-09-30T00:00:00|||S-002"

    client.get("/api/transactions", params={"limit": 1, "cursor": body["pagination"]["cursor"]})
    header_queries = [q for q in fake_db.queries if "(sales_date, sales_num) < (%s, %s)" in q[0]]
    assert len(header_queries) == 1
    params = header_queries[0][1]
    assert "2026-09-30T00:00:00" in params and "S-002" in params


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


def test_old_truncating_export_endpoint_removed(client):
    assert client.post("/api/transactions/export", json={}).status_code in (404, 405)
