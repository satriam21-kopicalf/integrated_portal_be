"""Travel Path (app/travel_path.py, migration 153). Registered before the generic /api/forms/{form} and /api/monitoring/{form}.

    GET    /api/forms/travel?branch                      page form.travel: outlets, the sessions of today, open findings, latest
    POST   /api/forms/travel/reports                     {"branchCode", "session"}: the report of the session (started or continued)
    GET    /api/forms/travel/reports/{id}
    PATCH  /api/forms/travel/reports/{id}                {"answers": {area: {rating?, note?, fix?}}, "crew"?, "supervisor"?}
    POST   /api/forms/travel/reports/{id}/photos         {"item", "kind": MAIN|AFTER, "photo", …}
    DELETE /api/forms/travel/reports/{id}/photos/{photo}
    POST   /api/forms/travel/reports/{id}/submit
    DELETE /api/forms/travel/reports/{id}                a draft
    POST   /api/forms/travel/findings/{id}/fix           {"note", "photo", …}: closed with its after photo
    GET    /api/monitoring/travel                        page mon.travel: ?dateFrom&dateTo&branch&group
    GET    /api/monitoring/travel/reports/{id}
    POST   /api/monitoring/travel/findings/{id}/reopen   {"note"}
    GET    /api/monitoring/travel/settings               any monitoring page; PUT: page mon.branch-groups
    GET    /api/travel-photos/{id}                       the outlet's accounts or the monitor
"""
from datetime import date, datetime, timedelta
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import activity, branch_groups, travel_path as tp
from app.access import PAGE_KEYS, deny, has_page, require_page
from app.pic_checkin import CheckInError
from app.routes.auth import current_user
from app.scope import NO_BRANCH, allowed_branches, scoped_branch
from app.thumbs import image_response
from app.utils import parse_branches, today

router = APIRouter(tags=["travel path"])
FORM, MON = "form.travel", "mon.travel"
MON_PAGES = tuple(p for p in PAGE_KEYS if p.startswith("mon."))


class OpenIn(BaseModel):
    branchCode: str = Field(min_length=1, max_length=40)
    session: str = Field(min_length=1, max_length=30)


class SaveIn(BaseModel):
    answers: Optional[dict[str, dict]] = Field(None, max_length=200)
    crew: Optional[str] = Field(None, max_length=200)
    supervisor: Optional[str] = Field(None, max_length=200)


class ShotIn(BaseModel):
    photo: str = Field(min_length=20, max_length=21_000_000)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    takenAt: Optional[datetime] = None


class PhotoIn(ShotIn):
    item: str = Field(min_length=1, max_length=80)
    kind: Literal["MAIN", "AFTER"] = "MAIN"


class FixIn(ShotIn):
    note: str = Field(min_length=1, max_length=1000)


class SubmitIn(BaseModel):
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    accuracyM: Optional[float] = Field(None, ge=0, le=100_000)


class NoteIn(BaseModel):
    note: str = Field(min_length=1, max_length=500)


def _run(fn, *args):
    try:
        return fn(*args)
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from None
    except CheckInError as e:
        raise HTTPException(422, detail=str(e)) from None


@router.get("/api/forms/travel")
def state(branch: Optional[str] = None, user: dict = Depends(require_page(FORM))) -> dict:
    return tp.form_state(user, branch)


@router.post("/api/forms/travel/reports")
def open_report(body: OpenIn, user: dict = Depends(require_page(FORM))) -> dict:
    return _run(tp.open_report, user, body.branchCode, body.session)


@router.get("/api/forms/travel/reports/{report_id}")
def get_report(report_id: str, user: dict = Depends(require_page(FORM))) -> dict:
    return _run(tp.get_report, user, report_id)


@router.patch("/api/forms/travel/reports/{report_id}")
def save(report_id: str, body: SaveIn, user: dict = Depends(require_page(FORM))) -> dict:
    return _run(tp.save, user, report_id, body.model_dump(exclude_unset=True))


@router.post("/api/forms/travel/reports/{report_id}/photos", status_code=201)
def add_photo(report_id: str, body: PhotoIn, user: dict = Depends(require_page(FORM))) -> dict:
    return _run(tp.add_photo, user, report_id, body.item, body.kind, body.photo, body.latitude, body.longitude, body.takenAt)


@router.delete("/api/forms/travel/reports/{report_id}/photos/{photo_id}")
def delete_photo(report_id: str, photo_id: str, user: dict = Depends(require_page(FORM))) -> dict:
    return _run(tp.delete_photo, user, report_id, photo_id)


@router.post("/api/forms/travel/reports/{report_id}/submit")
def submit(report_id: str, body: SubmitIn, request: Request, user: dict = Depends(require_page(FORM))) -> dict:
    out = _run(tp.submit, user, report_id, body.latitude, body.longitude, body.accuracyM)
    c = out["counts"] or {}
    activity.record("form.travel", user=user, request=request, page="/forms/travel",
                    summary=f"Travel Path {out['sessionLabel']} {out['branchName']}: skor {out['score']}, {c.get('nok', 0)} temuan",
                    details={"id": report_id, "branch": out["branchCode"], "counts": c, "late": out["late"]})
    return out


