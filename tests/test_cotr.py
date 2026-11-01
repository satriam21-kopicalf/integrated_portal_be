"""Calf On The Road (app/cotr.py, app/routes/cotr.py): the stock on the fleet, what a sale may take, the recap and the
difference at the end of the shift, the production plan through the BOMs, who opens the driver API and the portal."""
import pytest

from app import cotr
from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)

MENUS = {"EKC": {"code": "EKC", "name": "Es Kopi Calf", "price": 15000, "is_active": True},
         "AMR": {"code": "AMR", "name": "Americano", "price": 12000, "is_active": True},
         "MTC": {"code": "MTC", "name": "Es Matcha", "price": 16000, "is_active": False}}


def test_stock_is_start_plus_reloads_received_minus_sales():
    reloads = [{"status": "received", "lines": {"EKC": 20}}, {"status": "approved", "lines": {"EKC": 50}}]
    sales = [{"lines": [{"code": "EKC", "qty": 30}], "voided_at": None},
             {"lines": [{"code": "EKC", "qty": 5}, {"code": "AMR", "qty": 2}], "voided_at": "2026-11-01T10:00"}]
    assert cotr.stock_now({"EKC": 100, "AMR": 10}, reloads, sales) == {"EKC": 90, "AMR": 10}


def test_a_sale_takes_the_menu_price_and_never_more_than_the_stock():
    lines, total = cotr.sale_lines([{"code": "EKC", "qty": 2}, {"code": "AMR", "qty": 1}], MENUS, {"EKC": 5, "AMR": 1})
    assert total == 42000 and lines[0] == {"code": "EKC", "name": "Es Kopi Calf", "qty": 2, "price": 15000.0}
    with pytest.raises(cotr.CotrError, match="tinggal 1"):
        cotr.sale_lines([{"code": "AMR", "qty": 2}], MENUS, {"AMR": 1})
    with pytest.raises(cotr.CotrError, match="tidak dijual"):
        cotr.sale_lines([{"code": "MTC", "qty": 1}], MENUS, {"MTC": 9})
    with pytest.raises(cotr.CotrError, match="dua kali"):
        cotr.sale_lines([{"code": "EKC", "qty": 1}, {"code": "EKC", "qty": 1}], MENUS, {"EKC": 9})
    with pytest.raises(cotr.CotrError):
        cotr.sale_lines([], MENUS, {})


def test_stock_entered_is_cleaned():
    assert cotr.clean_stock({"EKC": "3", "AMR": 0}, MENUS) == {"EKC": 3}
    with pytest.raises(cotr.CotrError, match="tidak dikenal"):
        cotr.clean_stock({"XXX": 1}, MENUS)
    with pytest.raises(cotr.CotrError):
        cotr.clean_stock({"EKC": -1}, MENUS)


def test_recap_and_difference_at_the_end():
    shift = {"end_stock": {"EKC": 8}, "expected_stock": {"EKC": 10, "AMR": 1}}
    sales = [{"lines": [{"code": "EKC", "name": "Es Kopi Calf", "qty": 3, "price": 15000}], "payment": "cash", "total": 45000, "voided_at": None},
             {"lines": [{"code": "AMR", "name": "Americano", "qty": 1, "price": 12000}], "payment": "qris", "total": 12000, "voided_at": None},
             {"lines": [{"code": "EKC", "name": "Es Kopi Calf", "qty": 1, "price": 15000}], "payment": "cash", "total": 15000, "voided_at": "x"}]
    r = cotr.recap(shift, sales, [{"status": "received", "lines": {"EKC": 20}}], [{"status": "sent", "amount": 25000}, {"status": "rejected", "amount": 9}])
    assert (r["cups"], r["cash"], r["qris"], r["voided"], r["reloaded"], r["requested"]) == (4, 45000, 12000, 1, 20, 25000)
    assert r["difference"] == {"AMR": -1, "EKC": -2}
    assert cotr.recap({"end_stock": None}, [], [], [])["difference"] is None  # still open


def test_production_plan_explodes_premix_to_raw_materials():
    boms = {"CUP": {"code": "CUP", "output": "Es Kopi", "yield_qty": 1, "lines": [{"item": "Premix", "qty": 150, "unit": "g"},
                                                                              {"item": "Cup", "qty": 1, "unit": "pcs"}]},
            "PRM": {"code": "PRM", "output": "Premix", "yield_qty": 1000, "lines": [{"item": "Susu", "qty": 600, "unit": "g"},
                                                                                {"item": "Kopi", "qty": 400, "unit": "g"}]}}
    out = cotr.plan([{"code": "EKC", "name": "Es Kopi", "bom_code": "CUP"}], {"EKC": [90, 110]}, boms, 10)
    assert out["menus"][0] == {"code": "EKC", "name": "Es Kopi", "average": 100.0, "highest": 110, "plan": 110, "bom": "CUP"}
    assert {m["item"]: m["qty"] for m in out["materials"]} == {"Susu": 9900, "Kopi": 6600, "Cup": 110}


def test_driver_api_is_for_drivers_and_drivers_stay_out_of_the_portal(anon, store):
    login(anon, "kasir", "Kasir1234")
    assert anon.get("/api/cotr/driver/today").status_code == 403
    assert anon.get("/api/cotr/monitor").status_code == 403
    assert anon.get("/api/cotr/masters/drivers").status_code == 403
    store.add("drv.rizki", "rizki@kopicalf.co.id", "Rizki Pratama", "Driver1234", role="driver")
    portal = {"x-kopicalf-app": "portal"}
    res = anon.post("/api/auth/login", json={"identifier": "drv.rizki", "password": "Driver1234"}, headers=portal)
    assert res.status_code == 403 and "cotr.kopicalf.co.id" in res.json()["redirect"]
    assert login(anon, "drv.rizki", "Driver1234").status_code == 200  # the driver app sends no portal header
    assert anon.get("/api/auth/me", headers=portal).status_code == 403
    assert anon.get("/api/cotr/monitor").status_code == 403


def test_end_of_shift_follows_the_report_armada():
    # left = on the fleet - wasted - cupping (sold = taken - left - cupping - waste)
    assert cotr.left_expected({"EKC": 20, "AMR": 3}, {"EKC": 2}, {"EKC": 1, "AMR": 3}) == {"EKC": 17}
    # balance = cash counted - (cash sales - cash spent on the road): - short, + over ("Rekap Minus")
    assert cotr.balance(584000, 25000, 550000) == -9000
    assert cotr.balance(584000, None, 584000) == 0
    assert cotr.balance(584000, 0, None) is None  # still on the road
    shift = {"end_stock": {"EKC": 17}, "expected_stock": {"EKC": 17}, "end_waste": {"EKC": 2}, "end_cupping": {"EKC": 1},
             "cash_actual": 30000, "cash_spent": 5000}
    sales = [{"lines": [{"code": "EKC", "name": "Es Kopi Calf", "qty": 2, "price": 15000}], "payment": "cash", "total": 30000, "voided_at": None}]
    r = cotr.recap(shift, sales, [], [])
    assert (r["waste"], r["cupping"], r["cashExpected"], r["balance"], r["difference"]) == (2, 1, 25000, 5000, {})
