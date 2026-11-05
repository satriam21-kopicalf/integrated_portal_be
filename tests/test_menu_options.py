"""Sales Calculator by Menu, variants & modifiers and the cards from the ESB BOM (app/menu_options.py, migration 071)."""
import pytest

from app import menu_costing as mc, menu_options as mo

CARD = {"code": "ESP", "name": "Es Kopi Calf Premium", "lines": [
    {"type": "item", "code": "RM-ESP", "name": "ESPRESSO PREMIUM BLEND", "qty": 18, "unit": "GR"},
    {"type": "item", "code": "RM-AREN", "name": "GULA AREN 1300GR", "qty": 25, "unit": "GR"},
    {"type": "item", "code": "PC-CUP", "name": "CUP ES", "qty": 1, "unit": "PCS"},
    {"type": "item", "code": "RM-VAN", "name": "SYRUP DRIPP VANILLA 760ML", "qty": 9, "unit": "GR"},   # a flavour, not sugar
]}
RULES = {"normal sugar": {"name": "Normal Sugar", "effect": "none", "lines": []},
         "less sugar": {"name": "Less Sugar", "effect": "adjust", "lines": [{"target": "sugar", "mode": "pct", "value": -50}]},
         "no sugar": {"name": "No Sugar", "effect": "adjust", "lines": [{"target": "sugar", "mode": "pct", "value": -100}]},
         "extra shoot espresso premium": {"name": "Extra Shoot Espresso Premium", "effect": "adjust",
                                          "lines": [{"target": "item", "code": "RM-ESP", "mode": "qty", "value": 18}]}}


def test_choices_change_the_card_of_their_menu(monkeypatch):
    monkeypatch.setattr(mo, "_items", lambda codes: {"RM-ESP": {"name": "ESPRESSO PREMIUM BLEND", "unit": "GR"}})
    rows = [{"parent": "CALF:1", "option": "CALF:91", "name": "Normal Sugar", "qty": 60},
            {"parent": "CALF:1", "option": "CALF:92", "name": "Less Sugar", "qty": 30},
            {"parent": "CALF:1", "option": "CALF:93", "name": "No Sugar", "qty": 10},
            {"parent": "CALF:1", "option": "CALF:94", "name": "Extra Shoot Espresso Premium", "qty": 5},
            {"parent": "CALF:1", "option": "CALF:95", "name": "Oat Milk", "qty": 4},        # no rule yet
            {"parent": "CALF:7", "option": "CALF:92", "name": "Less Sugar", "qty": 9}]      # menu without a card
    per_menu, materials, unruled = mo.adjustments(rows, {"ESP": CARD}, {"CALF:1": "ESP"}, {}, RULES, mc.explode)
    # 100 portions sold: sugar 2500 g by the card, − 30 × 12.5 − 10 × 25 = 1875 g; espresso + 5 × 18 g
    assert per_menu["CALF:1"] == {"RM-AREN": -30 * 12.5 - 10 * 25, "RM-ESP": 90}
    assert materials["RM-AREN"]["name"] == "GULA AREN 1300GR" and "CALF:7" not in per_menu
    assert unruled == [{"name": "Oat Milk", "qty": 4}]


def test_rules_are_checked():
    assert mo._clean_lines([{"target": "sugar", "mode": "pct", "value": "-50"}]) == [
        {"target": "sugar", "code": None, "mode": "pct", "value": -50.0}]
    with pytest.raises(mo.OptionError):
        mo._clean_lines([{"target": "sugar", "mode": "qty", "value": 5}])   # sugar of the card in % only
    with pytest.raises(mo.OptionError):
        mo._clean_lines([{"target": "item", "mode": "qty", "value": 5}])    # which item
    with pytest.raises(mo.OptionError):
        mo._clean_lines([{"target": "sugar", "mode": "pct", "value": -150}])
    assert mo.key_of("  Less   SUGAR ") == "less sugar"


def test_a_card_from_the_esb_bom(monkeypatch):
    monkeypatch.setattr(mo.db, "fetchrow", lambda sql, params=None: {"bom_id": "B1", "bom_code": "BOM-01", "bom_name": "Es Kopi Calf Premium",
                                                                    "product_name": "Es Kopi Calf Premium"})

    def fetch(sql, params=None):
        if "master_bom_lines" in sql:
            return [{"line_no": 1, "product_code": "RM-ESP", "product_name": "ESPRESSO PREMIUM BLEND", "uom_name": "KG", "qty": 0.018, "last_hpp": None},
                    {"line_no": 2, "product_code": "RM-MILK", "product_name": "FRESH MILK", "uom_name": "BTL", "qty": 0.1, "last_hpp": None},
                    {"line_no": 3, "product_code": "X-9", "product_name": "Premix Premium", "uom_name": "GR", "qty": 40, "last_hpp": 50},
                    {"line_no": 4, "product_code": "Z-1", "product_name": "Stiker", "uom_name": "PCS", "qty": 1, "last_hpp": 120}]
        return [{"code": "RM-ESP", "name": "ESPRESSO PREMIUM BLEND", "base": "GR", "units": {"KG": 1000}},
                {"code": "RM-MILK", "name": "FRESH MILK DIAMOND 946ML", "base": "GR", "units": {"BTL": 946}}]
    monkeypatch.setattr(mo.db, "fetch", fetch)
    monkeypatch.setattr(mc, "wips", lambda: {"PMX-P": {"code": "PMX-P", "name": "Premix Premium", "components": []}})
    card = mo.bom_card("B1")
    lines = {ln["name"]: ln for ln in card["lines"]}
    assert lines["ESPRESSO PREMIUM BLEND"]["qty"] == 18 and lines["ESPRESSO PREMIUM BLEND"]["unit"] == "GR"
    assert lines["FRESH MILK DIAMOND 946ML"]["qty"] == 94.6                      # 0.1 BTL × 946 GR
    assert lines["Premix Premium"]["type"] == "wip" and lines["Premix Premium"]["qty"] == 40
    assert lines["Stiker"]["type"] == "other" and lines["Stiker"]["price"] == 120 and len(card["notes"]) == 1
