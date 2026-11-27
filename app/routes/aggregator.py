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
from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import activity, aggregator, aggregator_reports
from app import aggregator_quality as quality
from app.access import require_page
from app.utils import today

router = APIRouter(prefix="/api/aggregator", tags=["aggregator insight"])
VIEW = require_page("agg.overview", "agg.schedule", "agg.opening", "agg.oos", "agg.reviews", "agg.complaints", "agg.void", "agg.menu", "agg.reports")
REVIEWS = require_page("agg.reviews")
COMPLAINTS = require_page("agg.complaints")
TICKET = require_page("agg.complaints", "agg.reviews")
VOID = require_page("agg.void")
MENU = require_page("agg.menu")
REPORTS = require_page("agg.reports")
OVERVIEW = require_page("agg.overview")
EDIT = require_page("agg.schedule")
OPENING = require_page("agg.opening")
OOS = require_page("agg.oos")
PLATFORM = Optional[str]


def _range(d_from: Optional[date], d_to: Optional[date], days: int = 6) -> tuple[date, date]:
    b = d_to or today()
    a = d_from or b - timedelta(days=days)
    if (b - a).days > 92:
        raise HTTPException(400, "Periode maksimal 93 hari")
    return a, b


@router.get("/context")
def context(_: dict = Depends(VIEW)):
    """What the filters of every page need (2026-11-25): the outlets with their branch group and zone, and how fresh the data is."""
    from app.pic_checkin import groups_of
    names = quality._branch_names()
    groups, ahead = groups_of(list(names)), aggregator_reports.wib_shift()
    st = aggregator.status()
    read = aggregator.last_read()
    done = lambda k: read.get(k)  # noqa: E731
    return {"outlets": [{"code": k, "name": v, "group": groups.get(k), "zone": {0: "WIB", 60: "WITA", 120: "WIT"}[ahead.get(k, 0)]} for k, v in names.items()],
            "groups": sorted(set(groups.values())), "unmapped": st["unmapped"], "reviewsFrom": st["reviewsFrom"],
            "freshness": {"synced": st["syncedMedian"], "oldest": st["syncedOldest"], "global": done("global"), "histories": done("histories"),
                          "reviews": done("reviews"), "daily": done("daily"), "errors": [k for k, v in st["pulls"].items() if v["error"]]}}


@router.get("/status-now")
def status_now(_: dict = Depends(OPENING)):
    """Every outlet x platform now: OPEN / HOLD (closed inside its hours) / CLOSE (outside them), since when (2026-11-27)."""
    return aggregator_reports.now_status()


@router.get("/opening")
def opening(date_from: Optional[date] = Query(None, alias="dateFrom"), date_to: Optional[date] = Query(None, alias="dateTo"),
            platform: PLATFORM = None, _: dict = Depends(OPENING)):
    """Opening & Closing per outlet, platform and day (according to BotFood, with the grace period)."""
    return aggregator_reports.opening(*_range(date_from, date_to), platform)


class Grace(BaseModel):
    minutes: int = Field(ge=0, le=180)


@router.put("/opening/grace")
def set_grace(body: Grace, _: dict = Depends(OPENING)):
    aggregator_reports.set_grace(body.minutes)
    return {"grace": body.minutes}


@router.get("/oos")
def oos(date_from: Optional[date] = Query(None, alias="dateFrom"), date_to: Optional[date] = Query(None, alias="dateTo"),
        platform: PLATFORM = None, packaging: bool = False, stale_days: int = Query(14, alias="staleDays", ge=1, le=365),
        _: dict = Depends(OOS)):
    """Out of Stock: out now, and how often / how long each item went out in the period."""
    return aggregator_reports.oos(*_range(date_from, date_to), platform, packaging, stale_days)


def _ask(fn, *args):
    try:
        return fn(*args)
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from None
    except ValueError as e:  # QualityError, a date that cannot be read
        raise HTTPException(422, detail=str(e)) from None


# ---------------------------------------------------------------- stage 3 (app/aggregator_quality.py, migration 151)

@router.get("/reviews")
def reviews(date_from: Optional[date] = Query(None, alias="dateFrom"), date_to: Optional[date] = Query(None, alias="dateTo"),
            platform: PLATFORM = None, _: dict = Depends(REVIEWS)):
    return quality.reviews(*_range(date_from, date_to), platform)


