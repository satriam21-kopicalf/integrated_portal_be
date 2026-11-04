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


def test_actual_usage_from_the_stock_opname_and_purchases(monkeypatch):
    from datetime import date

    from app import ratio
    # A: ESB documents both ends; B: ESB SO awal only (left out); C: portal counts (they win over its ESB documents)
    docs = {date(2026, 10, 31): {"A": {"doc": "SO-1", "date": "2026-10-31"}, "B": {"doc": "SO-9", "date": "2026-10-31"}},
            date(2026, 11, 7): {"A": {"doc": "SO-2", "date": "2026-11-07"}, "C": {"doc": "SO-7", "date": "2026-11-07"}}}
    monkeypatch.setattr(ratio, "so_values", lambda day, codes: docs[day])
    portal = {date(2026, 11, 1): {"C": {"source": "portal", "date": "2026-11-01", "refs": ["SC-1"], "items": {"RM-MILK": (1.5, "KG")}, "esb": {}}},
              date(2026, 11, 8): {"C": {"source": "portal", "date": "2026-11-08", "refs": ["SC-2"], "items": {"RM-MILK": (500.0, "GR")},
                                        "esb": {"RM-MILK": ("PACK", 1000.0)}}}}
    monkeypatch.setattr(mc, "portal_counts", lambda day, codes, back=1, ahead=1: portal[day])

    def fetch(sql, params=None):
        if "item_uom" in sql:  # the item master: a BTL of milk is 946 GR
            return [{"code": "RM-MILK", "base": "GR", "uom": "BTL", "factor": 946}]
        if "stock_opname" in sql:
            return [{"doc_num": d, "product_code": "RM-MILK", "uom": "GR", "qty": q} for d, q in (("SO-1", 5000), ("SO-2", 2000), ("SO-7", 9))
                    if d in params[0]]
        assert params[3] == ["A", "C"]  # only the branches with both counts
        return [{"branch_code": "A", "product_code": "RM-MILK", "uom": "GR", "qty": 10000},
                {"branch_code": "A", "product_code": "RM-MILK", "uom": "PACK", "qty": 3},       # A has no pack factor: left out
                {"branch_code": "A", "product_code": "RM-MILK", "uom": "BTL", "qty": 2},        # 2 × 946 GR (item master)
                {"branch_code": "C", "product_code": "RM-MILK", "uom": "PACK", "qty": 2}]       # C: the count list says 1000 GR
    monkeypatch.setattr(mc.db, "fetch", fetch)
    out = mc.actual_usage(date(2026, 11, 1), date(2026, 11, 7), None, {"RM-MILK": "GR"})
    assert out["covered"] == ["A", "C"]
    milk = out["items"]["RM-MILK"]
    assert milk["start"] == 5000 + 1500 and milk["bought"] == 10000 + 1892 + 2000 and milk["end"] == 2000 + 500
    assert milk["actual"] == (5000 + 10000 + 1892 - 2000) + (1500 + 2000 - 500) and milk["skipped"] == {"PACK": 3}
    c = next(b for b in out["branches"] if b["code"] == "C")
    assert c["start"]["source"] == "portal" and c["end"]["refs"] == ["SC-2"]


def test_units_compare_only_when_they_convert():
    assert mc.to_unit(1.5, "KG", "GR") == 1500 and mc.to_unit(500, "GR", "kg") == 0.5 and mc.to_unit(2, "PCS", "PCS") == 2
    assert mc.to_unit(2, "PACK", "GR") is None
