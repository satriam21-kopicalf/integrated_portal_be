"""Excel export job endpoints (see app/exports.py)."""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app import exports
from app.config import get_settings
from app.esb_report import TYPE_CONDITIONS
from app.utils import normalize_branch, resolve_date_range, today

router = APIRouter(prefix="/api/exports", tags=["exports"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ExportRequest(BaseModel):
    dateFrom: Optional[str] = None
    dateTo: Optional[str] = None
    branch: Optional[str] = None  # branch_code, or several separated by commas
    type: Optional[str] = None  # sales (default, = ESB report) | void | other_cost | all
    report: Optional[str] = None  # detail (default) | daily


def _public(job: dict) -> dict:
    body = dict(job)
    body["downloadUrl"] = None
    if job["status"] == "done" and job.get("fileName"):
        base = get_settings().public_base_url.rstrip("/")
        body["downloadUrl"] = f"{base}/api/exports/{job['id']}/download"
    return body


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@router.post("", status_code=202)
def create_export(req: ExportRequest):
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
    job = exports.create_job(date_from, date_to, normalize_branch(req.branch), tx_type, report)
    return JSONResponse(_public(job), status_code=202)


@router.get("/{job_id}")
def get_export(job_id: str):
    job = exports.read_job(job_id)
    if job is None:
        return _error("Export not found", 404)
    return JSONResponse(_public(job))


@router.get("/{job_id}/download")
def download_export(job_id: str):
    job = exports.read_job(job_id)
    if job is None:
        return _error("Export not found", 404)
    path = exports.job_file(job_id)
    if job["status"] != "done" or not job.get("fileName") or not path.exists():
        return _error("File export belum siap atau sudah kedaluwarsa", 409)
    return FileResponse(path, media_type=XLSX_MEDIA_TYPE, filename=job["fileName"])
