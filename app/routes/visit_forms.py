"""Form → Monitoring Product / Monitoring Outlet and their monitoring (app/visit_forms.py). Registered after PIC Check In
(its /api/forms/pic-check-in and /api/monitoring/pic-check-in come first).

    GET    /api/forms/{form}                              page form.<form>: definition, account, branches, drafts, latest
    POST   /api/forms/{form}/visits                       page form.<form>: {"branchCode"} → the draft of that branch
    GET    /api/forms/{form}/visits/{id}                  page form.<form>: the user's own visit
    PATCH  /api/forms/{form}/visits/{id}                  page form.<form>: {"answers": {item: {rating?, note?}}}
    POST   /api/forms/{form}/visits/{id}/photos           page form.<form>: {"item", "photo", "latitude", "longitude", "takenAt"}
    DELETE /api/forms/{form}/visits/{id}/photos/{photo}   page form.<form>
    POST   /api/forms/{form}/visits/{id}/submit           page form.<form>: {"latitude", "longitude", "accuracyM"}
    (the /api/forms responses carry no score: the PIC never sees it, only monitoring does)
    DELETE /api/forms/{form}/visits/{id}                  page form.<form>: discard the draft
    GET    /api/monitoring/{form}                         page mon.<form>: ?dateFrom&dateTo&branch&userId
    GET    /api/monitoring/{form}/visits/{id}             page mon.<form>: one submitted visit (branches in scope)
    GET    /api/visit-photos/{photo}                      the photo: the PIC's own visit or monitoring (scope)
    GET    /api/visit-signatures/{id}                     the visit's signature: same access as its photos

{form} = product | outlet | wwwr (the SWAT audit, app/wwwr.py: GET /api/forms/wwwr lists the audits of the member,
POST /api/forms/wwwr/visits {"auditId"} opens the shared visit; the lead sends it). Monitoring WW-WR (page mon.wwwr):
    GET    /api/monitoring/wwwr/team                      the accounts with the page form.wwwr
    GET    /api/monitoring/wwwr/schedule                  ?dateFrom&dateTo&branch: the audits scheduled
    POST   /api/monitoring/wwwr/schedule                  {"branchCode", "day", "leadId", "memberIds", "note"}
    PUT    /api/monitoring/wwwr/schedule/{id}             the same, until it is sent
    POST   /api/monitoring/wwwr/schedule/{id}/cancel
    GET    /api/monitoring/wwwr/comparison                ?dateFrom&dateTo&branch&group: audits against the PIC visits
    GET    /api/monitoring/wwwr/comparison/{visit id}     one audit item by item against the PIC
The team (page form.wwwr, migration 065): GET /api/forms/wwwr/colleagues; POST /api/forms/wwwr/audits/{id}/members
{"ids"} and DELETE …/members/{user} (the lead); PUT …/audits/{id}/evaluation {"summary", "revisitOn", "actions"};
POST /api/forms/wwwr/actions/{id} {"done", "note"}; POST …/audits/{id}/complete {"note"} (the lead).
Public (no sign-in): GET /api/public/wwwr/{token} and …/photos/{photo}: the result shared with the PIC.
"""
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import activity, branch_groups, visit_forms as vf, wwwr
from app.pic_checkin import CheckInError
from app.access import deny, has_page
from app.activity import client_ip
from app.routes.auth import current_user
from app.scope import NO_BRANCH, allowed_branches, scoped_branch
from app.utils import parse_branches, today

router = APIRouter(tags=["visit forms"])
FORMS = tuple(vf.DEFINITIONS)


class OpenIn(BaseModel):
    branchCode: Optional[str] = Field(None, min_length=1, max_length=40)
    auditId: Optional[str] = Field(None, min_length=1, max_length=40)  # WW-WR


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


def _no_score(v):
    """The PIC filling a form never sees a score (top level or in the counts): it is for the management in monitoring."""
    if isinstance(v, dict):
        return {k: _no_score(x) for k, x in v.items() if k != "score"}
    if isinstance(v, list):
        return [_no_score(x) for x in v]
    return v


def _run(fn, *args):
    try:
        return _no_score(fn(*args))
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from None
    except CheckInError as e:  # VisitError, or a photo / signature that cannot be read
        raise HTTPException(422, detail=str(e)) from None


