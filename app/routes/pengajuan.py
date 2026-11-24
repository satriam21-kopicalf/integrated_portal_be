"""Form → Pengajuan and Monitoring → Pengajuan (app/pengajuan.py, 2026-10-28).

Outlet (page form.pengajuan):
    GET    /api/pengajuan/form                      branches, baristas per branch, own pengajuan
    POST   /api/pengajuan/requests                  {"branchCode", "kind"} → the draft (an open draft of the branch and kind is reused)
    GET    /api/pengajuan/requests/{id}
    PATCH  /api/pengajuan/requests/{id}             baristas, periodFrom, periodTo, openingBalance, openingNote, incomeAmount,
                                                    incomeNote, note, requesterName, requesterPhone, payeeBank, payeeAccount, payeeName,
                                                    purpose, neededOn, lines [{id?, spentOn, category, description, amount}]
    POST   /api/pengajuan/requests/{id}/files?kind=LINE|INCOME|OTHER&line=&docType=&name=   raw body (PDF / photo ≤ 10 MB)
    DELETE /api/pengajuan/requests/{id}/files/{file}
    POST   /api/pengajuan/requests/{id}/submit
    DELETE /api/pengajuan/requests/{id}             a draft
Team (page mon.pengajuan, the portal; a step is taken by its users set in the approval settings):
    GET    /api/pengajuan/monitor?dateFrom=&dateTo=&branch=&group=
    GET    /api/pengajuan/monitor/{id}
    POST   /api/pengajuan/monitor/{id}/status       {"to": APPROVE|REVISION|REJECTED|PAID, "note", "paidAmount", "paidOn"}
    POST   /api/pengajuan/monitor/{id}/lines/{line} {"review": "OK"|"REJECTED", "note"}
    POST   /api/pengajuan/monitor/{id}/notes        {"note"}
    POST   /api/pengajuan/monitor/{id}/payment-files?docType=&name=   raw body (payment step, while waiting for payment)
    DELETE /api/pengajuan/monitor/{id}/payment-files/{file}
Settings (page mon.pengajuan to read; superadmin changes the approvals, superadmin or the payment step of the petty cash
the plafon):
    GET    /api/pengajuan/settings                  steps of every kind with their users, approver options, plafon
    PUT    /api/pengajuan/settings/flow/{kind}      {"steps": [{"label", "type": APPROVE|PAY, "userIds"}]}
    PUT    /api/pengajuan/settings/plafon           {"branchCode": "*"|code, "amount": number|null}
Both: GET /api/pengajuan/files/{file}
"""
from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import activity, branch_groups, fund_units
from app import pengajuan as pg
from app.access import deny, has_page
from app.pic_checkin import CheckInError
from app.routes.auth import current_user
from app.scope import NO_BRANCH, allowed_branches, scoped_branch
from app.utils import parse_branches, today

router = APIRouter(tags=["pengajuan"])
FORM, MON = "form.pengajuan", "mon.pengajuan"
# the proof of a request is sent by its owner: an outlet / person (the form) or the Maintenance & Procurement team (its
# technicians' Cash in Advance, migration 143) — the ownership is checked in any case
PROOF_PAGES = (FORM, "mon.damaged-equipment")


def _need(request: Request, user: dict, *pages: str) -> None:
    if not any(has_page(user, p) for p in pages):
        deny(request, user)


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from None
    except PermissionError as e:
        raise HTTPException(403, detail=str(e)) from None
    except CheckInError as e:  # PengajuanError
        raise HTTPException(422, detail=str(e)) from None


def _team_row(request: Request, user: dict, pengajuan_id: str) -> dict:
    _need(request, user, MON)
    row = pg.get_row(pengajuan_id)
    scope = allowed_branches()
    if row is None or row["status"] == "DRAFT" or (scope is not None and row["branch_code"] and row["branch_code"] not in scope) \
            or not fund_units.in_monitor(user, row):
        raise HTTPException(404, detail="Pengajuan tidak ditemukan")
    return row


def _team_view(user: dict, pengajuan_id: str) -> dict:
    out = pg.full(pengajuan_id)
    out["can"] = pg.can(user, pg.get_row(pengajuan_id))
    return out


def _record(request: Request, user: dict, summary: str, row_id: str, **details) -> None:
    activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan", summary=summary, details={"id": row_id, **details})


async def _body(request: Request) -> bytes:
    if int(request.headers.get("content-length") or 0) > pg.MAX_FILE_BYTES + 1024:
        raise HTTPException(422, detail=f"Lampiran maksimal {pg.MAX_FILE_BYTES // 1048576} MB")
    return await request.body()


# ---------------------------------------------------------------- outlet

class NewIn(BaseModel):
    branchCode: Optional[str] = Field(None, max_length=40)  # a personal request may have no outlet (2026-11-20)
    kind: str = Field("PETTY_CASH", max_length=20)
    unit: Optional[str] = Field(None, max_length=20)  # personal / vendor: the unit that asks (2026-11-24)


class LineIn(BaseModel):
    id: Optional[int] = None
    spentOn: Optional[str] = Field(None, max_length=10)
    category: Optional[str] = Field(None, max_length=20)
    description: Optional[str] = Field(None, max_length=300)
    amount: Optional[float] = None


class BaristaIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    userId: Optional[str] = Field(None, max_length=40)


class DraftIn(BaseModel):
    baristas: Optional[list[BaristaIn]] = Field(None, max_length=pg.MAX_BARISTAS + 5)
    periodFrom: Optional[str] = Field(None, max_length=10)
    periodTo: Optional[str] = Field(None, max_length=10)
    openingBalance: Optional[float] = None
    openingNote: Optional[str] = Field(None, max_length=1000)
    incomeAmount: Optional[float] = None
    incomeNote: Optional[str] = Field(None, max_length=1000)
    note: Optional[str] = Field(None, max_length=1000)
    lines: Optional[list[LineIn]] = Field(None, max_length=pg.MAX_LINES + 1)
    requesterName: Optional[str] = Field(None, max_length=80)
    requesterPhone: Optional[str] = Field(None, max_length=30)
    payeeBank: Optional[str] = Field(None, max_length=60)
    payeeAccount: Optional[str] = Field(None, max_length=40)
    payeeName: Optional[str] = Field(None, max_length=80)
    purpose: Optional[str] = Field(None, max_length=1000)
    neededOn: Optional[str] = Field(None, max_length=10)
    # personal (2026-11-20, migration 124)
    purposeId: Optional[int] = None
    transactionTypeId: Optional[int] = None
    projectId: Optional[int] = None
    accountTypeId: Optional[int] = None
    requestedAmount: Optional[float] = None
    branchCode: Optional[str] = Field(None, max_length=40)
    settlesId: Optional[str] = Field(None, max_length=40)
    # 2026-11-24 (migration 144): a bank account or a Virtual Account; a vendor payment's invoice number
    payeeType: Optional[str] = Field(None, pattern="^(BANK|VA)$")
    vaProvider: Optional[str] = Field(None, max_length=60)
    invoiceNo: Optional[str] = Field(None, max_length=80)
    # 2026-11-24 (migration 148): the unit and the vendor payment
    unit: Optional[str] = Field(None, max_length=20)
    vendorName: Optional[str] = Field(None, max_length=120)
    paymentTerm: Optional[str] = Field(None, max_length=3)
    invoiceDate: Optional[str] = Field(None, max_length=10)
    payeePt: Optional[str] = Field(None, max_length=80)
    outstandingAmount: Optional[float] = None


