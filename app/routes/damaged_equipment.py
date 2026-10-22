"""Form → Damaged Equipment (page form.damaged-equipment) and Monitoring → Damaged Equipment (page mon.damaged-equipment);
app/damaged_equipment.py, migration 027.

PIC (form page; their own requests):
    GET    /api/de/form                                   branches, categories, own requests with progress
    POST   /api/de/requests                               {"kind", "branchCode"} → a new draft
    GET    /api/de/requests/{id}                          own request (draft or sent) in full
    PATCH  /api/de/requests/{id}                          draft: {"branchCode"?, "analysis"?, "cause"?, "items"?}
    POST   /api/de/requests/{id}/photos                   draft: camera photo {"photo", "latitude", "longitude", "takenAt"}
    POST   /api/de/requests/{id}/video?name=              draft: the video as the raw body (≤ 10 MB)
    DELETE /api/de/requests/{id}/media/{media}            draft
    POST   /api/de/requests/{id}/submit                   {"latitude", "longitude", "accuracyM", "signature", "signedName"}
    DELETE /api/de/requests/{id}                          discard the draft
Team (monitoring page; branches in scope):
    GET    /api/de/monitor?dateFrom&dateTo&branch&group   requests (open ones always) with progress, technicians
    GET    /api/de/monitor/{id}                           one request + related requests of the branch / PIC + allowed steps
    POST   /api/de/monitor/{id}/status                    {"to", "note"}
    POST   /api/de/monitor/{id}/notes                     {"note"}
    PUT    /api/de/monitor/{id}/cost                      {"estCost", "actualCost", "vendor"}
    POST   /api/de/monitor/{id}/assignments               {"technicianId", "scheduledFor", "note"}
    POST   /api/de/assignments/{id}/release               {"note"}
    POST   /api/de/assignments/{id}/swap                  {"toRequestId", "scheduledFor", "note"}
    POST   /api/de/monitor/{id}/duplicate                 {"ofId", "note"}
    POST   /api/de/monitor/{id}/after-photos              photo of the work done; DELETE …/after-photos/{media}
    POST   /api/de/monitor/{id}/invoices?name=&note=      invoice / receipt of the cost (PDF or photo, raw body ≤ 10 MB);
                                                          DELETE …/invoices/{media}
    GET|POST|PATCH /api/de/categories[/{id}]              master categories (GET: form or monitoring page)
    GET|POST|PATCH /api/de/technicians[/{id}]             technicians with their active jobs
Both: GET /api/de/requests/{id}/pdf, POST /api/de/requests/{id}/share (renew the link), GET /api/de/media/{id},
      GET /api/de/requests/{id}/signature
Public (share link, no sign-in): GET /api/public/de/{token}, …/pdf, …/media/{id}, …/signature
"""
from datetime import date, datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import activity, branch_groups
from app import damaged_equipment as de
from app import de_pdf
from app.access import deny, has_page
from app.config import get_settings
from app.pic_checkin import CheckInError
from app.routes.auth import current_user
from app.scope import NO_BRANCH, allowed_branches, scoped_branch
from app.utils import parse_branches, today

router = APIRouter(tags=["damaged equipment"])
FORM, MON = "form.damaged-equipment", "mon.damaged-equipment"


def _need(request: Request, user: dict, *pages: str) -> None:
    if not any(has_page(user, p) for p in pages):
        deny(request, user)


def _run(fn, *args):
    try:
        return fn(*args)
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from None
    except CheckInError as e:  # DeError, or a photo / signature that cannot be read
        raise HTTPException(422, detail=str(e)) from None


def _team_row(request: Request, user: dict, request_id: str) -> dict:
    _need(request, user, MON)
    row = de.get_row(request_id)
    scope = allowed_branches()
    if row is None or row["status"] == "DRAFT" or (scope is not None and row["branch_code"] not in scope):
        raise HTTPException(404, detail="Pengajuan tidak ditemukan")
    return row


