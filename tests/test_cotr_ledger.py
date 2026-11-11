"""Stock of the finished products of a pool (migration 089): opening once, then the left of a day starts the next."""
from datetime import date, datetime, timezone

import pytest

from app import cotr_ledger as L


def _setup(monkeypatch, opening, transfers=(), dispatch=(), reloads=(), shifts=(), waste=()):
    monkeypatch.setattr(L, "_menu_map", lambda: {"LATTE": {"code": "LATTE", "name": "Latte", "sort": 1}})
    monkeypatch.setattr(L, "opening_of", lambda pool, day: {"day": date(2026, 11, 1), "at": None, "by": None, "lines": opening})

    def fetch(q, p=None):
        if "cotr_transfer" in q: return list(transfers)
        if "cotr_dispatch" in q: return list(dispatch)
        if "cotr_reload" in q: return list(reloads)
        if "cotr_shift" in q: return list(shifts)
        if "cotr_waste" in q: return list(waste)
        return []
    monkeypatch.setattr(L.db, "fetch", fetch)


def test_the_left_of_a_day_is_the_start_of_the_next(monkeypatch):
    _setup(monkeypatch, [{"menu": "LATTE", "qty": 50, "expiresOn": "2026-11-05"}],
           transfers=[{"day": date(2026, 11, 2), "menu_code": "LATTE", "expires_on": date(2026, 11, 9), "qty": 30, "batch_no": "B-0211"}],
           dispatch=[{"day": date(2026, 11, 1), "final_lines": {"LATTE": 40}}, {"day": date(2026, 11, 2), "final_lines": {"LATTE": 20}}],
           shifts=[{"day": date(2026, 11, 1), "end_stock": {"LATTE": 15}}],
           waste=[{"day": date(2026, 11, 2), "menu_code": "LATTE", "kind": "cupping", "qty": 5}])
    out = L.run("POOL", date(2026, 11, 2))
    r = out["rows"][0]
    # day 1: 50 − 40 + 15 back = 25; day 2: 25 + 30 in − 20 out − 5 cupping = 30
    assert (r["start"], r["in"], r["out"], r["cupping"], r["left"]) == (25, 30, 20, 5, 30)
    # first expired first: the 25 of the opening lot (exp 5 Nov) went first, 30 of the batch are left
    assert r["lots"] == [{"expiresOn": "2026-11-09", "qty": 30, "daysLeft": 7, "batchNo": "B-0211"}]


def test_cups_past_their_expiry_are_not_carried_over(monkeypatch):
    _setup(monkeypatch, [{"menu": "LATTE", "qty": 10, "expiresOn": "2026-11-02"}])
    r = L.run("POOL", date(2026, 11, 3))["rows"]
    assert r == [] or r[0]["left"] == 0
    day2 = L.run("POOL", date(2026, 11, 2))["rows"][0]
    assert (day2["expired"], day2["left"]) == (10, 0)


def test_an_assignment_never_more_than_the_pool_has(monkeypatch):
    _setup(monkeypatch, [{"menu": "LATTE", "qty": 10, "expiresOn": None}])
    monkeypatch.setattr(L.db, "fetchrow", lambda q, p=None: None)
    monkeypatch.setattr(L, "clean_stock", lambda lines, menus: lines)
    with pytest.raises(L.CotrError, match="tidak cukup"):
        L.check_assignment("POOL", date(2026, 11, 1), "D1", {"LATTE": 11})
    L.check_assignment("POOL", date(2026, 11, 1), "D1", {"LATTE": 10})
