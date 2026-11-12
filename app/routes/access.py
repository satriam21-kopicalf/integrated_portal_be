"""Page access and the job title / department lists (app/access.py).

    GET    /api/access/pages                     catalog + pages per role (superadmin; a COTR admin: COTR pages)
    PUT    /api/access/roles/{role}              {"pages": [...]} (superadmin; role cost_control or user)
    GET    /api/access/platforms                 menus per platform: catalog with the platforms each page exists on and
                                                 the set ones (superadmin; migration 054)
    PUT    /api/access/platforms                 {"platforms": {page: [portal|operations|cotr] | null}} (null = default)
    GET    /api/access/masters/{kind}            kind = jobTitle | department (any signed-in user: profile form)
    POST   /api/access/masters/{kind}            {"name": "..."} add (superadmin)
    PATCH  /api/access/masters/{kind}/{id}       {"name"?, "isActive"?} rename / (de)activate (superadmin)
"""
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import access, activity
from app.routes.auth import current_user, forget_sessions, require_account_admin, require_superadmin

router = APIRouter(prefix="/api/access", tags=["access"])
Kind = Literal["jobTitle", "department"]
KIND_LABEL = {"jobTitle": "Job title", "department": "Department"}


def catalog() -> list[dict]:
    return [{"key": k, "label": label, "group": group} for k, label, group in access.PAGES]


@router.get("/pages")
def pages(user: dict = Depends(require_account_admin)) -> dict:
    """The page catalog and the pages per role; a COTR admin (2026-11-13) gets the COTR pages only."""
    roles = {**access.role_pages(), access.COTR_ADMIN: access.role_base(access.COTR_ADMIN)}
    if user.get("role") == access.COTR_ADMIN:
        return {"pages": [p for p in catalog() if access.cotr_page(p["key"])],
                "roles": {r: [p for p in v if access.cotr_page(p)] for r, v in roles.items() if r in access.COTR_ROLES},
                "defaults": {}}
    return {"pages": catalog(), "roles": roles, "defaults": access.DEFAULT_ROLE_PAGES}


@router.get("/platforms")
def platforms(_user: dict = Depends(require_superadmin)) -> dict:
    pages = catalog() + [{"key": k, "label": label, "group": "Calf On The Road (driver)"} for k, label in access.DRIVER_MENU]
    return {"pages": [{**p, "available": access.available(p["key"])} for p in pages], "platforms": access.page_platforms()}


class PlatformsIn(BaseModel):
    platforms: dict[str, Optional[list[str]]]


@router.put("/platforms")
def put_platforms(body: PlatformsIn, request: Request, user: dict = Depends(require_superadmin)) -> dict:
    try:
        out = access.set_page_platforms(str(user["id"]), body.platforms)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from None
    activity.record("access.platforms", user=user, request=request, summary="Mengatur menu per platform",
                    details={"platforms": body.platforms})
    forget_sessions()
    return {"platforms": out}


class RolePages(BaseModel):
    pages: list[str] = Field(max_length=len(access.PAGE_KEYS))


@router.put("/roles/{role}")
def put_role(role: str, body: RolePages, request: Request, user: dict = Depends(require_superadmin)) -> dict:
    if role not in access.EDITABLE_ROLES:
        raise HTTPException(400, detail="Halaman Super Admin tidak dapat diubah (selalu semua halaman)")
    unknown = sorted(set(body.pages) - set(access.PAGE_KEYS))
    if unknown:
        raise HTTPException(422, detail=f"Halaman tidak dikenal: {', '.join(unknown)}")
    before = access.role_pages().get(role, [])
    access.set_role_pages(role, [p for p in access.PAGE_KEYS if p in body.pages], str(user["id"]))
    forget_sessions()
    activity.record("access.role_pages", user=user, request=request, summary=f"Halaman role {role} diubah",
                    details={"role": role, "before": before, "after": body.pages})
    return {"roles": access.role_pages()}


@router.get("/masters/{kind}")
def list_master(kind: Kind, _user: dict = Depends(current_user)) -> list[dict]:
    return access.list_master(kind)


class MasterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.post("/masters/{kind}", status_code=201)
def add_master(kind: Kind, body: MasterIn, request: Request, user: dict = Depends(require_superadmin)) -> dict:
    try:
        out = access.add_master(kind, body.name, str(user["id"]))
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    if out["created"]:
        activity.record("access.master_add", user=user, request=request, summary=f"{KIND_LABEL[kind]} ditambah: {out['name']}",
                        details={"kind": kind, "name": out["name"]})
    return out


class MasterPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    isActive: Optional[bool] = None


@router.patch("/masters/{kind}/{master_id}")
def patch_master(kind: Kind, master_id: UUID, body: MasterPatch, request: Request,
                 user: dict = Depends(require_superadmin)) -> dict:
    try:
        out = access.update_master(kind, str(master_id), body.name, body.isActive)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    if not out:
        raise HTTPException(404, detail="Data tidak ditemukan")
    forget_sessions()
    activity.record("access.master_update", user=user, request=request, summary=f"{KIND_LABEL[kind]} diubah: {out['name']}",
                    details={"kind": kind, **body.model_dump(exclude_none=True)})
    return out
