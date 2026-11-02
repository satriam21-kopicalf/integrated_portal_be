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


def _xlsx(rows: list[list]) -> bytes:
    from io import BytesIO
    import openpyxl
    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_drivers_are_read_from_the_template_or_the_report_armada():
    template = _xlsx([["Armada", "Nama", "HP"], ["bni 1", "budi  santoso", "0812"], ["BRI 05", "Ade H", None], ["", "tanpa armada", ""]])
    assert cotr.parse_drivers(template) == [{"armada": "BNI 01", "name": "Budi Santoso", "phone": "0812"},
                                            {"armada": "BRI 05", "name": "Ade H", "phone": ""}]
    report = _xlsx([[None, "Calf", "NAMA PRODUK"], [None, "BNI 47\nPESENAN / CUSTOMER POOL", "Calf Premium Cup"],
                    [None, "BNI 02 RIDWAN", "Calf Premium Cup"], [None, "TOTAL BNI", None], [None, "BRI 19\nLARAS", "Matcha"]])
    assert cotr.parse_drivers(report) == [{"armada": "BNI 02", "name": "Ridwan", "phone": ""}, {"armada": "BRI 19", "name": "Laras", "phone": ""}]
    with pytest.raises(cotr.CotrError):
        cotr.parse_drivers(b"not an excel file")


def test_positions_are_thinned_in_time_order():
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    pts = [{"at": t0 + timedelta(seconds=s), "lat": -6.18, "lng": 106.84} for s in (30, 0, 5, 60)]
    pts.append({"at": t0 + timedelta(seconds=90), "lat": 0, "lng": 0})  # no fix
    assert [int((p["at"] - t0).total_seconds()) for p in cotr.thin(pts, None)] == [0, 30, 60]
    assert cotr.thin(pts, t0 + timedelta(seconds=45)) == [pts[3]]  # 10 s at least after what was kept before


def test_assignment_and_reload_checks():
    # the driver's check: all there, or the difference per menu (found - assigned)
    assert cotr.check_counts({"EKC": 100, "AMR": 10}, {"EKC": 100, "AMR": 10}) == (True, {})
    assert cotr.check_counts({"EKC": 100, "AMR": 10}, {"EKC": 96, "AMR": 10}) == (False, {"EKC": -4})
    # a received reload adds what the storage settled, else what it approved, else what was asked
    asked = {"status": "received", "lines": {"EKC": 50}}
    assert cotr.received(asked) == {"EKC": 50}
    assert cotr.received({**asked, "approved_lines": {"EKC": 40}}) == {"EKC": 40}
    assert cotr.received({**asked, "approved_lines": {"EKC": 40}, "received_lines": {"EKC": 38}}) == {"EKC": 38}
    stock = cotr.stock_now({"EKC": 10}, [{**asked, "approved_lines": {"EKC": 40}, "received_lines": {"EKC": 38}},
                                         {"status": "disputed", "lines": {"EKC": 20}}], [])
    assert stock == {"EKC": 48}  # a reload in dispute is not on the fleet yet


def test_salary_bank_and_nik_only_for_the_cotr_management():
    row = {"code": "DRV-BNI01", "name": "Budi", "salary_monthly": 3500000, "bank_account": "123", "nik": "317", "photo": b"x",
           "photo_type": "image/jpeg", "updated_by": "u"}
    shown = cotr._hide("drivers", cotr._plain(row), sensitive=False)
    assert shown == {"code": "DRV-BNI01", "name": "Budi", "has_photo": True}  # no bytes, no salary / bank / NIK
    full = cotr._hide("drivers", cotr._plain(row), sensitive=True)
    assert (full["salary_monthly"], full["bank_account"], full["nik"]) == (3500000.0, "123", "317")


def test_recipe_card_explodes_premix_by_item_code():
    # Premix Premium (NEW) of the Cost Control calculator: 6615 g per Pail; a cup takes 150 g of it and a cup
    boms = {"BOM-PRM-PREMIUM": {"code": "BOM-PRM-PREMIUM", "output": "PRM-PREMIUM", "yield_qty": 6615,
                                "lines": [{"item": "RM-FRESHMILK", "qty": 2700, "unit": "g"}, {"item": "RM-ESP-PREMIUM", "qty": 1665, "unit": "g"},
                                          {"item": "RM-CREAMER", "qty": 1125, "unit": "g"}, {"item": "RM-AIR", "qty": 1125, "unit": "g"}]},
            "BOM-CUP": {"code": "BOM-CUP", "output": "COTR-PRM-CUP", "yield_qty": 1,
                        "lines": [{"item": "PRM-PREMIUM", "qty": 150, "unit": "g"}, {"item": "PK-CUP-10OZ", "qty": 1, "unit": "pcs"}]}}
    out: dict = {}
    cotr.explode(boms, "BOM-CUP", 441, out)  # 441 cups = 66150 g of premix = 10 Pail
    assert round(out["RM-FRESHMILK"]["qty"]) == 27000 and round(out["RM-ESP-PREMIUM"]["qty"]) == 16650
    assert out["PK-CUP-10OZ"]["qty"] == 441 and "PRM-PREMIUM" not in out
