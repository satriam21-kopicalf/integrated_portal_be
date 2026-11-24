"""Aggregator Insight stage 2 (migration 140): the opening rules of one outlet, platform and day (no database)."""
from datetime import date, datetime, time

from app.aggregator_reports import WIB, day_row

D = date(2026, 11, 24)
NIGHT = datetime(2026, 11, 24, 23, 0, tzinfo=WIB)


def ev(h, m, is_open):
    return {"at": datetime(2026, 11, 24, h, m, tzinfo=WIB), "is_open": is_open}


def row(events, was_open=False, now=NIGHT, ot=time(8), ct=time(20), g=15, have=True):
    return day_row(events, was_open, D, ot, ct, g, now, have)


def test_on_time_within_grace():
    r = row([ev(8, 10, True)])
    assert r["status"] == "on_time" and r["openedAt"] == "08:10" and r["lateMin"] == 0


def test_late_beyond_grace():
    r = row([ev(8, 40, True)])
    assert r["status"] == "late" and r["lateMin"] == 40


def test_not_opened_and_waiting():
    assert row([])["status"] == "not_opened"
    assert row([], now=datetime(2026, 11, 24, 8, 5, tzinfo=WIB))["status"] == "waiting"


def test_pause_then_early_close():
    r = row([ev(8, 0, True), ev(12, 0, False), ev(12, 30, True), ev(18, 0, False)])
    assert r["pauseMin"] == 30 and r["earlyMin"] == 120


def test_open_overnight_counts_on_time():
    r = row([ev(21, 0, False)], was_open=True)
    assert r["status"] == "on_time" and r["openedAt"] is None and r["earlyMin"] is None


def test_no_hours_and_no_data():
    assert row([ev(8, 0, True)], ot=None)["status"] == "no_hours"
    assert row([], have=False)["status"] == "no_data"