@router.get("/complaints")
def complaints(date_from: Optional[date] = Query(None, alias="dateFrom"), date_to: Optional[date] = Query(None, alias="dateTo"),
               platform: PLATFORM = None, _: dict = Depends(COMPLAINTS)):
    return quality.complaints(*_range(date_from, date_to), platform)


class ComplaintIn(BaseModel):
    reviewId: Optional[str] = Field(None, max_length=30)
    branchCode: Optional[str] = Field(None, max_length=40)
    platform: Optional[str] = Field(None, max_length=20)
    orderNo: Optional[str] = Field(None, max_length=60)
    day: Optional[str] = Field(None, max_length=10)
    source: Optional[str] = Field(None, max_length=10)
    category: Optional[str] = Field(None, max_length=60)
    detail: Optional[str] = Field(None, max_length=2000)


@router.post("/complaints", status_code=201)
def add_complaint(body: ComplaintIn, request: Request, user: dict = Depends(TICKET)):
    out = _ask(quality.create_complaint, user, body.model_dump())
    activity.record("aggregator", user=user, request=request, page="/aggregator-insight/complaints", summary=f"Komplain #{out['id']}", details=body.model_dump())
    return out


class ComplaintPatch(BaseModel):
    status: Optional[str] = Field(None, max_length=10)
    category: Optional[str] = Field(None, max_length=60)
    resolution: Optional[str] = Field(None, max_length=2000)
    compensation: Optional[float] = None
    # migration 167: the details of the handling
    action: Optional[str] = Field(None, max_length=20)
    rootCause: Optional[str] = Field(None, max_length=20)
    contacted: Optional[bool] = None
    contactVia: Optional[str] = Field(None, max_length=20)
    replyText: Optional[str] = Field(None, max_length=2000)
    compBearer: Optional[str] = Field(None, max_length=20)
    esbBill: Optional[str] = Field(None, max_length=60)


@router.patch("/complaints/{complaint_id}")
def edit_complaint(complaint_id: int, body: ComplaintPatch, request: Request, user: dict = Depends(COMPLAINTS)):
    out = _ask(quality.update_complaint, user, complaint_id, body.model_dump(exclude_unset=True))
    activity.record("aggregator", user=user, request=request, page="/aggregator-insight/complaints", summary=f"Komplain #{complaint_id}: {out['status']}", details=body.model_dump(exclude_unset=True))
    return out


@router.get("/voids")
def voids(date_from: Optional[date] = Query(None, alias="dateFrom"), date_to: Optional[date] = Query(None, alias="dateTo"),
          platform: PLATFORM = None, _: dict = Depends(VOID)):
    return quality.voids(*_range(date_from, date_to), platform)


class VoidReason(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=500)
    reason: str = Field(max_length=60)
    note: Optional[str] = Field(None, max_length=500)


@router.put("/voids/reason")
def void_reason(body: VoidReason, request: Request, user: dict = Depends(VOID)):
    n = _ask(quality.set_void_reason, user, body.keys, body.reason, body.note)
    activity.record("aggregator", user=user, request=request, page="/aggregator-insight/void", summary=f"Alasan {n} void: {body.reason}", details=body.model_dump())
    return {"saved": n}


class VoidIn(BaseModel):
    branchCode: str = Field(max_length=40)
    platform: str = Field(max_length=20)
    orderNo: str = Field(max_length=60)
    day: str = Field(max_length=10)
    amount: float = 0
    reason: str = Field(max_length=60)
    note: Optional[str] = Field(None, max_length=500)


@router.post("/voids", status_code=201)
def add_void(body: VoidIn, request: Request, user: dict = Depends(VOID)):
    out = _ask(quality.add_void, user, body.model_dump())
    activity.record("aggregator", user=user, request=request, page="/aggregator-insight/void", summary=f"Void manual {body.orderNo}", details=body.model_dump())
    return {"id": out}


@router.get("/menu")
def menu(platform: PLATFORM = None, _: dict = Depends(MENU)):
    return quality.menu(platform)


