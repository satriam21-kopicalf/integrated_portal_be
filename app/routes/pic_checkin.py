"""PIC Check In (app/pic_checkin.py). Registered before the Monitoring Product / Outlet routes (their
/api/forms/{form} and /api/monitoring/{form} would match pic-check-in too).

    GET  /api/forms/pic-check-in               page form.pic-check-in: the account, its branches (with the outlet
                                               location) and its latest check-ins
    POST /api/forms/pic-check-in/face-check    page form.pic-check-in: {"photo"} -> {"faces"} (feedback right after the
                                               photo; the check-in itself checks again)
    POST /api/forms/pic-check-in               page form.pic-check-in: check in (outside the radius: a note is required;
                                               the position is scored for signs of a fake GPS, app/pic_checkin.py assess)
    GET  /api/monitoring/pic-check-in          page mon.pic-check-in: ?dateFrom&dateTo&branch&userId&location
                                               (location: outside | low | unlocated | suspect | alert | unreviewed)
    POST /api/monitoring/pic-check-in/{id}/review  page mon.pic-check-in: {"status": valid|invalid, "note"}
    GET  /api/pic-check-in/{id}/photo?out=     the selfie (out=1: of the clock out): Monitoring (branches in scope) or
                                               the PIC's own check-in
    POST /api/forms/surveyor-check-in/{id}/clock-out   page form.surveyor-check-in: the clock out of the own open visit
                                               (Cost Control Clock In / Clock Out, migration 073)

The Surveyor Check In (migration 039) has the same endpoints under surveyor-check-in (pages form.surveyor-check-in,
mon.surveyor-check-in); its selfies are served by the same photo endpoint.
"""
from datetime import date, datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import activity, branch_groups, pic_checkin, pic_settings
from app.access import deny, has_page, require_page
from app.activity import client_ip
from app.routes.auth import current_user
from app.scope import NO_BRANCH, allowed_branches, scoped_branch
from app.utils import parse_branches, today
from app.thumbs import image_response

router = APIRouter(tags=["pic check in"])
FORM = "form.pic-check-in"
MON = "mon.pic-check-in"
# kind → (slug of the paths, form page, monitoring page, activity action, page path)
KINDS = {"pic": ("pic-check-in", FORM, MON, "form.pic_check_in"),
         "surveyor": ("surveyor-check-in", "form.surveyor-check-in", "mon.surveyor-check-in", "form.surveyor_check_in")}


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


class ClockOutIn(PhotoIn):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracyM: Optional[float] = Field(None, ge=0, le=100_000)
    locatedAt: Optional[datetime] = None
    deviceTime: Optional[datetime] = None
    note: Optional[str] = Field(None, max_length=500)
    sentAt: Optional[datetime] = None
    samples: Optional[list["GpsSample"]] = Field(None, max_length=30)
    touch: Optional[bool] = None
    webdriver: Optional[bool] = None


class ReviewIn(BaseModel):
    status: Literal["valid", "invalid"]
    note: Optional[str] = Field(None, max_length=500)


CheckInIn.model_rebuild()
ClockOutIn.model_rebuild()


def _schedule(user_id: str) -> list[dict]:
    from app import cc_schedule
    from app.utils import today
    try:
        return cc_schedule.mine(user_id, today())
    except Exception:  # before migration 108: no plan, the form works as before
        return []


def _refuse(exc: pic_checkin.CheckInError):
    raise HTTPException(status_code=422, detail=str(exc))


