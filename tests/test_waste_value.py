"""Waste value in Rp (app/waste.py with_values, 2026-11-05): Pricelist of the day, else the ESB HPP, units converted."""
from datetime import date, datetime

from app import menu_costing as mc, waste

DAY = date(2026, 11, 4)


def _row(**kw):
    base = {"id": "1", "user_id": "u", "username": "crew", "full_name": "Crew", "role": "outlet", "branch_code": "JTW",
            "branch_name": "Jatiwarna", "business_date": DAY, "scope": "waste", "item_code": "RM-MILK", "item_name": "Milk",
            "unit": "GR", "qty": 500, "category": "Expired", "note": None, "photo_taken_at": None, "latitude": None,
            "longitude": None, "status": "submitted", "review_note": None, "reviewed_by": None, "reviewed_at": None,
            "created_at": datetime(2026, 11, 4, 9)}
    return {**base, **kw}


def test_values_from_the_pricelist_and_the_esb_hpp(monkeypatch):
    monkeypatch.setattr(mc, "item_units", lambda codes: {"RM-MILK": ("GR", {"BTL": 946}), "RM-SUGAR": ("GR", {})})
    monkeypatch.setattr(mc, "prices_on", lambda day, codes: {"RM-MILK": 20})
    monkeypatch.setattr(mc, "esb_hpp", lambda day: {"RM-SUGAR": (15.0, 3)})
    rows = [_row(),                                                            # 500 GR × 20
            _row(id="2", unit="BTL", qty=2),                                   # 2 BTL × 946 GR × 20
            _row(id="3", item_code="RM-SUGAR", unit="KG", qty=1.5),            # ESB HPP, KG → GR
            _row(id="4", item_code="X-NONE", qty=3),                           # no price
            _row(id="5", scope="premix", item_code="PMX", unit="Gram", qty=100,
                 exploded=[{"code": "RM-MILK", "grams": 60, "isStock": True}, {"code": None, "name": "Air", "grams": 40, "isStock": False}])]
    v = {x["id"]: x for x in waste.with_values(rows)}
    assert v["1"]["value"] == 10000 and v["1"]["priceSource"] == "pricelist" and v["1"]["unitPrice"] == 20
    assert v["2"]["value"] == 2 * 946 * 20
    assert v["3"]["value"] == 1500 * 15 and v["3"]["priceSource"] == "esb"
    assert v["4"]["value"] is None and v["4"]["priceSource"] == "none"
    assert v["5"]["value"] == 1200 and v["5"]["priceSource"] == "pricelist"   # the water is no stock item
    assert waste._sum_values(list(v.values()), "branchCode") == {"JTW": 10000 + 37840 + 22500 + 1200}


def test_portal_waste_beside_esb_and_the_same_item_on_the_same_day(monkeypatch):
    """2026-11-13 (app/waste_compare.py): per outlet the portal waste, the ESB item journals and POS waste bills; a
    material in both the portal waste and an ESB item journal of the same outlet and day is shown as maybe twice."""
    from datetime import date
    from app import waste, waste_compare
    views = [{"branchCode": "A", "businessDate": "2026-11-10", "itemCode": "RM-MILK", "itemName": "Milk", "scope": "waste", "status": "approved", "value": 5000.0},
             {"branchCode": "A", "businessDate": "2026-11-11", "itemCode": "RM-CUP", "itemName": "Cup", "scope": "waste", "status": "submitted", "value": 700.0},
             {"branchCode": "B", "businessDate": "2026-11-10", "itemCode": "PX-1", "itemName": "Premix", "scope": "premix", "status": "approved", "value": None}]
    monkeypatch.setattr(waste, "with_values", lambda rows: views)
    journal = [{"branch_code": "A", "doc_date": date(2026, 11, 10), "product_code": "RM-MILK", "value": 4800},
               {"branch_code": "B", "doc_date": date(2026, 11, 12), "product_code": "RM-ICE", "value": 100}]

    def fetch(q, p=None):
        if "item_journal" in q:
            return journal
        if "salesMenus" in q:
            return [{"branch_code": "A", "bills": 2, "qty": 3}]
        return []
    monkeypatch.setattr(waste_compare.db, "fetch", fetch)
    import app.pic_checkin as pc
    monkeypatch.setattr(pc, "_names", lambda codes: {"A": "Outlet A", "B": "Outlet B"})
    out = waste_compare.compare(date(2026, 11, 10), date(2026, 11, 12), None)
    a = next(r for r in out["data"] if r["branchCode"] == "A")
    assert a["portalApproved"] == 5000 and a["portalWaiting"] == 700 and a["journalValue"] == 4800 and a["bills"] == 2 and a["billPortions"] == 3
    assert a["sameCount"] == 1 and a["same"][0] == {"day": "2026-11-10", "code": "RM-MILK", "name": "Milk"}
    b = next(r for r in out["data"] if r["branchCode"] == "B")
    assert b["portalUnpriced"] == 1 and b["sameCount"] == 0
    assert out["data"][0]["branchCode"] == "A" and out["total"]["sameCount"] == 1
