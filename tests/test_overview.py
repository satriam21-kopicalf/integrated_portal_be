"""Overview endpoints: the aggregate queries are emulated in Python over a small
data set, so the builders (totals, comparison period, buckets, shares, rates)
are tested without a database."""
from collections import defaultdict
from datetime import date

import pytest

from app.routes import overview as ov


def day_row(day, branch, channel, tx_type, bills, subtotal, nett=None, method="Qris", lines=None, bev=None, food=0):
    return {"sales_date": date.fromisoformat(day), "branch_code": branch, "channel": channel, "payment_type": "CARD",
            "payment_method": method, "tx_type": tx_type, "bills": bills, "subtotal": subtotal,
            "nett": subtotal if nett is None else nett, "lines": lines or bills, "qty": (lines or bills) * 2,
            "bev": bills if bev is None else bev, "food": food, "both": food}


DAILY = [
    # current period 2026-09-01 .. 2026-09-02
    day_row("2026-09-01", "CCI01", "Dine In", "sales", 10, 1_000_000, 1_000_000, food=2),
    day_row("2026-09-01", "CCI01", "GoFood", "sales", 5, 1_000_000, 800_000, method="GOFOOD_INT"),
    day_row("2026-09-02", "TGP17", "Dine In", "sales", 5, 500_000, lines=10),
    day_row("2026-09-02", "CCI01", "Dine In", "void", 2, 100_000),
    day_row("2026-09-02", "TGP17", "Dine In", "other_cost", 1, 50_000, method="CUPPING"),
    # previous period 2026-08-30 .. 2026-08-31
    day_row("2026-08-30", "CCI01", "Dine In", "sales", 10, 2_000_000),
    day_row("2026-08-31", "OLD01", "Dine In", "sales", 1, 100_000),
]
HOURLY = [
    {"sales_date": date(2026, 9, 1), "branch_code": "CCI01", "channel": "Dine In", "hour": 9, "bills": 10, "subtotal": 1_000_000},
    {"sales_date": date(2026, 9, 1), "branch_code": "CCI01", "channel": "GoFood", "hour": 12, "bills": 5, "subtotal": 1_000_000},
    {"sales_date": date(2026, 9, 2), "branch_code": "TGP17", "channel": "Dine In", "hour": 9, "bills": 5, "subtotal": 500_000},
]
MENUS = [
    {"menu_id": "1", "kind": "menu", "menu_name": "Es Kopi Calf Premium", "category": "BEVERAGE",
     "category_detail": "ORIGINAL KOPI SUSU", "bills": 12, "qty": 20, "subtotal": 2_000_000, "discount": 0},
    {"menu_id": "2", "kind": "menu", "menu_name": "Dimsum", "category": "FOOD",
     "category_detail": "Dimsum Series", "bills": 2, "qty": 30, "subtotal": 500_000, "discount": 0},
    {"menu_id": "139", "kind": "package", "menu_name": "Normal Sugar", "category": "EXTRA",
     "category_detail": "LEVEL SUGAR", "bills": 10, "qty": 15, "subtotal": 0, "discount": 0},
    {"menu_id": "137", "kind": "package", "menu_name": "Less Sugar", "category": "EXTRA",
     "category_detail": "LEVEL SUGAR", "bills": 3, "qty": 5, "subtotal": 0, "discount": 0},
    {"menu_id": "137", "kind": "extra", "menu_name": "Less Sugar", "category": "EXTRA",
     "category_detail": "LEVEL SUGAR", "bills": 1, "qty": 1, "subtotal": 0, "discount": 0},
]
EXPRESSIONS = {
    "date_trunc('month', sales_date)::date": lambda r: r["sales_date"].replace(day=1),
    "date_trunc('week', sales_date)::date": lambda r: ov.bucket_of(r["sales_date"], "week"),
}


def group_key(g):
    expr, _, alias = g.partition(" AS ")
    name = alias or expr
    return name, EXPRESSIONS.get(expr, lambda r, c=expr: r[c])


def fake_sales_rows(f, start, end, group=(), select="", table=ov.DAILY, tx_type="sales"):
    rows = [r for r in DAILY if start <= r["sales_date"] <= end
            and (not f.branch or r["branch_code"] == f.branch)
            and (not f.channels or r["channel"] in f.channels)
            and (not tx_type or r["tx_type"] == tx_type)]
    keys = [group_key(g) for g in group]
    out: dict[tuple, dict] = {}
    days = defaultdict(set)
    for r in rows:
        k = tuple(fn(r) for _, fn in keys)
        o = out.setdefault(k, {**{name: v for (name, _), v in zip(keys, k)},
                               **{m: 0 for m in ("bills", "subtotal", "nett", "lines", "qty", "bev", "food", "both")}})
        for m in o:
            if m in r and m not in dict(keys):
                o[m] += r[m]
        days[k].add(r["sales_date"])
    for k, o in out.items():
        o["days"] = len(days[k])
    if not group:  # SQL aggregate without GROUP BY always returns one row (NULLs when empty)
        return list(out.values()) or [{"bills": None, "subtotal": None, "nett": None, "lines": None, "qty": None,
                                       "bev": None, "food": None, "both": None}]
    return list(out.values())


