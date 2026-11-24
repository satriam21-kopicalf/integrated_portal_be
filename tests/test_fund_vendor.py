from datetime import date
from io import BytesIO

import openpyxl
import pytest

from app import fund_vendor as fv
from app import pengajuan as pg


def _xlsx(rows):
    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


HEAD = ["Timestamp", "NAMA VENDOR", "Keperluan Dana", "Deskripsi dan Detail", "Nomor Invoice/Nota/Kwitansi", "Tanggal Invoice", "Tanggal Jatuh Tempo",
        "Total Outstanding Invoice", "Total Dana Diajukan", "Nomor rekening", "Nama Rekening", "Bank Tujuan", "Departemen"]


def test_the_recap_is_read():
    ok = [None, "SUKANDA", "TOP", "Fresh milk", "FP-1", date(2026, 11, 2), "14/11/2026", 100, "1.250.000", "0146304", "PT SUKANDA", "BCA", "Warehouse"]
    bad = [None, "X", "LAIN", "", "-", None, date(2026, 11, 1), None, 0, 1234567890123456789, "", "VA BCA", "CK Food"]
    rows = fv.parse(_xlsx([HEAD, ok, bad]))
    assert rows[0]["problems"] == ["dana diajukan melebihi outstanding"] and rows[0]["account"] == "0146304" and rows[0]["amount"] == 1250000
    assert rows[0]["unit"] == "WAREHOUSE" and rows[1]["unit"] == "CK" and rows[1]["va"] and len(rows[1]["problems"]) >= 4


def test_a_sheet_without_the_columns_is_refused():
    with pytest.raises(pg.PengajuanError):
        fv.parse(_xlsx([["a", "b"], [1, 2]]))


def test_a_file_goes_to_the_longest_invoice():
    inv = [("a", "INV-100"), ("b", "INV-1001"), ("c", "-")]
    assert fv.match_file("INV 1001 sukanda.pdf", inv) == ["b"] and fv.match_file("inv-100.pdf", inv) == ["a"] and fv.match_file("lain.pdf", inv) == []


def test_one_refused_never_stops_the_others():
    def f(i):
        if i == "x":
            raise ValueError("tidak")
        return i
    assert fv.each(["a", "x", "b", "a"], f) == {"done": ["a", "b"], "failed": [{"id": "x", "reason": "tidak"}]}


def test_intercompany_needs_another_pt_a_period_and_the_report():
    row = {"payee_pt": "B", "payer": "A", "period_from": date(2026, 11, 1), "period_to": date(2026, 11, 13), "purpose": "retur", "requested_amount": 5,
           "payee_bank": "BCA", "payee_account": "1", "payee_name": "B"}
    assert pg.missing_intercompany(row, [{"kind": "OTHER"}], ["A", "B"]) == []
    assert len(pg.missing_intercompany({**row, "payee_pt": "A", "period_to": None}, [], ["A", "B"])) == 3


def test_the_payer_never_confirms_its_own_transfer():
    u = {"id": "1", "role": "superadmin", "username": "fin", "full_name": "Fin"}
    row = {"kind": "INTERCOMPANY", "status": "PAID", "paid_by": pg._actor(u), "payee_pt": "B"}
    assert not pg.can_receive(u, row) and pg.can_receive(u, {**row, "paid_by": "Lain"})
