"""Good Receipt corrections (app/goods_receipt.py, migration 072)."""
import base64
from datetime import date

import pytest

from app import goods_receipt as gr

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()


def test_evidence_files_are_checked():
    assert gr.parse_file({"name": "sj.png", "data": PNG})[:2] == ("sj.png", "image/png")
    with pytest.raises(gr.GrError):
        gr.parse_file({"name": "x.exe", "data": "data:application/x-msdownload;base64,TVqQ"})
    with pytest.raises(gr.GrError):   # a PDF that is not one
        gr.parse_file({"name": "x.pdf", "data": "data:application/pdf;base64," + base64.b64encode(b"hello").decode()})


def test_a_correction_needs_a_reason_evidence_and_a_change(monkeypatch):
    head = {"doc_num": "GR1", "doc_date": date(2026, 11, 3), "date": date(2026, 11, 3)}
    monkeypatch.setattr(gr, "_head", lambda doc, br: head)
    monkeypatch.setattr(gr, "_lines", lambda doc: [{"line_no": 1, "product_code": "RM", "product_name": "Milk", "uom_name": "BTL",
                                                    "qty": 4, "new_qty": None, "correction_id": None}])
    user = {"username": "cc", "full_name": "Cost Control"}
    with pytest.raises(gr.GrError, match="alasan"):
        gr.correct("GR1", {"reason": "", "files": [{"data": PNG}]}, user, None)
    with pytest.raises(gr.GrError, match="evidence"):
        gr.correct("GR1", {"reason": "salah input", "lines": [{"lineNo": 1, "qty": 3}]}, user, None)
    with pytest.raises(gr.GrError, match="perubahan"):
        gr.correct("GR1", {"reason": "salah input", "date": "2026-11-03", "lines": [{"lineNo": 1, "qty": 4}], "files": [{"data": PNG}]}, user, None)
    with pytest.raises(gr.GrError, match="Baris 9"):
        gr.correct("GR1", {"reason": "salah input", "lines": [{"lineNo": 9, "qty": 4}], "files": [{"data": PNG}]}, user, None)


def test_adjustments_move_a_receipt_and_change_a_quantity(monkeypatch):
    rows = [  # GR moved from 31 Oct into November, 4 → 3 BTL at the PO price 20.000
        {"doc_num": "GR1", "doc_date": date(2026, 10, 31), "eff_date": date(2026, 11, 2), "ref_num": "PO1", "branch_code": "JTW",
         "supplier": "Calf Warehouse", "product_code": "RM", "uom": "BTL", "qty": 4, "eff_qty": 3, "price": 20000},
        # quantity only, inside the period, no PO price and no Pricelist
        {"doc_num": "GR2", "doc_date": date(2026, 11, 3), "eff_date": date(2026, 11, 3), "ref_num": None, "branch_code": "JTW",
         "supplier": "Calf Warehouse", "product_code": "PK", "uom": "PCS", "qty": 10, "eff_qty": 12, "price": None}]
    monkeypatch.setattr(gr.db, "fetch", lambda sql, params=None: rows)
    monkeypatch.setattr(gr, "_fallback_prices", lambda codes, day: {})
    out = gr.adjustments(date(2026, 11, 1), date(2026, 11, 30))
    assert out["qty"] == {"JTW": {("RM", "BTL"): 3, ("PK", "PCS"): 2}}
    assert out["value"] == {"JTW": {"Calf Warehouse": 60000}} and out["unpriced"] == 1
    october = gr.adjustments(date(2026, 10, 1), date(2026, 10, 31))
    assert october["qty"]["JTW"][("RM", "BTL")] == -4   # it left October
