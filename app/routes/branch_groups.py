"""Monitoring → Branch groups and Overview (app/branch_groups.py, app/monitoring_overview.py). Registered before the
generic /api/monitoring/{form} routes.

    GET    /api/monitoring/branch-groups               any monitoring page: groups with their branches + outlets without a group
    POST   /api/monitoring/branch-groups               page mon.branch-groups: {"code", "name", "description", "sort"}
    PATCH  /api/monitoring/branch-groups/{id}          page mon.branch-groups
    DELETE /api/monitoring/branch-groups/{id}          page mon.branch-groups (its branches become "without a group")
    PUT    /api/monitoring/branch-groups/{id}/branches page mon.branch-groups: {"branchCodes": [...]}
    GET    /api/monitoring/overview                    page mon.overview: ?dateFrom&dateTo&group
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import activity
from app import branch_groups as bg
from app import monitoring_overview
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
    except bg.GroupError as e:
        raise HTTPException(422, detail=str(e)) from None


@router.get("/api/monitoring/branch-groups")
def groups(request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, *MON_PAGES)
    return bg.list_groups()


@router.post("/api/monitoring/branch-groups", status_code=201)
def create(body: GroupIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, "mon.branch-groups")
    out = _run(bg.save_group, None, body.code, body.name, body.description, body.sort, str(user["id"]))
    activity.record("user.branch_group", user=user, request=request, page="/monitoring/branch-groups", summary=f"Membuat grup cabang {body.code.upper()}")
    return out


@router.patch("/api/monitoring/branch-groups/{group_id}")
def update(group_id: str, body: GroupIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _need(request, user, "mon.branch-groups")
    out = _run(bg.save_group, group_id, body.code, body.name, body.description, body.sort, str(user["id"]))
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