@router.get("/api/pengajuan/form")
def form(request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    return pg.form_state(user)


@router.post("/api/pengajuan/requests", status_code=201)
def new_request(body: NewIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    return _run(pg.create_draft, user, body.branchCode, body.kind, body.unit)


@router.get("/api/pengajuan/requests/{pengajuan_id}")
def get_request(pengajuan_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, *PROOF_PAGES)
    _run(pg.own, user, pengajuan_id)
    return pg.full(pengajuan_id)


@router.patch("/api/pengajuan/requests/{pengajuan_id}")
def save(pengajuan_id: str, body: DraftIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    data = body.model_dump(exclude_unset=True)
    return _run(pg.save_draft, user, pengajuan_id, data)


@router.post("/api/pengajuan/requests/{pengajuan_id}/files", status_code=201)
async def add_file(pengajuan_id: str, request: Request, kind: str = Query("LINE", max_length=10), line: Optional[int] = None,
                   docType: str = Query("NOTA", max_length=10), name: str = Query("lampiran", max_length=160),
                   user: dict = Depends(current_user)) -> dict:
    await run_in_threadpool(_need, request, user, FORM)
    await run_in_threadpool(_run, pg.own, user, pengajuan_id, True)
    if kind == "PAYMENT":
        raise HTTPException(422, detail="Bukti pembayaran diunggah oleh Finance")
    data = await _body(request)
    return await run_in_threadpool(_run, pg.add_file, user, pengajuan_id, kind, data, name, docType, line)


@router.delete("/api/pengajuan/requests/{pengajuan_id}/files/{file_id}")
def delete_file(pengajuan_id: str, file_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    _run(pg.own, user, pengajuan_id, True)
    return pg.delete_file(pengajuan_id, file_id, ("LINE", "INCOME", "OTHER"))


@router.post("/api/pengajuan/requests/{pengajuan_id}/submit")
def submit(pengajuan_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    out = _run(pg.submit, user, pengajuan_id)
    activity.record("pengajuan", user=user, request=request, page="/forms/pengajuan", summary=f"Mengirim pengajuan {out['number']}",
                    details={"id": pengajuan_id, "totalOut": out["totalOut"]})
    return out


class CancelIn(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


@router.post("/api/pengajuan/requests/{pengajuan_id}/cancel")
def cancel(pengajuan_id: str, body: CancelIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, *PROOF_PAGES)
    out = _run(pg.cancel, user, pengajuan_id, body.note)
    activity.record("pengajuan", user=user, request=request, page="/forms/pengajuan", summary=f"Membatalkan pengajuan {out['number']}",
                    details={"id": pengajuan_id, "note": body.note})
    return out


@router.delete("/api/pengajuan/requests/{pengajuan_id}", status_code=204)
def discard(pengajuan_id: str, request: Request, user: dict = Depends(current_user)) -> None:
    _need(request, user, FORM)
    _run(pg.discard, user, pengajuan_id)


@router.get("/api/pengajuan/files/{file_id}")
def file(file_id: str, request: Request, user: dict = Depends(current_user)) -> FileResponse:
    f = pg.file_row(file_id)
    row = pg.get_row(f["pengajuan_id"]) if f else None
    if row is None:
        raise HTTPException(404, detail="File tidak ditemukan")
    own = row["user_id"] == str(user["id"]) and has_page(user, FORM)
    scope = allowed_branches()
    team = row["status"] != "DRAFT" and fund_units.in_monitor(user, row) and has_page(user, MON) and (scope is None or not row["branch_code"] or row["branch_code"] in scope)
    if not (own or team):
        deny(request, user)
    if f["path"] is None:
        raise HTTPException(404, detail="File tidak ditemukan")
    return FileResponse(f["path"], media_type=f["mime"], filename=f["file_name"] or None, content_disposition_type="inline",
                        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


# ---------------------------------------------------------------- team

class StatusIn(BaseModel):
    to: str = Field(min_length=1, max_length=20)
    note: Optional[str] = Field(None, max_length=2000)
    paidAmount: Optional[float] = None
    paidOn: Optional[str] = Field(None, max_length=10)


class ReviewIn(BaseModel):
    review: str = Field(min_length=1, max_length=10)
    note: Optional[str] = Field(None, max_length=500)


class NoteIn(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


@router.get("/api/pengajuan/monitor")
def monitor(request: Request, dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None),
            branch: Optional[str] = None, group: Optional[str] = Query(None, max_length=60), unit: Optional[str] = Query(None, max_length=20), user: dict = Depends(current_user)) -> dict:
    _need(request, user, MON)
    end = dateTo or today()
    start = dateFrom or end - timedelta(days=89)
    if start > end or (end - start).days > 366:
        raise HTTPException(422, detail="Periode maksimal 1 tahun dan tanggal mulai sebelum tanggal akhir")
    scoped = scoped_branch(branch)
    branches = None if scoped is None else ([] if scoped == NO_BRANCH else parse_branches(scoped))
    branches = branch_groups.narrow(branches, group)
    mine = fund_units.monitors(user)
    units = [u for u in fund_units.units() if mine is None or u["code"] in mine]
    shown = [u["code"] for u in units] if not unit or unit == "ALL" else [unit] if unit in {u["code"] for u in units} else []
    res = pg.monitor(start, end, branches, shown)
    part = fund_units.receives_only(user)
    if part:
        res["data"] = [x for x in res["data"] if x["unit"] not in part or x["kind"] == "INTERCOMPANY"]
    return {"period": {"from": start.isoformat(), "to": end.isoformat()}, "me": str(user["id"]), "superadmin": user.get("role") == "superadmin",
            "units": units, "openByUnit": fund_units.open_counts(), **res}


@router.get("/api/pengajuan/monitor/{pengajuan_id}")
def team_get(pengajuan_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _team_row(request, user, pengajuan_id)
    return _team_view(user, pengajuan_id)


@router.post("/api/pengajuan/monitor/{pengajuan_id}/status")
def team_status(pengajuan_id: str, body: StatusIn, request: Request, user: dict = Depends(current_user)) -> dict:
    row = _team_row(request, user, pengajuan_id)
    _run(pg.change_status, user, pengajuan_id, body.to, body.note, {"amount": body.paidAmount, "on": body.paidOn})
    _record(request, user, f"{row['number']}: {pg.STATUS_LABEL[row['status']]} → {body.to}", pengajuan_id,
            to=body.to, note=body.note)
    return _team_view(user, pengajuan_id)


@router.post("/api/pengajuan/monitor/{pengajuan_id}/lines/{line_id}")
def team_review_line(pengajuan_id: str, line_id: int, body: ReviewIn, request: Request, user: dict = Depends(current_user)) -> dict:
    row = _team_row(request, user, pengajuan_id)
    _run(pg.review_line, user, pengajuan_id, line_id, body.review, body.note)
    _record(request, user, f"{row['number']}: pengeluaran {body.review}", pengajuan_id, line=line_id, note=body.note)
    return _team_view(user, pengajuan_id)


@router.post("/api/pengajuan/monitor/{pengajuan_id}/notes")
def team_note(pengajuan_id: str, body: NoteIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _team_row(request, user, pengajuan_id)
    pg.add_note(user, pengajuan_id, body.note)
    return _team_view(user, pengajuan_id)


@router.post("/api/pengajuan/monitor/{pengajuan_id}/payment-files", status_code=201)
async def team_payment_file(pengajuan_id: str, request: Request, docType: str = Query("TRANSFER", max_length=10),
                            name: str = Query("bukti-transfer", max_length=160), user: dict = Depends(current_user)) -> dict:
    row = await run_in_threadpool(_team_row, request, user, pengajuan_id)
    if row["status"] != "APPROVED" or not pg.can(user, row)["payment"]:
        raise HTTPException(422, detail="Bukti transfer diunggah oleh Finance saat pengajuan menunggu pembayaran")
    data = await _body(request)
    await run_in_threadpool(_run, pg.add_file, user, pengajuan_id, "PAYMENT", data, name, docType, None)
    return await run_in_threadpool(_team_view, user, pengajuan_id)


@router.delete("/api/pengajuan/monitor/{pengajuan_id}/payment-files/{file_id}")
def team_delete_payment_file(pengajuan_id: str, file_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    row = _team_row(request, user, pengajuan_id)
    if row["status"] != "APPROVED" or not pg.can(user, row)["payment"]:
        raise HTTPException(422, detail="Bukti transfer hanya dapat diubah sebelum pembayaran dicatat")
    pg.delete_file(pengajuan_id, file_id, ("PAYMENT",))
    return _team_view(user, pengajuan_id)


@router.post("/api/pengajuan/monitor/{pengajuan_id}/refund-files", status_code=201)
async def team_refund_file(pengajuan_id: str, request: Request, docType: str = Query("TRANSFER", max_length=10),
                           name: str = Query("bukti-dana-kembali", max_length=160), user: dict = Depends(current_user)) -> dict:
    """Finance: the proof that a Maintenance & Procurement fund gave the money not spent back (app/de_funds.py)."""
    row = await run_in_threadpool(_team_row, request, user, pengajuan_id)
    if not pg.can(user, row).get("refund"):
        raise HTTPException(422, detail="Bukti dana kembali diunggah Finance saat ada pengembalian yang belum dikonfirmasi")
    data = await _body(request)
    await run_in_threadpool(_run, pg.add_file, user, pengajuan_id, "REFUND", data, name, docType, None)
    return await run_in_threadpool(_team_view, user, pengajuan_id)


@router.delete("/api/pengajuan/monitor/{pengajuan_id}/refund-files/{file_id}")
def team_delete_refund_file(pengajuan_id: str, file_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    row = _team_row(request, user, pengajuan_id)
    if not pg.can(user, row).get("refund"):
        raise HTTPException(422, detail="Bukti dana kembali hanya dapat diubah sebelum dikonfirmasi")
    pg.delete_file(pengajuan_id, file_id, ("REFUND",))
    return _team_view(user, pengajuan_id)


@router.post("/api/pengajuan/monitor/{pengajuan_id}/refunded")
def team_refunded(pengajuan_id: str, body: NoteIn, request: Request, user: dict = Depends(current_user)) -> dict:
    """Finance confirms the money not spent came back (Maintenance & Procurement funds)."""
    row = _team_row(request, user, pengajuan_id)
    _run(pg.confirm_refund, user, pengajuan_id, body.note)
    _record(request, user, f"{row['number']}: dana kembali dikonfirmasi", pengajuan_id, note=body.note)
    return _team_view(user, pengajuan_id)


# ---------------------------------------------------------------- after the money went out (migration 144)

class ProofIn(BaseModel):
    actualAmount: Optional[float] = Field(None, ge=0)
    note: Optional[str] = Field(None, max_length=2000)


@router.post("/api/pengajuan/requests/{pengajuan_id}/proof-files", status_code=201)
async def add_proof_file(pengajuan_id: str, request: Request, docType: str = Query("NOTA", max_length=10),
                         name: str = Query("bukti", max_length=160), user: dict = Depends(current_user)) -> dict:
    """The requester's proof while the request is In Progress (money out): receipts, invoice, goods received."""
    await run_in_threadpool(_need, request, user, *PROOF_PAGES)
    row = await run_in_threadpool(_run, pg.own, user, pengajuan_id)
    if row["status"] != "PAID":
        raise HTTPException(422, detail="Bukti dilampirkan setelah dana cair dan sebelum verifikasi")
    data = await _body(request)
    await run_in_threadpool(_run, pg.add_file, user, pengajuan_id, "PROOF", data, name, docType, None)
    return await run_in_threadpool(pg.full, pengajuan_id)


@router.delete("/api/pengajuan/requests/{pengajuan_id}/proof-files/{file_id}")
def delete_proof_file(pengajuan_id: str, file_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, *PROOF_PAGES)
    row = _run(pg.own, user, pengajuan_id)
    if row["status"] != "PAID":
        raise HTTPException(422, detail="Bukti hanya dapat diubah sebelum dikirim ke Finance")
    pg.delete_file(pengajuan_id, file_id, ("PROOF",))
    return pg.full(pengajuan_id)


@router.post("/api/pengajuan/requests/{pengajuan_id}/proof")
def send_proof(pengajuan_id: str, body: ProofIn, request: Request, user: dict = Depends(current_user)) -> dict:
    """In Progress → Verifikasi: the proof and the amount really spent go to Finance."""
    from app import fund_lifecycle
    _need(request, user, *PROOF_PAGES)
    out = _run(fund_lifecycle.submit_proof, user, pengajuan_id, body.actualAmount, body.note)
    activity.record("pengajuan", user=user, request=request, page="/forms/pengajuan", summary=f"Mengirim bukti {out['number']}",
                    details={"id": pengajuan_id, "actual": body.actualAmount})
    return out


class VerifyIn(BaseModel):
    action: str = Field(pattern="^(ADJUST|MISSING|TOPUP|DONE)$")
    amount: Optional[float] = Field(None, ge=0)
    note: Optional[str] = Field(None, max_length=2000)


@router.post("/api/pengajuan/monitor/{pengajuan_id}/verify")
def team_verify(pengajuan_id: str, body: VerifyIn, request: Request, user: dict = Depends(current_user)) -> dict:
    """Finance on a request in Verifikasi: adjust the accepted amount, send back for missing proof, pay the shortfall, Done."""
    from app import fund_lifecycle
    row = _team_row(request, user, pengajuan_id)
    _run(fund_lifecycle.verify, user, pengajuan_id, body.action, body.amount, body.note)
    _record(request, user, f"{row['number']}: verifikasi {body.action}", pengajuan_id, action=body.action, amount=body.amount, note=body.note)
    return _team_view(user, pengajuan_id)


@router.post("/api/pengajuan/monitor/{pengajuan_id}/topup-files", status_code=201)
async def team_topup_file(pengajuan_id: str, request: Request, docType: str = Query("TRANSFER", max_length=10),
                          name: str = Query("bukti-transfer-kekurangan", max_length=160), user: dict = Depends(current_user)) -> dict:
    """Finance: the transfer of the shortfall (more spent than paid), while the request is in Verifikasi."""
    row = await run_in_threadpool(_team_row, request, user, pengajuan_id)
    if not pg.can(user, row).get("verify"):
        raise HTTPException(422, detail="Bukti transfer kekurangan diunggah Finance saat verifikasi")
    data = await _body(request)
    await run_in_threadpool(_run, pg.add_file, user, pengajuan_id, "TOPUP", data, name, docType, None)
    return await run_in_threadpool(_team_view, user, pengajuan_id)


@router.delete("/api/pengajuan/monitor/{pengajuan_id}/topup-files/{file_id}")
def team_delete_topup_file(pengajuan_id: str, file_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    row = _team_row(request, user, pengajuan_id)
    if not pg.can(user, row).get("verify") or row.get("topup_paid_at"):
        raise HTTPException(422, detail="Bukti transfer kekurangan hanya dapat diubah sebelum dicatat")
    pg.delete_file(pengajuan_id, file_id, ("TOPUP",))
    return _team_view(user, pengajuan_id)


# ---------------------------------------------------------------- settings (approvals, plafon)

class StepIn(BaseModel):
    label: str = Field("", max_length=60)
    type: str = Field("APPROVE", pattern="^(APPROVE|PAY)$")
    userIds: list[str] = Field(default_factory=list, max_length=50)


class FlowIn(BaseModel):
    steps: list[StepIn] = Field(min_length=2, max_length=pg.MAX_STEPS + 1)


class SuggestIn(BaseModel):
    codes: list[str] = Field(..., min_length=1, max_length=500)
    factor: float = Field(1.2, ge=0.5, le=5)
    roundTo: int = Field(50_000, ge=1_000, le=1_000_000)


class PlafonIn(BaseModel):
    branchCode: str = Field(min_length=1, max_length=40)
    amount: Optional[float] = None


def _plafon_editor(user: dict) -> bool:
    """Superadmins and Finance: the users of the payment step of the petty cash."""
    return user.get("role") == "superadmin" or pg.takes(user, pg._pay(pg.flow("PETTY_CASH")))


def _superadmin(request: Request, user: dict) -> None:
    if user.get("role") != "superadmin":
        deny(request, user)


@router.get("/api/pengajuan/settings")
def settings(request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, MON)
    from app import fund_lifecycle, fund_options
    options = fund_options.all_options()
    flows = {k: pg.flow(k) for k in pg.KINDS}
    flows.update({f"PERSONAL:{o['id']}": pg.flow(f"PERSONAL:{o['id']}") for o in options["PURPOSE"]})
    flows.update({k: pg.flow(k) for k in fund_units.unit_flows()})  # "<kind>@<unit>": the units with a flow of their own
    return {"kinds": pg.KINDS, "flows": flows, "users": pg.approver_options(), "plafon": pg.plafons(), "outlets": pg.outlet_options(),
            "canEdit": user.get("role") == "superadmin", "canPlafon": _plafon_editor(user),
            # the lists of the personal form and the plafon per department (management: superadmin / admin)
            "options": options, "lists": pg.list_names(), "listDefaults": fund_options.LISTS, "deptPlafon": fund_options.dept_plafons(),
            "departments": fund_options.departments(), "canOptions": _options_editor(user), "kindLabels": pg.kind_labels(),
            # the life after the money went out (migration 144): the rules of every kind and the global settings, Finance's
            "rules": fund_lifecycle.rules(), "lifecycle": fund_lifecycle.settings(), "canRules": _finance_editor(user),
            "units": fund_units.units(), "receivers": fund_units.receivers(), "proofDocs": {d: pg.DOC_TYPES[d] for d in fund_lifecycle.DOCS}}


def _finance_editor(user: dict) -> bool:
    """The rules of the requests: management (superadmin / admin) and Finance (the payment step of the petty cash)."""
    return _options_editor(user) or _plafon_editor(user)


class RuleIn(BaseModel):
    proofAfter: bool = False
    proofDays: int = Field(7, ge=1, le=180)
    proofDocs: list[str] = Field(default_factory=list, max_length=8)
    vendor: bool = False


@router.put("/api/pengajuan/settings/rules/{key}")
def set_rule(key: str, body: RuleIn, request: Request, user: dict = Depends(current_user)) -> list[dict]:
    from app import fund_lifecycle
    _need(request, user, MON)
    if not _finance_editor(user):
        deny(request, user)
    out = _run(fund_lifecycle.set_rule, user, key, body.model_dump())
    activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan", summary=f"Mengatur aturan pengajuan {key}",
                    details={"key": key, **body.model_dump()})
    return out


class LifecycleIn(BaseModel):
    blockOverdueProof: Optional[bool] = None
    minLeadDays: Optional[int] = Field(None, ge=0, le=60)


@router.put("/api/pengajuan/settings/lifecycle")
def set_lifecycle(body: LifecycleIn, request: Request, user: dict = Depends(current_user)) -> dict:
    from app import fund_lifecycle
    _need(request, user, MON)
    if not _finance_editor(user):
        deny(request, user)
    out = _run(fund_lifecycle.set_settings, body.model_dump(exclude_none=True))
    activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan", summary="Mengatur pengaturan pengajuan", details=out)
    return out


@router.put("/api/pengajuan/settings/flow/{kind}")
def set_flow(kind: str, body: FlowIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _superadmin(request, user)
    out = _run(pg.set_flow, user, kind, [s.model_dump() for s in body.steps])
    activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan",
                    summary=f"Mengatur approval {pg.KINDS.get(kind, kind)}", details={"kind": kind, "steps": [s.model_dump() for s in body.steps]})
    return {"flow": out}


@router.get("/api/pengajuan/settings/plafon-suggestions")
def plafon_suggestions(request: Request, factor: float = Query(1.2, ge=0.5, le=5), roundTo: int = Query(50_000, ge=1_000, le=1_000_000),
                       user: dict = Depends(current_user)) -> dict:
    """A plafon per outlet from its Simple Purchase in ESB (weekly average × factor, rounded up)."""
    if not _plafon_editor(user):
        deny(request, user)
    return {"weeks": pg.SP_WEEKS, "factor": factor, "roundTo": roundTo, "data": pg.plafon_suggestions(factor, roundTo)}


@router.post("/api/pengajuan/settings/plafon-suggestions")
def apply_plafon_suggestions(body: SuggestIn, request: Request, user: dict = Depends(current_user)) -> dict:
    if not _plafon_editor(user):
        deny(request, user)
    out = pg.apply_suggestions(user, body.codes, body.factor, body.roundTo)
    activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan",
                    summary=f"Plafon dari usulan Simple Purchase: {out['applied']} outlet", details=body.model_dump())
    return out


@router.put("/api/pengajuan/settings/plafon")
def set_plafon(body: PlafonIn, request: Request, user: dict = Depends(current_user)) -> dict:
    if not _plafon_editor(user):
        deny(request, user)
    out = _run(pg.set_plafon, user, body.branchCode, body.amount)
    activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan",
                    summary=f"Plafon petty cash {body.branchCode}: {body.amount}", details=body.model_dump())
    return out


# ---------------------------------------------------------------- the lists of the personal form (2026-11-20)

class OptionIn(BaseModel):
    list: str = Field(pattern="^(PURPOSE|TRANSACTION|PROJECT|ACCOUNT)$")
    name: str = Field(min_length=1, max_length=120)
    settings: Optional[dict] = None


class OptionPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    isActive: Optional[bool] = None
    sort: Optional[int] = Field(None, ge=0, le=100_000)
    settings: Optional[dict] = None


class DeptPlafonIn(BaseModel):
    department: str = Field(min_length=1, max_length=120)
    amount: Optional[float] = None


def _options_editor(user: dict) -> bool:
    return user.get("role") in ("superadmin", "admin")


def _need_options(request: Request, user: dict) -> None:
    _need(request, user, MON)
    if not _options_editor(user):
        deny(request, user)


@router.post("/api/pengajuan/settings/options", status_code=201)
def add_option(body: OptionIn, request: Request, user: dict = Depends(current_user)) -> dict:
    from app import fund_options
    _need_options(request, user)
    out = _run(fund_options.create, user, body.list, body.name, body.settings)
    activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan",
                    summary=f"Menambah {fund_options.LISTS[body.list]}: {out['name']}", details=out)
    return out


class OrderIn(BaseModel):
    list: str = Field(pattern="^(PURPOSE|TRANSACTION|PROJECT|ACCOUNT)$")
    ids: List[int] = Field(min_length=1, max_length=500)


@router.put("/api/pengajuan/settings/options/order")
def order_options(body: OrderIn, request: Request, user: dict = Depends(current_user)) -> dict:
    from app import fund_options
    _need_options(request, user)
    out = _run(fund_options.reorder, user, body.list, body.ids)
    activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan",
                    summary=f"Mengurutkan {fund_options.LISTS[body.list]}", details={"ids": body.ids})
    return {"data": out}


@router.patch("/api/pengajuan/settings/options/{option_id}")
def edit_option(option_id: int, body: OptionPatch, request: Request, user: dict = Depends(current_user)) -> dict:
    from app import fund_options
    _need_options(request, user)
    out = _run(fund_options.update, user, option_id, body.model_dump(exclude_unset=True))
    activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan",
                    summary=f"Mengubah {fund_options.LISTS[out['list']]}: {out['name']}{'' if out['isActive'] else ' (nonaktif)'}", details=out)
    return out


@router.delete("/api/pengajuan/settings/options/{option_id}")
def delete_option(option_id: int, request: Request, user: dict = Depends(current_user)) -> dict:
    """Deletes an option never used; a used one is deactivated instead (the old requests keep their label)."""
    from app import fund_options
    _need_options(request, user)
    out = _run(fund_options.delete, user, option_id)
    activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan",
                    summary=f"Menghapus {fund_options.LISTS[out['list']]}: {out['name']}", details=out)
    return {"deleted": out["id"]}


class PayerIn(BaseModel):
    payer: str = Field(min_length=1, max_length=80)


@router.put("/api/pengajuan/settings/units/{code}")
def set_unit_payer(code: str, body: PayerIn, request: Request, user: dict = Depends(current_user)) -> dict:
    """The PT that pays a unit (superadmins and Finance); requests already sent keep theirs."""
    _need(request, user, MON)
    if not _finance_editor(user):
        deny(request, user)
    out = _run(fund_units.set_payer, code, body.payer)
    activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan", summary=f"PT pembayar {code}: {body.payer}",
                    details={"unit": code, "payer": body.payer})
    return {"units": out}


class ReceiversIn(BaseModel):
    payer: str = Field(min_length=1, max_length=80)
    userIds: List[str] = Field(default_factory=list, max_length=50)


@router.put("/api/pengajuan/settings/receivers")
def set_receivers(body: ReceiversIn, request: Request, user: dict = Depends(current_user)) -> dict:
    """Who confirms the money a PT receives (Antar-PT)."""
    _superadmin(request, user)
    out = _run(fund_units.set_receivers, body.payer, body.userIds)
    activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan", summary=f"Penerima Antar-PT {body.payer}", details=body.model_dump())
    return {"receivers": out}


# ---------------------------------------------------------------- vendor payments in bulk (app/fund_vendor.py, migration 150)

class IdsIn(BaseModel):
    ids: List[str] = Field(min_length=1, max_length=200)
    note: Optional[str] = Field(None, max_length=2000)


@router.get("/api/pengajuan/vendor/template")
def vendor_template(request: Request, user: dict = Depends(current_user)) -> Response:
    from app import fund_vendor
    _need(request, user, FORM)
    return Response(fund_vendor.template(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="Template Pembayaran Vendor.xlsx"'})


@router.get("/api/pengajuan/vendor/drafts")
def vendor_drafts(request: Request, user: dict = Depends(current_user)) -> dict:
    from app import fund_vendor
    _need(request, user, FORM)
    return {"data": fund_vendor.drafts(user)}


@router.post("/api/pengajuan/vendor/import")
async def vendor_import(request: Request, unit: Optional[str] = Query(None, max_length=20), apply: bool = False, user: dict = Depends(current_user)) -> dict:
    from app import fund_vendor
    await run_in_threadpool(_need, request, user, FORM)
    data = await _body(request)
    out = await run_in_threadpool(_run, fund_vendor.import_rows, user, data, unit, apply)
    if apply:
        await run_in_threadpool(lambda: activity.record("pengajuan", user=user, request=request, page="/forms/pengajuan",
                                                        summary=f"Import pembayaran vendor: {out['created']} draf", details={"created": out["created"]}))
    return out


@router.post("/api/pengajuan/vendor/attach")
async def vendor_attach(request: Request, name: str = Query(max_length=160), ids: Optional[str] = Query(None, max_length=8000),
                        user: dict = Depends(current_user)) -> dict:
    from app import fund_vendor
    await run_in_threadpool(_need, request, user, FORM)
    data = await _body(request)
    return await run_in_threadpool(_run, fund_vendor.attach, user, data, name, [i for i in (ids or "").split(",") if i] or None)


@router.post("/api/pengajuan/vendor/send")
def vendor_send(body: IdsIn, request: Request, user: dict = Depends(current_user)) -> dict:
    from app import fund_vendor
    _need(request, user, FORM)

    def send(i: str) -> str:
        if _run(pg.own, user, i, True)["kind"] != "VENDOR":
            raise HTTPException(422, detail="Hanya pembayaran vendor")
        return _run(pg.submit, user, i)["number"]
    out = fund_vendor.each(body.ids, send)
    activity.record("pengajuan", user=user, request=request, page="/forms/pengajuan", summary=f"Mengirim {len(out['done'])} pembayaran vendor", details=out)
    return out


@router.post("/api/pengajuan/vendor/discard")
def vendor_discard(body: IdsIn, request: Request, user: dict = Depends(current_user)) -> dict:
    from app import fund_vendor
    _need(request, user, FORM)
    return fund_vendor.each(body.ids, lambda i: _run(pg.discard, user, i) or i)


@router.post("/api/pengajuan/vendor/history")
async def vendor_history(request: Request, user: dict = Depends(current_user)) -> dict:
    """The recap of what the Google Form already paid (Finance): its invoices are refused from now on."""
    from app import fund_vendor
    await run_in_threadpool(_need, request, user, MON)
    if not await run_in_threadpool(_finance_editor, user):
        await run_in_threadpool(deny, request, user)
    data = await _body(request)
    out = await run_in_threadpool(_run, fund_vendor.history, user, data)
    await run_in_threadpool(lambda: activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan",
                                                    summary=f"Riwayat bayar Google Form: {out['added']} invoice", details=out))
    return out


@router.post("/api/pengajuan/monitor/bulk/approve")
def bulk_approve(body: IdsIn, request: Request, user: dict = Depends(current_user)) -> dict:
    from app import fund_vendor
    _need(request, user, MON)

    def one(i: str) -> str:
        row = _team_row(request, user, i)
        if row["kind"] == "INTERCOMPANY":
            raise HTTPException(422, detail="Antar-PT disetujui satu per satu")
        if row["status"] != "SUBMITTED":
            raise HTTPException(422, detail="Bukan menunggu approval")
        twin = pg.similar(row)
        if twin:
            raise HTTPException(422, detail=f"Rekening dan nominal sama dengan {', '.join(twin)}: buka dan periksa dulu")
        _run(pg.change_status, user, i, "APPROVE", body.note)
        _record(request, user, f"{row['number']}: approve massal", i, to="APPROVE", note=body.note)
        return row["number"]
    return fund_vendor.each(body.ids, one)


@router.post("/api/pengajuan/monitor/bulk/pay")
async def bulk_pay(request: Request, ids: str = Query(max_length=8000), on: date = Query(...), name: str = Query("bukti-transfer", max_length=160),
                   user: dict = Depends(current_user)) -> dict:
    """One bank batch: the approved requests of one paying PT, paid in full on one day with the same proof."""
    from app import fund_vendor
    await run_in_threadpool(_need, request, user, MON)
    data = await _body(request)

    def run() -> dict:
        todo = [i for i in ids.split(",") if i]
        rows = {i: _team_row(request, user, i) for i in todo}
        if len({r["payer"] for r in rows.values()}) != 1:
            raise HTTPException(422, detail="Satu kali bayar massal hanya untuk satu PT pembayar")
        if on > today():
            raise HTTPException(422, detail="Tanggal transfer tidak boleh di masa depan")

        def one(i: str) -> str:
            row = rows[i]
            if row["kind"] == "INTERCOMPANY":
                raise HTTPException(422, detail="Antar-PT dibayar satu per satu")
            if row["status"] != "APPROVED" or not pg.can(user, row)["payment"]:
                raise HTTPException(422, detail="Bukan menunggu pembayaran Anda")
            before = {f["id"] for f in pg.files_of(i)}
            files = _run(pg.add_file, user, i, "PAYMENT", data, name, "TRANSFER", None)["files"]
            try:
                _run(pg.change_status, user, i, "PAID", None, {"amount": float(row["approved_amount"]), "on": on.isoformat()})
            except Exception:
                for f in files:
                    if f["id"] not in before:
                        pg.delete_file(i, f["id"], ("PAYMENT",))
                raise
            _record(request, user, f"{row['number']}: dibayar massal", i, to="PAID", on=on.isoformat())
            return row["number"]
        return fund_vendor.each(todo, one)
    return await run_in_threadpool(run)


class ReceivedIn(BaseModel):
    note: Optional[str] = Field(None, max_length=2000)


@router.post("/api/pengajuan/monitor/{pengajuan_id}/received")
def received(pengajuan_id: str, body: ReceivedIn, request: Request, user: dict = Depends(current_user)) -> dict:
    """Antar-PT: the receiving PT confirms the money."""
    row = _team_row(request, user, pengajuan_id)
    _run(pg.confirm_received, user, pengajuan_id, body.note)
    _record(request, user, f"{row['number']}: uang masuk dikonfirmasi {row['payee_pt']}", pengajuan_id, note=body.note)
    return _team_view(user, pengajuan_id)


class UserUnitsIn(BaseModel):
    units: List[str] = Field(default_factory=list, max_length=20)


@router.get("/api/pengajuan/user-units/{user_id}")
def user_units(user_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _superadmin(request, user)
    return {"units": fund_units.units(), "chosen": fund_units.chosen(user_id)}


@router.put("/api/pengajuan/user-units/{user_id}")
def set_user_units(user_id: str, body: UserUnitsIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _superadmin(request, user)
    out = _run(fund_units.set_chosen, user_id, body.units)
    activity.record("user", user=user, request=request, page="/users", summary=f"Unit Fund Request akun: {', '.join(out) or 'semua'}",
                    details={"userId": user_id, "units": out})
    return {"chosen": out}


@router.put("/api/pengajuan/settings/dept-plafon")
def set_dept_plafon(body: DeptPlafonIn, request: Request, user: dict = Depends(current_user)) -> dict:
    from app import fund_options
    _need_options(request, user)
    out = _run(fund_options.set_dept_plafon, user, body.department, body.amount)
    activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan",
                    summary=f"Plafon departemen {body.department}: {body.amount if body.amount else 'tanpa batas'}", details=body.model_dump())
    return {"deptPlafon": out}


class KindLabelIn(BaseModel):
    label: Optional[str] = Field(None, max_length=60)


@router.put("/api/pengajuan/settings/kind-label/{kind}")
def set_kind_label(kind: str, body: KindLabelIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need_options(request, user)
    out = _run(pg.set_kind_label, user, kind, body.label)
    activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan",
                    summary=f"Nama jenis {kind}: {body.label or '(default)'}", details={"kind": kind, "label": body.label})
    return {"kindLabels": out}