def _readable(request: Request, user: dict, row: Optional[dict]) -> dict:
    """The PIC's own request (form page) or a sent request in scope (monitoring page)."""
    if row is None:
        raise HTTPException(404, detail="Pengajuan tidak ditemukan")
    own = row["user_id"] == str(user["id"]) and has_page(user, FORM)
    scope = allowed_branches()
    team = row["status"] != "DRAFT" and has_page(user, MON) and (scope is None or row["branch_code"] in scope)
    if not (own or team):
        deny(request, user)
    return row


def _share_url(token: Optional[str]) -> Optional[str]:
    return f"{get_settings().portal_url.rstrip('/')}/share/de/{token}" if token else None


def _record(request: Request, user: dict, summary: str, row_id: str, **details) -> None:
    activity.record("de.request", user=user, request=request, page="/monitoring/damaged-equipment", summary=summary,
                    details={"id": row_id, **details})


# ---------------------------------------------------------------- PIC

class NewIn(BaseModel):
    kind: Literal["REPAIR", "PROCUREMENT"]
    branchCode: str = Field(min_length=1, max_length=40)


class ItemIn(BaseModel):
    categoryId: int
    name: str = Field(min_length=1, max_length=120)
    qty: int = Field(1, ge=1, le=9999)
    location: Optional[str] = Field(None, max_length=120)
    urgency: Optional[str] = None
    note: Optional[str] = Field(None, max_length=1000)


class DraftIn(BaseModel):
    branchCode: Optional[str] = Field(None, max_length=40)
    analysis: Optional[str] = Field(None, max_length=4000)
    cause: Optional[str] = None
    items: Optional[list[ItemIn]] = Field(None, max_length=de.MAX_ITEMS)


class PhotoIn(BaseModel):
    photo: str = Field(min_length=20, max_length=5_000_000)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    takenAt: Optional[datetime] = None


class SubmitIn(BaseModel):
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    accuracyM: Optional[float] = Field(None, ge=0, le=100_000)
    signature: Optional[str] = Field(None, min_length=20, max_length=5_000_000)
    signedName: Optional[str] = Field(None, max_length=120)


