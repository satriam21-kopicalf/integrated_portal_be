"""Estimasi Belanja per outlet (app/purchase_plan.py): the buffer of an outlet from its own data, the plan per outlet."""
from datetime import date

import pytest

from app import purchase_plan as pp


def days(*ds):
    return [date(2026, 11, d) for d in ds]


def test_cover_days_are_the_median_gap_between_the_receipts():
    assert pp.median_gap(days(1, 4, 7, 11)) == 3  # gaps 3, 3, 4
    assert pp.cover_days(days(1, 4, 7, 11)) == (3, "gr")
    assert pp.cover_days(days(1, 1, 4, 4, 8)) == (4, "gr")  # one receipt a day counted once: gaps 3, 4 → 3.5 → 4
    assert pp.cover_days(days(1, 8)) == (pp.DEFAULT_COVER, "default")  # too few receipts
    assert pp.cover_days([date(2026, 9, 1), date(2026, 10, 1), date(2026, 10, 31)]) == (pp.COVER_MAX, "gr")  # capped


def test_loss_is_the_waste_against_the_usage_and_capped():
    assert pp.loss_pct(50_000, 1_000_000) == 5.0
    assert pp.loss_pct(900_000, 1_000_000) == pp.CAP_LOSS
    assert pp.loss_pct(10, 0) is None


def test_safety_follows_the_swing_of_the_sales_over_the_cover_days():
    flat = [1_000_000.0] * 28
    assert pp.safety_pct(flat, 3) == 0.0
    swing = [800_000.0, 1_200_000.0] * 14  # CV 0.2
    assert pp.safety_pct(swing, 4) == round(pp.Z * 0.2 / 2 * 100, 2)
    assert pp.safety_pct(swing, 1) == round(pp.Z * 0.2 * 100, 2)
    assert pp.safety_pct([0.0] * 25 + [1.0, 2.0, 3.0], 3) is None  # fewer than 7 open days
    wild = [100.0, 10_000.0] * 14
    assert pp.safety_pct(wild, 1) == pp.CAP_SAFETY


def test_waste_ordered_too_much_is_no_loss():
    assert pp.excluded("Sisa Produk tidak terjual") and pp.excluded("SISA PRODUK TIDAK TERJUAL")
    assert not pp.excluded("Bahan Baku Expired") and not pp.excluded(None)


def test_packs_round_up():
    assert pp.to_pack(1001, 1000) == 2 and pp.to_pack(1000, 1000) == 1 and pp.to_pack(0, 1000) == 0
    assert pp.to_pack(10, None) is None


def test_a_plan_line_per_material_of_one_outlet():
    used = {"RM-MILK": {"code": "RM-MILK", "name": "Milk", "unit": "ML", "qty": 28_000.0},   # 1 000 ml a day
            "RM-CUP": {"code": "RM-CUP", "name": "Cup", "unit": "PCS", "qty": 280.0},          # 10 a day
            "wip:X": {"code": None, "name": "Premix", "unit": "GR", "qty": 5.0}}
    stock = {"RM-MILK": {"estimate": 1.5, "unit": "L"},     # 1 500 ml counted in litres
             "RM-CUP": {"estimate": -4.0, "unit": "PCS"}}   # below zero: nothing to give
    packs = {"RM-MILK": ("ML", {"KARTON": 12_000.0}), "RM-CUP": ("PCS", {})}
    rows = pp.plan_rows(used, 28, 3, 10, stock, packs, {"RM-MILK": "KARTON"}, {"RM-MILK": 20.0}, set())
    milk = next(r for r in rows if r["code"] == "RM-MILK")
    assert milk["perDay"] == 1000 and milk["required"] == pytest.approx(3300) and milk["stock"] == 1500
    assert milk["packs"] == 1 and milk["packUom"] == "KARTON" and milk["toOrder"] == 12_000  # 1 800 ml → one carton
    assert milk["value"] == 240_000 and milk["priceSource"] == "pricelist"
    cup = next(r for r in rows if r["code"] == "RM-CUP")
    assert cup["stock"] == 0 and cup["toOrder"] == pytest.approx(33) and cup["packs"] is None and cup["price"] is None
    assert len(rows) == 2  # the WIP line without a code is no material to buy


