"""Building & Renovation (app/building.py, migration 162), page mon.building.

    GET    /api/building/catalog                     materials, groups, vendors and the summary (?all=1: the inactive too)
    POST   /api/building/materials                   a material {group, category, spec, detail, unit, price, priceOn, vendorId | vendor, note, repeat}
    PUT    /api/building/materials/{id}              the same, plus {active}
    GET    /api/building/materials/{id}/prices       the price history
    GET    /api/building/pick?q=                     the materials matching q, for a cost line (page mon.building or mon.damaged-equipment)
    POST   /api/building/vendors                     a store / supplier {kind, name, location, goods, contactName, phone, maps, website, bank, accountNo, accountName, note}
    PUT    /api/building/vendors/{id}                the same, plus {active}

Projects (app/building_project.py, migration 164); the RAB is approved by management (the approvers, migration 165):
    GET    /api/building/projects                    every project with its RAB total, actual and progress
    POST   /api/building/projects                    {kind, name, branchCode | locationName, address, city, areaM2, businessUnit, picName, contractor, planStart, planEnd, budget, note}
    GET    /api/building/projects/{id}               the project, RAB (sections, lines, versions), stages, reports, S-curve
    PUT    /api/building/projects/{id}               the same fields as POST (plus actualStart, actualEnd)
    POST   /api/building/projects/{id}/status        {to, note}
    POST   /api/building/projects/{id}/sections      {name}; PUT /sections/{sid} {name}; DELETE /sections/{sid}
    POST   /api/building/projects/{id}/lines         {sectionId, kind, materialId, description, spec, unit, qty, unitPrice, priceOn, vendorId | vendor, note, actualQty, actualAmount}
    PUT    /api/building/projects/{id}/lines/{lid}   the same; DELETE /lines/{lid}
    PUT    /api/building/projects/{id}/lines/{lid}/actual   {actualQty, actualAmount} (an approved RAB stays approved)
    POST   /api/building/projects/{id}/rab/submit
    POST   /api/building/projects/{id}/rab/decide    {approve, note}
    GET    /api/building/projects/{id}/rab/{version} an approved version
    PUT    /api/building/projects/{id}/stages        {stages: [{id?, name, weight, planStart, planEnd}]}
    POST   /api/building/projects/{id}/progress      {stageId, progress, note, reportedOn, photos: [data URL]}
    GET    /api/building/photos/{photoId}
    POST   /api/building/projects/{id}/fund          {lineIds, payee: {type, bank, account, name}, note}: a Fund Request (kind BUILDING)
    GET    /api/building/approvers                   who approves the RAB (management, migration 165); PUT {ids} superadmin only
"""
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app import activity
from app import building as bd
from app import building_project as bp
from app.access import deny, has_page
from app.routes.auth import current_user

router = APIRouter(tags=["building"])
PAGE = "mon.building"
CAMEL = {"contactName": "contact_name", "accountNo": "account_no", "accountName": "account_name"}


def _need(request: Request, user: dict) -> None:
    if not has_page(user, PAGE):
        deny(request, user)


def _run(fn, *args):
    try:
        return fn(*args)
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from None
    except (bd.BuildingError, ValueError) as e:
        raise HTTPException(422, detail=str(e)) from None


def _body(b: Any) -> dict:
    if not isinstance(b, dict):
        raise HTTPException(422, detail="Data tidak valid")
    return {CAMEL.get(k, k): v for k, v in b.items()}


