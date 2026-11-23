"""Arrived / Transfer with the other pool (migration 133, 2026-11-23): a transfer is kept as a pair, out at the pool it
leaves and in at the pool it reaches, tied by one ref."""
from contextlib import contextmanager
from datetime import date

import pytest

from app import cotr_ledger
from app.cotr import CotrError


class Conn:
    def __init__(self):
        self.sql: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.sql.append((" ".join(sql.split()), params))


@pytest.fixture
def fake(monkeypatch):
    conn = Conn()

    @contextmanager
    def tx():
        yield conn

    monkeypatch.setattr(cotr_ledger, "_menu_map", lambda: {"KS": {}, "AM": {}})
    monkeypatch.setattr(cotr_ledger.db, "transaction", tx)
    monkeypatch.setattr(cotr_ledger.db, "fetchrow", lambda sql, p=None: {"x": 1})
    monkeypatch.setattr(cotr_ledger, "run", lambda pool, a, b: {"pool": pool})
    return conn


USER = {"id": "u1"}
DAY = date(2026, 11, 23)


def test_transfer_to_a_pool_is_a_pair(fake):
    cotr_ledger.save_moves(USER, "HUB-PLM", DAY, "transfer", {"KS": 10, "AM": 0}, "pindah", "HUB-TBT")
    inserts = [p for s, p in fake.sql if s.startswith("INSERT")]
    assert [(p[0], p[2], p[3], p[6]) for p in inserts] == [("HUB-PLM", "KS", -10, "HUB-TBT"), ("HUB-TBT", "KS", 10, "HUB-PLM")]
    assert inserts[0][7] == inserts[1][7]  # one ref ties both rows
    assert any(s.startswith("DELETE") and "ref IN" in s for s, _ in fake.sql)  # the pair entered before is replaced


def test_transfer_needs_positive_cups_and_another_pool(fake):
    with pytest.raises(CotrError):
        cotr_ledger.save_moves(USER, "HUB-PLM", DAY, "transfer", {"KS": -3}, None, "HUB-TBT")
    with pytest.raises(CotrError):
        cotr_ledger.save_moves(USER, "HUB-PLM", DAY, "transfer", {"KS": 3}, None, "HUB-PLM")


def test_arrived_keeps_where_it_came_from(fake):
    cotr_ledger.save_moves(USER, "HUB-PLM", DAY, "arrived", {"KS": 5}, None, "STORAGE")
    ins = [p for s, p in fake.sql if s.startswith("INSERT")]
    assert ins == [("HUB-PLM", DAY, "KS", "arrived", 5, None, "u1", "STORAGE")]
    # the one-sided rows are replaced, never half of a transfer pair
    assert any("ref IS NULL" in s for s, _ in fake.sql if s.startswith("DELETE"))
