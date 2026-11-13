"""Cost Control: Estimasi Belanja per outlet (page cc.purchase-plan), app/purchase_plan.py, migration 100.

    GET /api/cost-control/purchase-plan/buffers?branch=        the buffer of every outlet (cover days, loss %, safety %)
    PUT /api/cost-control/purchase-plan/buffers/{code}         {"coverDays"?, "lossPct"?, "safetyPct"?, "note"} — all
                                                               null = back to the recommendation
    GET /api/cost-control/purchase-plan/summary?branch=        the estimate in Rp of every outlet
    GET /api/cost-control/purchase-plan?branch=&cover=&buffer= the estimate of one outlet, per material
    GET /api/cost-control/purchase-plan/backtest?branch=       the plan of 4 weeks ago against what was then used
A role "user" given the page sees only its own branches (app/scope.py).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import activity, purchase_plan as pp
from app.access import require_page
from app.scope import NO_BRANCH, in_scope, scoped_branch
from app.utils import parse_branches

router = APIRouter(prefix="/api/cost-control/purchase-plan", tags=["cost-control"])
PAGE = "cc.purchase-plan"


def _branches(branch: Optional[str]) -> Optional[list[str]]:
    scoped = scoped_branch(branch)
    return None if scoped is None else ([] if scoped == NO_BRANCH else parse_branches(scoped))


def _one(branch: str) -> str:
    code = (branch or "").strip()
    if not code or "," in code or not in_scope(code):
        raise HTTPException(422, detail="Pilih satu outlet")
    return code


class BufferIn(BaseModel):
    coverDays: Optional[int] = Field(None, ge=1, le=31)
    lossPct: Optional[float] = Field(None, ge=0, le=100)
    safetyPct: Optional[float] = Field(None, ge=0, le=100)
    note: str = Field("", max_length=300)


@router.get("/buffers")
def buffers(branch: Optional[str] = None, _: dict = Depends(require_page(PAGE))) -> dict:
    return pp.recommendations(_branches(branch))


@router.put("/buffers/{code}")
def set_buffer(code: str, body: BufferIn, request: Request, user: dict = Depends(require_page(PAGE))) -> dict:
    code = _one(code)
    try:
        pp.save_setting(code, body.coverDays, body.lossPct, body.safetyPct, body.note, str(user["id"]))
    except pp.PlanError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    back = body.coverDays is None and body.lossPct is None and body.safetyPct is None
    activity.record("cc.purchase-plan", user=user, request=request, page="/cost-control/purchase-plan",
                    summary=f"Buffer {code}: " + ("kembali ke rekomendasi" if back else
                                                  f"{body.coverDays or '-'} hari, susut {body.lossPct if body.lossPct is not None else '-'} %, "
                                                  f"pengaman {body.safetyPct if body.safetyPct is not None else '-'} % — {body.note[:120]}"),
                    details={"branch": code, **body.model_dump()})
    rows = pp.recommendations([code])["data"]
    return rows[0] if rows else {}


@router.get("/summary")
def summary(branch: Optional[str] = None, _: dict = Depends(require_page(PAGE))) -> dict:
    return pp.summary(_branches(branch))


@router.get("/backtest")
def backtest(branch: str = Query(...), _: dict = Depends(require_page(PAGE))) -> dict:
    return pp.backtest(_one(branch))


@router.get("")
def plan(branch: str = Query(...), cover: Optional[int] = Query(None, ge=1, le=31), buffer: Optional[float] = Query(None, ge=0, le=100),
         _: dict = Depends(require_page(PAGE))) -> dict:
    try:
        return pp.plan(_one(branch), cover, buffer)
    except pp.PlanError as exc:
        raise HTTPException(422, detail=str(exc)) from None