@router.delete("/api/forms/travel/reports/{report_id}")
def discard(report_id: str, user: dict = Depends(require_page(FORM))) -> dict:
    _run(tp.discard, user, report_id)
    return {"ok": True}


@router.post("/api/forms/travel/findings/{finding_id}/fix")
def fix(finding_id: str, body: FixIn, request: Request, user: dict = Depends(require_page(FORM))) -> dict:
    out = _run(tp.fix, user, finding_id, body.note, body.photo, body.latitude, body.longitude, body.takenAt)
    activity.record("form.travel-fix", user=user, request=request, page="/forms/travel",
                    summary=f"Temuan Travel Path selesai: {out['label']} ({out['branchName']})", details={"id": finding_id})
    return out


@router.get("/api/monitoring/travel/settings")
def settings(request: Request, user: dict = Depends(current_user)) -> dict:
    if not any(has_page(user, p) for p in MON_PAGES):
        deny(request, user)
    return {"settings": tp.settings(), "groups": [g["code"] for g in branch_groups.list_groups()["groups"]]}


@router.put("/api/monitoring/travel/settings")
def save_settings(body: dict[str, Any], request: Request, user: dict = Depends(require_page("mon.branch-groups"))) -> dict:
    try:
        out = _run(tp.save_settings, body, str(user["id"]))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(422, detail="Pengaturan Travel Path tidak valid") from None
    activity.record("user.branch_group", user=user, request=request, page="/monitoring/branch-groups", summary="Mengatur Travel Path", details=out)
    return out


@router.get("/api/monitoring/travel")
def monitor(dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None), branch: Optional[str] = None,
            group: Optional[str] = Query(None, max_length=60), _user: dict = Depends(require_page(MON))) -> dict:
    end = dateTo or today()
    start = dateFrom or end - timedelta(days=6)
    if start > end:
        raise HTTPException(422, detail="Tanggal mulai harus sebelum tanggal akhir")
    if (end - start).days > 92:
        raise HTTPException(422, detail="Maksimal 3 bulan sekali tampil")
    scoped = scoped_branch(branch)
    branches = None if scoped is None else ([] if scoped == NO_BRANCH else parse_branches(scoped))
    return {"period": {"from": start.isoformat(), "to": end.isoformat()}, **tp.monitor(start, end, branch_groups.narrow(branches, group))}


@router.get("/api/monitoring/travel/reports/{report_id}")
def monitor_report(report_id: str, _user: dict = Depends(require_page(MON))) -> dict:
    out = _run(tp.full, report_id)
    scope = allowed_branches()
    if out["status"] != "SUBMITTED" or (scope is not None and out["branchCode"] not in scope):
        raise HTTPException(404, detail="Laporan Travel Path tidak ditemukan")
    return out


@router.post("/api/monitoring/travel/findings/{finding_id}/reopen")
def reopen(finding_id: str, body: NoteIn, request: Request, user: dict = Depends(require_page(MON))) -> dict:
    out = _run(tp.reopen, user, finding_id, body.note, allowed_branches())
    activity.record("travel.reopen", user=user, request=request, page="/monitoring/travel",
                    summary=f"Temuan Travel Path dibuka lagi: {out['label']} ({out['branchName']})", details={"id": finding_id, "note": body.note})
    return out


@router.get("/api/public/travel/{token}")
def shared(token: str) -> dict:
    """A sent Travel Path from its link in the WhatsApp group: anyone may open it (2026-11-26)."""
    out = tp.shared(token)
    if out is None:
        raise HTTPException(404, detail="Link tidak ditemukan")
    return out


@router.get("/api/public/travel/{token}/photos/{photo_id}")
def shared_photo(token: str, photo_id: str, w: Optional[int] = Query(None, ge=48, le=1600)) -> Response:
    row = tp.shared_photo(token, photo_id)
    if row is None:
        raise HTTPException(404, detail="Foto tidak ditemukan")
    return image_response(row["photo"], row["photo_type"], w, "private, max-age=3600")


@router.get("/api/travel-photos/{photo_id}")
def photo(photo_id: str, request: Request, w: Optional[int] = Query(None, ge=48, le=1600), user: dict = Depends(current_user)) -> Response:
    row = tp.photo(photo_id)
    if row is None:
        raise HTTPException(404, detail="Foto tidak ditemukan")
    scope = allowed_branches()
    if not ((scope is None or row["branch_code"] in scope) and (has_page(user, FORM) or (has_page(user, MON) and row["status"] == "SUBMITTED"))):
        deny(request, user)
    return image_response(row["photo"], row["photo_type"], w, "private, max-age=86400")
