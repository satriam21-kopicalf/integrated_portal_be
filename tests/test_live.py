"""/api/live: today's totals vs yesterday at the same time and the latest sales."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.routes import live


class LiveDB:
    def __init__(self):
        self.queries = []

    def fetch(self, query, params=None):
        self.queries.append((query, params))
        if "GROUP BY 1" in query:  # today per hour
            return [{"hour": 9, "bills": 3, "subtotal": Decimal(150_000), "nett": Decimal(140_000),
                     "synced": datetime(2026, 10, 3, 3, 5, tzinfo=timezone.utc)},
                    {"hour": 8, "bills": 1, "subtotal": Decimal(50_000), "nett": Decimal(50_000),
                     "synced": datetime(2026, 10, 3, 2, 5, tzinfo=timezone.utc)}]
        if "::time <=" in query:  # yesterday until the same clock time
            return [{"bills": 2, "subtotal": Decimal(160_000)}]
        if "ORDER BY date_trunc('hour', h.synced_at)" in query:
            return [{"sales_num": "S-2", "bill_num": "B-2", "branch_code": "CCI01", "visit_purpose": "GoFood",
                     "payment_method": "GOFOOD_INT", "subtotal": Decimal(80_000), "total_amount": Decimal(70_000),
                     "sales_date_in": datetime(2026, 10, 3, 9, 58, 1, tzinfo=timezone.utc),
                     "synced_at": datetime(2026, 10, 3, 3, 5, tzinfo=timezone.utc)}]
        if "jsonb_agg" in query:
            return [{"sales_num": "S-2", "items": [{"name": f"Menu {i}", "qty": "1"} for i in range(6)]}]
        if "master_branches" in query:
            return [{"branch_code": "CCI01", "branch_name": "Kopi Calf Supratman Bandung"}]
        raise AssertionError(query)

    def fetchrow(self, query, params=None):
        rows = self.fetch(query, params)
        return rows[0] if rows else None


@pytest.fixture
def live_db(monkeypatch):
    fake = LiveDB()
    live._cache._data.clear()
    monkeypatch.setattr(live.db, "fetch", fake.fetch)
    monkeypatch.setattr(live.db, "fetchrow", fake.fetchrow)
    monkeypatch.setattr(live, "now_local", lambda: datetime(2026, 10, 3, 10, 0, 0))
    return fake


def test_live_today_and_latest(client, live_db):
    body = client.get("/api/live?limit=5").json()
    t = body["today"]
    assert t["date"] == "2026-10-03" and t["bills"] == 4 and t["subtotal"] == 200_000 and t["avgTicket"] == 50_000
    assert [h["hour"] for h in t["hours"]] == [8, 9]
    assert t["yesterdaySameTime"] == {"bills": 2, "subtotal": 160_000} and t["deltaPct"] == 25.0
    assert body["lastSyncedAt"].startswith("2026-10-03T03:05")
    tx = body["transactions"][0]
    assert tx["branchName"] == "Kopi Calf Supratman Bandung" and tx["orderTime"] == "2026-10-03T09:58:01"
    assert len(tx["items"]) == live.MAX_ITEMS and tx["moreItems"] == 2 and tx["itemQty"] == 6
    clock = next(p for q, p in live_db.queries if "::time <=" in q)
    assert clock["day"] == "2026-10-02" and clock["clock"] == "10:00:00"


def test_live_filters_use_header_columns(client, live_db):
    client.get("/api/live?branch=CCI01&channel=GoFood, Dine In")
    query, params = live_db.queries[0]
    assert "h.branch_code = %(branch)s" in query and "h.visit_purpose = ANY(%(channels)s)" in query
    assert params["channels"] == ["Dine In", "GoFood"]
    assert live.TYPE_CONDITIONS["sales"] in query
