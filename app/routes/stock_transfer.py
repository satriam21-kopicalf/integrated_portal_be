"""Monitoring Stock and Form Transfer Stock (TITO, app/stock_transfer.py, migration 075).

Form (page form.stock-transfer: the PICs and the outlet accounts, portal and Operations)
    GET  /api/stock-transfer/form                    my outlets, every outlet, my requests, the requests to my outlets
    GET  /api/stock-transfer/available?branch=       the materials an outlet has available (latest SO − pending out)
    POST /api/stock-transfer                         {toBranch, fromBranch, lines: [{code, qty}], note, photo, signature}
    POST /api/stock-transfer/{id}/answer             the source outlet: {accept, note}
    POST /api/stock-transfer/{id}/receive            the requester, once approved: {photo (required), note}
    POST /api/stock-transfer/{id}/cancel             the requester, while the source has not answered
    GET  /api/stock-transfer/{id}                    one transfer (either side, or Monitoring Stock)
    GET  /api/stock-transfer/{id}/file/{kind}        photo | signature | receive
Monitoring Stock (pages cc.stock-list, cc.stock-transfer)
    GET  /api/monitoring/stock/list?branch=          Stock List: every branch × item of the latest SO, with the transfers
    GET  /api/monitoring/stock/transfers             ?dateFrom&dateTo&branch&status -> the transfers and their figures
    GET  /api/monitoring/stock/transfers/lines?ids=  the lines of transfers (Excel per material)
    POST /api/monitoring/stock/transfers/{id}/decide {approve, note, qty: {code: qty}} (approvers only; qty adjusts)
    POST /api/monitoring/stock/transfers/{id}/label  {label: esb | portal | null, note}: to be entered in ESB or not
    GET  /api/monitoring/stock/settings              the approvers and the Cost Control accounts
    PUT  /api/monitoring/stock/settings              {"approvers": [user id]}
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import activity, stock_transfer as st
from app.access import deny, has_page, require_page
from app.routes.auth import current_user
from app.scope import NO_BRANCH, scoped_branch
from app.thumbs import image_response
from app.utils import parse_branches, today

router = APIRouter(tags=["stock transfer"])
FORM, LIST, TRANSFER = "form.stock-transfer", "cc.stock-list", "cc.stock-transfer"


class LineIn(BaseModel):
    code: str = Field(min_length=1, max_length=80)
    qty: float = Field(gt=0, le=10_000_000)


class RequestIn(BaseModel):
    toBranch: str = Field(min_length=1, max_length=40)
    fromBranch: str = Field(min_length=1, max_length=40)
    lines: list[LineIn] = Field(min_length=1, max_length=st.MAX_LINES)
    note: Optional[str] = Field(None, max_length=500)
    photo: str = Field(min_length=20, max_length=5_000_000)
    signature: str = Field(min_length=20, max_length=2_000_000)


class AnswerIn(BaseModel):
    accept: bool
    note: Optional[str] = Field(None, max_length=500)


class DecideIn(BaseModel):
    approve: bool
    note: Optional[str] = Field(None, max_length=500)
    qty: Optional[dict[str, float]] = None  # {item code: quantity} adjusted by Cost Control when approving (migration 076)


class ReceiveIn(BaseModel):
    photo: Optional[str] = Field(None, max_length=5_000_000)
    note: Optional[str] = Field(None, max_length=500)


class LabelIn(BaseModel):
    label: Optional[str] = None
    note: Optional[str] = Field(None, max_length=300)


class SettingsIn(BaseModel):
    approvers: list[str] = Field(max_length=200)


def _run(f, *args):
    try:
        return f(*args)
    except st.TransferError as exc:
        raise HTTPException(422, detail=str(exc)) from None


def _log(request: Request, user: dict, action: str, t: dict, what: str) -> None:
    activity.record(f"stock_transfer.{action}", user=user, request=request, page="/forms/stock-transfer",
                    summary=f"Transfer Stock {t['number']} ({t['fromName']} → {t['toName']}): {what}",
                    details={"id": t["id"], "number": t["number"], "status": t["status"]})


# ---------------------------------------------------------------- form

@router.get("/api/stock-transfer/form")
def form(user: dict = Depends(require_page(FORM))) -> dict:
    return st.form(user)


@router.get("/api/stock-transfer/available")
def available(branch: str = Query(..., min_length=1, max_length=40), _: dict = Depends(require_page(FORM))) -> dict:
    return st.available_of(branch)


@router.post("/api/stock-transfer", status_code=201)
def create(body: RequestIn, request: Request, user: dict = Depends(require_page(FORM))) -> dict:
    t = _run(st.create, user, body.model_dump())
    _log(request, user, "request", t, f"diajukan, {len(t['items'])} material")
    return t


@router.post("/api/stock-transfer/{transfer_id}/answer")
def answer(transfer_id: str, body: AnswerIn, request: Request, user: dict = Depends(require_page(FORM))) -> dict:
    t = _run(st.answer, user, transfer_id, body.accept, body.note)
    _log(request, user, "answer", t, "disetujui outlet tujuan" if body.accept else "ditolak outlet tujuan")
    return t


@router.post("/api/stock-transfer/{transfer_id}/receive")
def receive(transfer_id: str, body: ReceiveIn, request: Request, user: dict = Depends(require_page(FORM))) -> dict:
    t = _run(st.receive, user, transfer_id, body.photo, body.note)
    _log(request, user, "receive", t, f"diterima, nilai Rp{t['value'] or 0:,.0f}")
    return t


@router.post("/api/stock-transfer/{transfer_id}/cancel")
def cancel(transfer_id: str, request: Request, user: dict = Depends(require_page(FORM))) -> dict:
    t = _run(st.cancel, user, transfer_id)
    _log(request, user, "cancel", t, "dibatalkan")
    return t


def _may_see(user: dict, t: dict) -> bool:
    if has_page(user, TRANSFER) or has_page(user, LIST):
        return True
    mine = {b["code"] for b in st.branch_options(user)}
    return t["to_branch"] in mine or t["from_branch"] in mine


@router.get("/api/stock-transfer/{transfer_id}")
def detail(transfer_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    t = _run(st.detail, transfer_id)
    if not _may_see(user, {"to_branch": t["toBranch"], "from_branch": t["fromBranch"]}):
        deny(request, user)
    return {**t, "canDecide": st.is_approver(user)}


@router.get("/api/stock-transfer/{transfer_id}/file/{kind}")
def file(transfer_id: str, kind: str, request: Request, w: Optional[int] = Query(None, ge=48, le=1600),
         user: dict = Depends(current_user)) -> Response:
    if kind not in ("photo", "signature", "receive"):
        raise HTTPException(404, detail="Berkas tidak ditemukan")
    row = st.file_of(transfer_id, kind)
    if not row or row["data"] is None:
        raise HTTPException(404, detail="Berkas tidak ditemukan")
    if not _may_see(user, row):
        deny(request, user)
    return image_response(row["data"], row["type"], w, "private, max-age=86400")


# ---------------------------------------------------------------- monitoring stock

def _branches(branch: Optional[str]) -> Optional[list[str]]:
    scoped = scoped_branch(branch)
    return None if scoped is None else ([] if scoped == NO_BRANCH else parse_branches(scoped))


@router.get("/api/monitoring/stock/list")
def stock_list(branch: Optional[str] = None, _: dict = Depends(require_page(LIST))) -> dict:
    return st.stock_list(_branches(branch))


@router.get("/api/monitoring/stock/transfers")
def transfers(dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None), branch: Optional[str] = None,
              status: str = Query("", max_length=20), user: dict = Depends(require_page(TRANSFER))) -> dict:
    end = dateTo or today()
    start = dateFrom or end - timedelta(days=29)
    if start > end:
        raise HTTPException(422, detail="Tanggal mulai harus sebelum tanggal akhir")
    if (end - start).days > 366:
        raise HTTPException(422, detail="Maksimal 1 tahun sekali tampil")
    return {"period": {"from": start.isoformat(), "to": end.isoformat()}, "canDecide": st.is_approver(user),
            **st.listing(start, end, _branches(branch), status)}


@router.get("/api/monitoring/stock/transfers/lines")
def transfer_lines(ids: str = Query(..., max_length=40_000), _: dict = Depends(require_page(TRANSFER))) -> dict:
    return {"data": st.lines_of([i for i in ids.split(",") if i][:1000])}


@router.post("/api/monitoring/stock/transfers/{transfer_id}/decide")
def decide(transfer_id: str, body: DecideIn, request: Request, user: dict = Depends(require_page(TRANSFER))) -> dict:
    t = _run(st.decide, user, transfer_id, body.approve, body.note, body.qty)
    _log(request, user, "decide", t, "di-approve Cost Control" if body.approve else "ditolak Cost Control")
    return t


@router.post("/api/monitoring/stock/transfers/{transfer_id}/label")
def label(transfer_id: str, body: LabelIn, request: Request, user: dict = Depends(require_page(TRANSFER))) -> dict:
    t = _run(st.label, user, transfer_id, body.label, body.note)
    _log(request, user, "label", t, {"esb": "dinaikkan ke ESB", "portal": "hanya di portal"}.get(body.label or "", "label dihapus"))
    return t


@router.get("/api/monitoring/stock/settings")
def settings(user: dict = Depends(require_page(TRANSFER))) -> dict:
    return {"approvers": st.approvers(), "candidates": st.candidates(), "canEdit": user.get("role") in ("superadmin", "cost_control")}


@router.put("/api/monitoring/stock/settings")
def save_settings(body: SettingsIn, request: Request, user: dict = Depends(require_page(TRANSFER))) -> dict:
    if user.get("role") not in ("superadmin", "cost_control"):
        raise HTTPException(403, detail="Hanya tim Cost Control yang bisa mengatur approver")
    out = _run(st.set_approvers, user, body.approvers)
    activity.record("stock_transfer.settings", user=user, request=request, page="/cost-control/stock-transfer",
                    summary=f"Approver Transfer Stock: {', '.join(a['name'] for a in out) or '—'}", details={"approvers": [a["id"] for a in out]})
    return {"approvers": out}
