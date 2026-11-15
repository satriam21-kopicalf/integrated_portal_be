"""Job title / department of the person accounts from HRMS (superadmin; app/hrms_sync.py, migration 110).

    GET  /api/hrms-sync/preview                    every User / Cost Control / Admin account with its HRMS match
    POST /api/hrms-sync/apply      {"ids": [...]}  write the HRMS values (ids omitted = every matched account)
    PUT  /api/hrms-sync/link/{id}  {"employeeId"}  link by hand ("" / null removes the link), then apply that account
    GET  /api/hrms-sync/employees?q=               HRMS employees: manual link, and the User Accounts form (account admins)
    GET  /api/hrms-sync/employees/{id}             one HRMS employee: the HRMS data in the User Accounts detail (account admins)
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import hrms_sync
from app.routes.auth import require_account_admin, require_superadmin

router = APIRouter(prefix="/api/hrms-sync", tags=["hrms-sync"])


class ApplyBody(BaseModel):
    ids: Optional[list[str]] = Field(default=None, max_length=1000)


class LinkBody(BaseModel):
    employeeId: Optional[str] = Field(default=None, max_length=64)


def _err(exc: Exception) -> JSONResponse:
    return JSONResponse({"error": str(exc), "field": None}, status_code=422)


@router.get("/preview")
def preview(user: dict = Depends(require_superadmin)):
    try:
        return hrms_sync.preview()
    except hrms_sync.HrmsSyncError as exc:
        return _err(exc)


@router.post("/apply")
def apply(body: ApplyBody, request: Request, user: dict = Depends(require_superadmin)):
    try:
        return hrms_sync.apply(body.ids, user, request)
    except hrms_sync.HrmsSyncError as exc:
        return _err(exc)


@router.put("/link/{account_id}")
def link(account_id: str, body: LinkBody, request: Request, user: dict = Depends(require_superadmin)):
    try:
        return hrms_sync.link(account_id, (body.employeeId or "").strip() or None, user, request)
    except hrms_sync.HrmsSyncError as exc:
        return _err(exc)


@router.get("/employees")
def employees(q: str = Query(min_length=2, max_length=80), user: dict = Depends(require_account_admin)):
    return hrms_sync.search(q)


@router.get("/employees/{employee_id}")
def one_employee(employee_id: str, user: dict = Depends(require_account_admin)):
    if not hrms_sync.available():
        return JSONResponse({"error": "Data HRMS (sk_hrms) tidak tersedia di database ini.", "field": None}, status_code=422)
    e = hrms_sync.employee(employee_id)
    if not e:
        return JSONResponse({"error": "Karyawan HRMS tidak ditemukan.", "field": None}, status_code=404)
    return e
