"""PIC Check In (app/pic_checkin.py). Registered before app/routes/monitoring.py, whose generic /api/forms/{form} and
/api/monitoring/{form} keep serving the forms still being built.

    GET  /api/forms/pic-check-in               page form.pic-check-in: the account, its branches (with the outlet
                                               location) and its latest check-ins
    POST /api/forms/pic-check-in/face-check    page form.pic-check-in: {"photo"} -> {"faces"} (feedback right after the
                                               photo; the check-in itself checks again)
    POST /api/forms/pic-check-in               page form.pic-check-in: check in (outside the radius: a note is required;
                                               the position is scored for signs of a fake GPS, app/pic_checkin.py assess)
    GET  /api/monitoring/pic-check-in          page mon.pic-check-in: ?dateFrom&dateTo&branch&userId&location
                                               (location: outside | low | unlocated | suspect | alert | unreviewed)
    POST /api/monitoring/pic-check-in/{id}/review  page mon.pic-check-in: {"status": valid|invalid, "note"}
    GET  /api/pic-check-in/{id}/photo          the selfie: Monitoring (branches in scope) or the PIC's own check-in
"""
from datetime import date, datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import activity, branch_groups, pic_checkin
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
    # anti fake GPS (app/pic_checkin.py assess): the phone's clock when sending, the GPS readings while the form was
    # open, touch screen, browser automation
    sentAt: Optional[datetime] = None
    samples: Optional[list["GpsSample"]] = Field(None, max_length=30)
    touch: Optional[bool] = None
    webdriver: Optional[bool] = None


class GpsSample(BaseModel):
    t: float = Field(ge=0)                  # device timestamp, ms
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    acc: Optional[float] = Field(None, ge=0, le=100_000)


class ReviewIn(BaseModel):
    status: Literal["valid", "invalid"]
    note: Optional[str] = Field(None, max_length=500)


CheckInIn.model_rebuild()


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
                                 client_ip(request), sent_at=body.sentAt,
                                 samples=[x.model_dump() for x in body.samples or []], touch=body.touch,
                                 webdriver=body.webdriver)
    except pic_checkin.CheckInError as exc:
        activity.record("form.pic_check_in", user=user, request=request, status="failed", page="/forms/pic-check-in",
                        summary=f"Check in ditolak: {exc}", details={"branch": body.branchCode})
        _refuse(exc)
    activity.record("form.pic_check_in", user=user, request=request, page="/forms/pic-check-in",
                    summary=f"Check in {out['branchName']}"
                    + (f" · {out['distanceM']} m" if out["distanceM"] is not None else " · outlet tanpa koordinat")
                    + (" · di luar radius" if out["outsideRadius"] else "")
                    + (f" · risiko lokasi {out['riskLevel']}" if out["riskLevel"] in ("medium", "high") else ""),
                    details={k: out[k] for k in ("id", "branchCode", "distanceM", "outsideRadius", "lowAccuracy", "faces",
                                                 "riskLevel", "riskSignals")})
    # the PIC is not told which risk signal fired
    return {k: v for k, v in out.items() if k not in ("riskLevel", "riskSignals")}


@router.get("/api/monitoring/pic-check-in")
def monitoring(dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None), branch: Optional[str] = None,
               userId: Optional[str] = None, location: str = "", group: Optional[str] = Query(None, max_length=60),
               _user: dict = Depends(require_page(MON))) -> dict:
    end = dateTo or today()
    start = dateFrom or end - timedelta(days=6)
    if start > end:
        raise HTTPException(422, detail="Tanggal mulai harus sebelum tanggal akhir")
    if (end - start).days > 92:
        raise HTTPException(422, detail="Maksimal 3 bulan sekali tampil")
    scoped = scoped_branch(branch)
    branches = None if scoped is None else ([] if scoped == NO_BRANCH else parse_branches(scoped))
    branches = branch_groups.narrow(branches, group)  # Monitoring → Branch groups (CCI, CTG, CTGP …)
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


@router.post("/api/monitoring/pic-check-in/{checkin_id}/review")
def review(checkin_id: str, body: ReviewIn, request: Request, user: dict = Depends(require_page(MON))) -> dict:
    row = pic_checkin.review(checkin_id, body.status, body.note, user, allowed_branches())
    if row is None:
        raise HTTPException(404, detail="Check in tidak ditemukan")
    activity.record("form.pic_check_in_review", user=user, request=request, page="/monitoring/pic-check-in",
                    summary=f"Check in {row['username']} di {row['branch_name']}: {'valid' if body.status == 'valid' else 'tidak valid'}",
                    details={"id": checkin_id, "status": body.status, "note": body.note})
    return {"id": checkin_id, "status": body.status}
