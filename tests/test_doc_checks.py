"""Unusual ESB documents and their corrections (app/doc_checks.py, migration 097, 2026-11-12)."""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app import doc_checks

DAY = date(2026, 11, 1)


def _fix(**kw):
    base = {"id": 1, "module": "simple_manufacturing", "doc_num": "SM1", "line_type": "result", "product_code": "ESPR-00001",
            "uom": "KG@1000GR", "branch_code": "TGP04", "doc_date": DAY, "old_qty": Decimal("27720"), "new_qty": Decimal("27.72"),
            "reason": "KG typed for GR", "created_by": "cc", "created_at": datetime(2026, 11, 12, 9), "cancelled_at": None}
    return {**base, **kw}


ESB_NOW = {("simple_manufacturing", "SM1", "result", "ESPR-00001", "KG@1000GR"): 27720.0,
           ("simple_manufacturing", "SM1", "material", "RBAP-00003", "GR"): 100.0,
           ("goods_delivery", "SM1", "detail", "X", "PCS"): 5.0}


def test_adjustments_take_the_sign_of_the_kind_of_line(monkeypatch):
    monkeypatch.setattr(doc_checks, "_fixes", lambda a, b, br: [
        _fix(),                                                                          # a result comes in: 27.72 − 27720
        _fix(id=2, line_type="material", product_code="RBAP-00003", uom="GR", old_qty=Decimal("100"), new_qty=Decimal("10")),
        _fix(id=3, module="goods_delivery", line_type="detail", product_code="X", uom="PCS", old_qty=Decimal("5"), new_qty=Decimal("50"))])
    monkeypatch.setattr(doc_checks, "_esb_now", lambda fixes: ESB_NOW)
    out = doc_checks.adjustments(DAY, DAY, ["TGP04"])
    assert out["TGP04"]["produced"][("ESPR-00001", "KG@1000GR")] == pytest.approx(27.72 - 27720)
    assert out["TGP04"]["consumed"][("RBAP-00003", "GR")] == pytest.approx(90)          # less material went out
    assert out["TGP04"]["delivered"][("X", "PCS")] == pytest.approx(-45)               # more sent out


def test_esb_fixed_later_is_not_counted_twice_and_a_cancelled_document_drops_its_correction(monkeypatch):
    monkeypatch.setattr(doc_checks, "_fixes", lambda a, b, br: [_fix(), _fix(id=2, doc_num="SM2")])
    # SM1 fixed in ESB to 27.72 after the correction; SM2 cancelled in ESB (not counted any more)
    monkeypatch.setattr(doc_checks, "_esb_now", lambda fixes: {("simple_manufacturing", "SM1", "result", "ESPR-00001", "KG@1000GR"): 27.72})
    out = doc_checks.adjustments(DAY, DAY, ["TGP04"])
    assert out["TGP04"]["produced"][("ESPR-00001", "KG@1000GR")] == pytest.approx(0)
    view = doc_checks._fix_view(_fix(), 27.72)
    assert view["esbChanged"] is True and view["docGone"] is False
    assert doc_checks._fix_view(_fix(), 27720.0)["esbChanged"] is False


def test_adjustments_without_branches_or_table(monkeypatch):
    assert doc_checks.adjustments(DAY, DAY, []) == {}

    def boom(*a):
        raise RuntimeError("relation does not exist")
    monkeypatch.setattr(doc_checks, "_fixes", boom)
    assert doc_checks.adjustments(DAY, DAY, ["TGP04"]) == {}


@pytest.mark.parametrize("body, message", [
    ({"module": "goods_receipt", "lineType": "detail"}, "Good Receipt"),
    ({"module": "purchase_invoice", "lineType": "detail"}, "tidak dikoreksi"),
    ({"module": "simple_manufacturing", "lineType": "result", "reason": "no"}, "alasan"),
    ({"module": "simple_manufacturing", "lineType": "result", "reason": "KG for GR", "qty": "x"}, "tidak valid"),
    ({"module": "simple_manufacturing", "lineType": "result", "reason": "KG for GR", "qty": -1}, "negatif"),
])
def test_save_fix_refuses(body, message):
    with pytest.raises(doc_checks.DocFixError, match=message):
        doc_checks.save_fix({"username": "cc"}, body)


def test_save_fix_needs_the_line_and_a_change(monkeypatch):
    body = {"module": "simple_manufacturing", "lineType": "result", "docNum": "SM1", "code": "ESPR-00001", "uom": "kg@1000gr",
            "qty": 27720, "reason": "KG typed for GR"}
    monkeypatch.setattr(doc_checks, "_line", lambda *a: None)
    with pytest.raises(doc_checks.DocFixError, match="tidak ditemukan"):
        doc_checks.save_fix({"username": "cc"}, body)
    seen = {}

    def line(*a):
        seen["args"] = a
        return {"branch_code": "TGP04", "doc_date": DAY, "qty": Decimal("27720")}
    monkeypatch.setattr(doc_checks, "_line", line)
    with pytest.raises(doc_checks.DocFixError, match="sama"):
        doc_checks.save_fix({"username": "cc"}, body)
    assert seen["args"][-1] == "KG@1000GR"  # the unit is matched in upper case, as ESB lines are read
