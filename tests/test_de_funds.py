"""Maintenance & Procurement funded through Fund Request (migration 130, app/de_funds.py): the fund's lines from the cost
lines, the work held until Finance approves, and the settlement of the actual cost at Done."""
from decimal import Decimal

import pytest

from app import de_funds as df
from app.damaged_equipment import DeError


def test_lines_from_the_estimated_cost_lines():
    lines = df.lines_from([
        {"kind": "SERVICE", "description": "Servis mesin espresso", "qty": 1, "estAmount": 750000},
        {"kind": "MATERIAL", "description": "Gasket", "qty": 2, "estAmount": 120000},
        {"kind": "OTHER", "description": "Belum ada harga", "qty": 1, "estAmount": None},
    ])
    assert lines == [{"category": "SERVICE", "amount": Decimal(750000), "description": "Servis mesin espresso"},
                     {"category": "MATERIAL", "amount": Decimal(120000), "description": "Gasket (×2)"}]


@pytest.mark.parametrize("fund,to,ok", [
    (None, "IN_PROGRESS", False), ({"status": "SUBMITTED", "number": "PGJ-1"}, "ORDERED", False),
    ({"status": "REVISION", "number": "PGJ-1"}, "IN_PROGRESS", False), ({"status": "REJECTED", "number": "PGJ-1"}, "IN_PROGRESS", False),
    ({"status": "APPROVED", "number": "PGJ-1"}, "IN_PROGRESS", True), ({"status": "PAID", "number": "PGJ-1"}, "ORDERED", True),
    (None, "ACCEPTED", True), (None, "HANDOVER", True),  # only starting the work is held
])
def test_the_work_waits_for_the_approved_fund(monkeypatch, fund, to, ok):
    monkeypatch.setattr(df, "fund_of", lambda de_id: fund)
    row = {"id": "r1", "est_cost": 870000}
    if ok:
        df.gate(row, to)
    else:
        with pytest.raises(DeError):
            df.gate(row, to)


def test_a_request_without_cost_is_not_held(monkeypatch):
    monkeypatch.setattr(df, "fund_of", lambda de_id: None)
    df.gate({"id": "r1", "est_cost": None}, "IN_PROGRESS")
    df.gate({"id": "r1", "est_cost": 0}, "ORDERED")


def test_settlement_of_the_actual_cost():
    assert df.settlement(Decimal(870000), Decimal(950000), paid=True) == ("TOP_UP", Decimal(80000))
    assert df.settlement(Decimal(870000), Decimal(800000), paid=True) == ("REFUND", Decimal(70000))
    assert df.settlement(Decimal(870000), Decimal(800000), paid=False) == ("UNPAID_LESS", Decimal(70000))
    assert df.settlement(Decimal(870000), Decimal(870000), paid=True) == ("EVEN", Decimal(0))


def test_fund_lines_carry_the_jenis_transaksi():
    """Migration 142: a cost line's Jenis Transaksi becomes the fund line's category TRX:<id>; old lines keep their kind."""
    from app.de_funds import lines_from
    out = lines_from([{"kind": "MATERIAL", "transactionId": 7, "description": "Gasket", "qty": 2, "estAmount": 50000},
                      {"kind": "TRANSPORT", "transactionId": None, "description": "Ojek", "qty": 1, "estAmount": 20000},
                      {"kind": "OTHER", "transactionId": 3, "description": "No estimate", "qty": 1, "estAmount": None}])
    assert [ln["category"] for ln in out] == ["TRX:7", "TRANSPORT"]
    assert out[0]["description"] == "Gasket (×2)"


def test_category_label_of_a_jenis_transaksi():
    from app.pengajuan import category_label
    assert category_label("TRX:7", {7: "Purchase of Tools/Assets/Machinery"}) == "Purchase of Tools/Assets/Machinery"
    assert category_label("TRX:9", {}) == "TRX:9"
    assert category_label("RAW") == "Bahan baku"
