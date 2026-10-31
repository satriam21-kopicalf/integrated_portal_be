"""Waste (app/waste.py, migration 040): outlets enter the waste of their branch, Cost Control monitors and reviews it.

    GET    /api/waste/entries?branch&date   page "waste": branches, days, items (waste / usage / premix), categories and
                                           the entries of the branch and day
    POST   /api/waste/entries               page "waste": one item {branchCode, businessDate, scope, itemCode, qty,
                                           category, note, photo, takenAt, latitude, longitude} (photo and category
                                           required)
    DELETE /api/waste/entries/{id}          page "waste": the author removes an entry not reviewed yet
    GET    /api/waste/monitor               page cc.waste-monitor (or cc.waste-review): ?dateFrom&dateTo&branch&group&
                                           scope&category&status&userId -> entries, totals per item / branch / category
    GET    /api/waste/reviews               page cc.waste-review: the entries waiting for review (same filters)
    POST   /api/waste/reviews               page cc.waste-review: {"ids": [...], "status": approved|rejected, "note"}
    GET    /api/waste/entries/{id}/photo    the evidence: the author's branches, or a monitor / review account
    PUT    /api/waste/usage                 page "waste": the Usage Management list {branchCode, businessDate,
                                           lines: {itemCode: qty | null}} of the branch and day
    PUT    /api/waste/no-waste              page "waste": {branchCode, businessDate, noWaste} "no waste today"
    GET    /api/waste/daily                 monitor / review: per branch and day whether waste was reported, the usage
                                           list filled, the branches not reported yet (?dateFrom&dateTo&branch&group)
    GET    /api/waste/usage                 monitor / review: the Usage Management quantities of the period

Role "user" / "outlet" see their own branches only (app/scope.py, app/pic_checkin.branch_options).
"""
from datetime import date, datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import activity, branch_groups, waste
from app.access import deny, has_page, require_page
from app.activity import client_ip
from app.routes.auth import current_user
from app.scope import NO_BRANCH, allowed_branches, scoped_branch
from app.utils import parse_branches, today

router = APIRouter(prefix="/api/waste", tags=["waste"])
MON, REV = "cc.waste-monitor", "cc.waste-review"


class EntryIn(BaseModel):
    branchCode: str = Field(min_length=1, max_length=40)
    businessDate: Optional[date] = None
    scope: Literal["waste", "premix"]
    itemCode: str = Field(min_length=1, max_length=80)
    qty: float = Field(gt=0, le=1_000_000)
    category: str = Field(min_length=1, max_length=120)
    note: Optional[str] = Field(None, max_length=500)
    photo: str = Field(min_length=20, max_length=5_000_000)
    takenAt: Optional[datetime] = None
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)


class UsageIn(BaseModel):
    branchCode: str = Field(min_length=1, max_length=40)
    businessDate: Optional[date] = None
    lines: dict[str, Optional[float]] = Field(max_length=500)


class NoWasteIn(BaseModel):
    branchCode: str = Field(min_length=1, max_length=40)
    businessDate: Optional[date] = None
    noWaste: bool = True


class ReviewIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)
    status: Literal["approved", "rejected"]
    note: Optional[str] = Field(None, max_length=500)


def _refuse(exc: Exception):
    raise HTTPException(status_code=422, detail=str(exc))


@router.get("/entries")
def entries(branch: Optional[str] = None, day: Optional[date] = Query(None, alias="date"),
            user: dict = Depends(require_page("waste"))) -> dict:
    return waste.form(user, branch, day)


@router.post("/entries", status_code=201)
def create(body: EntryIn, request: Request, user: dict = Depends(require_page("waste"))) -> dict:
    try:
        out = waste.create(user, body.model_dump(), request.headers.get("user-agent"), client_ip(request))
    except waste.CheckInError as exc:
        activity.record("waste.entry", user=user, request=request, status="failed", page="/waste",
                        summary=f"Waste ditolak: {exc}", details={"branch": body.branchCode, "item": body.itemCode})
        _refuse(exc)
    activity.record("waste.entry", user=user, request=request, page="/waste",
                    summary=f"Waste {out['branchName']}: {out['itemName']} {out['qty']:g} {out['unit']} ({out['category']})",
                    details={k: out[k] for k in ("id", "branchCode", "businessDate", "scope", "itemCode", "qty", "category")})
    return out


@router.delete("/entries/{entry_id}")
def remove(entry_id: str, request: Request, user: dict = Depends(require_page("waste"))) -> dict:
    row = waste.delete(user, entry_id)
    if row is None:
        raise HTTPException(404, detail="Waste tidak ditemukan atau sudah direview")
    activity.record("waste.delete", user=user, request=request, page="/waste",
                    summary=f"Waste dihapus: {row['branch_name']} {row['item_name']}", details={"id": entry_id})
    return {"id": entry_id, "deleted": True}


