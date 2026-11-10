"""Monitoring → Branch groups and Overview (app/branch_groups.py, app/monitoring_overview.py). Registered before the
generic /api/monitoring/{form} routes.

    GET    /api/monitoring/branch-groups               any monitoring page: groups with their branches + outlets without a group
    POST   /api/monitoring/branch-groups               page mon.branch-groups: {"code", "name", "description", "sort"}
    PATCH  /api/monitoring/branch-groups/{id}          page mon.branch-groups
    DELETE /api/monitoring/branch-groups/{id}          page mon.branch-groups (its branches become "without a group")
    PUT    /api/monitoring/branch-groups/{id}/branches page mon.branch-groups: {"branchCodes": [...]}
    GET    /api/monitoring/overview                    page mon.overview: ?dateFrom&dateTo&group

The PIC assessment settings (migration 069, app/pic_settings.py, app/form_scope.py), page mon.branch-groups to change:
    GET    /api/monitoring/outlet-settings             every outlet: group deadline, own check-in deadline, time zone
    PUT    /api/monitoring/outlet-settings/{code}      {"deadline": "HH:MM" | null, "timezone": "Asia/Makassar" …}
    GET    /api/monitoring/form-items                  the Product / Outlet items asked per branch group
    PUT    /api/monitoring/form-items                  {"changes": [{"form", "itemKey", "group", "shown"}]}
    GET    /api/monitoring/sales-targets?month=YYYY-MM the net sales of every outlet against its own months before
"""
from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import activity
from app import branch_groups as bg
from app import form_scope, monitoring_overview, pic_settings
from app import visit_forms as vf
from app.access import PAGE_KEYS, deny, has_page
from app.routes.auth import current_user
from app.scope import allowed_branches
from app.utils import today

router = APIRouter(tags=["branch groups"])
MON_PAGES = tuple(p for p in PAGE_KEYS if p.startswith("mon."))


class GroupIn(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=80)
    description: Optional[str] = Field(None, max_length=300)
    sort: int = Field(0, ge=0, le=9999)
    checkinMaxPerDay: Optional[int] = Field(None, ge=1, le=50)  # PIC check-ins per outlet per day (migration 066)
    checkinDeadline: Optional[str] = Field(None, max_length=5)   # HH:MM, on time until then (migration 069)


class MembersIn(BaseModel):
    branchCodes: list[str] = Field(max_length=1000)


def _need(request: Request, user: dict, *pages: str) -> None:
    if not any(has_page(user, p) for p in pages):
        deny(request, user)


def _run(fn, *args):
    try:
        return fn(*args)
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from None
    except (bg.GroupError, pic_settings.SettingError, ValueError) as e:
        raise HTTPException(422, detail=str(e)) from None


@router.get("/api/monitoring/branch-groups")
def groups(request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, *MON_PAGES)
    return bg.list_groups()


@router.post("/api/monitoring/branch-groups", status_code=201)
def create(body: GroupIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, "mon.branch-groups")
    out = _run(bg.save_group, None, body.code, body.name, body.description, body.sort, str(user["id"]), body.checkinMaxPerDay, body.checkinDeadline)
    activity.record("user.branch_group", user=user, request=request, page="/monitoring/branch-groups", summary=f"Membuat grup cabang {body.code.upper()}")
    return out


@router.patch("/api/monitoring/branch-groups/{group_id}")
def update(group_id: str, body: GroupIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, "mon.branch-groups")
    out = _run(bg.save_group, group_id, body.code, body.name, body.description, body.sort, str(user["id"]), body.checkinMaxPerDay, body.checkinDeadline)
    activity.record("user.branch_group", user=user, request=request, page="/monitoring/branch-groups", summary=f"Mengubah grup cabang {body.code.upper()}")
    return out


@router.delete("/api/monitoring/branch-groups/{group_id}")
def delete(group_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, "mon.branch-groups")
    _run(bg.delete_group, group_id)
    activity.record("user.branch_group", user=user, request=request, page="/monitoring/branch-groups", summary="Menghapus grup cabang", details={"id": group_id})
    return {"ok": True}


