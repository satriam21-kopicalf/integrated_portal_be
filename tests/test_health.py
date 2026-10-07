"""Company health (app/health.py): bands, same-store growth and score on a small fake network."""
from datetime import date, timedelta

from app import health

TODAY = date(2026, 10, 7)


def totals(subtotal, nett, bills, online=0, all_bills=None, void=0, bev=0, both=0):
    return {"subtotal": subtotal, "nett": nett, "bills": bills, "online": online,
            "all_bills": all_bills or bills + void, "void_bills": void, "bev": bev, "both": both}


def test_health_indicators_and_score(monkeypatch):
    end = TODAY - timedelta(days=1)
    start = end - timedelta(days=27)
    windows = {
        start: totals(1_100_000, 1_000_000, 100, online=300_000, void=1, bev=90, both=27),    # current 28 days
        start - timedelta(days=364): totals(1_000_000, 950_000, 100),                        # last year
        start - timedelta(days=28): totals(1_000_000, 950_000, 95),                          # previous 28 days
    }
    monkeypatch.setattr(health, "window_totals", lambda s, e: windows[s])
    cur_outlets = [{"branch_code": "A", "days": 28, "subtotal": 600_000, "bills": 50},
                   {"branch_code": "B", "days": 28, "subtotal": 450_000, "bills": 45},
                   {"branch_code": "NEW", "days": 10, "subtotal": 50_000, "bills": 5}]
    ly_outlets = [{"branch_code": "A", "days": 28, "subtotal": 560_000, "bills": 55},
                  {"branch_code": "B", "days": 28, "subtotal": 440_000, "bills": 45}]
    monkeypatch.setattr(health, "outlet_rows", lambda s, e: cur_outlets if s == start else ly_outlets)
    monkeypatch.setattr(health, "menu_totals", lambda s, e: [{"menu_id": str(i), "subtotal": 100} for i in range(20)])
    monkeypatch.setattr(health, "hour_rows", lambda s, e: [{"hour": h, "bills": b} for h, b in ((8, 30), (9, 20), (12, 10), (15, 40))])
    monkeypatch.setattr(health, "day_rows", lambda s, e: [{"sales_date": start + timedelta(days=i), "subtotal": 100, "bills": 1}
                                                          for i in range(28)])
    monkeypatch.setattr(health, "month_rows", lambda: [
        {"month": date(2025, 7, 1), "subtotal": 5, "bills": 1, "outlets": 1, "outlet_days": 1, "days": 1},   # before the data start
        {"month": date(2025, 8, 1), "subtotal": 1000, "bills": 10, "outlets": 2, "outlet_days": 40, "days": 31}])

    h = health.build_health(TODAY, date(2025, 8, 1))
    by = {i["key"]: i for i in h["indicators"]}
    assert by["salesYoY"]["value"] == 10.0 and by["salesYoY"]["status"] == "good"
    # same stores A + B only (NEW opened later): 1,050,000 vs 1,000,000
    assert by["sssg"]["value"] == 5.0 and by["sssg"]["outlets"] == 2 and by["sssg"]["status"] == "good"
    assert by["sssTraffic"]["value"] == -5.0 and by["sssTraffic"]["status"] == "risk"
    assert by["discountRate"]["value"] == round(100_000 / 1_100_000 * 100, 2) and by["discountRate"]["status"] == "watch"
    assert by["voidRate"]["value"] == round(1 / 101 * 100, 2) and by["voidRate"]["status"] == "good"
    assert by["onlineShare"]["value"] == round(300_000 / 1_100_000 * 100, 2) and by["onlineShare"]["status"] == "good"
    assert by["foodAttach"]["value"] == 30.0 and by["foodAttach"]["status"] == "good"
    assert by["menuTop10"]["value"] == 50.0 and by["menuTop10"]["status"] == "info"
    assert by["peakHours"]["value"] == 90.0
    assert by["weakOutlets"]["outlets"] == ["NEW"]          # 5,000 a day vs a median of 16,071
    assert [m["month"] for m in h["months"]] == ["2025-08-01"] and h["months"][0]["perOutletDay"] == 25
    scored = [i for i in h["indicators"] if i["status"] != "info"]
    assert h["score"] == round(sum(health.SCORE[i["status"]] for i in scored) / len(scored))
    assert set(h["concerns"]) == {i["label"] for i in scored if i["status"] != "good"}


def test_health_without_a_year_of_history(monkeypatch):
    monkeypatch.setattr(health, "window_totals", lambda s, e: totals(100, 90, 10))
    monkeypatch.setattr(health, "outlet_rows", lambda s, e: [])
    for name in ("menu_totals", "hour_rows", "day_rows"):
        monkeypatch.setattr(health, name, lambda s, e: [])
    monkeypatch.setattr(health, "month_rows", lambda: [])
    h = health.build_health(date(2026, 3, 1), date(2025, 8, 1))
    assert h["window"]["lastYear"] is None
    assert not {"salesYoY", "sssg", "sssTraffic", "ticketYoY"} & {i["key"] for i in h["indicators"]}