def _period(date_from: Optional[date], date_to: Optional[date], branch: Optional[str], group: Optional[str]):
    end = date_to or today()
    start = date_from or end - timedelta(days=6)
    if start > end:
        raise HTTPException(422, detail="Tanggal mulai harus sebelum tanggal akhir")
    if (end - start).days > 92:
        raise HTTPException(422, detail="Maksimal 3 bulan sekali tampil")
    scoped = scoped_branch(branch)
    branches = None if scoped is None else ([] if scoped == NO_BRANCH else parse_branches(scoped))
    return start, end, branch_groups.narrow(branches, group)


@router.get("/monitor")
def monitor(dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None), branch: Optional[str] = None,
            group: Optional[str] = Query(None, max_length=60), scope: str = "", category: str = "", status: str = "",
            userId: str = "", _user: dict = Depends(require_page(MON, REV))) -> dict:
    start, end, branches = _period(dateFrom, dateTo, branch, group)
    return {"period": {"from": start.isoformat(), "to": end.isoformat()}, "categories": waste.catalog()["categories"],
            **waste.monitor(start, end, branches, scope, category, status, userId)}


@router.get("/reviews")
def reviews(dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None), branch: Optional[str] = None,
            group: Optional[str] = Query(None, max_length=60), scope: str = "", category: str = "",
            status: str = "submitted", _user: dict = Depends(require_page(REV))) -> dict:
    start, end, branches = _period(dateFrom, dateTo, branch, group)
    return {"period": {"from": start.isoformat(), "to": end.isoformat()}, "categories": waste.catalog()["categories"],
            **waste.monitor(start, end, branches, scope, category, status)}


@router.post("/reviews")
def review(body: ReviewIn, request: Request, user: dict = Depends(require_page(REV))) -> dict:
    rows = waste.review(body.ids, body.status, body.note, user, allowed_branches())
    if not rows:
        raise HTTPException(404, detail="Waste tidak ditemukan")
    verdict = "disetujui" if body.status == "approved" else "ditolak"
    names = {r["branch_name"] for r in rows}
    activity.record("waste.review", user=user, request=request, page="/cost-control/waste/reviews",
                    summary=f"Waste {verdict}: {len(rows)} item" + (f" ({next(iter(names))})" if len(names) == 1 else ""),
                    details={"ids": [r["id"] for r in rows], "status": body.status, "note": body.note})
    return {"updated": len(rows), "status": body.status}


@router.get("/entries/{entry_id}/photo")
def photo(entry_id: str, request: Request, user: dict = Depends(current_user)) -> Response:
    row = waste.photo_of(entry_id)
    if row is None:
        raise HTTPException(404, detail="Waste tidak ditemukan")
    scope = allowed_branches()
    in_scope = scope is None or row["branch_code"] in scope
    if not (in_scope and any(has_page(user, p) for p in ("waste", MON, REV))):
        deny(request, user)
    return Response(bytes(row["photo"]), media_type=row["photo_type"], headers={"Cache-Control": "private, max-age=86400"})


@router.put("/usage")
def save_usage(body: UsageIn, request: Request, user: dict = Depends(require_page("waste"))) -> dict:
    try:
        out = waste.save_usage(user, body.model_dump())
    except waste.CheckInError as exc:
        _refuse(exc)
    activity.record("waste.usage", user=user, request=request, page="/waste",
                    summary=f"Usage Management {out['branchName']} {out['businessDate']}: {out['saved']} item",
                    details=out)
    return out


@router.put("/no-waste")
def no_waste(body: NoWasteIn, request: Request, user: dict = Depends(require_page("waste"))) -> dict:
    try:
        out = waste.set_no_waste(user, body.model_dump())
    except waste.CheckInError as exc:
        _refuse(exc)
    activity.record("waste.no-waste", user=user, request=request, page="/waste",
                    summary=f"{'Tidak ada waste' if out['noWaste'] else 'Batal tidak ada waste'}: {out['branchName']} "
                            f"{out['businessDate']}", details=out)
    return out


@router.get("/daily")
def daily(dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None), branch: Optional[str] = None,
          group: Optional[str] = Query(None, max_length=60), _user: dict = Depends(require_page(MON, REV))) -> dict:
    start, end, branches = _period(dateFrom, dateTo, branch, group)
    return {"period": {"from": start.isoformat(), "to": end.isoformat()}, **waste.daily(start, end, branches)}


@router.get("/usage")
def usage(dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None), branch: Optional[str] = None,
          group: Optional[str] = Query(None, max_length=60), _user: dict = Depends(require_page(MON, REV))) -> dict:
    start, end, branches = _period(dateFrom, dateTo, branch, group)
    return {"period": {"from": start.isoformat(), "to": end.isoformat()}, "data": waste.usage_report(start, end, branches)}
