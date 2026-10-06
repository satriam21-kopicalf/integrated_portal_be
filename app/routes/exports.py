"""Excel / Google Sheets export job endpoints (see app/exports.py).

Jobs run in their own process on the server, independent of the browser; the dashboard
keeps polling them from any page (GET /api/exports lists the signed-in user's jobs).
A job belongs to the user who started it; role "user" exports only their branches.
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app import activity, exports, gsheets
from app.config import get_settings
from app.esb_report import TYPE_CONDITIONS
from app.routes.auth import current_user
from app.scope import scoped_branch
from app.utils import resolve_date_range, today

router = APIRouter(prefix="/api/exports", tags=["exports"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ExportRequest(BaseModel):
    dateFrom: Optional[str] = None
    dateTo: Optional[str] = None
    branch: Optional[str] = None  # branch_code, or several separated by commas
    type: Optional[str] = None  # sales (default, = ESB report) | void | other_cost | all
    report: Optional[str] = None  # detail (default) | daily
    format: Optional[str] = None  # xlsx (default) | gsheet (Google Sheets, when configured)


def _public(job: dict) -> dict:
    body = {k: v for k, v in job.items() if not k.startswith("owner")}
    body["downloadUrl"] = None
    if job["status"] == "done" and job.get("fileName"):
        base = get_settings().public_base_url.rstrip("/")
        body["downloadUrl"] = f"{base}/api/exports/{job['id']}/download"
    return body


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _may_read(user: dict, job: dict) -> bool:
    return user["role"] == "superadmin" or job.get("owner") == str(user["id"])


@router.get("")
def my_exports(user: dict = Depends(current_user)):
    """The signed-in user's recent export jobs (newest first) and the formats available."""
    return JSONResponse({"jobs": [_public(j) for j in exports.list_jobs(str(user["id"]))],
                         "googleSheets": gsheets.enabled()})


@router.post("", status_code=202)
def create_export(req: ExportRequest, request: Request, user: dict = Depends(current_user)):
    raw_from, raw_to = resolve_date_range(req.dateFrom, req.dateTo)
    try:
        date_to = date.fromisoformat(raw_to) if raw_to else today()
        date_from = (
            date.fromisoformat(raw_from) if raw_from
            else date_to - timedelta(days=get_settings().default_days)
        )
    except ValueError:
        return _error("Format tanggal harus YYYY-MM-DD", 422)
    if date_from > date_to:
        return _error("Tanggal mulai harus sebelum tanggal akhir", 422)

    tx_type = req.type if req.type in TYPE_CONDITIONS else "sales"
    report = req.report if req.report in exports.REPORTS else "detail"
    fmt = req.format if req.format in exports.FORMATS else "xlsx"
    if fmt == "gsheet" and not gsheets.enabled():
        return _error("Export ke Google Sheets belum dikonfigurasi di server", 422)
    job = exports.create_job(date_from, date_to, scoped_branch(req.branch), tx_type, report, user, fmt)
    activity.record("export.create", user=user, request=request, page="/sales",
                    summary=f"Export {exports.REPORTS[report][0]} {date_from:%d-%m-%Y} s/d {date_to:%d-%m-%Y}"
                    + (" ke Google Sheets" if fmt == "gsheet" else ""),
                    details={**exports.job_details(job), "requestedBranch": req.branch})
    return JSONResponse(_public(job), status_code=202)


@router.get("/{job_id}")
def get_export(job_id: str, user: dict = Depends(current_user)):
    job = exports.read_job(job_id)
    if job is None or not _may_read(user, job):
        return _error("Export not found", 404)
    return JSONResponse(_public(job))


@router.get("/{job_id}/download")
def download_export(job_id: str, request: Request, user: dict = Depends(current_user)):
    job = exports.read_job(job_id)
    if job is None or not _may_read(user, job):
        if job is not None:
            activity.record("export.download", user=user, request=request, status="denied", page="/sales",
                            summary=f"Mencoba mengunduh export milik user lain ({job.get('ownerName')})",
                            details=exports.job_details(job))
        return _error("Export not found", 404)
    path = exports.job_file(job_id)
    if job["status"] != "done" or not job.get("fileName") or not path.exists():
        return _error("File export belum siap atau sudah kedaluwarsa", 409)
    activity.record("export.download", user=user, request=request, page="/sales",
                    summary=f"Mengunduh {job['fileName']}",
                    details={**exports.job_details(job), "ownerName": job.get("ownerName")})
    return FileResponse(path, media_type=XLSX_MEDIA_TYPE, filename=job["fileName"])