def fake_hourly_rows(f):
    out = {}
    for r in HOURLY:
        if f.start <= r["sales_date"] <= f.end and (not f.branch or r["branch_code"] == f.branch)                 and (not f.channels or r["channel"] in f.channels):
            o = out.setdefault((r["sales_date"].isoweekday(), r["hour"]),
                               {"dow": r["sales_date"].isoweekday(), "hour": r["hour"], "bills": 0, "subtotal": 0})
            o["bills"] += r["bills"]
            o["subtotal"] += r["subtotal"]
    return list(out.values())


@pytest.fixture(autouse=True)
def fake_aggregates(monkeypatch):
    ov._cache._data.clear()
    monkeypatch.setattr(ov, "sales_rows", fake_sales_rows)
    monkeypatch.setattr(ov, "day_rows", lambda f, start, end, group, tx_type=None:
                        fake_sales_rows(f, start, end, group, tx_type=tx_type))
    monkeypatch.setattr(ov, "menu_rows", lambda f: [dict(m) for m in MENUS])
    monkeypatch.setattr(ov, "hourly_rows", fake_hourly_rows)
    monkeypatch.setattr(ov, "branch_names", lambda: {"CCI01": "Kopi Calf Supratman", "TGP17": "Kopi Calf To Go Pamulang"})
    monkeypatch.setattr(ov, "freshness", lambda: {"dataFrom": "2026-08-01", "dataTo": "2026-09-02",
                                                  "lastSyncedAt": "2026-09-02T10:05:00+00:00",
                                                  "refreshedAt": "2026-09-02T10:25:00+00:00"})
    monkeypatch.setattr(ov, "data_start", lambda: date(2026, 8, 1))
    monkeypatch.setattr(ov, "today", lambda: date(2026, 9, 3))


Q = "dateFrom=2026-09-01&dateTo=2026-09-02"


def test_filters_default_and_validation(client):
    body = client.get("/api/overview/kpis").json()
    assert body["filters"]["from"] == "2026-08-04" and body["filters"]["to"] == "2026-09-02"  # 30 days to yesterday
    assert client.get("/api/overview/kpis?dateFrom=2026-09-05&dateTo=2026-09-01").status_code == 400
    assert client.get("/api/overview/kpis?dateFrom=bad").status_code == 400
    assert client.get("/api/overview/kpis?dateFrom=2020-01-01&dateTo=2026-09-01").status_code == 400


def test_kpis_vs_previous_period(client):
    body = client.get(f"/api/overview/kpis?{Q}").json()
    k = body["kpis"]
    assert body["filters"]["previous"] == {"from": "2026-08-30", "to": "2026-08-31", "complete": True}
    assert k["sales"] == {"value": 2_500_000, "previous": 2_100_000, "deltaPct": 19.05}
    assert k["nettSales"]["value"] == 2_300_000
    assert k["bills"]["value"] == 20 and k["avgTicket"]["value"] == 125_000
    assert [d["subtotal"] for d in body["daily"]] == [2_000_000, 500_000]
    assert body["freshness"]["lastSyncedAt"]


def test_previous_before_complete_history_is_empty(client):
    body = client.get("/api/overview/kpis?dateFrom=2026-08-01&dateTo=2026-09-02").json()
    assert body["filters"]["previous"]["complete"] is False
    assert body["kpis"]["sales"]["previous"] == 0 and body["kpis"]["sales"]["deltaPct"] is None


def test_channel_and_branch_filters(client):
    k = client.get(f"/api/overview/kpis?{Q}&channel=GoFood").json()["kpis"]
    assert k["sales"]["value"] == 1_000_000 and k["sales"]["previous"] == 0 and k["sales"]["deltaPct"] is None
    k = client.get(f"/api/overview/kpis?{Q}&branch=TGP17&channel=Dine In, GoFood").json()
    assert k["filters"]["channels"] == ["Dine In", "GoFood"] and k["kpis"]["bills"]["value"] == 5


def test_trend_buckets_and_previous_alignment(client):
    body = client.get(f"/api/overview/trend?{Q}").json()
    assert body["granularity"] == "day"
    first, second = body["series"]
    assert first["date"] == "2026-09-01" and first["previous"]["subtotal"] == 2_000_000  # 30 Aug shifted by 2 days
    assert second["previous"]["subtotal"] == 100_000
    assert first["discountPct"] == 10.0
    week = client.get(f"/api/overview/trend?{Q}&granularity=week").json()
    assert [s["date"] for s in week["series"]] == ["2026-08-31"] and week["series"][0]["subtotal"] == 2_500_000


def test_channels_shares_sum_to_total(client):
    body = client.get(f"/api/overview/channels?{Q}").json()
    ch = {c["channel"]: c for c in body["channels"]}
    assert sum(c["subtotal"] for c in body["channels"]) == 2_500_000
    assert ch["Dine In"]["share"] == 60.0 and ch["GoFood"]["share"] == 40.0
    assert ch["GoFood"]["discountPct"] == 20.0 and ch["Dine In"]["deltaPct"] == -28.57
    assert body["series"][0]["values"]["GoFood"] == {"subtotal": 1_000_000, "bills": 5}


