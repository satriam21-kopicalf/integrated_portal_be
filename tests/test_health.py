"""Company health (app/health.py): all ESB data, no comparison; bands and score on a small fake network."""
from datetime import date, timedelta

from app import health

TODAY = date(2026, 10, 7)
START = date(2025, 8, 1)
END = TODAY - timedelta(days=1)


def test_health_over_all_data(monkeypatch):
    seen = []
    monkeypatch.setattr(health, "totals", lambda s, e: seen.append((s, e)) or {
        "subtotal": 1_100_000, "nett": 1_000_000, "bills": 100, "online": 300_000, "all_bills": 101, "void_bills": 1, "bev": 90, "both": 27})
    monkeypatch.setattr(health, "outlet_rows", lambda s, e: [
        {"branch_code": "A", "days": 400, "subtotal": 600_000, "bills": 50, "last_day": END},
        {"branch_code": "B", "days": 400, "subtotal": 450_000, "bills": 45, "last_day": END - timedelta(days=2)},
        {"branch_code": "SMALL", "days": 400, "subtotal": 50_000, "bills": 5, "last_day": END},
        {"branch_code": "CLOSED", "days": 30, "subtotal": 9_000, "bills": 1, "last_day": date(2026, 1, 31)},
    ])
    monkeypatch.setattr(health, "menu_totals", lambda s, e: [{"menu_id": str(i), "subtotal": 100} for i in range(20)])
    monkeypatch.setattr(health, "hour_rows", lambda s, e: [{"hour": h, "bills": b} for h, b in ((8, 30), (9, 20), (12, 10), (15, 40))])
    monkeypatch.setattr(health, "day_rows", lambda s, e: [{"sales_date": date(2026, 10, 3 + i), "subtotal": 100} for i in range(4)])  # Sat..Tue

    h = health.build_health(TODAY, START)
    assert seen == [(START, END)]                                   # the whole history, nothing else
    assert h["period"] == {"from": "2025-08-01", "to": "2026-10-06", "days": 432}
    by = {i["key"]: i for i in h["indicators"]}
    assert not {"salesYoY", "sssg", "sssTraffic", "ticketYoY", "sales28"} & set(by)   # no comparisons
    assert by["sales"]["value"] == 1_100_000 and by["avgTicket"]["value"] == 11_000
    assert by["activeOutlets"]["value"] == 3 and by["activeOutlets"]["total"] == 4    # CLOSED stopped selling
    assert by["outletDaily"]["value"] == round(1_100_000 / 1230)
    assert by["discountRate"]["status"] == "watch" and by["voidRate"]["status"] == "good"
    assert by["onlineShare"]["status"] == "good" and by["foodAttach"]["value"] == 30.0
    assert by["weakOutlets"]["outlets"] == ["SMALL"] and by["weakOutlets"]["status"] == "risk"   # 1 of 3 active
    assert by["menuTop10"]["value"] == 50.0 and by["peakHours"]["value"] == 90.0
    assert by["weekendShare"]["value"] == 50.0 and by["weekendShare"]["status"] == "info"
    scored = [i for i in h["indicators"] if i["status"] != "info"]
    assert {i["key"] for i in scored} == {"discountRate", "voidRate", "onlineShare", "foodAttach", "weakOutlets"}
    assert h["score"] == round(sum(health.SCORE[i["status"]] for i in scored) / len(scored))
    assert "detail" not in by["sales"] and "window" not in h
