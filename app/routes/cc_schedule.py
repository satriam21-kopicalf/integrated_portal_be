"""Cost Control · Clock In → Schedule (page cc.schedule edits; Monitoring of Cost Control Clock In reads), app/cc_schedule.py.

    GET  /api/cost-control/schedule?dateFrom&dateTo     the plan with its status, clock ins outside it, the team
    PUT  /api/cost-control/schedule                     {userId, day, branches[], note}: the outlets of a member on a day
    POST /api/cost-control/schedule/copy-week           {from}: the 7 days from `from` copied to the week after
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import activity, cc_schedule
from app.access import require_page
from app.utils import today

router = APIRouter(prefix="/api/cost-control/schedule", tags=["cost-control"])
EDIT = "cc.schedule"


def _run(fn, *args):
    try:
        return fn(*args)
    except cc_schedule.ScheduleError as exc:
        raise HTTPException(422, detail=str(exc)) from None


@router.get("")
def week(dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None),
         _: dict = Depends(require_page(EDIT, "mon.surveyor-check-in"))) -> dict:
    start = dateFrom or today() - timedelta(days=today().weekday())
    end = dateTo or start + timedelta(days=6)
    return _run(cc_schedule.week, start, end)


class DayIn(BaseModel):
    userId: str = Field(min_length=1, max_length=64)
    day: date
    branches: list[str] = Field(default_factory=list, max_length=20)
    note: Optional[str] = Field(None, max_length=300)


@router.put("")
def set_day(body: DayIn, request: Request, user: dict = Depends(require_page(EDIT))) -> dict:
    out = _run(cc_schedule.set_day, body.userId, body.day, body.branches, body.note, str(user["id"]))
    activity.record("cc.schedule", user=user, request=request, page="/cost-control/schedule",
                    summary=f"Jadwal Cost Control {body.day:%d/%m/%Y}: {', '.join(out['branches']) or 'dikosongkan'}", details=out)
    return out


class CopyIn(BaseModel):
    from_: date = Field(alias="from")


@router.post("/copy-week")
def copy_week(body: CopyIn, request: Request, user: dict = Depends(require_page(EDIT))) -> dict:
    out = _run(cc_schedule.copy_week, body.from_, str(user["id"]))
    activity.record("cc.schedule", user=user, request=request, page="/cost-control/schedule",
                    summary=f"Salin jadwal minggu {body.from_:%d/%m/%Y} ke minggu berikutnya ({out['copied']} kunjungan)", details=out)
    return out
