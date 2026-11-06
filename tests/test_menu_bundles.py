"""Sales Calculator by Menu, bundling and list prices (app/menu_costing.py, 2026-11-06): a bundle card holds other cards,
a bundle with a card takes over the menus ESB records inside it, the list prices of the menus per channel."""
from datetime import date

import pytest

from app import menu_costing as mc
from app import menu_options

KOPI = {"code": "KOPI", "name": "Es Kopi", "isActive": True, "lines": [
    {"type": "item", "code": "RM-MILK", "name": "Fresh Milk", "qty": 100, "unit": "ML"},
    {"type": "item", "code": "PK-CUP", "name": "Cup", "qty": 1, "unit": "PCS"}]}
CHOCO = {"code": "CHOCO", "name": "Iced Chocolate", "isActive": True, "lines": [
    {"type": "item", "code": "RM-CHOCO", "name": "Choco", "qty": 30, "unit": "GR"}]}
B3 = {"code": "B3", "name": "Bundling 3 Es Kopi", "isActive": True, "lines": [
    {"type": "menu", "code": "KOPI", "name": "Es Kopi", "qty": 3, "unit": "PORSI"},
    {"type": "item", "code": "PK-BAG", "name": "Bag", "qty": 1, "unit": "PCS"}]}


def test_a_bundle_uses_the_materials_of_its_cards():
    cards = mc.resolve({"KOPI": KOPI, "CHOCO": CHOCO, "B3": B3})
    assert cards["B3"]["bundle"] and not cards["KOPI"]["bundle"]
    out: dict = {}
    mc.explode(cards["B3"], 2, {}, out)
    assert out["RM-MILK"]["qty"] == 600 and out["PK-CUP"]["qty"] == 6 and out["PK-BAG"]["qty"] == 2
    # a loop (refused when saved) never recurses forever
    loop = mc.resolve({"A": {"lines": [{"type": "menu", "code": "B", "qty": 1}]}, "B": {"lines": [{"type": "menu", "code": "A", "qty": 1}]}})
    assert loop["A"]["lines"] == []


def test_a_bundle_never_holds_itself(monkeypatch):
    monkeypatch.setattr(mc.db, "fetch", lambda sql, params=None: [{"code": "KOPI", "lines": []},
                                                                   {"code": "X", "lines": [{"type": "menu", "code": "B3"}]}])
    with pytest.raises(mc.CostingError, match="sendiri"):
        mc.save_recipe("B3", {"code": "B3", "name": "B", "lines": [{"type": "menu", "code": "X", "qty": 1}]}, {"username": "u"})
    with pytest.raises(mc.CostingError, match="tidak ditemukan"):
        mc.save_recipe("B3", {"code": "B3", "name": "B", "lines": [{"type": "menu", "code": "NOPE", "qty": 1}]}, {"username": "u"})


def test_list_prices_per_channel(monkeypatch):
    templates = [{"menuTemplateName": "DI CTG", "price": "20000.0000", "flagActive": 1},
                 {"menuTemplateName": "DI TGP", "price": "20000.0000", "flagActive": 1},
                 {"menuTemplateName": "OL CTG Grab", "price": "25000.0000", "flagActive": 1},
                 {"menuTemplateName": "OL TGP SHOPEE", "price": "26000.0000", "flagActive": 1},
                 {"menuTemplateName": "OL CCI", "price": "25000.0000", "flagActive": 1},
                 {"menuTemplateName": "event bulk order", "price": "25.0000", "flagActive": 1},
                 {"menuTemplateName": "DI OLD", "price": "18000.0000", "flagActive": 0}]
    monkeypatch.setattr(mc.db, "fetch", lambda sql, params=None: [{"id": "CALF:1", "price": None, "templates": templates},
                                                                   {"id": "CALF:9", "price": 15000, "templates": []}])
    out = mc.menu_prices(["CALF:1", "CALF:9"])
    assert out["CALF:1"] == [{"channel": "dine-in", "price": 20000, "min": 20000, "max": 20000},
                             {"channel": "online", "price": 25000, "min": 25000, "max": 26000}]
    assert out["CALF:9"] == [{"channel": "other", "price": 15000, "min": 15000, "max": 15000}]


def _sold(monkeypatch, mapped, choices):
    sold = [{"menu_id": "CALF:1", "name": "Es Kopi", "category": "BEVERAGE", "qty": 50, "sales": 800000},
            {"menu_id": "CALF:271", "name": "Bundling 3 Es Kopi", "category": "BEVERAGE", "qty": 10, "sales": 750000},
            {"menu_id": "CALF:300", "name": "Bundling Kopi + Choco", "category": "BEVERAGE", "qty": 5, "sales": 300000}]
    monkeypatch.setattr(mc.db, "fetch", lambda sql, params=None: sold)
    monkeypatch.setattr(mc.db, "fetchrow", lambda sql, params=None: {"bills": 0, "days": 0})
    monkeypatch.setattr(mc, "extras", lambda: [])
    monkeypatch.setattr(menu_options, "ensure_days", lambda a, b: [])
    monkeypatch.setattr(menu_options, "sold", lambda a, b, br: choices)
    monkeypatch.setattr(menu_options, "rules", lambda: {})
    cards = mc.resolve({"KOPI": KOPI, "CHOCO": CHOCO, "B3": B3})
    return mc._sold(date(2026, 11, 1), date(2026, 11, 1), None, cards, mapped, {}, 7, 0)


def test_a_bundle_with_a_card_takes_over_its_packages(monkeypatch):
    # 5 of the 50 Es Kopi were the package of bundle 300 (no card: counted through them), 30 inside bundle 271 (a card)
    choices = [{"parent": "CALF:271", "option": "CALF:1", "name": "Es Kopi", "qty": 30},
               {"parent": "CALF:300", "option": "CALF:1", "name": "Es Kopi", "qty": 5}]
    got = _sold(monkeypatch, {"CALF:1": "KOPI", "CALF:271": "B3"}, choices)
    by = {m["id"]: m for m in got["menus"]}
    assert by["CALF:1"]["qty"] == 20 and by["CALF:271"]["qty"] == 10
    assert got["system"]["RM-MILK"]["qty"] == 20 * 100 + 10 * 300  # no double count
    assert got["unmapped"] == [{"id": "CALF:300", "name": "Bundling Kopi + Choco", "category": "BEVERAGE", "qty": 5,
                                "sales": 300000, "viaPackages": True}]
    assert got["unruled"] == []  # the menus inside a bundle are no choices to rule


def test_fixed_bundle_contents():
    one = {"menus": [{"menuID": 1, "menuName": "Es Kopi", "flagActive": 1}], "minQty": "3.0000"}
    pair = {"menus": [{"menuID": 5, "menuName": "Choco", "flagActive": 1}, {"menuID": 1, "menuName": "Es Kopi", "flagActive": 1}], "minQty": "2.0000"}
    pick = {"menus": [{"menuID": 5}, {"menuID": 1}, {"menuID": 7}], "minQty": "2.0000"}
    assert menu_options._fixed_contents([one]) == [("1", "Es Kopi", 3.0)]
    assert menu_options._fixed_contents([pair]) == [("5", "Choco", 1.0), ("1", "Es Kopi", 1.0)]
    assert menu_options._fixed_contents([one, pick]) is None
