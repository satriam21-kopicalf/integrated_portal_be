"""The notification bell (app/alerts.py, migration 098, 2026-11-13)."""
from datetime import date

from app import alerts, doc_checks


def _line(**kw):
    base = {"module": "simple_manufacturing", "lineType": "result", "kind": "produced", "docNum": "SM1", "docDate": "2026-11-01",
            "branch": "TGP04", "branchName": "Tuparev", "code": "ESPR-00001", "name": "ESPRESSO PREMIUM BLEND", "uom": "KG@1000GR",
            "qty": 27720.0, "unit": "GR", "usual": 24000.0, "factor": 1155.0, "unusual": True, "severe": True, "impact": 2631120000.0,
            "ownPage": False, "fix": None}
    return {**base, **kw}


def test_number_in_indonesian_format():
    assert alerts._num(27720.0) == "27.720"
    assert alerts._num(25.25) == "25,25"
    assert alerts._num(0.5) == "0,5"


def test_unusual_documents_become_alerts_once_per_line(monkeypatch):
    monkeypatch.setattr(doc_checks, "check", lambda a, b, br: {"data": [
        _line(),
        _line(docNum="GR1", module="goods_receipt", lineType="detail", kind="received", severe=False, ownPage=True, docDate="2026-11-11"),
        _line(docNum="SM2", fix={"id": 1}),          # corrected already: no alert
        _line(docNum="SM3", unusual=False)]})        # shown only because of a correction: no alert
    out = alerts.unusual_docs(date(2026, 11, 13))
    assert [a["doc"] for a in out] == ["SM1", "GR1"]
    first, gr = out
    assert first["severity"] == "high" and "salah satuan" in first["title"] and first["link"] == "/cost-control"
    assert "27.720 KG@1000GR, biasanya 24.000 GR" in first["body"]
    assert gr["severity"] == "warn" and gr["link"] == "/cost-control/goods-receipt?doc=GR1&date=2026-11-11"
    assert first["dedup"] != gr["dedup"]


def test_reading_nothing_marks_nothing():
    assert alerts.mark_read({"id": "u"}, None, False) == 0
