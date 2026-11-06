"""Test branches (app/test_branches.py, migration 078): read the ESB data of their source, stay out of every-branch figures."""
from app import test_branches as tb


def test_a_test_branch_reads_its_source(monkeypatch):
    monkeypatch.setattr(tb, "sources", lambda: {"DMY-SBG": "CTG18", "DMY-MRK": "CTG47"})
    assert tb.real(["DMY-SBG", "CTG01"]) == ["CTG01", "CTG18"] and tb.real(None) is None
    assert tb.readers(["DMY-SBG", "CTG18", "CTG01"]) == {"CTG18": ["DMY-SBG", "CTG18"], "CTG01": ["CTG01"]}
    assert tb.drop(["DMY-SBG", "CTG18"]) == ["CTG18"] and tb.is_test("DMY-MRK") and not tb.is_test("CTG47")


def test_the_moves_of_a_test_branch_come_from_its_source(monkeypatch):
    from app import goods_receipt, stock_moves, stock_transfer
    monkeypatch.setattr(tb, "sources", lambda: {"DMY-SBG": "CTG18"})
    seen = {}

    def fetch(q, p=None):
        seen["branches"] = p[5]
        return [{"branch_code": "CTG18", "module": "goods_receipt", "line_type": "detail", "product_code": "RM-MILK", "uom": "GR", "qty": 500}]
    monkeypatch.setattr(stock_moves.db, "fetch", fetch)
    monkeypatch.setattr(stock_transfer, "moves", lambda a, b, branches: [{"branch": "DMY-SBG", "kind": "transferIn", "item_code": "RM-MILK", "unit": "GR", "qty": 100}])
    monkeypatch.setattr(goods_receipt, "adjustments", lambda a, b, branches: {"qty": {}})
    out = stock_moves.moves(None, None, ["DMY-SBG"])
    assert seen["branches"] == ["CTG18"]
    assert out == {"DMY-SBG": {"received": {("RM-MILK", "GR"): 500.0}, "transferIn": {("RM-MILK", "GR"): 100.0}}}