@router.get("/menu/fit")
def menu_fit(platform: PLATFORM = None, branch: Optional[str] = Query(None, max_length=40), _: dict = Depends(MENU)):
    return quality.fit(platform, branch) | {"outlets": [{"code": k, "name": v} for k, v in quality._branch_names().items()]}


@router.get("/report")
def report(date_from: Optional[date] = Query(None, alias="dateFrom"), date_to: Optional[date] = Query(None, alias="dateTo"),
           platform: PLATFORM = None, _: dict = Depends(REPORTS)):
    return quality.report(*_range(date_from, date_to), platform)


@router.get("/brief")
def brief(_: dict = Depends(OVERVIEW)):
    """The last 7 days for the Overview page."""
    z = today()
    return quality.brief(z - timedelta(days=6), z)


@router.get("/hours")
def hours(_: dict = Depends(OPENING)):
    return {"data": quality.hours_list()}


class HourRow(BaseModel):
    code: str = Field(max_length=40)
    platform: Literal["GrabFood", "GoFood", "ShopeeFood"]
    status: Literal["ok", "diff"]
    open: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    close: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    validFrom: Optional[date] = None


class HoursIn(BaseModel):
    rows: list[HourRow] = Field(min_length=1, max_length=400)


@router.put("/hours")
def check_hours(body: HoursIn, request: Request, user: dict = Depends(OPENING)):
    _ask(quality.check_hours, user, [r.model_dump() for r in body.rows])
    activity.record("aggregator", user=user, request=request, page="/aggregator-insight/opening", summary=f"Cek jam aplikasi merchant: {len(body.rows)}",
                    details={"rows": [r.model_dump(mode="json") for r in body.rows][:50]})
    return {"data": quality.hours_list()}


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



# ---------------------------------------------------------------- a ticket: details, evidence, history (migration 167)

@router.get("/complaints/{complaint_id}")
def complaint_get(complaint_id: int, _: dict = Depends(TICKET)):
    return _ask(quality.complaint, complaint_id)


@router.post("/complaints/{complaint_id}/evidence", status_code=201)
async def complaint_evidence(complaint_id: int, request: Request, kind: str = Query(..., max_length=10), name: Optional[str] = Query(None, max_length=200),
                             note: Optional[str] = Query(None, max_length=500), user: dict = Depends(TICKET)):
    """Raw body: the file (image or PDF, 10 MB at most)."""
    data = await request.body()
    out = _ask(quality.add_evidence, user, complaint_id, kind, data, name, note)
    activity.record("aggregator", user=user, request=request, page="/aggregator-insight/complaints", summary=f"Bukti komplain #{complaint_id}: {kind}")
    return out


@router.delete("/complaints/{complaint_id}/evidence/{evidence_id}")
def complaint_evidence_remove(complaint_id: int, evidence_id: str, request: Request, user: dict = Depends(TICKET)):
    out = _ask(quality.remove_evidence, user, complaint_id, evidence_id)
    activity.record("aggregator", user=user, request=request, page="/aggregator-insight/complaints", summary=f"Bukti komplain #{complaint_id} dihapus")
    return out


@router.get("/evidence/{evidence_id}")
def complaint_evidence_file(evidence_id: str, _: dict = Depends(TICKET)):
    f = quality.evidence_file(evidence_id)
    if f is None or not f["path"].exists():
        raise HTTPException(404, detail="Bukti tidak ditemukan")
    return FileResponse(f["path"], media_type=f["mime"], headers={"Cache-Control": "private, max-age=86400"})


@router.get("/report.pdf")
def report_pdf(request: Request, date_from: Optional[date] = Query(None, alias="dateFrom"), date_to: Optional[date] = Query(None, alias="dateTo"),
               platform: PLATFORM = None, user: dict = Depends(require_page("agg.reports", "agg.overview"))):
    """The report of a period as a PDF: summary, charts, analysis & recommendations, the outlets to look at (app/aggregator_pdf.py)."""
    from fastapi.responses import Response
    from app import aggregator_pdf
    a, z = _range(date_from, date_to)
    data = aggregator_pdf.build(a, z, platform)
    activity.record("aggregator", user=user, request=request, page="/aggregator-insight/reports", summary=f"Laporan PDF {a} – {z}")
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="Aggregator Insight {a} - {z}.pdf"', "Cache-Control": "no-store"})