@router.put("/api/monitoring/branch-groups/{group_id}/branches")
def members(group_id: str, body: MembersIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, "mon.branch-groups")
    out = _run(bg.set_members, group_id, sorted(set(body.branchCodes)))
    activity.record("user.branch_group", user=user, request=request, page="/monitoring/branch-groups",
                    summary=f"Mengatur {out['branches']} cabang grup", details={"id": group_id, "branches": body.branchCodes[:200]})
    return out


@router.get("/api/monitoring/overview")
def overview(request: Request, dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None),
             group: Optional[str] = Query(None, max_length=60), user: dict = Depends(current_user)) -> dict:
    _need(request, user, "mon.overview")
    end = dateTo or today()
    start = dateFrom or end - timedelta(days=29)
    if start > end:
        raise HTTPException(422, detail="Tanggal mulai harus sebelum tanggal akhir")
    if (end - start).days > 366:
        raise HTTPException(422, detail="Maksimal 1 tahun sekali tampil")
    scope = allowed_branches()
    return {"period": {"from": start.isoformat(), "to": end.isoformat()},
            **monitoring_overview.overview(start, end, None if scope is None else list(scope), group or None)}


# ---------------------------------------------------------------- PIC assessment settings (migration 069)

class OutletSettingIn(BaseModel):
    deadline: Optional[str] = Field(None, max_length=5)
    timezone: Optional[str] = Field(None, max_length=30)


class ScopeChange(BaseModel):
    form: str = Field(pattern="^(product|outlet)$")
    itemKey: str = Field(max_length=40)
    group: str = Field(max_length=20)
    shown: bool


class ScopeIn(BaseModel):
    changes: list[ScopeChange] = Field(max_length=2000)


@router.get("/api/monitoring/outlet-settings")
def outlet_settings(request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, *MON_PAGES)
    return pic_settings.outlet_settings()


@router.put("/api/monitoring/outlet-settings/{code}")
def save_outlet_setting(code: str, body: OutletSettingIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, "mon.branch-groups")
    out = _run(pic_settings.save_outlet, code, body.deadline, body.timezone, str(user["id"]))
    activity.record("user.branch_group", user=user, request=request, page="/monitoring/branch-groups",
                    summary=f"Mengatur batas check in {code}", details=out)
    return out


@router.get("/api/monitoring/form-items")
def form_items(request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, *MON_PAGES)
    return form_scope.matrix(vf.DEFINITIONS)


@router.put("/api/monitoring/form-items")
def save_form_items(body: ScopeIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, "mon.branch-groups")
    out = _run(form_scope.save, [c.model_dump() for c in body.changes], str(user["id"]), vf.DEFINITIONS)
    activity.record("user.branch_group", user=user, request=request, page="/monitoring/branch-groups",
                    summary=f"Mengatur butir form per grup ({out['saved']} perubahan)")
    return out


class BranchScopeChange(BaseModel):
    form: Literal["product", "outlet"]
    itemKey: str = Field(min_length=1, max_length=120)
    branch: str = Field(min_length=1, max_length=40)
    shown: Optional[bool] = None  # None: the outlet follows its group again


class BranchScopeIn(BaseModel):
    changes: list[BranchScopeChange] = Field(min_length=1, max_length=2000)


@router.put("/api/monitoring/form-items/branches")
def save_form_item_branches(body: BranchScopeIn, request: Request, user: dict = Depends(current_user)) -> dict:
    """An item shown / hidden at single outlets, apart from their group (migration 085)."""
    _need(request, user, "mon.branch-groups")
    out = _run(form_scope.save_branches, [c.model_dump() for c in body.changes], str(user["id"]), vf.DEFINITIONS)
    activity.record("user.branch_group", user=user, request=request, page="/monitoring/branch-groups",
                    summary=f"Mengatur butir form per outlet ({out['saved']} perubahan)")
    return out


@router.get("/api/monitoring/sales-targets")
def sales_targets(request: Request, month: str = Query(..., pattern=r"^\d{4}-\d{2}$"), user: dict = Depends(current_user)) -> dict:
    _need(request, user, "mon.branch-groups", "mon.overview")
    return _run(pic_settings.sales_overview, month)
