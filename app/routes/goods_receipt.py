"""Cost Control: Good Receipt (page cc.goods-receipt), app/goods_receipt.py, migration 072.

    GET    /api/cost-control/goods-receipts?dateFrom&dateTo&branch&search&supplier&corrected   the GRs of the period
    GET    /api/cost-control/goods-receipts/{doc}                     lines (ESB and corrected), corrections, evidence
    POST   /api/cost-control/goods-receipts/{doc}/corrections         {"date"?, "lines"?: [{lineNo, qty}], "reason", "files"}
    DELETE /api/cost-control/goods-receipts/{doc}/corrections/{id}    cancel a correction (back to ESB's figure)
    GET    /api/cost-control/goods-receipts/{doc}/evidence/{id}       an evidence file
A role "user" given the page sees only its own branches (app/scope.py).
"""
from datetime import date, timedelta
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app import activity, goods_receipt as gr
from app.access import require_page
from app.scope import NO_BRANCH, allowed_branches, scoped_branch
from app.utils import parse_branches, today

router = APIRouter(prefix="/api/cost-control/goods-receipts", tags=["cost-control"])
PAGE = "cc.goods-receipt"


def _scope() -> Optional[list[str]]:
    allowed = allowed_branches()
    return None if allowed is None else list(allowed)


class LineIn(BaseModel):
    lineNo: int = Field(ge=1)
    qty: float = Field(ge=0, le=1_000_000_000)


class FileIn(BaseModel):
    name: str = Field("", max_length=200)
    data: str = Field(min_length=10, max_length=7_500_000)


class CorrectionIn(BaseModel):
    date: Optional[date] = None
    lines: list[LineIn] = Field(default_factory=list, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    files: list[FileIn] = Field(default_factory=list, max_length=gr.MAX_FILES)


@router.get("")
def documents(dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None), branch: Optional[str] = None,
              search: str = "", supplier: str = "", corrected: bool = False, _: dict = Depends(require_page(PAGE))) -> dict:
    end = dateTo or today()
    start = dateFrom or end - timedelta(days=6)
    if start > end or (end - start).days > 92:
        raise HTTPException(422, detail="Periode 1 hari sampai 3 bulan")
    scoped = scoped_branch(branch)
    branches = None if scoped is None else ([] if scoped == NO_BRANCH else parse_branches(scoped))
    return gr.documents(start, end, branches, search[:60], supplier[:120], corrected)


@router.get("/{doc_num}")
def detail(doc_num: str, _: dict = Depends(require_page(PAGE))) -> dict:
    out = gr.detail(doc_num, _scope())
    if out is None:
        raise HTTPException(404, detail="GR tidak ditemukan")
    return out


@router.post("/{doc_num}/corrections")
def correct(doc_num: str, body: CorrectionIn, request: Request, user: dict = Depends(require_page(PAGE))) -> dict:
    try:
        out = gr.correct(doc_num, body.model_dump(mode="json"), user, _scope())
    except LookupError:
        raise HTTPException(404, detail="GR tidak ditemukan") from None
    except gr.GrError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    activity.record("cc.goods-receipt", user=user, request=request, page="/cost-control/goods-receipt",
                    summary=f"Koreksi GR {doc_num}: {body.reason[:120]}",
                    details={"doc": doc_num, "date": body.date.isoformat() if body.date else None,
                             "lines": [ln.model_dump() for ln in body.lines][:50], "files": len(body.files)})
    return out


@router.delete("/{doc_num}/corrections/{correction_id}")
def cancel(doc_num: str, correction_id: int, request: Request, user: dict = Depends(require_page(PAGE))) -> dict:
    try:
        out = gr.cancel(doc_num, correction_id, user, _scope())
    except LookupError:
        raise HTTPException(404, detail="GR tidak ditemukan") from None
    except gr.GrError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    activity.record("cc.goods-receipt", user=user, request=request, page="/cost-control/goods-receipt",
                    summary=f"Koreksi GR {doc_num} dibatalkan", details={"doc": doc_num, "correction": correction_id})
    return out


class FinishIn(BaseModel):
    note: Optional[str] = Field(None, max_length=300)


@router.post("/{doc_num}/finish")
def finish(doc_num: str, body: FinishIn, request: Request, user: dict = Depends(require_page(PAGE))) -> dict:
    """The corrections of the GR are done: they count in the Stock List and the COGS from now on (migration 077)."""
    try:
        out = gr.finish(doc_num, body.note, user, _scope())
    except gr.GrError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    activity.record("cc.goods-receipt", user=user, request=request, page="/cost-control/goods-receipt",
                    summary=f"GR {doc_num} ditandai Finished", details={"doc": doc_num, "note": body.note})
    return out


@router.post("/{doc_num}/reopen")
def reopen(doc_num: str, request: Request, user: dict = Depends(require_page(PAGE))) -> dict:
    try:
        out = gr.reopen(doc_num, user, _scope())
    except gr.GrError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    activity.record("cc.goods-receipt", user=user, request=request, page="/cost-control/goods-receipt",
                    summary=f"GR {doc_num} dibuka lagi (draft)", details={"doc": doc_num})
    return out


@router.get("/{doc_num}/evidence/{file_id}")
def evidence(doc_num: str, file_id: int, _: dict = Depends(require_page(PAGE))) -> Response:
    f = gr.evidence(doc_num, file_id, _scope())
    if f is None:
        raise HTTPException(404, detail="File tidak ditemukan")
    return Response(bytes(f["data"]), media_type=f["content_type"],
                    headers={"content-disposition": f"inline; filename*=UTF-8''{quote(f['file_name'])}",
                             "cache-control": "private, max-age=3600"})
