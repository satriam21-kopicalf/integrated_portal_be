"""Page access and the job title / department lists (app/access.py).

    GET    /api/access/pages                     catalog + pages per role (superadmin)
    PUT    /api/access/roles/{role}              {"pages": [...]} (superadmin; role cost_control or user)
    GET    /api/access/masters/{kind}            kind = jobTitle | department (any signed-in user: profile form)
    POST   /api/access/masters/{kind}            {"name": "..."} add (superadmin)
    PATCH  /api/access/masters/{kind}/{id}       {"name"?, "isActive"?} rename / (de)activate (superadmin)
"""
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import access, activity
from app.routes.auth import current_user, forget_sessions, require_superadmin

router = APIRouter(prefix="/api/access", tags=["access"])
Kind = Literal["jobTitle", "department"]
KIND_LABEL = {"jobTitle": "Job title", "department": "Department"}


def catalog() -> list[dict]:
    return [{"key": k, "label": label, "group": group} for k, label, group in access.PAGES]


@router.get("/pages")
def pages(_user: dict = Depends(require_superadmin)) -> dict:
    return {"pages": catalog(), "roles": access.role_pages(), "defaults": access.DEFAULT_ROLE_PAGES}


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
