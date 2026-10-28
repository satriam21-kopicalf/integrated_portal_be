"""Form → Pengajuan and Monitoring → Pengajuan (app/pengajuan.py, 2026-10-28).

Outlet (page form.pengajuan):
    GET    /api/pengajuan/form                      branches, baristas per branch, own pengajuan
    POST   /api/pengajuan/requests                  {"branchCode"} → the draft (an open draft of the branch is reused)
    GET    /api/pengajuan/requests/{id}
    PATCH  /api/pengajuan/requests/{id}             baristas, periodFrom, periodTo, openingBalance, openingNote, incomeAmount,
                                                    incomeNote, note, lines [{id?, spentOn, category, description, amount}]
    POST   /api/pengajuan/requests/{id}/files?kind=LINE|INCOME|OTHER&line=&docType=&name=   raw body (PDF / photo ≤ 10 MB)
    DELETE /api/pengajuan/requests/{id}/files/{file}
    POST   /api/pengajuan/requests/{id}/submit
    DELETE /api/pengajuan/requests/{id}             a draft
Team (page mon.pengajuan; the steps need mon.pengajuan.verify (Operations) or mon.pengajuan.finance (Finance)):
    GET    /api/pengajuan/monitor?dateFrom=&dateTo=&branch=&group=
    GET    /api/pengajuan/monitor/{id}
    POST   /api/pengajuan/monitor/{id}/status       {"to", "note", "paidAmount", "paidOn"}
    POST   /api/pengajuan/monitor/{id}/lines/{line} {"review": "OK"|"REJECTED", "note"}
    POST   /api/pengajuan/monitor/{id}/notes        {"note"}
    POST   /api/pengajuan/monitor/{id}/payment-files?docType=&name=   raw body (Finance, while waiting for payment)
    DELETE /api/pengajuan/monitor/{id}/payment-files/{file}
Both: GET /api/pengajuan/files/{file}
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import activity, branch_groups
from app import pengajuan as pg
from app.access import deny, has_page
from app.pic_checkin import CheckInError
from app.routes.auth import current_user
from app.scope import NO_BRANCH, allowed_branches, scoped_branch
from app.utils import parse_branches, today

router = APIRouter(tags=["pengajuan"])
FORM, MON, VERIFY, FINANCE = "form.pengajuan", "mon.pengajuan", "mon.pengajuan.verify", "mon.pengajuan.finance"


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


def perms_of(user: dict) -> set[str]:
    return {k for k, page in (("verify", VERIFY), ("finance", FINANCE)) if has_page(user, page)}


def _team_row(request: Request, user: dict, pengajuan_id: str) -> dict:
    _need(request, user, MON)
    row = pg.get_row(pengajuan_id)
    scope = allowed_branches()
    if row is None or row["status"] == "DRAFT" or (scope is not None and row["branch_code"] not in scope):
        raise HTTPException(404, detail="Pengajuan tidak ditemukan")
    return row


def _team_view(user: dict, pengajuan_id: str) -> dict:
    out = pg.full(pengajuan_id)
    perms = perms_of(user)
    out["can"] = {"next": pg.allowed_next(pg.get_row(pengajuan_id), perms), "reviewLines": pg.LINE_REVIEW.get(out["status"]) in perms,
                  "payment": out["status"] == "APPROVED" and "finance" in perms, "perms": sorted(perms)}
    return out


def _record(request: Request, user: dict, summary: str, row_id: str, **details) -> None:
    activity.record("pengajuan", user=user, request=request, page="/monitoring/pengajuan", summary=summary, details={"id": row_id, **details})


async def _body(request: Request) -> bytes:
    if int(request.headers.get("content-length") or 0) > pg.MAX_FILE_BYTES + 1024:
        raise HTTPException(422, detail=f"Lampiran maksimal {pg.MAX_FILE_BYTES // 1048576} MB")
    return await request.body()


# ---------------------------------------------------------------- outlet

class NewIn(BaseModel):
    branchCode: str = Field(min_length=1, max_length=40)


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


@router.get("/api/pengajuan/form")
def form(request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    return pg.form_state(user)


@router.post("/api/pengajuan/requests", status_code=201)
def new_request(body: NewIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    return _run(pg.create_draft, user, body.branchCode)


@router.get("/api/pengajuan/requests/{pengajuan_id}")
def get_request(pengajuan_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
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
    team = row["status"] != "DRAFT" and has_page(user, MON) and (scope is None or row["branch_code"] in scope)
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
            branch: Optional[str] = None, group: Optional[str] = Query(None, max_length=60), user: dict = Depends(current_user)) -> dict:
    _need(request, user, MON)
    end = dateTo or today()
    start = dateFrom or end - timedelta(days=89)
    if start > end or (end - start).days > 366:
        raise HTTPException(422, detail="Periode maksimal 1 tahun dan tanggal mulai sebelum tanggal akhir")
    scoped = scoped_branch(branch)
    branches = None if scoped is None else ([] if scoped == NO_BRANCH else parse_branches(scoped))
    branches = branch_groups.narrow(branches, group)
    return {"period": {"from": start.isoformat(), "to": end.isoformat()}, "perms": sorted(perms_of(user)), **pg.monitor(start, end, branches)}


@router.get("/api/pengajuan/monitor/{pengajuan_id}")
def team_get(pengajuan_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _team_row(request, user, pengajuan_id)
    return _team_view(user, pengajuan_id)


@router.post("/api/pengajuan/monitor/{pengajuan_id}/status")
def team_status(pengajuan_id: str, body: StatusIn, request: Request, user: dict = Depends(current_user)) -> dict:
    row = _team_row(request, user, pengajuan_id)
    _run(pg.change_status, user, pengajuan_id, body.to, body.note, perms_of(user), {"amount": body.paidAmount, "on": body.paidOn})
    _record(request, user, f"{row['number']}: {pg.STATUS_LABEL[row['status']]} → {pg.STATUS_LABEL.get(body.to, body.to)}", pengajuan_id,
            to=body.to, note=body.note)
    return _team_view(user, pengajuan_id)


@router.post("/api/pengajuan/monitor/{pengajuan_id}/lines/{line_id}")
def team_review_line(pengajuan_id: str, line_id: int, body: ReviewIn, request: Request, user: dict = Depends(current_user)) -> dict:
    row = _team_row(request, user, pengajuan_id)
    _run(pg.review_line, user, pengajuan_id, line_id, body.review, body.note, perms_of(user))
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
    if row["status"] != "APPROVED" or "finance" not in perms_of(user):
        raise HTTPException(422, detail="Bukti transfer diunggah oleh Finance saat pengajuan menunggu pembayaran")
    data = await _body(request)
    await run_in_threadpool(_run, pg.add_file, user, pengajuan_id, "PAYMENT", data, name, docType, None)
    return await run_in_threadpool(_team_view, user, pengajuan_id)


@router.delete("/api/pengajuan/monitor/{pengajuan_id}/payment-files/{file_id}")
def team_delete_payment_file(pengajuan_id: str, file_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    row = _team_row(request, user, pengajuan_id)
    if row["status"] != "APPROVED" or "finance" not in perms_of(user):
        raise HTTPException(422, detail="Bukti transfer hanya dapat diubah sebelum pembayaran dicatat")
    pg.delete_file(pengajuan_id, file_id, ("PAYMENT",))
    return _team_view(user, pengajuan_id)
