"""Pusat Informasi (app/data_health.py): one card per source, a failing source never breaks the others, daily alerts."""
from datetime import date, datetime, timedelta, timezone

from app import data_health as dh


def test_freshness_and_the_worst_status():
    assert dh.stale_status(0.5) == "ok" and dh.stale_status(3) == "warn" and dh.stale_status(30) == "high"
    assert dh.stale_status(None) == "high"
    assert dh.worst("ok", "warn") == "warn" and dh.worst("warn", "high", "ok") == "high" and dh.worst() == "ok"
    now = datetime(2026, 11, 13, 10, tzinfo=timezone.utc)
    assert dh._hours(now - timedelta(hours=5), now) == 5 and dh._hours(None, now) is None


def test_a_failing_check_is_an_error_card_and_the_others_stay(monkeypatch):
    def boom(now):
        raise RuntimeError("db down")
    monkeypatch.setattr(dh, "CHECKS", {"esbSales": boom, "so": lambda now: dh.card("so", "warn", "3/4", "x", "Portal SO", "/cost-control/reviews")})
    out = dh._compute()
    by = {c["key"]: c for c in out["cards"]}
    assert by["esbSales"]["status"] == "error" and "db down" in by["esbSales"]["detail"]
    assert by["so"]["status"] == "warn" and out["counts"] == {"ok": 0, "warn": 1, "high": 0, "error": 1}


def test_daily_alerts_from_the_cards(monkeypatch):
    cards = [dh.card("so", "high", "1/4", "3 outlet tanpa SO", "Portal SO", "/cost-control/reviews"),
             dh.card("waste", "ok", "4/4", "semua lapor", "Portal Waste", "/x"),
             dh.card("gr", "warn", "2", "GR", "ESB GR", "/g")]
    monkeypatch.setattr(dh, "health", lambda keys=None: {"cards": cards})
    assert dh.health_alerts(date(2026, 11, 13), 8) == []  # before 09:00 the night syncs may not be in
    out = dh.health_alerts(date(2026, 11, 13), 9)
    assert [a["kind"] for a in out] == ["health_so"]  # ok left out, the GRs have their own alerts
    assert out[0]["severity"] == "high" and out[0]["dedup"] == "health:so:2026-11-13" and out[0]["title"] == dh.TITLES["so"]


def test_the_cards_of_some_keys(monkeypatch):
    data = {"at": "x", "counts": {}, "cards": [dh.card("so", "ok", "", "", "", None), dh.card("prices", "ok", "", "", "", None)]}
    monkeypatch.setattr(dh._all, "get", lambda: data)
    assert [c["key"] for c in dh.health(["prices"])["cards"]] == ["prices"]
    assert len(dh.health()["cards"]) == 2