@router.get("/api/forms/{form}")
def form_state(form: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _form_page(form, request, user)
    return _no_score(wwwr.state(user) if form == "wwwr" else vf.form_state(form, user))


@router.post("/api/forms/{form}/visits")
def open_visit(form: str, body: OpenIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _form_page(form, request, user)
    if form == "wwwr":
        if not body.auditId:
            raise HTTPException(422, detail="Pilih jadwal audit")
        return _run(wwwr.open_audit, user, body.auditId)
    if not body.branchCode:
        raise HTTPException(422, detail="Pilih cabang")
    return _run(vf.open_visit, form, user, body.branchCode)


@router.get("/api/forms/{form}/visits/{visit_id}")
def get_visit(form: str, visit_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _form_page(form, request, user)
    row = vf.get_visit(visit_id)
    if row is None or row["form"] != form or not vf.can_fill(row, user):
        raise HTTPException(404, detail="Kunjungan tidak ditemukan")
    out = vf.full(visit_id)
    return _no_score(wwwr.with_team(out) if form == "wwwr" else out)


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
    return _no_score(out)


@router.delete("/api/forms/{form}/visits/{visit_id}")
def discard(form: str, visit_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _form_page(form, request, user)
    _run(vf.discard, form, user, visit_id)
    return {"ok": True}


# ---------------------------------------------------------------- monitoring

def _mon_page(form: str, request: Request, user: dict) -> None:
    if not has_page(user, f"mon.{form}"):
        deny(request, user)


# ---------------------------------------------------------------- WW-WR: the team, the evaluation, the follow-up

class MembersIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=30)


class ActionIn(BaseModel):
    id: Optional[str] = Field(None, max_length=40)
    title: str = Field(max_length=300)
    detail: Optional[str] = Field(None, max_length=2000)
    assigneeId: Optional[str] = Field(None, max_length=40)
    dueOn: Optional[date] = None


class EvaluationIn(BaseModel):
    summary: Optional[str] = Field(None, max_length=4000)
    revisitOn: Optional[date] = None
    actions: Optional[list[ActionIn]] = Field(None, max_length=50)


class DoneIn(BaseModel):
    done: bool
    note: Optional[str] = Field(None, max_length=1000)


class NoteIn(BaseModel):
    note: Optional[str] = Field(None, max_length=2000)


def _swat(request: Request, user: dict) -> None:
    _form_page("wwwr", request, user)


@router.get("/api/forms/wwwr/colleagues")
def wwwr_colleagues(request: Request, user: dict = Depends(current_user)) -> list[dict]:
    _swat(request, user)
    return wwwr.team()


@router.post("/api/forms/wwwr/audits/{audit_id}/members")
def wwwr_add_members(audit_id: str, body: MembersIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _swat(request, user)
    out = _run(wwwr.add_members, user, audit_id, body.ids)
    activity.record("wwwr.team", user=user, request=request, page="/forms/wwwr", summary=f"WW-WR {out['branchName']}: rekan ditambahkan", details={"id": audit_id, "ids": body.ids})
    return _no_score(out)


@router.delete("/api/forms/wwwr/audits/{audit_id}/members/{member_id}")
def wwwr_remove_member(audit_id: str, member_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _swat(request, user)
    return _no_score(_run(wwwr.remove_member, user, audit_id, member_id))


@router.put("/api/forms/wwwr/audits/{audit_id}/evaluation")
def wwwr_evaluation(audit_id: str, body: EvaluationIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _swat(request, user)
    data = body.model_dump()
    if body.actions is None:
        data.pop("actions")
    return _no_score(_run(wwwr.save_evaluation, user, audit_id, data))


@router.post("/api/forms/wwwr/actions/{action_id}")
def wwwr_action(action_id: str, body: DoneIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _swat(request, user)
    out = _run(wwwr.set_action, user, action_id, body.done, body.note)
    activity.record("wwwr.action", user=user, request=request, page="/forms/wwwr",
                    summary=f"WW-WR {out['branchName']}: action plan {'selesai' if body.done else 'dibuka lagi'}", details={"id": action_id})
    return _no_score(out)


@router.post("/api/forms/wwwr/audits/{audit_id}/complete")
def wwwr_complete(audit_id: str, body: NoteIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _swat(request, user)
    out = _run(wwwr.complete, user, audit_id, body.note)
    activity.record("wwwr.complete", user=user, request=request, page="/forms/wwwr", summary=f"WW-WR {out['branchName']} selesai", details={"id": audit_id})
    return _no_score(out)


@router.get("/api/public/wwwr/{token}")
def wwwr_shared(token: str) -> dict:
    out = wwwr.shared(token)
    if out is None:
        raise HTTPException(404, detail="Link tidak ditemukan")
    return out


@router.get("/api/public/wwwr/{token}/photos/{photo_id}")
def wwwr_shared_photo(token: str, photo_id: str) -> Response:
    row = wwwr.shared_photo(token, photo_id)
    if row is None:
        raise HTTPException(404, detail="Foto tidak ditemukan")
    return Response(bytes(row["photo"]), media_type=row["photo_type"], headers={"Cache-Control": "private, max-age=3600"})


# ---------------------------------------------------------------- WW-WR: schedule and comparison (page mon.wwwr)

class AuditIn(BaseModel):
    branchCode: str = Field(min_length=1, max_length=40)
    day: date
    leadId: str = Field(min_length=1, max_length=40)
    memberIds: list[str] = Field(default_factory=list, max_length=30)
    note: Optional[str] = Field(None, max_length=500)


def _wwwr_scope(request: Request, user: dict, branch: Optional[str], group: Optional[str] = None) -> Optional[list[str]]:
    _mon_page("wwwr", request, user)
    scoped = scoped_branch(branch)
    branches = None if scoped is None else ([] if scoped == NO_BRANCH else parse_branches(scoped))
    return branch_groups.narrow(branches, group)


def _period(dateFrom: Optional[date], dateTo: Optional[date], back: int = 29) -> tuple[date, date]:
    end = dateTo or today()
    start = dateFrom or end - timedelta(days=back)
    if start > end or (end - start).days > 366:
        raise HTTPException(422, detail="Periode tidak valid (maksimal 1 tahun)")
    return start, end


@router.get("/api/monitoring/wwwr/team")
def wwwr_team(request: Request, user: dict = Depends(current_user)) -> list[dict]:
    _mon_page("wwwr", request, user)
    return wwwr.team()


@router.get("/api/monitoring/wwwr/schedule")
def wwwr_schedule(request: Request, dateFrom: Optional[date] = None, dateTo: Optional[date] = None, branch: Optional[str] = None,
                  user: dict = Depends(current_user)) -> dict:
    branches = _wwwr_scope(request, user, branch)
    end = dateTo or today() + timedelta(days=30)
    start = dateFrom or today() - timedelta(days=30)
    if start > end or (end - start).days > 366:
        raise HTTPException(422, detail="Periode tidak valid (maksimal 1 tahun)")
    return {"from": start.isoformat(), "to": end.isoformat(), "data": wwwr.schedule(start, end, branches)}


@router.post("/api/monitoring/wwwr/schedule", status_code=201)
def wwwr_add(body: AuditIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _wwwr_scope(request, user, None)
    out = _run(wwwr.save_audit, user, None, body.model_dump())
    activity.record("wwwr.schedule", user=user, request=request, page="/monitoring/wwwr",
                    summary=f"Jadwal WW-WR {out['branchName']} {out['day']} ({len(out['members'])} anggota)", details={"id": out["id"]})
    return out


@router.put("/api/monitoring/wwwr/schedule/{audit_id}")
def wwwr_update(audit_id: str, body: AuditIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _wwwr_scope(request, user, None)
    out = _run(wwwr.save_audit, user, audit_id, body.model_dump())
    activity.record("wwwr.schedule", user=user, request=request, page="/monitoring/wwwr",
                    summary=f"Jadwal WW-WR {out['branchName']} {out['day']} diubah", details={"id": audit_id})
    return out


@router.post("/api/monitoring/wwwr/schedule/{audit_id}/cancel")
def wwwr_cancel(audit_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _wwwr_scope(request, user, None)
    _run(wwwr.cancel_audit, audit_id)
    activity.record("wwwr.schedule", user=user, request=request, page="/monitoring/wwwr", summary="Jadwal WW-WR dibatalkan", details={"id": audit_id})
    return {"ok": True}


@router.get("/api/monitoring/wwwr/comparison")
def wwwr_comparison(request: Request, dateFrom: Optional[date] = None, dateTo: Optional[date] = None, branch: Optional[str] = None,
                    group: Optional[str] = Query(None, max_length=60), user: dict = Depends(current_user)) -> dict:
    branches = _wwwr_scope(request, user, branch, group)
    start, end = _period(dateFrom, dateTo)
    return wwwr.comparison(start, end, branches)


@router.get("/api/monitoring/wwwr/comparison/{visit_id}")
def wwwr_comparison_of(visit_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _mon_page("wwwr", request, user)
    out = wwwr.comparison_of(visit_id)
    scope = allowed_branches()
    if out is None or (scope is not None and out["branchCode"] not in scope):
        raise HTTPException(404, detail="Audit tidak ditemukan")
    return out


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
    out = vf.monitor(form, start, end, branches, userId)
    if form == "wwwr":
        teams = wwwr.teams_of([v["id"] for v in out["data"]])
        for v in out["data"]:
            v["audit"] = teams.get(v["id"])
    return {"period": {"from": start.isoformat(), "to": end.isoformat()}, **out}


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
    out = vf.full(visit_id)
    return {"definition": vf.DEFINITIONS[form], **(wwwr.with_team(out) if form == "wwwr" else out)}


def _image(row: Optional[dict], request: Request, user: dict) -> Response:
    if row is None:
        raise HTTPException(404, detail="Foto tidak ditemukan")
    own = has_page(user, f"form.{row['form']}") and vf.can_fill(row, user)  # WW-WR: a member of the audit
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
