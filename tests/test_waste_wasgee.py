"""Wasgee Tea waste form (2026-11-08): a branch of another company offers the items of its own stock opname list."""
from app import waste


def test_a_branch_of_another_company_uses_its_stock_opname_items(monkeypatch):
    own = [{"code": "RM-TEA", "name": "Tea Leaves", "unit": "GR"}]
    monkeypatch.setattr(waste, "own_items", lambda branch: own if branch == "WSG02" else None)
    monkeypatch.setattr(waste, "premix_recipes", lambda facility: [])
    rows = [{"scope": "waste", "code": "RM-MILK", "name": "Milk", "unit": "ML"}, {"scope": "usage", "code": "RM-CUP", "name": "Cup", "unit": "PCS"}]
    monkeypatch.setattr(waste.db, "fetch", lambda q, p=None: rows if "waste_item" in q else [])
    wasgee = waste.catalog("OUTLET", "WSG02")["items"]
    assert wasgee["waste"] == own and wasgee["usage"] == own
    calf = waste.catalog("OUTLET", "CTG18")["items"]
    assert [i["code"] for i in calf["waste"]] == ["RM-MILK"] and [i["code"] for i in calf["usage"]] == ["RM-CUP"]