@router.get("/api/de/form")
def form(request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    return de.form_state(user)


@router.post("/api/de/requests", status_code=201)
def new_request(body: NewIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    return _run(de.create_draft, user, body.kind, body.branchCode)


@router.get("/api/de/requests/{request_id}")
def get_request(request_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    row = de.get_row(request_id)
    if row is None or row["user_id"] != str(user["id"]):
        raise HTTPException(404, detail="Pengajuan tidak ditemukan")
    out = de.full(request_id)
    out["shareUrl"] = _share_url(row["share_token"])
    return out


@router.patch("/api/de/requests/{request_id}")
def save_draft(request_id: str, body: DraftIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    return _run(de.save_draft, user, request_id, body.model_dump(exclude_unset=True))


@router.post("/api/de/requests/{request_id}/photos", status_code=201)
def add_photo(request_id: str, body: PhotoIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    _run(de.own_draft, user, request_id)
    return _run(de.add_photo, user, request_id, body.photo, body.latitude, body.longitude, body.takenAt)


@router.post("/api/de/requests/{request_id}/video", status_code=201)
async def add_video(request_id: str, request: Request, name: str = Query("video", max_length=160),
                    user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    if int(request.headers.get("content-length") or 0) > de.MAX_VIDEO_BYTES + 1024:
        raise HTTPException(422, detail=f"Video maksimal {de.MAX_VIDEO_BYTES // 1048576} MB")
    data = await request.body()
    return await run_in_threadpool(_run, de.add_video, user, request_id, data, name)


@router.delete("/api/de/requests/{request_id}/media/{media_id}")
def delete_media(request_id: str, media_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    _run(de.own_draft, user, request_id)
    return de.delete_media(request_id, media_id, ("PHOTO", "VIDEO"))


@router.post("/api/de/requests/{request_id}/submit")
def submit(request_id: str, body: SubmitIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    out = _run(de.submit, user, request_id, body.latitude, body.longitude, body.accuracyM, body.signature, body.signedName)
    _record(request, user, f"Pengajuan {out['number']} ({out['kindLabel']}) {out['branchName']} dikirim", request_id,
            urgency=out["urgency"], items=len(out["items"]))
    row = de.get_row(request_id)
    out["shareUrl"] = _share_url(row["share_token"])
    return out


@router.delete("/api/de/requests/{request_id}")
def discard(request_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, FORM)
    _run(de.discard, user, request_id)
    return {"ok": True}


# ---------------------------------------------------------------- both: document, link, media

@router.get("/api/de/requests/{request_id}/pdf")
def pdf(request_id: str, request: Request, user: dict = Depends(current_user)) -> Response:
    row = _readable(request, user, de.get_row(request_id))
    if row["status"] == "DRAFT":
        raise HTTPException(422, detail="Dokumen dibuat setelah pengajuan dikirim")
    data, name = de_pdf.build(request_id, _share_url(row["share_token"]))
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/api/de/requests/{request_id}/share")
def renew_share(request_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _readable(request, user, de.get_row(request_id))
    out = _run(de.renew_link, request_id)
    return {**out, "url": _share_url(out["token"])}


def _file(m: Optional[dict]) -> FileResponse:
    if m is None or m["path"] is None:
        raise HTTPException(404, detail="File tidak ditemukan")
    return FileResponse(m["path"], media_type=m["mime"], filename=m["file_name"] or None, content_disposition_type="inline",
                        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


@router.get("/api/de/media/{media_id}")
def media(media_id: str, request: Request, user: dict = Depends(current_user)) -> FileResponse:
    m = de.media_file(media_id)
    if m is None:
        raise HTTPException(404, detail="File tidak ditemukan")
    _readable(request, user, de.get_row(m["request_id"]))
    return _file(m)


@router.get("/api/de/requests/{request_id}/signature")
def signature(request_id: str, request: Request, user: dict = Depends(current_user)) -> Response:
    _readable(request, user, de.get_row(request_id))
    s = de.signature_of(request_id)
    if s is None:
        raise HTTPException(404, detail="Belum ditandatangani")
    return Response(bytes(s["signature"]), media_type=s["signature_type"], headers={"Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------- team

class StatusIn(BaseModel):
    to: str = Field(min_length=1, max_length=20)
    note: Optional[str] = Field(None, max_length=2000)


class NoteIn(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class CostIn(BaseModel):
    estCost: Optional[float] = Field(None, ge=0, le=1e12)
    actualCost: Optional[float] = Field(None, ge=0, le=1e12)
    vendor: Optional[str] = Field(None, max_length=120)


class AssignIn(BaseModel):
    technicianId: int
    scheduledFor: Optional[date] = None
    note: Optional[str] = Field(None, max_length=500)


class SwapIn(BaseModel):
    toRequestId: str = Field(min_length=1, max_length=40)
    scheduledFor: Optional[date] = None
    note: str = Field(min_length=1, max_length=500)


class DuplicateIn(BaseModel):
    ofId: str = Field(min_length=1, max_length=40)
    note: str = Field(min_length=1, max_length=2000)


@router.get("/api/de/monitor")
def monitor(request: Request, dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None),
            branch: Optional[str] = None, group: Optional[str] = Query(None, max_length=60), user: dict = Depends(current_user)) -> dict:
    _need(request, user, MON)
    end = dateTo or today()
    start = dateFrom or end - timedelta(days=89)
    if start > end:
        raise HTTPException(422, detail="Tanggal mulai harus sebelum tanggal akhir")
    if (end - start).days > 366:
        raise HTTPException(422, detail="Maksimal 1 tahun sekali tampil")
    scoped = scoped_branch(branch)
    branches = None if scoped is None else ([] if scoped == NO_BRANCH else parse_branches(scoped))
    branches = branch_groups.narrow(branches, group)
    return {"period": {"from": start.isoformat(), "to": end.isoformat()}, **de.monitor(start, end, branches)}


@router.get("/api/de/monitor/{request_id}")
def team_get(request_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    row = _team_row(request, user, request_id)
    out = de.team_full(request_id)
    out["shareUrl"] = _share_url(row["share_token"])
    return out


@router.post("/api/de/monitor/{request_id}/status")
def team_status(request_id: str, body: StatusIn, request: Request, user: dict = Depends(current_user)) -> dict:
    row = _team_row(request, user, request_id)
    out = _run(de.change_status, user, request_id, body.to, body.note)
    _record(request, user, f"{row['number']}: {de.STATUS_LABEL[row['status']]} → {de.STATUS_LABEL[body.to]}", request_id, note=body.note)
    return out


@router.post("/api/de/monitor/{request_id}/notes")
def team_note(request_id: str, body: NoteIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _team_row(request, user, request_id)
    return _run(de.add_note, user, request_id, body.note)


@router.put("/api/de/monitor/{request_id}/cost")
def team_cost(request_id: str, body: CostIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _team_row(request, user, request_id)
    return _run(de.set_cost, user, request_id, body.estCost, body.actualCost, body.vendor)


@router.post("/api/de/monitor/{request_id}/assignments", status_code=201)
def team_assign(request_id: str, body: AssignIn, request: Request, user: dict = Depends(current_user)) -> dict:
    row = _team_row(request, user, request_id)
    out = _run(de.assign, user, request_id, body.technicianId, body.scheduledFor, body.note)
    _record(request, user, f"{row['number']}: tukang ditugaskan", request_id, technicianId=body.technicianId)
    return out


def _assignment_row(request: Request, user: dict, assignment_id: int) -> dict:
    _need(request, user, MON)
    from app import database as db
    a = db.fetchrow("SELECT request_id::text AS rid FROM integration_portal.de_assignment WHERE id = %s", (assignment_id,))
    if a is None:
        raise HTTPException(404, detail="Penugasan tidak ditemukan")
    return _team_row(request, user, a["rid"])


@router.post("/api/de/assignments/{assignment_id}/release")
def team_release(assignment_id: int, body: NoteIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _assignment_row(request, user, assignment_id)
    return _run(de.release, user, assignment_id, body.note)


@router.post("/api/de/assignments/{assignment_id}/swap")
def team_swap(assignment_id: int, body: SwapIn, request: Request, user: dict = Depends(current_user)) -> dict:
    src = _assignment_row(request, user, assignment_id)
    dst = _team_row(request, user, body.toRequestId)
    out = _run(de.swap, user, assignment_id, body.toRequestId, body.scheduledFor, body.note)
    _record(request, user, f"Pergeseran tukang {src['number']} → {dst['number']}", body.toRequestId, note=body.note)
    return out


@router.post("/api/de/monitor/{request_id}/duplicate")
def team_duplicate(request_id: str, body: DuplicateIn, request: Request, user: dict = Depends(current_user)) -> dict:
    row = _team_row(request, user, request_id)
    _team_row(request, user, body.ofId)
    out = _run(de.mark_duplicate, user, request_id, body.ofId, body.note)
    _record(request, user, f"{row['number']} ditandai duplikat", request_id, of=body.ofId)
    return out


@router.post("/api/de/monitor/{request_id}/after-photos", status_code=201)
def team_after_photo(request_id: str, body: PhotoIn, request: Request, user: dict = Depends(current_user)) -> dict:
    row = _team_row(request, user, request_id)
    if row["status"] not in ("ACCEPTED", "IN_PROGRESS", "DONE"):
        raise HTTPException(422, detail="Foto hasil pekerjaan diunggah saat pengajuan dikerjakan")
    return _run(de.add_photo, user, request_id, body.photo, body.latitude, body.longitude, body.takenAt, "AFTER")


@router.post("/api/de/monitor/{request_id}/invoices", status_code=201)
async def team_invoice(request_id: str, request: Request, name: str = Query("invoice", max_length=160), note: Optional[str] = Query(None, max_length=200),
                       user: dict = Depends(current_user)) -> dict:
    await run_in_threadpool(_team_row, request, user, request_id)
    if int(request.headers.get("content-length") or 0) > de.MAX_INVOICE_BYTES + 1024:
        raise HTTPException(422, detail=f"File invoice maksimal {de.MAX_INVOICE_BYTES // 1048576} MB")
    data = await request.body()
    return await run_in_threadpool(_run, de.add_invoice, user, request_id, data, name, note)


@router.delete("/api/de/monitor/{request_id}/invoices/{media_id}")
def team_delete_invoice(request_id: str, media_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    row = _team_row(request, user, request_id)
    if row["status"] == "DONE":
        raise HTTPException(422, detail="Pengajuan sudah Done")
    return de.delete_media(request_id, media_id, ("INVOICE",))


@router.delete("/api/de/monitor/{request_id}/after-photos/{media_id}")
def team_delete_after(request_id: str, media_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    row = _team_row(request, user, request_id)
    if row["status"] == "DONE":
        raise HTTPException(422, detail="Pengajuan sudah Done")
    return de.delete_media(request_id, media_id, ("AFTER",))


# ---------------------------------------------------------------- masters

class CategoryIn(BaseModel):
    group: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=80)
    defaultUrgency: str
    examples: list[str] = Field(default_factory=list, max_length=30)
    sort: int = Field(0, ge=0, le=9999)
    isActive: bool = True


class TechnicianIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["INTERNAL", "VENDOR"] = "INTERNAL"
    company: Optional[str] = Field(None, max_length=120)
    skills: list[str] = Field(default_factory=list, max_length=20)
    city: Optional[str] = Field(None, max_length=80)
    phone: Optional[str] = Field(None, max_length=40)
    notes: Optional[str] = Field(None, max_length=500)
    isActive: bool = True
    wageType: Literal["DAILY", "MONTHLY"] = "DAILY"
    wageAmount: Optional[float] = Field(None, ge=0, le=1e12)


@router.get("/api/de/categories")
def get_categories(request: Request, all: bool = False, user: dict = Depends(current_user)) -> list[dict]:
    _need(request, user, FORM, MON)
    return de.categories(active_only=not (all and has_page(user, MON)))


@router.post("/api/de/categories", status_code=201)
def new_category(body: CategoryIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, MON)
    return _run(de.save_category, None, body.model_dump())


@router.patch("/api/de/categories/{category_id}")
def edit_category(category_id: int, body: CategoryIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, MON)
    return _run(de.save_category, category_id, body.model_dump())


@router.get("/api/de/technicians")
def get_technicians(request: Request, user: dict = Depends(current_user)) -> list[dict]:
    _need(request, user, MON)
    return de.technicians()


@router.post("/api/de/technicians", status_code=201)
def new_technician(body: TechnicianIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, MON)
    return _run(de.save_technician, None, body.model_dump())


@router.patch("/api/de/technicians/{technician_id}")
def edit_technician(technician_id: int, body: TechnicianIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, MON)
    return _run(de.save_technician, technician_id, body.model_dump())


# ---------------------------------------------------------------- public share link (no sign-in)

def _public(token: str) -> dict:
    row = de.by_token(token)
    if row is None:
        raise HTTPException(404, detail="Link tidak ditemukan atau sudah kedaluwarsa")
    return row


@router.get("/api/public/de/{token}")
def public_view(token: str) -> dict:
    row = _public(token)
    out = de.full(row["id"])
    out.pop("openInBranch", None)
    out.pop("userId", None)
    out = de.without_invoices(out)  # cost documents stay with the team and the PIC
    out["shareExpiresAt"] = row["share_expires_at"].isoformat()
    return out


@router.get("/api/public/de/{token}/pdf")
def public_pdf(token: str) -> Response:
    row = _public(token)
    data, name = de_pdf.build(row["id"], _share_url(token), public=True)
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/api/public/de/{token}/media/{media_id}")
def public_media(token: str, media_id: str) -> FileResponse:
    row = _public(token)
    m = de.media_file(media_id)
    if m is None or m["request_id"] != row["id"] or m["kind"] == "INVOICE":
        raise HTTPException(404, detail="File tidak ditemukan")
    return _file(m)


@router.get("/api/public/de/{token}/signature")
def public_signature(token: str) -> Response:
    row = _public(token)
    s = de.signature_of(row["id"])
    if s is None:
        raise HTTPException(404, detail="Belum ditandatangani")
    return Response(bytes(s["signature"]), media_type=s["signature_type"], headers={"Cache-Control": "private, max-age=3600"})
