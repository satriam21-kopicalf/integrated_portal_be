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