def test_branch_leaderboard(client):
    body = client.get(f"/api/overview/branches?{Q}").json()
    rows = {b["branchCode"]: b for b in body["branches"]}
    assert list(rows) == ["CCI01", "TGP17", "OLD01"]  # by sales; OLD01 only sold in the previous period
    cci = rows["CCI01"]
    assert cci["branchName"] == "Kopi Calf Supratman" and cci["bills"] == 15 and cci["voidBills"] == 2
    assert cci["voidRate"] == round(2 / 17 * 100, 2) and cci["deltaPct"] == 0.0
    assert cci["spark"] == [2_000_000, 0] and body["buckets"] == ["2026-09-01", "2026-09-02"]
    assert rows["TGP17"]["isNew"] is True and rows["OLD01"]["subtotal"] == 0
    assert sum(b["subtotal"] for b in body["branches"]) == 2_500_000


def test_hourly_averages_per_weekday(client):
    body = client.get(f"/api/overview/hourly?{Q}").json()
    assert sum(c["bills"] for c in body["cells"]) == 20
    tue = next(c for c in body["cells"] if c["dow"] == 2 and c["hour"] == 9)  # 1 Sep 2026 is a Tuesday
    assert tue["avgBills"] == 10 and body["daysPerDow"]["2"] == 1
    assert body["peak"]["avgBills"] == 10


def test_menus_top_categories_addons(client):
    body = client.get(f"/api/overview/menus?{Q}&limit=1").json()
    assert [m["name"] for m in body["top"]] == ["Es Kopi Calf Premium"] and body["top"][0]["share"] == 80.0
    assert client.get(f"/api/overview/menus?{Q}&limit=1&sort=qty").json()["top"][0]["name"] == "Dimsum"
    assert [c["category"] for c in body["categories"]] == ["BEVERAGE", "FOOD"]
    sugar = body["addons"][0]
    assert sugar["group"] == "LEVEL SUGAR" and sugar["qty"] == 21
    assert [(o["name"], o["qty"]) for o in sugar["options"]] == [("Normal Sugar", 15), ("Less Sugar", 6)]


def test_deductions(client):
    body = client.get(f"/api/overview/deductions?{Q}").json()
    t = body["totals"]
    assert t["gross"] == {"bills": 23, "subtotal": 2_650_000}
    assert t["void"]["bills"] == 2 and t["otherCost"]["subtotal"] == 50_000
    assert body["voidRate"] == round(2 / 23 * 100, 2)
    assert body["otherCostByMethod"] == [{"method": "CUPPING", "bills": 1, "subtotal": 50_000}]
    assert {b["branchCode"] for b in body["branches"]} == {"CCI01", "TGP17"}
    assert body["threshold"] is None  # too few branches for a percentile


def test_payments_and_basket(client):
    pay = client.get(f"/api/overview/payments?{Q}").json()
    assert [m["method"] for m in pay["methods"]] == ["Qris", "GOFOOD_INT"] and pay["types"][0]["share"] == 100.0
    basket = client.get(f"/api/overview/basket?{Q}").json()
    assert basket["totals"]["linesPerBill"] == 1.25 and basket["totals"]["foodAttachPct"] == 10.0
    assert basket["previous"]["bills"] == 11 and len(basket["series"]) == 2


def test_monthly_growth_per_calendar_day(client):
    months = client.get("/api/overview/monthly?dateFrom=2026-08-01&dateTo=2026-09-02").json()["months"]
    aug, sep = months
    assert aug["month"] == "2026-08-01" and aug["days"] == 31 and not aug["partial"]
    assert sep["partial"] and sep["days"] == 2 and sep["subtotal"] == 2_500_000
    assert sep["momPct"] == delta(2_500_000 / 2, 2_100_000 / 31)


def delta(cur, prev):
    return round((cur - prev) / prev * 100, 2)


def test_branch_spark_weekly_buckets(client):
    body = client.get(f"/api/overview/branches?{Q}&granularity=week").json()
    assert body["buckets"] == ["2026-08-31"]
    assert {b["branchCode"]: b["spark"] for b in body["branches"]}["CCI01"] == [2_000_000]


def test_helpers():
    assert ov.full_months(date(2026, 8, 15), date(2026, 10, 31)) == (date(2026, 9, 1), date(2026, 11, 1))
    assert ov.full_months(date(2026, 9, 2), date(2026, 9, 20)) == (date.max, date.max)
    assert ov.auto_granularity(30) == "day" and ov.auto_granularity(120) == "week" and ov.auto_granularity(400) == "month"
    assert ov.bucket_of(date(2026, 9, 3), "week") == date(2026, 8, 31)
    assert ov.bucket_sql("day") == "sales_date" and "week" in ov.bucket_sql("week")
    assert ov._p90([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == pytest.approx(9.0)
