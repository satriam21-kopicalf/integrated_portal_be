"""Report Armada entered in the portal (migration 134, 2026-11-23)."""
from contextlib import contextmanager
from datetime import date

import pytest

from app import cotr_report
from app.cotr import CotrError


@pytest.fixture
def fake(monkeypatch):
    sql = []

    class Conn:
        def execute(self, q, p=()):
            sql.append((" ".join(q.split()), p))

    @contextmanager
    def tx():
        yield Conn()

    monkeypatch.setattr(cotr_report, "_menu_map", lambda: {"KS": {"name": "Calf Premium Cup", "price": 15000}})
    monkeypatch.setattr(cotr_report.db, "transaction", tx)
    monkeypatch.setattr(cotr_report.db, "fetchrow", lambda q, p=None: {"code": "D1", "name": "Budi", "pool_code": "HUB-PLM", "fleet_code": "BNI 47"} if "cotr_driver" in q else None)
    monkeypatch.setattr(cotr_report, "sheet", lambda pool, d: {"ok": True})
    return sql


def test_sold_is_brought_minus_left_cupping_waste(fake):
    cotr_report.save_products({"username": "storage"}, "HUB-PLM", date(2026, 11, 23), "D1", {"KS": {"b": 50, "s": 10, "c": 2, "w": 3}})
    upd = [p for q, p in fake if q.startswith("UPDATE") and "lines" in q][0]
    assert '"t": 35' in upd[0]


def test_more_out_than_brought_is_refused(fake):
    with pytest.raises(CotrError):
        cotr_report.save_products({"username": "storage"}, "HUB-PLM", date(2026, 11, 23), "D1", {"KS": {"b": 5, "s": 4, "c": 1, "w": 1}})


def test_an_armada_of_another_pool_is_refused(fake):
    with pytest.raises(CotrError):
        cotr_report.save_products({"username": "storage"}, "HUB-TBT", date(2026, 11, 23), "D1", {"KS": {"b": 5}})
