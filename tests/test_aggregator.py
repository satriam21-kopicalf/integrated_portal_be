"""Aggregator Insight (migration 139): the BotFood helpers that need no database."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app import aggregator as a


def test_outlet_key_matches_botfood_and_esb_names():
    assert a._key("Kopi Calf, Koja") == a._key("Kopi Calf To Go Koja") == "koja"
    assert a._key("Calf To Go, BTR") == "btr"
    assert a._key("Kopi Calf, Depok Baru") == "depok baru"


def test_botfood_times_are_jakarta():
    assert a._ts("2026-11-24 12:31:59") == datetime(2026, 11, 24, 12, 31, 59, tzinfo=ZoneInfo("Asia/Jakarta"))
    assert a._ts(None) is None and a._ts("x") is None


def test_numbers_and_flags():
    assert a._int("8000") == 8000 and a._int(None) is None and a._int("") is None
    assert a._num("4.8") == 4.8 and a._bool("1") is True and a._bool("0") is False and a._bool(None) is None


def test_no_token_no_thread(monkeypatch):
    monkeypatch.delenv("BOTFOOD_TOKEN", raising=False)
    assert a.token() is None
    a.start()  # returns without a thread or a lock