@router.get("/api/building/catalog")
def catalog(request: Request, all: Optional[int] = None, user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    return {**bd.materials(bool(all)), "vendors": bd.vendors(bool(all)), "summary": bd.summary(), "kinds": bd.KINDS}


@router.post("/api/building/materials", status_code=201)
def add_material(request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    out = _run(bd.save_material, user, None, _body(body))
    activity.record("building.material", user=user, request=request, page="/monitoring/building", summary=f"Menambah material {out['category']} {out['spec'] or ''}".strip())
    return out


@router.put("/api/building/materials/{material_id}")
def edit_material(material_id: int, request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    out = _run(bd.save_material, user, material_id, _body(body))
    activity.record("building.material", user=user, request=request, page="/monitoring/building", summary=f"Mengubah material {out['category']} {out['spec'] or ''}".strip())
    return out


@router.get("/api/building/materials/{material_id}/prices")
def material_prices(material_id: int, request: Request, user: dict = Depends(current_user)) -> list[dict]:
    _need(request, user)
    return bd.prices(material_id)


@router.post("/api/building/vendors", status_code=201)
def add_vendor(request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    out = _run(bd.save_vendor, user, None, _body(body))
    activity.record("building.vendor", user=user, request=request, page="/monitoring/building", summary=f"Menambah {out['kindLabel'].lower()} {out['name']}")
    return out


@router.put("/api/building/vendors/{vendor_id}")
def edit_vendor(vendor_id: int, request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    out = _run(bd.save_vendor, user, vendor_id, _body(body))
    activity.record("building.vendor", user=user, request=request, page="/monitoring/building", summary=f"Mengubah {out['kindLabel'].lower()} {out['name']}")
    return out


@router.get("/api/building/pick")
def pick(request: Request, q: str = "", user: dict = Depends(current_user)) -> list[dict]:
    if not (has_page(user, PAGE) or has_page(user, "mon.damaged-equipment")):
        deny(request, user)
    return bd.search(q[:100])



# ---------------------------------------------------------------- projects, RAB, progress

def _log(request: Request, user: dict, pid: str, text: str) -> None:
    activity.record("building.project", user=user, request=request, page="/monitoring/building", summary=text, details={"project": pid})


def _project(out: dict, user: dict) -> dict:
    out["canApprove"] = bp.can_approve(user)
    out["approvers"] = [a["name"] for a in bp.approvers()]
    return out


@router.get("/api/building/projects")
def project_list(request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    return {"data": bp.projects(), "kinds": bp.KINDS, "status": bp.STATUS, "sections": list(bp.SECTIONS),
            "canApprove": bp.can_approve(user), "approvers": bp.approvers(), "canSetApprovers": user.get("role") == "superadmin"}


@router.post("/api/building/projects", status_code=201)
def project_create(request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    out = _run(bp.create, user, _body(body))
    _log(request, user, out["id"], f"Membuat proyek {out['number']} {out['name']}")
    return _project(out, user)


@router.get("/api/building/projects/{pid}")
def project_get(pid: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    return _project(_run(bp.full, pid), user)


@router.put("/api/building/projects/{pid}")
def project_edit(pid: str, request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    out = _run(bp.update, user, pid, _body(body))
    _log(request, user, pid, f"Mengubah proyek {out['number']}")
    return _project(out, user)


@router.post("/api/building/projects/{pid}/status")
def project_status(pid: str, request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    b = _body(body)
    out = _run(bp.set_status, user, pid, str(b.get("to") or ""), b.get("note"))
    _log(request, user, pid, f"Proyek {out['number']}: {out['statusLabel']}")
    return _project(out, user)


@router.post("/api/building/projects/{pid}/sections", status_code=201)
def section_add(pid: str, request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    return _project(_run(bp.save_section, user, pid, None, str(_body(body).get("name") or "")), user)


@router.put("/api/building/projects/{pid}/sections/{sid}")
def section_edit(pid: str, sid: int, request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    return _project(_run(bp.save_section, user, pid, sid, str(_body(body).get("name") or "")), user)


@router.delete("/api/building/projects/{pid}/sections/{sid}")
def section_delete(pid: str, sid: int, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    return _project(_run(bp.delete_section, user, pid, sid), user)


@router.post("/api/building/projects/{pid}/lines", status_code=201)
def line_add(pid: str, request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    return _project(_run(bp.save_line, user, pid, None, _body(body)), user)


@router.put("/api/building/projects/{pid}/lines/{lid}")
def line_edit(pid: str, lid: int, request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    return _project(_run(bp.save_line, user, pid, lid, _body(body)), user)


@router.put("/api/building/projects/{pid}/lines/{lid}/actual")
def line_actual(pid: str, lid: int, request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    b = _body(body)
    return _project(_run(bp.save_actual, user, pid, lid, b.get("actualQty"), b.get("actualAmount")), user)


@router.delete("/api/building/projects/{pid}/lines/{lid}")
def line_delete(pid: str, lid: int, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    return _project(_run(bp.delete_line, user, pid, lid), user)


@router.post("/api/building/projects/{pid}/rab/submit")
def rab_submit(pid: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    out = _run(bp.rab_submit, user, pid)
    _log(request, user, pid, f"Mengirim RAB {out['number']} untuk approval")
    return _project(out, user)


@router.post("/api/building/projects/{pid}/rab/decide")
def rab_decide(pid: str, request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    if not bp.can_approve(user):  # management only (migration 165)
        deny(request, user)
    b = _body(body)
    out = _run(bp.rab_decide, user, pid, bool(b.get("approve")), b.get("note"))
    _log(request, user, pid, f"RAB {out['number']} {'disetujui v' + str(out['rabVersion']) if b.get('approve') else 'dikembalikan'}")
    return _project(out, user)


@router.get("/api/building/projects/{pid}/rab/{version}")
def rab_version(pid: str, version: int, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    return _run(bp.rab_version, pid, version)


@router.put("/api/building/projects/{pid}/stages")
def stages_save(pid: str, request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    stages = _body(body).get("stages")
    if not isinstance(stages, list) or len(stages) > 40:
        raise HTTPException(422, detail="Tahapan tidak valid")
    return _project(_run(bp.save_stages, user, pid, stages), user)


@router.post("/api/building/projects/{pid}/progress", status_code=201)
def progress_add(pid: str, request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    b = _body(body)
    photos = b.get("photos") or []
    if not isinstance(photos, list):
        raise HTTPException(422, detail="Foto tidak valid")
    try:
        out = bp.report(user, pid, int(b.get("stageId") or 0), float(b.get("progress") or 0), b.get("note"), b.get("reportedOn"), [str(x) for x in photos])
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from None
    except ValueError as e:  # BuildingError, CheckInError
        raise HTTPException(422, detail=str(e)) from None
    _log(request, user, pid, f"Laporan progress {out['number']}: {out['progress']}%")
    return _project(out, user)


@router.get("/api/building/photos/{photo_id}")
def progress_photo(photo_id: str, request: Request, user: dict = Depends(current_user)):
    _need(request, user)
    f = bp.photo(photo_id)
    if f is None or not f["path"].exists():
        raise HTTPException(404, detail="Foto tidak ditemukan")
    return FileResponse(f["path"], media_type=f["mime"], headers={"Cache-Control": "private, max-age=86400"})


@router.get("/api/building/approvers")
def approvers_get(request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    return {"approvers": bp.approvers(), "candidates": bp.candidates() if user.get("role") == "superadmin" else []}


@router.put("/api/building/approvers")
def approvers_set(request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "superadmin":
        deny(request, user)
    ids = _body(body).get("ids")
    if not isinstance(ids, list):
        raise HTTPException(422, detail="Daftar approver tidak valid")
    out = _run(bp.set_approvers, user, ids)
    activity.record("building.approvers", user=user, request=request, page="/monitoring/building", summary=f"Approver RAB: {', '.join(a['name'] for a in out) or '-'}")
    return {"approvers": out, "candidates": bp.candidates()}


@router.post("/api/building/projects/{pid}/fund", status_code=201)
def project_fund(pid: str, request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    b = _body(body)
    ids = b.get("lineIds")
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        raise HTTPException(422, detail="Baris RAB tidak valid")
    out = _run(bp.request_fund, user, pid, ids, b.get("payee") or {}, b.get("note"))
    _log(request, user, pid, f"Mengajukan dana proyek {out['number']}: {len(ids)} baris RAB")
    return _project(out, user)
