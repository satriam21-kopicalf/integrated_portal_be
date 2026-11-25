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


# ---- BotFood's daily report (migration 141)
from app.aggregator_reports import from_daily, stretches  # noqa: E402


def rep(open_state, timeline, inactive, ot=time(7), ct=time(22)):
    return {"open_time": ot, "close_time": ct, "open_state": open_state, "timeline": timeline, "inactive_timeline": inactive}


def test_stretches_parse():
    assert stretches("07:00 - 08:11; 12:00 - 12:30;") == [(time(7), time(8, 11)), (time(12), time(12, 30))]
    assert stretches(None) == []


def test_daily_late_open():
    r = from_daily(rep("late", "08:11 - 22:00; ", "07:00 - 08:11;"), 15)
    assert r["status"] == "late" and r["lateMin"] == 71 and r["openedAt"] == "08:11" and r["earlyMin"] is None and r["pauseMin"] == 0


def test_daily_pause_and_early_close():
    r = from_daily(rep("on_time", "07:00 - 12:00; 12:30 - 20:00;", "12:00 - 12:30; 20:00 - 22:00;"), 15)
    assert r["status"] == "on_time" and r["pauseMin"] == 30 and r["earlyMin"] == 120


def test_daily_not_open_and_no_hours():
    assert from_daily(rep("not_open", None, "07:00 - 22:00;"), 15)["status"] == "not_opened"
    assert from_daily(rep("late", None, None, ot=None), 15)["status"] == "no_hours"


# ---------------------------------------------------------------- times (2026-11-25)

from datetime import datetime, time as _time  # noqa: E402

from app.aggregator_reports import _to_wib, open_minutes, proven, still_open  # noqa: E402


def test_daily_closed_before_the_hour_without_a_listed_stretch():
    r = from_daily(rep("on_time", "07:00 - 20:50; ", ""), 15)
    assert r["earlyMin"] == 70 and r["closedAt"] == "20:50"
    r = from_daily(rep("on_time", "07:00 - 21:50; ", ""), 15)  # within the grace
    assert r["earlyMin"] is None and r["closedAt"] is None


def test_a_later_order_proves_it_was_still_open():
    r = {"earlyMin": 70, "closedAt": "20:50"}
    assert still_open(r, _time(22), 21 * 60 + 20, 15) == {"earlyMin": 40, "closedAt": "21:20"}
    assert still_open(r, _time(22), 21 * 60 + 55, 15)["earlyMin"] is None
    assert still_open(r, _time(22), 20 * 60, 15) == r and still_open(r, _time(22), None, 15) == r


def test_hours_of_bali_are_moved_to_wib():
    assert _to_wib(_time(8), 60) == _time(7) and _to_wib(_time(8), 0) == _time(8) and _to_wib(None, 60) is None
    late = {"status": "late", "openedAt": "08:30", "lateMin": 90, "earlyMin": None}
    assert proven(late, _time(7), 7 * 60 + 5, 15)["status"] == "on_time"


def test_time_out_of_stock_counts_the_opening_hours_only():
    at = lambda d, h, m=0: datetime(2026, 11, d, h, m, tzinfo=WIB)  # noqa: E731
    assert open_minutes(at(24, 21), at(25, 8), _time(7), _time(22)) == 120  # 21:00-22:00 and 07:00-08:00
    assert open_minutes(at(24, 23), at(25, 6), _time(7), _time(22)) == 0
    assert open_minutes(at(24, 21), at(25, 8), None, None) == 660  # no hours known: every minute
    assert open_minutes(at(25, 8), at(24, 8), None, None) == 0
