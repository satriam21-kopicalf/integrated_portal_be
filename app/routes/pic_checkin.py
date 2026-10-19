"""PIC Check In (app/pic_checkin.py). Registered before app/routes/monitoring.py, whose generic /api/forms/{form} and
/api/monitoring/{form} keep serving the forms still being built.

    GET  /api/forms/pic-check-in               page form.pic-check-in: the account, its branches (with the outlet
                                               location) and its latest check-ins
    POST /api/forms/pic-check-in/face-check    page form.pic-check-in: {"photo"} -> {"faces"} (feedback right after the
                                               photo; the check-in itself checks again)
    POST /api/forms/pic-check-in               page form.pic-check-in: check in
    GET  /api/monitoring/pic-check-in          page mon.pic-check-in: ?dateFrom&dateTo&branch&userId&location
    GET  /api/pic-check-in/{id}/photo          the selfie: Monitoring (branches in scope) or the PIC's own check-in
"""
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import activity, pic_checkin
from app.access import deny, has_page, require_page
from app.activity import client_ip
from app.routes.auth import current_user
from app.scope import NO_BRANCH, allowed_branches, scoped_branch
from app.utils import parse_branches, today

router = APIRouter(tags=["pic check in"])
FORM = "form.pic-check-in"
MON = "mon.pic-check-in"


class PhotoIn(BaseModel):
    photo: str = Field(min_length=20, max_length=5_000_000)


class CheckInIn(PhotoIn):
    branchCode: str = Field(min_length=1, max_length=40)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracyM: Optional[float] = Field(None, ge=0, le=100_000)
    locatedAt: Optional[datetime] = None
    deviceTime: Optional[datetime] = None
    note: Optional[str] = Field(None, max_length=500)


def _refuse(exc: pic_checkin.CheckInError):
    raise HTTPException(status_code=422, detail=str(exc))


@router.get("/api/forms/pic-check-in")
def form(user: dict = Depends(require_page(FORM))) -> dict:
    return {
        "user": {"id": str(user["id"]), "username": user["username"], "name": user.get("full_name") or user["username"]},
        "branches": pic_checkin.branch_options(user),
        "recent": pic_checkin.recent_of(str(user["id"])),
        "serverTime": datetime.now().astimezone().isoformat(),
    }


@router.post("/api/forms/pic-check-in/face-check")
def face_check(body: PhotoIn, _user: dict = Depends(require_page(FORM))) -> dict:
    try:
        return {"faces": pic_checkin.face_check(body.photo)}
    except pic_checkin.CheckInError as exc:
        _refuse(exc)


@router.post("/api/forms/pic-check-in", status_code=201)
def check_in(body: CheckInIn, request: Request, user: dict = Depends(require_page(FORM))) -> dict:
    try:
        out = pic_checkin.create(user, body.branchCode, body.photo, body.latitude, body.longitude, body.accuracyM,
                                 body.locatedAt, body.deviceTime, body.note, request.headers.get("user-agent"),
                                 client_ip(request))
    except pic_checkin.CheckInError as exc:
        activity.record("form.pic_check_in", user=user, request=request, status="failed", page="/forms/pic-check-in",
                        summary=f"Check in ditolak: {exc}", details={"branch": body.branchCode})
        _refuse(exc)
    activity.record("form.pic_check_in", user=user, request=request, page="/forms/pic-check-in",
                    summary=f"Check in {out['branchName']}"
                    + (f" · {out['distanceM']} m" if out["distanceM"] is not None else " · outlet tanpa koordinat")
                    + (" · di luar radius" if out["outsideRadius"] else ""),
                    details={k: out[k] for k in ("id", "branchCode", "distanceM", "outsideRadius", "lowAccuracy", "faces")})
    return out


@router.get("/api/monitoring/pic-check-in")
def monitoring(dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None), branch: Optional[str] = None,
               userId: Optional[str] = None, location: str = "", _user: dict = Depends(require_page(MON))) -> dict:
    end = dateTo or today()
    start = dateFrom or end - timedelta(days=6)
    if start > end:
        raise HTTPException(422, detail="Tanggal mulai harus sebelum tanggal akhir")
    if (end - start).days > 92:
        raise HTTPException(422, detail="Maksimal 3 bulan sekali tampil")
    scoped = scoped_branch(branch)
    branches = None if scoped is None else ([] if scoped == NO_BRANCH else parse_branches(scoped))
    return {"period": {"from": start.isoformat(), "to": end.isoformat()},
            **pic_checkin.monitor(start, end, branches, userId, location)}


@router.get("/api/pic-check-in/{checkin_id}/photo")
def photo(checkin_id: str, request: Request, user: dict = Depends(current_user)) -> Response:
    row = pic_checkin.photo_of(checkin_id)
    if row is None:
        raise HTTPException(404, detail="Check in tidak ditemukan")
    own = row["user_id"] == str(user["id"]) and has_page(user, FORM)
    scope = allowed_branches()
    monitor = has_page(user, MON) and (scope is None or row["branch_code"] in scope)
    if not (own or monitor):
        deny(request, user)
    return Response(bytes(row["photo"]), media_type=row["photo_type"], headers={"Cache-Control": "private, max-age=86400"})
