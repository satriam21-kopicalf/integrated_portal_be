"""Form → Monitoring Product / Monitoring Outlet and their monitoring (app/visit_forms.py). Registered after PIC Check In
(its /api/forms/pic-check-in and /api/monitoring/pic-check-in come first).

    GET    /api/forms/{form}                              page form.<form>: definition, account, branches, drafts, latest
    POST   /api/forms/{form}/visits                       page form.<form>: {"branchCode"} → the draft of that branch
    GET    /api/forms/{form}/visits/{id}                  page form.<form>: the user's own visit
    PATCH  /api/forms/{form}/visits/{id}                  page form.<form>: {"answers": {item: {rating?, note?}}}
    POST   /api/forms/{form}/visits/{id}/photos           page form.<form>: {"item", "photo", "latitude", "longitude", "takenAt"}
    DELETE /api/forms/{form}/visits/{id}/photos/{photo}   page form.<form>
    POST   /api/forms/{form}/visits/{id}/submit           page form.<form>: {"latitude", "longitude", "accuracyM"}
    DELETE /api/forms/{form}/visits/{id}                  page form.<form>: discard the draft
    GET    /api/monitoring/{form}                         page mon.<form>: ?dateFrom&dateTo&branch&userId
    GET    /api/monitoring/{form}/visits/{id}             page mon.<form>: one submitted visit (branches in scope)
    GET    /api/visit-photos/{photo}                      the photo: the PIC's own visit or monitoring (scope)
    GET    /api/visit-signatures/{id}                     the visit's signature: same access as its photos

{form} = product | outlet.
"""
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import activity, branch_groups, visit_forms as vf
from app.pic_checkin import CheckInError
from app.access import deny, has_page
from app.activity import client_ip
from app.routes.auth import current_user
from app.scope import NO_BRANCH, allowed_branches, scoped_branch
from app.utils import parse_branches, today

router = APIRouter(tags=["visit forms"])
FORMS = tuple(vf.DEFINITIONS)


class OpenIn(BaseModel):
    branchCode: str = Field(min_length=1, max_length=40)


class AnswersIn(BaseModel):
    answers: dict[str, dict] = Field(max_length=200)


class PhotoIn(BaseModel):
    item: str = Field(min_length=1, max_length=80)
    photo: str = Field(min_length=20, max_length=5_000_000)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    takenAt: Optional[datetime] = None


class SubmitIn(BaseModel):
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    accuracyM: Optional[float] = Field(None, ge=0, le=100_000)
    signature: Optional[str] = Field(None, min_length=20, max_length=5_000_000)  # optional: JPEG on white, drawn in the browser
    signedName: Optional[str] = Field(None, max_length=120)


def _form_page(form: str, request: Request, user: dict) -> None:
    if form not in FORMS:
        raise HTTPException(404, detail="Form tidak dikenal")
    if not has_page(user, f"form.{form}"):
        deny(request, user)


def _run(fn, *args):
    try:
        return fn(*args)
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from None
    except CheckInError as e:  # VisitError, or a photo / signature that cannot be read
        raise HTTPException(422, detail=str(e)) from None


