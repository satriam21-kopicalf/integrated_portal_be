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
            "income_amount": Decimal(0), "income_note": None, "closing_balance": Decimal(400000), "note": None, "kind": "PETTY_CASH",
            "requester_name": "Budi", "requester_phone": "0812 3456 789", "payee_bank": "BCA", "payee_account": "123456",
            "payee_name": "Budi", "purpose": None, "needed_on": None}
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


STEPS = [{"id": 1, "position": 1, "label": "Area Manager", "type": "APPROVE", "users": [{"id": "am", "name": "AM"}]},
         {"id": 2, "position": 2, "label": "Finance Manager", "type": "APPROVE", "users": []},
         {"id": 3, "position": 3, "label": "Pembayaran", "type": "PAY", "users": [{"id": "fin", "name": "Finance"}]}]
AM, FIN, SA = {"id": "am", "username": "am"}, {"id": "fin", "username": "fin"}, {"id": "sa", "username": "sa", "role": "superadmin"}


def test_steps_are_taken_by_their_users(monkeypatch):
    monkeypatch.setattr(pg, "flow", lambda kind: STEPS)
    step1 = {"status": "SUBMITTED", "approval_step": 1, "kind": "BILL"}
    assert pg.allowed_next(AM, step1) == ["APPROVE", "REVISION", "REJECTED"]
    assert pg.allowed_next(FIN, step1) == []
    assert pg.allowed_next(SA, step1) == ["APPROVE", "REVISION", "REJECTED"]  # a superadmin may always act
    step2 = {**step1, "approval_step": 2}  # a step without users: superadmins only
    assert pg.allowed_next(AM, step2) == [] and pg.allowed_next(SA, step2)
    paying = {"status": "APPROVED", "approval_step": 2, "kind": "BILL"}
    assert pg.allowed_next(FIN, paying) == ["PAID", "REVISION"] and pg.allowed_next(AM, paying) == []
    assert pg.allowed_next(FIN, {"status": "PAID", "approval_step": 2, "kind": "BILL"}) == []
    monkeypatch.setattr(pg, "get_row", lambda i: {"status": "SUBMITTED", "approval_step": 1, "kind": "BILL", "id": i})
    with pytest.raises(PermissionError):
        pg.change_status(FIN, "p", "APPROVE", None)
    with pytest.raises(pg.PengajuanError):
        pg.change_status(AM, "p", "REVISION", " ")  # a revision needs a note
    with pytest.raises(pg.PengajuanError):
        pg.change_status(AM, "p", "PAID", None)  # not before the approvals


def test_petty_cash_asks_the_top_up_to_the_plafon():
    # plafon 2 jt, opening 1.5 jt, nothing in, 1.2 jt spent → closing 300 rb → top-up 1.7 jt
    assert pg.requested("PETTY_CASH", 1500000, 0, 1200000, Decimal(2000000)) == Decimal(1700000)
    assert pg.requested("PETTY_CASH", 3000000, 0, 100000, Decimal(2000000)) == Decimal(0)  # above the plafon: nothing
    assert pg.requested("PETTY_CASH", 1500000, 0, 1200000, None) == Decimal(1200000)  # no plafon: the money spent
    assert pg.requested("BILL", None, 0, 450000, Decimal(2000000)) == Decimal(450000)


def test_budget_requests_need_their_purpose_date_and_receipts():
    bill = _row(kind="BILL", baristas=[], period_from=None, period_to=None, opening_balance=None, closing_balance=None)
    line = {"id": 1, "spentOn": None, "description": "Listrik Oktober", "amount": 850000.0}
    assert pg.missing(bill, [line], []) == ["Tulis keperluan pengajuan", "Isi jatuh tempo", "Rincian 1: nota / kwitansi / invoice"]
    ok = {**bill, "purpose": "Tagihan PLN", "needed_on": date(2026, 11, 5)}
    assert pg.missing(ok, [line], [RECEIPT]) == []
    marketing = {**ok, "kind": "MARKETING"}  # an estimate: no receipt needed
    assert pg.missing(marketing, [line], []) == []
    assert "Isi rekening tujuan: bank, no. rekening dan nama pemilik" in pg.missing({**ok, "payee_account": ""}, [line], [RECEIPT])
    assert "Isi no. handphone pribadi pengaju" in pg.missing({**ok, "requester_phone": "0812"}, [line], [RECEIPT])


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
                 outlet=["stock-opname", "waste"], full=["overview", "sales", "stock-opname", "waste", "guide", *access.FORM_PAGES],
                 farhan=["overview", "form.product", "mon.product"], cc=["overview", "cc.monitor"], admin=[])
    assert access.operations_only({"username": "pic", "role": "user"})
    assert not access.operations_only({"username": "tech", "role": "user"})  # Monitoring is in the portal only
    assert not access.operations_only({"username": "outlet", "role": "outlet"})  # no form: the portal
    assert access.operations_only({"username": "full", "role": "user"})  # every page of a PIC is in Operations
    assert not access.operations_only({"username": "farhan", "role": "user"})
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
    page_access.overrides[pic["id"]] = {}  # the PIC pages of the role: all in Operations
    with TestClient(app, base_url="https://testserver") as c:
        res = c.post("/api/auth/login", headers={"X-Kopicalf-App": "portal"},
                     json={"identifier": "pic.satu", "password": "Pic12345", "method": "username"})
        assert res.status_code == 403 and "operations.kopicalf.co.id" in res.json()["error"] and "set-cookie" not in res.headers
        assert login(c, "pic.satu", "Pic12345").status_code == 200  # Operations sends no header
        assert c.get("/api/auth/me").status_code == 200
        assert c.get("/api/auth/me", headers={"X-Kopicalf-App": "portal"}).status_code == 403  # an old portal session


def test_roles_of_the_pics_and_the_outlets():
    pic = set(access.DEFAULT_ROLE_PAGES["user"])
    assert pic == {"overview", "sales", "stock-opname", "waste", "guide", "form.pic-check-in", "form.product", "form.outlet",
                   "form.damaged-equipment", "form.pengajuan"}
    assert pic <= access.OPERATIONS_PAGES
    assert access.so_role({"id": "x", "role": "outlet"}) == "user"  # the outlet accounts count as crew in kopicalf
    from app import accounts
    assert "outlet" in accounts.ROLES and "outlet" not in accounts.GLOBAL_ROLES  # branch scoped like role user
