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
        mc._lines([{"type": "bogus", "code": "X", "qty": 1}])


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
        raise AssertionError(sql)
    monkeypatch.setattr(mc.db, "fetch", fetch)
    from app import stock_moves

    def moves(a, b, branches):
        assert branches == ["A", "C"]  # only the branches with both counts
        return {"A": {"received": {("RM-MILK", "GR"): 10000.0, ("RM-MILK", "PACK"): 3.0,  # A has no pack factor: left out
                                   ("RM-MILK", "BTL"): 2.0},                            # 2 × 946 GR (item master)
                      "delivered": {("RM-MILK", "GR"): -400.0}},                         # sent to the warehouse
                "C": {"received": {("RM-MILK", "PACK"): 2.0}}}                           # C: the count list says 1000 GR
    monkeypatch.setattr(stock_moves, "moves", moves)
    out = mc.actual_usage(date(2026, 11, 1), date(2026, 11, 7), None, {"RM-MILK": "GR"})
    assert out["covered"] == ["A", "C"]
    milk = out["items"]["RM-MILK"]
    assert milk["start"] == 5000 + 1500 and milk["bought"] == 10000 + 1892 + 2000 - 400 and milk["end"] == 2000 + 500
    assert milk["actual"] == (5000 + 10000 + 1892 - 400 - 2000) + (1500 + 2000 - 500) and milk["skipped"] == {"PACK": 3}
    assert milk["moves"] == {"received": 10000 + 1892 + 2000, "delivered": -400}
    c = next(b for b in out["branches"] if b["code"] == "C")
    assert c["start"]["source"] == "portal" and c["end"]["refs"] == ["SC-2"]


def test_units_compare_only_when_they_convert():
    assert mc.to_unit(1.5, "KG", "GR") == 1500 and mc.to_unit(500, "GR", "kg") == 0.5 and mc.to_unit(2, "PCS", "PCS") == 2
    assert mc.to_unit(2, "PACK", "GR") is None


def test_count_anomalies():
    usage = {"RM-JUICE": {"name": "Juice Apple", "price": 36.0}, "RM-MILK": {"name": "Fresh Milk", "price": 20.0}}
    rows = [
        {"branch": "A", "code": "RM-JUICE", "unit": "GR", "start": 6_000_000, "moves": 0.0, "in": 0.0, "end": 9_000, "actual": 5_991_000},
        {"branch": "A", "code": "RM-MILK", "unit": "GR", "start": 10_000, "moves": 20_000, "in": 20_000, "end": 90_000, "actual": -60_000},
        {"branch": "B", "code": "RM-MILK", "unit": "GR", "start": 10_000, "moves": 20_000, "in": 20_000, "end": 12_000, "actual": 18_000},
    ]
    import app.pic_checkin as pc
    old = pc._names
    pc._names = lambda codes: {"A": "Outlet A"}
    try:
        out = mc.count_anomalies(rows, usage)
    finally:
        pc._names = old
    assert [(x["code"], x["reasons"]) for x in out] == [("RM-JUICE", ["startHigh"]), ("RM-MILK", ["negative"])]  # B is plausible
    assert out[0]["branchName"] == "Outlet A" and out[0]["impact"] == round((6_000_000 - 9_000) * 36.0, 2)