@router.get("/api/forms/{form}")
def form_state(form: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _form_page(form, request, user)
    return vf.form_state(form, user)


@router.post("/api/forms/{form}/visits")
def open_visit(form: str, body: OpenIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _form_page(form, request, user)
    return _run(vf.open_visit, form, user, body.branchCode)


@router.get("/api/forms/{form}/visits/{visit_id}")
def get_visit(form: str, visit_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _form_page(form, request, user)
    row = vf.get_visit(visit_id)
    if row is None or row["form"] != form or row["user_id"] != str(user["id"]):
        raise HTTPException(404, detail="Kunjungan tidak ditemukan")
    return vf.full(visit_id)


@router.patch("/api/forms/{form}/visits/{visit_id}")
def save(form: str, visit_id: str, body: AnswersIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _form_page(form, request, user)
    return _run(vf.save_answers, form, user, visit_id, body.answers)


@router.post("/api/forms/{form}/visits/{visit_id}/photos", status_code=201)
def add_photo(form: str, visit_id: str, body: PhotoIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _form_page(form, request, user)
    return _run(vf.add_photo, form, user, visit_id, body.item, body.photo, body.latitude, body.longitude, body.takenAt)


@router.delete("/api/forms/{form}/visits/{visit_id}/photos/{photo_id}")
def delete_photo(form: str, visit_id: str, photo_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _form_page(form, request, user)
    return _run(vf.delete_photo, form, user, visit_id, photo_id)


@router.post("/api/forms/{form}/visits/{visit_id}/submit")
def submit(form: str, visit_id: str, body: SubmitIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _form_page(form, request, user)
    try:
        out = vf.submit(form, user, visit_id, body.latitude, body.longitude, body.accuracyM,
                        request.headers.get("user-agent"), client_ip(request), body.signature, body.signedName)
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from None
    except CheckInError as e:  # VisitError, or a photo / signature that cannot be read
        activity.record(f"form.{form}_visit", user=user, request=request, status="failed", page=f"/forms/{form}",
                        summary=f"{vf.DEFINITIONS[form]['title']} ditolak: {e}", details={"id": visit_id})
        raise HTTPException(422, detail=str(e)) from None
    c = out["counts"]
    activity.record(f"form.{form}_visit", user=user, request=request, page=f"/forms/{form}",
                    summary=f"{vf.DEFINITIONS[form]['title']} {out['branchName']}: {c['under']} UNDER · {c['meet']} MEET · {c['exceed']} EXCEED",
                    details={"id": visit_id, "branch": out["branchCode"], "counts": c, "distanceM": out["distanceM"]})
    return out


@router.delete("/api/forms/{form}/visits/{visit_id}")
def discard(form: str, visit_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _form_page(form, request, user)
    _run(vf.discard, form, user, visit_id)
    return {"ok": True}


# ---------------------------------------------------------------- monitoring

def _mon_page(form: str, request: Request, user: dict) -> None:
    if not has_page(user, f"mon.{form}"):
        deny(request, user)


@router.get("/api/monitoring/{form}")
def monitoring(form: str, request: Request, dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None),
               branch: Optional[str] = None, userId: Optional[str] = None, group: Optional[str] = Query(None, max_length=60),
               user: dict = Depends(current_user)) -> dict:
    if form not in FORMS:
        raise HTTPException(404, detail="Form tidak dikenal")
    _mon_page(form, request, user)
    end = dateTo or today()
    start = dateFrom or end - timedelta(days=29)
    if start > end:
        raise HTTPException(422, detail="Tanggal mulai harus sebelum tanggal akhir")
    if (end - start).days > 366:
        raise HTTPException(422, detail="Maksimal 1 tahun sekali tampil")
    scoped = scoped_branch(branch)
    branches = None if scoped is None else ([] if scoped == NO_BRANCH else parse_branches(scoped))
    branches = branch_groups.narrow(branches, group)  # Monitoring → Branch groups (CCI, CTG, CTGP …)
    return {"period": {"from": start.isoformat(), "to": end.isoformat()}, **vf.monitor(form, start, end, branches, userId)}


@router.get("/api/monitoring/{form}/attendance")
def monitoring_attendance(form: str, request: Request, month: str = Query(..., pattern=r"^\d{4}-\d{2}$"), branch: Optional[str] = None,
                          group: Optional[str] = Query(None, max_length=60), user: dict = Depends(current_user)) -> dict:
    """The attendance of a calendar month (28-31 days): per PIC the days with a visit and the days off."""
    if form not in FORMS:
        raise HTTPException(404, detail="Form tidak dikenal")
    _mon_page(form, request, user)
    y, m = (int(x) for x in month.split("-"))
    if not 1 <= m <= 12 or not 2020 <= y <= 2100:
        raise HTTPException(422, detail="Bulan tidak valid")
    first = date(y, m, 1)
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    scoped = scoped_branch(branch)
    branches = None if scoped is None else ([] if scoped == NO_BRANCH else parse_branches(scoped))
    branches = branch_groups.narrow(branches, group)
    return {"month": month, "first": first.isoformat(), "last": last.isoformat(), **vf.attendance(form, first, last, branches)}


@router.get("/api/monitoring/{form}/visits/{visit_id}")
def monitoring_visit(form: str, visit_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    if form not in FORMS:
        raise HTTPException(404, detail="Form tidak dikenal")
    _mon_page(form, request, user)
    row = vf.get_visit(visit_id)
    scope = allowed_branches()
    if row is None or row["form"] != form or row["status"] != "SUBMITTED" or (scope is not None and row["branch_code"] not in scope):
        raise HTTPException(404, detail="Kunjungan tidak ditemukan")
    return {"definition": vf.DEFINITIONS[form], **vf.full(visit_id)}


def _image(row: Optional[dict], request: Request, user: dict) -> Response:
    if row is None:
        raise HTTPException(404, detail="Foto tidak ditemukan")
    own = row["user_id"] == str(user["id"]) and has_page(user, f"form.{row['form']}")
    scope = allowed_branches()
    monitor = row["status"] == "SUBMITTED" and has_page(user, f"mon.{row['form']}") and (scope is None or row["branch_code"] in scope)
    if not (own or monitor):
        deny(request, user)
    return Response(bytes(row["photo"]), media_type=row["photo_type"], headers={"Cache-Control": "private, max-age=86400"})


@router.get("/api/visit-photos/{photo_id}")
def photo(photo_id: str, request: Request, user: dict = Depends(current_user)) -> Response:
    return _image(vf.photo(photo_id), request, user)


@router.get("/api/visit-signatures/{visit_id}")
def signature(visit_id: str, request: Request, user: dict = Depends(current_user)) -> Response:
    return _image(vf.signature_of(visit_id), request, user)