def _base(monkeypatch, used_a, used_b):
    pp.reset()
    monkeypatch.setattr(pp, "today", lambda: date(2026, 11, 13))
    monkeypatch.setattr(pp, "outlets", lambda branches: {"A": "Outlet A", "B": "Outlet B"})
    monkeypatch.setattr(pp, "gr_days", lambda codes, end: {"A": days(1, 4, 7, 10), "B": []})
    monkeypatch.setattr(pp, "daily_sales", lambda codes, a, b: {"A": [1_000_000.0] * 28, "B": [500_000.0] * 28})
    monkeypatch.setattr(pp, "waste_values", lambda codes, a, b: {
        "A": {"value": 50_000.0, "excluded": 30_000.0, "entries": 3, "unpriced": 0, "categories": {}},
        "B": {"value": 0.0, "excluded": 0.0, "entries": 0, "unpriced": 0, "categories": {}}})
    monkeypatch.setattr(pp, "usage", lambda codes, a, b: {"A": used_a, "B": used_b})
    monkeypatch.setattr(pp, "_prices", lambda codes, day: ({"RM-MILK": 1.0}, set()))
    monkeypatch.setattr(pp, "received_packs", lambda codes, end: {})
    monkeypatch.setattr(pp, "overrides", lambda codes=None: {"B": {"coverDays": 5, "lossPct": None, "safetyPct": 2.5, "note": "x"}})


def test_every_outlet_its_own_buffer_and_an_override_of_cost_control(monkeypatch):
    milk = lambda q: {"RM-MILK": {"code": "RM-MILK", "name": "Milk", "unit": "ML", "qty": q}}  # noqa: E731
    _base(monkeypatch, milk(1_000_000.0), milk(10.0))
    rows = {r["branchCode"]: r for r in pp.recommendations()["data"]}
    a, b = rows["A"], rows["B"]
    assert a["recommended"] == {"coverDays": 3, "lossPct": 5.0, "safetyPct": 0.0, "bufferPct": 5.0}
    assert a["basis"]["wasteExcluded"] == 30_000 and a["override"] is None
    assert b["recommended"]["coverDays"] == pp.DEFAULT_COVER and "fewGr" in b["notes"]
    assert b["used"] == {"coverDays": 5, "lossPct": 0.0, "safetyPct": 2.5, "bufferPct": 2.5}
    assert [r["branchCode"] for r in pp.recommendations(["B"])["data"]] == ["B"]
    pp.reset()


def test_the_plan_of_an_outlet_uses_its_own_stock_never_the_others(monkeypatch):
    from app import running_stock
    milk = lambda q: {"RM-MILK": {"code": "RM-MILK", "name": "Milk", "unit": "ML", "qty": q}}  # noqa: E731
    _base(monkeypatch, milk(28_000.0), milk(28_000.0))
    stock = {"A": {"so": {"date": "2026-11-10"}, "items": {"RM-MILK": {"estimate": 0.0, "unit": "ML"}}},
             "B": {"so": {"date": "2026-11-10"}, "items": {"RM-MILK": {"estimate": 100_000.0, "unit": "ML"}}}}
    monkeypatch.setattr(running_stock, "running", lambda branches: {b: stock[b] for b in branches})
    monkeypatch.setattr(pp, "_units", lambda codes: {})
    a = pp.plan("A")
    assert a["coverDays"] == 3 and a["bufferPct"] == pp.CAP_LOSS  # waste 50 000 against a usage of 28 000: capped
    assert a["data"][0]["toOrder"] == pytest.approx(1000 * 3 * 1.2)  # B's 100 l does not cover A
    assert pp.plan("B")["data"][0]["toOrder"] == 0
    assert pp.plan("A", cover=7, buffer_pct=0)["data"][0]["toOrder"] == pytest.approx(7000)
    with pytest.raises(pp.PlanError):
        pp.plan("Z")
    s = pp.summary()
    assert {x["branchCode"] for x in s["data"]} == {"A", "B"} and s["total"] == pytest.approx(3600)
    pp.reset()


def test_an_override_needs_a_note(monkeypatch):
    class Conn:
        def execute(self, *a):
            return None

    class Tx:
        def __enter__(self):
            return Conn()

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(pp.db, "transaction", lambda: Tx())
    with pytest.raises(pp.PlanError):
        pp.save_setting("A", 3, None, None, "  ", "u")
    with pytest.raises(pp.PlanError):
        pp.save_setting("A", 40, None, None, "x", "u")
    pp.save_setting("A", None, None, None, "", "u")  # back to the recommendation: no note needed
    pp.save_setting("A", 3, 5, None, "jadwal kirim Senin-Kamis", "u")


def test_the_page_is_cost_control_only(client):
    assert client.get("/api/cost-control/purchase-plan?branch=A,B").status_code == 422  # one outlet
