"""Social Media Analytics (page social.analytics, migration 135): what the public says about Kopi Calf, collected by
the portal itself (app/social.py).

    GET  /api/social/summary?dateFrom=&dateTo=        per platform, per day, the top authors, the last run of each source
    GET  /api/social/posts?platform=&sentiment=&q=&dateFrom=&dateTo=&limit=&offset=
    POST /api/social/run                              superadmin / admin: collect now (in the background)
"""
from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

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


@router.get("/posts")
def posts(platform: Optional[str] = None, sentiment: Optional[Literal["positive", "negative", "neutral"]] = None, q: str = "",
          date_from: Optional[date] = Query(None, alias="dateFrom"), date_to: Optional[date] = Query(None, alias="dateTo"),
          limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), _: dict = Depends(require_page(PAGE))):
    a, b = _period(date_from, date_to)
    return social.posts(platform, q, sentiment, a, b, limit, offset)


@router.post("/run")
def run_now(request: Request, user: dict = Depends(require_page(PAGE))):
    if user["role"] not in ("superadmin", "admin"):
        raise HTTPException(403, detail="Hanya superadmin / admin yang dapat menjalankan pengambilan data")
    social.run_now()
    activity.record("social.run", user=user, request=request, page="/social-media", summary="Social Media Analytics: ambil data sekarang")
    return {"started": True}
