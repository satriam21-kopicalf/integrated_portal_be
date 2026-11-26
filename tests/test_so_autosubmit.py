"""SO Daily auto-submit (2026-11-26, app/so_autosubmit.py)."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app import so_autosubmit as so

WIB = ZoneInfo("Asia/Jakarta")


def test_it_runs_from_seven_in_the_morning():
    assert not so.due(datetime(2026, 11, 27, 6, 59, tzinfo=WIB)) and so.due(datetime(2026, 11, 27, 7, 0, tzinfo=WIB))
    assert so.submit_open(datetime(2026, 11, 27, 6, 0, tzinfo=WIB)) == [] and so.record_missing(datetime(2026, 11, 27, 6, 0, tzinfo=WIB)) == []


def test_the_note_says_what_was_left_empty():
    assert so.note_of(0, 50) == "[Auto-submit 07:00 WIB: tidak di-submit outlet]"
    assert so.note_of(3, 50) == "[Auto-submit 07:00 WIB: tidak di-submit outlet; 3 dari 50 baris belum diisi (tidak dihitung)]"


def test_a_day_without_so_daily_notes_nobody():
    assert so.is_so_day(114, 112) and so.is_so_day(10, 5)
    assert not so.is_so_day(114, 0) and not so.is_so_day(10, 4) and not so.is_so_day(0, 0)
