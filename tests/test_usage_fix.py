from datetime import date
from decimal import Decimal

import pytest

from app import usage_fix, waste


@pytest.mark.parametrize("unit,size", [("PACK@100PCS", 100), ("PACK@1KG", 1000), ("ROLL@1000PCS", 1000), ("PCS@250GR", 250),
                                       ("PCS", None), ("", None), (None, None)])
def test_pack_size(unit, size):
    assert usage_fix.pack_size(unit) == size


def test_a_quantity_in_pieces_is_unusual_and_suggested_in_packs():
    assert usage_fix.judge(2000, "ROLL@1000PCS", 1, 100) == {"unusual": True, "packSize": 1000, "suggested": 2.0}
    assert usage_fix.judge(24504, "PACK@1KG", 1, 100)["suggested"] is None  # 24.5 packs is not usual either
    assert usage_fix.judge(5, "PACK@1KG", 1, 100)["unusual"] is False
    assert usage_fix.judge(500, "PCS", 1, 100) == {"unusual": True, "packSize": None, "suggested": None}
    assert usage_fix.judge(500, "PACK@1KG", 1, 5)["unusual"] is False  # too few lines to tell
    assert usage_fix.judge(500, "PACK@1KG", None, 0)["unusual"] is False


def test_the_form_gets_the_limit_of_the_items_that_tell():
    items = [{"code": "A", "unit": "PACK@1KG"}, {"code": "B", "unit": "PCS"}, {"code": "C", "unit": "PCS"}]
    med = {("A", "PACK@1KG"): (1.0, 50), ("B", "PCS"): (2.0, 3), ("C", "PACK@100PCS"): (1.0, 90)}  # C has another unit here
    assert usage_fix.limits(items, med) == {"A": {"usual": 1.0, "limit": 10.0, "packSize": 1000}}


def test_an_unusual_quantity_needs_a_reason(monkeypatch):
    monkeypatch.setattr(waste, "_branch_day", lambda u, b: ({"code": "B1", "name": "B"}, date(2026, 11, 25)))
    monkeypatch.setattr(waste, "catalog", lambda *a, **k: {"items": {"usage": [{"code": "A", "name": "SEALER", "unit": "ROLL@1000PCS"}]},
                                                           "usageCategories": ["S"]})
    monkeypatch.setattr(usage_fix, "locked", lambda *a: set())
    monkeypatch.setattr(usage_fix, "usual", lambda: {("A", "ROLL@1000PCS"): (1.0, 50)})

    def err(qty, note):
        with pytest.raises(waste.WasteError) as e:
            waste.save_usage(USER, {"lines": {"A": {"qty": qty, "category": "S", "note": note}}})
        return str(e.value)
    assert "jauh di atas" in err(10, None) and "jauh di atas" in err(2000, " ok ")
    assert "Tanda tangan" in err(2000, "event besar") and "Tanda tangan" in err(9, None)  # on to the next check


class Cur:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self

    def execute(self, q, p=None):
        self.sql.append((q, p))

    def executemany(self, q, p):
        self.sql.append((q, p))

    def fetchone(self):
        return self.rows.pop(0)


ROW = {"branch_name": "Madiun", "item_name": "SEALER", "unit": "ROLL@1000PCS", "qty": Decimal("2000.000"), "category": "A", "note": None}
USER = {"id": "u", "username": "cc", "full_name": "CC"}
LINE = {"branchCode": "B1", "businessDate": date(2026, 11, 24), "itemCode": "I1", "qty": 2, "seen": 2000}


def _run(monkeypatch, rows, line, branches=None):
    cur = Cur(rows)
    monkeypatch.setattr(usage_fix.db, "transaction", lambda *a, **k: cur)
    monkeypatch.setattr(waste, "catalog", lambda *a, **k: {"items": {"usage": [{"code": "I2", "name": "LAIN", "unit": "PCS"}]},
                                                           "usageCategories": ["A", "B"]})
    return usage_fix.correct([line], "satuan salah", USER, branches), cur


def test_correct_logs_every_change(monkeypatch):
    out, cur = _run(monkeypatch, [ROW], {**LINE, "category": "B"})
    assert out["updated"][0]["changed"] == ["qty", "category"] and not out["skipped"]
    assert [r[3:6] for r in cur.sql[-1][1]] == [("qty", "2000 ROLL@1000PCS", "2 ROLL@1000PCS"), ("category", "A", "B")]


@pytest.mark.parametrize("rows,line,branches,msg", [
    ([None], LINE, None, "tidak ditemukan"), ([ROW], {**LINE, "seen": 5}, None, "sudah berubah"),
    ([ROW], {**LINE, "qty": 2000}, None, "Tidak ada perubahan"), ([], LINE, ("B2",), "luar akses"),
    ([ROW], {**LINE, "category": "Z"}, None, "kategori"), ([ROW, {"x": 1}], {**LINE, "newItemCode": "I2"}, None, "sudah ada"),
    ([ROW], {**LINE, "qty": -1}, None, "kurang dari 0")])
def test_refused_lines_are_skipped(monkeypatch, rows, line, branches, msg):
    out, _ = _run(monkeypatch, list(rows), line, branches)
    assert not out["updated"] and msg in out["skipped"][0]["reason"]


def test_reason_is_required():
    with pytest.raises(waste.WasteError):
        usage_fix.correct([LINE], " ", USER, None)
