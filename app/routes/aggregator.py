"""Aggregator Insight (migration 139, app/aggregator.py), stage 1.

    GET  /api/aggregator/status                 how fresh the BotFood data is, the counts            (agg.overview)
    GET  /api/aggregator/handle?date=           every outlet and who handles it that day, per slot    (agg.overview / agg.schedule)
    GET  /api/aggregator/members, /slots        the team (accounts with Aggregator Insight) and the slots of a day
    PUT  /api/aggregator/handle                 {memberId, branchCodes, slot, weekdays?, validFrom}   (agg.schedule)
    POST /api/aggregator/handle/remove          {branchCodes, slot, validFrom}
    PUT  /api/aggregator/handle/exception       {date, branchCode, slot, memberId?, note?, clear?}
    POST /api/aggregator/slots                  {code, label, sort}
    GET  /api/aggregator/branch-map, PUT /api/aggregator/branch-map/{bfBranchId} {branchCode}   BotFood outlet → ours
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app import aggregator
from app.access import require_page
from app.utils import today

router = APIRouter(prefix="/api/aggregator", tags=["aggregator insight"])
VIEW = require_page("agg.overview", "agg.schedule")
EDIT = require_page("agg.schedule")


@router.get("/status")
def status(_: dict = Depends(VIEW)):
    return aggregator.status()


@router.get("/handle")
def handle(day: Optional[date] = Query(None, alias="date"), _: dict = Depends(VIEW)):
    d = day or today()
    return {"date": d.isoformat(), "slots": aggregator.slots(), "outlets": aggregator.who_handles(d)}


@router.get("/members")
def members(_: dict = Depends(VIEW)):
    return aggregator.members()


@router.get("/slots")
def slots(_: dict = Depends(VIEW)):
    return aggregator.slots()


class Assign(BaseModel):
    member_id: str = Field(alias="memberId")
    branch_codes: list[str] = Field(alias="branchCodes", min_length=1)
    slot: str = "day"
    weekdays: Optional[list[int]] = None
    valid_from: date = Field(alias="validFrom")


@router.put("/handle")
def assign(body: Assign, user: dict = Depends(EDIT)):
    return {"saved": aggregator.assign(user, body.member_id, body.branch_codes, body.slot, body.weekdays, body.valid_from)}


class Remove(BaseModel):
    branch_codes: list[str] = Field(alias="branchCodes", min_length=1)
    slot: str = "day"
    valid_from: date = Field(alias="validFrom")


@router.post("/handle/remove")
def remove(body: Remove, user: dict = Depends(EDIT)):
    return {"removed": aggregator.unassign(user, body.branch_codes, body.slot, body.valid_from)}


class Exception_(BaseModel):
    day: date = Field(alias="date")
    branch_code: str = Field(alias="branchCode")
    slot: str = "day"
    member_id: Optional[str] = Field(None, alias="memberId")
    note: Optional[str] = Field(None, max_length=300)
    clear: bool = False


@router.put("/handle/exception")
def exception(body: Exception_, user: dict = Depends(EDIT)):
    aggregator.set_exception(user, body.day, body.branch_code, body.slot, body.member_id, body.note, body.clear)
    return {"ok": True}


class Slot(BaseModel):
    code: str = Field(pattern=r"^[a-z0-9-]{1,24}$")
    label: str = Field(min_length=1, max_length=60)
    sort: int = 0


@router.post("/slots")
def add_slot(body: Slot, _: dict = Depends(EDIT)):
    aggregator.add_slot(body.code, body.label, body.sort)
    return aggregator.slots()


@router.get("/branch-map")
def branch_map(_: dict = Depends(VIEW)):
    return {"map": aggregator.branch_map(), "outlets": aggregator.outlets()}


class Map(BaseModel):
    branch_code: Optional[str] = Field(None, alias="branchCode")


@router.put("/branch-map/{bf_branch_id}")
def set_map(bf_branch_id: int, body: Map, user: dict = Depends(EDIT)):
    aggregator.set_branch(user, bf_branch_id, body.branch_code)
    return {"ok": True}
