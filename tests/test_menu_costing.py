"""Sales Calculator by Menu (app/menu_costing.py, migration 056): a card exploded into raw materials (WIP per gram of
its batch), the lines of a card checked."""
import pytest

from app import menu_costing as mc

WIP = {"PRM-MATCHA": {"code": "PRM-MATCHA", "name": "Premix Matcha", "components": [
    {"code": "RM-MILK", "name": "Fresh Milk", "unit": "GR", "isStock": True, "perGram": 0.5},
    {"code": None, "name": "Air", "unit": "GR", "isStock": False, "perGram": 0.5}]}}
CARD = {"lines": [{"type": "wip", "code": "PRM-MATCHA", "name": "Premix Matcha", "qty": 200, "unit": "GR"},
                  {"type": "item", "code": "PK-CUP", "name": "Cup 16oz", "qty": 1, "unit": "PCS"}]}


def test_card_exploded_into_raw_materials():
    out: dict = {}
    mc.explode(CARD, 10, WIP, out)
    assert out["RM-MILK"]["qty"] == 1000 and out["n:Air"]["qty"] == 1000 and out["PK-CUP"]["qty"] == 10
    mc.explode({"lines": [{"type": "wip", "code": "NOPE", "name": "X", "qty": 5}]}, 2, WIP, out)
    assert out["wip:NOPE"]["qty"] == 10  # a WIP without an active recipe stays visible


def test_lines_are_checked():
    assert mc._lines([{"type": "wip", "code": "PRM-MATCHA", "qty": "150"}])[0] == {
        "type": "wip", "code": "PRM-MATCHA", "name": "PRM-MATCHA", "qty": 150.0, "unit": "GR"}
    with pytest.raises(mc.CostingError):
        mc._lines([{"type": "item", "code": "PK-CUP", "qty": 0}])
    with pytest.raises(mc.CostingError):
        mc._lines([{"type": "menu", "code": "X", "qty": 1}])


def test_free_items_in_a_card():
    lines = mc._lines([{"type": "other", "code": "x", "name": "Stiker", "qty": 1, "unit": "PCS", "price": 150}])
    assert lines[0]["price"] == 150.0
    out: dict = {}
    mc.explode({"lines": lines}, 4, WIP, out)
    assert out["o:stiker"]["qty"] == 4 and out["o:stiker"]["price"] == 150.0
