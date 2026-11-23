"""Social Media Analytics (page social.analytics, migration 135): what the public says about Kopi Calf, collected by
the portal itself (app/social.py).

    GET  /api/social/summary?dateFrom=&dateTo=        per platform, per day, the top authors, the last run of each source
    GET  /api/social/posts?platform=&sentiment=&q=&dateFrom=&dateTo=&limit=&offset=
    GET  /api/social/post?platform=&id=               one mention in full (the preview)
    GET  /api/social/overview?dateFrom=&dateTo=       the analysis (Overview page), from 1 January by default
    POST /api/social/run                              superadmin / admin: collect now {source?, backfillFrom?}
    GET  /api/social/schedule, PUT /api/social/schedule/{source} {enabled, everyHours}   the schedule (migration 136)
"""
from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import activity, social
from app.access import require_page
from app.utils import today

router = APIRouter(prefix="/api/social", tags=["social media analytics"])
PAGE = "social.analytics"


def _period(d_from: Optional[date], d_to: Optional[date]) -> tuple[date, date]:
    b = d_to or today()
    return d_from or b - timedelta(days=29), b


@router.get("/summary")
def summary(date_from: Optional[date] = Query(None, alias="dateFrom"), date_to: Optional[date] = Query(None, alias="dateTo"),
            _: dict = Depends(require_page(PAGE))):
    return social.summary(*_period(date_from, date_to))


@router.get("/overview")
def overview(date_from: Optional[date] = Query(None, alias="dateFrom"), date_to: Optional[date] = Query(None, alias="dateTo"),
             _: dict = Depends(require_page(PAGE))):
    """The Overview page: from 1 January of the year by default."""
    b = date_to or today()
    return social.overview(date_from or b.replace(month=1, day=1), b)


@router.get("/insights")
def insights(date_from: Optional[date] = Query(None, alias="dateFrom"), date_to: Optional[date] = Query(None, alias="dateTo"),
             _: dict = Depends(require_page(PAGE))):
    """The Overview after a media-monitoring dashboard: sentiment vs the period before, channels, tone per keyword and per
    source, words, cities, keyword x week, trending posts. The last 30 days by default."""
    return social.insights(*_period(date_from, date_to))


@router.get("/posts")
def posts(platform: Optional[str] = None, sentiment: Optional[Literal["positive", "negative", "neutral"]] = None, q: str = "",
          date_from: Optional[date] = Query(None, alias="dateFrom"), date_to: Optional[date] = Query(None, alias="dateTo"),
          limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), keyword: Optional[str] = None,
          who: Optional[str] = None, kind: Optional[Literal["post"]] = None, _: dict = Depends(require_page(PAGE))):
    a, b = _period(date_from, date_to)
    return social.posts(platform, q, sentiment, a, b, limit, offset, keyword, who, kind)


@router.get("/keywords")
def keywords(_: dict = Depends(require_page(PAGE))):
    """The keywords searched and labelled (migration 137)."""
    return {"data": social.KEYWORDS}


@router.get("/post")
def post(platform: str, id: str, _: dict = Depends(require_page(PAGE))):
    """One mention in full (the preview drawer of the page)."""
    out = social.post(platform, id)
    if not out:
        raise HTTPException(404, detail="Data tidak ditemukan")
    return out


def _admin(user: dict) -> None:
    if user["role"] not in ("superadmin", "admin"):
        raise HTTPException(403, detail="Hanya superadmin / admin yang dapat mengatur pengambilan data")


class RunIn(BaseModel):
    source: Optional[Literal["youtube", "news", "tiktok"]] = None
    backfillFrom: Optional[date] = None


@router.post("/run")
def run_now(request: Request, body: Optional[RunIn] = None, user: dict = Depends(require_page(PAGE))):
    """Collect now (every source, or one); with backfillFrom the run reaches back to that date."""
    _admin(user)
    b = body or RunIn()
    social.run_now(b.source, b.backfillFrom)
    activity.record("social.run", user=user, request=request, page="/social-media",
                    summary=f"Social Media Analytics: ambil data {b.source or 'semua sumber'}" + (f" sejak {b.backfillFrom}" if b.backfillFrom else ""))
    return {"started": True}


@router.get("/schedule")
def get_schedule(_: dict = Depends(require_page(PAGE))):
    return {"data": social.schedule()}


class ScheduleIn(BaseModel):
    enabled: bool
    everyHours: float = Field(ge=1, le=168)


@router.put("/schedule/{source}")
def put_schedule(source: Literal["youtube", "news", "tiktok"], body: ScheduleIn, request: Request, user: dict = Depends(require_page(PAGE))):
    _admin(user)
    out = social.set_schedule(user, source, body.enabled, body.everyHours)
    activity.record("social.schedule", user=user, request=request, page="/social-media",
                    summary=f"Social Media Analytics: jadwal {source} {'aktif' if body.enabled else 'mati'}, tiap {body.everyHours:g} jam")
    return {"data": out}