def _add(kind: str) -> None:
    slug, form_page, mon_page, action = KINDS[kind]
    title = "Check in" if kind == "pic" else "Check in surveyor"

    @router.get(f"/api/forms/{slug}", name=f"{kind}_form")
    def form(user: dict = Depends(require_page(form_page))) -> dict:
        branches = pic_checkin.branch_options(user, every=kind == "surveyor")
        if kind == "pic":  # the on-time deadline of every outlet, as set in Monitoring OPS → Branch Groups (migration 069)
            try:
                labels = pic_settings.deadline_labels([b["code"] for b in branches])
            except Exception:  # never keep a PIC from checking in over a setting
                labels = {}
            branches = [{**b, "checkinDeadline": labels.get(b["code"])} for b in branches]
        return {
            "user": {"id": str(user["id"]), "username": user["username"], "name": user.get("full_name") or user["username"]},
            "branches": branches,
            "recent": pic_checkin.recent_of(str(user["id"]), kind=kind),
            "today": pic_checkin.done_today(str(user["id"]), kind),  # branches already checked in today
            # the Cost Control visit still open (clocked in, not out yet)
            **({"open": _open(str(user["id"]))} if kind == "surveyor" else {}),
            # the outlets the leader of Cost Control planned for the member today (Schedule, migration 108)
            **({"schedule": _schedule(str(user["id"]))} if kind == "surveyor" else {}),
            "serverTime": datetime.now().astimezone().isoformat(),
        }

    @router.post(f"/api/forms/{slug}/face-check", name=f"{kind}_face_check")
    def face_check(body: PhotoIn, _user: dict = Depends(require_page(form_page))) -> dict:
        try:
            return {"faces": pic_checkin.face_check(body.photo)}
        except pic_checkin.CheckInError as exc:
            _refuse(exc)

    @router.post(f"/api/forms/{slug}", status_code=201, name=f"{kind}_check_in")
    def check_in(body: CheckInIn, request: Request, user: dict = Depends(require_page(form_page))) -> dict:
        try:
            out = pic_checkin.create(user, body.branchCode, body.photo, body.latitude, body.longitude, body.accuracyM,
                                     body.locatedAt, body.deviceTime, body.note, request.headers.get("user-agent"),
                                     client_ip(request), sent_at=body.sentAt,
                                     samples=[x.model_dump() for x in body.samples or []], touch=body.touch,
                                     webdriver=body.webdriver, kind=kind)
        except pic_checkin.CheckInError as exc:
            activity.record(action, user=user, request=request, status="failed", page=f"/forms/{slug}",
                            summary=f"{title} ditolak: {exc}", details={"branch": body.branchCode})
            _refuse(exc)
        activity.record(action, user=user, request=request, page=f"/forms/{slug}",
                        summary=f"{title} {out['branchName']}"
                        + (f" · {out['distanceM']} m" if out["distanceM"] is not None else " · outlet tanpa koordinat")
                        + (" · di luar radius" if out["outsideRadius"] else "")
                        + (f" · risiko lokasi {out['riskLevel']}" if out["riskLevel"] in ("medium", "high") else ""),
                        details={k: out[k] for k in ("id", "branchCode", "distanceM", "outsideRadius", "lowAccuracy",
                                                     "faces", "riskLevel", "riskSignals")})
        # the PIC is not told which risk signal fired
        return {k: v for k, v in out.items() if k not in ("riskLevel", "riskSignals")}

    @router.get(f"/api/monitoring/{slug}", name=f"{kind}_monitoring")
    def monitoring(dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None),
                   branch: Optional[str] = None, userId: Optional[str] = None, location: str = "",
                   group: Optional[str] = Query(None, max_length=60),
                   _user: dict = Depends(require_page(mon_page))) -> dict:
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
                **pic_checkin.monitor(start, end, branches, userId, location, kind=kind)}

    @router.post(f"/api/monitoring/{slug}/{{checkin_id}}/review", name=f"{kind}_review")
    def review(checkin_id: str, body: ReviewIn, request: Request, user: dict = Depends(require_page(mon_page))) -> dict:
        row = pic_checkin.review(checkin_id, body.status, body.note, user, allowed_branches(), kind=kind)
        if row is None:
            raise HTTPException(404, detail="Check in tidak ditemukan")
        activity.record(f"{action}_review", user=user, request=request, page=f"/monitoring/{slug}",
                        summary=f"{title} {row['username']} di {row['branch_name']}: "
                                f"{'valid' if body.status == 'valid' else 'tidak valid'}",
                        details={"id": checkin_id, "status": body.status, "note": body.note})
        return {"id": checkin_id, "status": body.status}


for _kind in KINDS:
    _add(_kind)


def _open(user_id: str) -> Optional[dict]:
    o = pic_checkin.open_visit(user_id)
    return next((r for r in pic_checkin.recent_of(user_id, kind="surveyor") if r["id"] == o["id"]), None) if o else None


@router.post("/api/forms/surveyor-check-in/{checkin_id}/clock-out", name="surveyor_clock_out")
def clock_out(checkin_id: str, body: ClockOutIn, request: Request, user: dict = Depends(require_page("form.surveyor-check-in"))) -> dict:
    try:
        out = pic_checkin.clock_out(user, checkin_id, body.photo, body.latitude, body.longitude, body.accuracyM, body.locatedAt,
                                    body.deviceTime, body.note, request.headers.get("user-agent"), client_ip(request),
                                    sent_at=body.sentAt, samples=[x.model_dump() for x in body.samples or []], touch=body.touch,
                                    webdriver=body.webdriver)
    except pic_checkin.CheckInError as exc:
        activity.record("form.surveyor_clock_out", user=user, request=request, status="failed", page="/forms/surveyor-check-in",
                        summary=f"Clock out ditolak: {exc}", details={"id": checkin_id})
        _refuse(exc)
    activity.record("form.surveyor_clock_out", user=user, request=request, page="/forms/surveyor-check-in",
                    summary=f"Clock out {out['branchName']} · {out['durationMin']} menit"
                    + (f" · {out['distanceM']} m" if out["distanceM"] is not None else "")
                    + (" · di luar radius" if out["outsideRadius"] else ""),
                    details={k: out[k] for k in ("id", "branchCode", "durationMin", "distanceM", "outsideRadius", "riskLevel")})
    return {k: v for k, v in out.items() if k not in ("riskLevel", "riskSignals")}


@router.get("/api/pic-check-in/{checkin_id}/photo")
def photo(checkin_id: str, request: Request, w: Optional[int] = Query(None, ge=48, le=1600), out: bool = False,
          user: dict = Depends(current_user)) -> Response:
    row = pic_checkin.photo_of(checkin_id, out)
    if row is None:
        raise HTTPException(404, detail="Check in tidak ditemukan")
    _, form_page, mon_page, _ = KINDS.get(row.get("kind") or "pic", KINDS["pic"])
    own = row["user_id"] == str(user["id"]) and has_page(user, form_page)
    scope = allowed_branches()
    monitor = has_page(user, mon_page) and (scope is None or row["branch_code"] in scope)
    if not (own or monitor):
        deny(request, user)
    return image_response(row["photo"], row["photo_type"], w, "private, max-age=86400")
