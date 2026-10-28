"""Form & Monitoring → Pengajuan (app/pengajuan.py, app/routes/pengajuan.py): what blocks the sending, the approval flow
and its permissions, the portal / Operations split of the accounts (app/access.py)."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import access
from app import pengajuan as pg


def _row(**kw):
    base = {"baristas": [{"name": "Budi"}], "period_from": date(2026, 10, 1), "period_to": date(2026, 10, 7),
            "opening_balance": Decimal(500000), "opening_expected": Decimal(500000), "opening_note": None,
            "income_amount": Decimal(0), "income_note": None, "closing_balance": Decimal(400000), "note": None}
    return {**base, **kw}


LINE = {"id": 1, "spentOn": "2026-10-03", "description": "Gas 3 kg", "amount": 100000.0}
RECEIPT = {"kind": "LINE", "lineId": 1}


def test_complete_pengajuan_can_be_sent():
    assert pg.missing(_row(), [LINE], [RECEIPT]) == []


def test_every_expense_needs_its_receipt_date_and_amount():
    out = pg.missing(_row(), [LINE, {"id": 2, "spentOn": "2026-10-20", "description": "", "amount": 0}], [RECEIPT])
    assert out == ["Pengeluaran 2: tanggal di luar periode, keterangan, nominal, nota / kwitansi / invoice"]
    assert pg.missing(_row(), [], []) == ["Tambahkan minimal satu pengeluaran"]


def test_balances_and_money_in_need_their_evidence():
    out = pg.missing(_row(opening_balance=Decimal(450000), income_amount=Decimal(1000000), closing_balance=Decimal(-1)), [LINE], [RECEIPT])
    assert "Saldo awal berbeda dari saldo akhir pengajuan sebelumnya: tulis alasannya" in out
    assert "Tulis sumber dana masuk (mis. transfer Finance tanggal …)" in out
    assert "Lampirkan bukti dana masuk (bukti transfer / setoran)" in out
    assert "Saldo akhir minus: tulis penjelasan di catatan" in out
    ok = _row(opening_balance=Decimal(450000), opening_note="selisih kas", income_amount=Decimal(1000000), income_note="Transfer Finance")
    assert pg.missing(ok, [LINE], [RECEIPT, {"kind": "INCOME", "lineId": None}]) == []


def test_period_and_baristas():
    future = date.today() + timedelta(days=3)
    assert "Isi minimal satu nama barista" in pg.missing(_row(baristas=[]), [LINE], [RECEIPT])
    assert "Periode tidak boleh melewati hari ini" in pg.missing(_row(period_to=future), [LINE], [RECEIPT])
    assert "Isi periode pengajuan (dari – sampai)" in pg.missing(_row(period_from=None), [LINE], [RECEIPT])


def test_flow_steps_follow_the_permissions(monkeypatch):
    assert pg.allowed_next({"status": "SUBMITTED"}, {"verify"}) == ["VERIFIED", "REVISION", "REJECTED"]
    assert pg.allowed_next({"status": "SUBMITTED"}, {"finance"}) == []
    assert pg.allowed_next({"status": "VERIFIED"}, {"finance"}) == ["APPROVED", "REVISION", "REJECTED"]
    assert pg.allowed_next({"status": "APPROVED"}, {"verify"}) == []
    assert pg.allowed_next({"status": "PAID"}, {"verify", "finance"}) == []
    monkeypatch.setattr(pg, "get_row", lambda i: {"status": "SUBMITTED", "id": i})
    with pytest.raises(PermissionError):
        pg.change_status({"id": "u", "username": "x"}, "p", "VERIFIED", None, {"finance"})
    with pytest.raises(pg.PengajuanError):
        pg.change_status({"id": "u", "username": "x"}, "p", "REVISION", " ", {"verify"})  # a revision needs a note
    with pytest.raises(pg.PengajuanError):
        pg.change_status({"id": "u", "username": "x"}, "p", "PAID", None, {"finance"})  # not from SUBMITTED


def test_amounts_are_whole_rupiah():
    assert pg._amount("12500.4", "x") == Decimal(12500)
    assert pg._amount(None, "x") is None
    for bad in ("abc", -1, 10 ** 13):
        with pytest.raises(pg.PengajuanError):
            pg._amount(bad, "x")
    assert pg._amount(-5000, "x", allow_negative=True) == Decimal(-5000)


def test_receipts_must_be_pdf_or_photo():
    assert pg._file_type(b"%PDF-1.7 ...")[0] == "application/pdf"
    assert pg._file_type(b"\xff\xd8\xff\xe0....")[0] == "image/jpeg"
    with pytest.raises(pg.PengajuanError):
        pg._file_type(b"PK\x03\x04 a zip")


def test_form_accounts_use_operations(monkeypatch):
    pages = {}
    monkeypatch.setattr(access, "pages_for", lambda u: pages[u["username"]])
    pages.update(pic=["overview", "sales", "form.pic-check-in", "form.product", "form.outlet", "form.damaged-equipment"],
                 tech=["overview", "form.damaged-equipment", "mon.damaged-equipment"],
                 outlet=["stock-opname", "waste", "form.pengajuan"], cc=["overview", "cc.monitor"], admin=[])
    assert access.operations_only({"username": "pic", "role": "user"})
    assert not access.operations_only({"username": "tech", "role": "user"})  # Monitoring is in the portal only
    assert not access.operations_only({"username": "outlet", "role": "user"})  # Pengajuan and Stock Opname: portal
    assert not access.operations_only({"username": "cc", "role": "user"})  # no form
    assert not access.operations_only({"username": "admin", "role": "superadmin"})


def test_portal_refuses_operations_accounts(page_access, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.test_auth import MemoryAccounts, login  # noqa: F401
    from app import accounts, security
    from app.main import app
    from app.routes import auth as auth_module
    mem = MemoryAccounts()
    for name in ("find_for_login", "get_user", "record_login_failure", "record_login_success", "create_session", "session_user", "revoke_session"):
        monkeypatch.setattr(accounts, name, getattr(mem, name))
    auth_module.forget_sessions()
    pic = mem.add("pic.satu", "pic@kopicalf.co.id", "PIC Satu", "Pic12345", branch_codes=["CCI01"])
    page_access.overrides[pic["id"]] = {"stock-opname": False, "guide": False}  # forms, Dashboard and Sales
    with TestClient(app, base_url="https://testserver") as c:
        res = c.post("/api/auth/login", headers={"X-Kopicalf-App": "portal"},
                     json={"identifier": "pic.satu", "password": "Pic12345", "method": "username"})
        assert res.status_code == 403 and "operations.kopicalf.co.id" in res.json()["error"] and "set-cookie" not in res.headers
        assert login(c, "pic.satu", "Pic12345").status_code == 200  # Operations sends no header
        assert c.get("/api/auth/me").status_code == 200
        assert c.get("/api/auth/me", headers={"X-Kopicalf-App": "portal"}).status_code == 403  # an old portal session
