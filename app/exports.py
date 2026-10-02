"""Excel export jobs.

An export runs in a background thread: it reads the date range one day at a
time (newest first), streams the rows into an .xlsx file (app/xlsx_stream.py)
and starts a new sheet whenever Excel's row limit is reached. Job state lives in a JSON file next to the export so every uvicorn
worker in the container can report progress and serve the download.
"""
import json
import logging
import os
import re
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app import database as db
from app.config import get_settings
from app.database import HEADER_COLUMNS, TABLE_TRANSACTIONS
from app.report import EXCEL_COLUMN_WIDTHS, EXCEL_HEADERS, fetch_items, group_items, report_rows
from app.xlsx_stream import StreamingXlsxWriter

logger = logging.getLogger(__name__)

# Excel allows 1,048,576 rows per sheet, including the header row.
MAX_SHEET_ROWS = 1_048_575
# A "running" job that has not reported progress for this long was interrupted
# (e.g. the container restarted).
STALE_AFTER = timedelta(minutes=10)
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_semaphore: Optional[threading.BoundedSemaphore] = None
_semaphore_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _export_dir() -> Path:
    path = Path(get_settings().export_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_file(job_id: str) -> Path:
    return _export_dir() / f"{job_id}.xlsx"


def _state_file(job_id: str) -> Path:
    return _export_dir() / f"{job_id}.json"


def _write_job(job: dict) -> None:
    job["updatedAt"] = _now()
    final = _state_file(job["id"])
    tmp = final.with_name(f"{final.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(job), encoding="utf-8")
    os.replace(tmp, final)


def _update(job: dict, **fields) -> None:
    job.update(fields)
    _write_job(job)


def read_job(job_id: str) -> Optional[dict]:
    if not JOB_ID_RE.match(job_id):
        return None
    try:
        job = json.loads(_state_file(job_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if job["status"] in ("queued", "running"):
        updated = datetime.fromisoformat(job["updatedAt"])
        if datetime.now(timezone.utc) - updated > STALE_AFTER:
            job.update(status="error", error="Export terhenti (server restart). Silakan export ulang.")
    return job


def cleanup_old_exports() -> None:
    cutoff = datetime.now().timestamp() - get_settings().export_ttl_hours * 3600
    for path in _export_dir().iterdir():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except FileNotFoundError:
            pass


def _file_name(date_from: date, date_to: date, branch: Optional[str]) -> str:
    name = f"ESB_Sales_{date_from.isoformat()}_to_{date_to.isoformat()}"
    if branch:
        name += "_" + re.sub(r"[^A-Za-z0-9]+", "_", branch).strip("_")
    return f"{name}.xlsx"


def create_job(date_from: date, date_to: date, branch: Optional[str]) -> dict:
    cleanup_old_exports()
    job = {
        "id": uuid.uuid4().hex,
        "status": "queued",
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "branch": branch,
        "totalDays": (date_to - date_from).days + 1,
        "daysDone": 0,
        "currentDate": None,
        "rows": 0,
        "headers": 0,
        "items": 0,
        "sheets": 0,
        "fileName": _file_name(date_from, date_to, branch),
        "fileSize": None,
        "error": None,
        "createdAt": _now(),
        "finishedAt": None,
    }
    _write_job(job)
    threading.Thread(target=_run, args=(job,), name=f"export-{job['id']}", daemon=True).start()
    return job


def _get_semaphore() -> threading.BoundedSemaphore:
    global _semaphore
    with _semaphore_lock:
        if _semaphore is None:
            _semaphore = threading.BoundedSemaphore(get_settings().export_max_concurrent)
        return _semaphore


def _run(job: dict) -> None:
    with _get_semaphore():
        try:
            _update(job, status="running")
            _generate(job)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Export %s failed", job["id"])
            job_file(job["id"]).unlink(missing_ok=True)
            _update(job, status="error", error=str(exc), finishedAt=_now())


def _fetch_day_headers(day: date, branch: Optional[str]) -> list[dict]:
    where = ["sales_date >= %s", "sales_date < %s"]
    params: list = [day.isoformat(), (day + timedelta(days=1)).isoformat()]
    if branch:
        where.append("branch_name = %s")
        params.append(branch)
    return db.fetch(
        f"SELECT {HEADER_COLUMNS} FROM {TABLE_TRANSACTIONS} WHERE {' AND '.join(where)} "
        f"ORDER BY sales_date DESC, sales_num DESC",
        params,
    )


def _generate(job: dict) -> None:
    date_to = date.fromisoformat(job["dateTo"])
    branch = job["branch"]
    path = job_file(job["id"])

    sheet = None
    sheets = 0
    rows = headers_count = items_count = 0

    with StreamingXlsxWriter(str(path)) as xlsx:
        for offset in range(job["totalDays"]):
            day = date_to - timedelta(days=offset)
            headers = _fetch_day_headers(day, branch)
            items_by_sales = group_items(fetch_items([h["sales_num"] for h in headers])) if headers else {}

            batch: list[list] = []
            for row, has_item in report_rows(headers, items_by_sales):
                # sheet.rows includes the header row
                if sheet is None or sheet.rows - 1 + len(batch) >= MAX_SHEET_ROWS:
                    if sheet is not None:
                        sheet.write_rows(batch)
                        batch = []
                    sheets += 1
                    name = "Transactions" if sheets == 1 else f"Transactions ({sheets})"
                    sheet = xlsx.add_sheet(name, widths=EXCEL_COLUMN_WIDTHS, header=EXCEL_HEADERS)
                batch.append(row)
                rows += 1
                items_count += has_item
            if batch:
                sheet.write_rows(batch)

            headers_count += len(headers)
            _update(job, daysDone=offset + 1, currentDate=day.isoformat(),
                    rows=rows, headers=headers_count, items=items_count, sheets=sheets)

        xlsx.add_static_sheet(
            "Summary",
            [
                ["ESB Sales Report"],
                ["Generated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
                ["Period", f"{job['dateFrom']} - {job['dateTo']}"],
                ["Branch", branch or "All Branches"],
                [],
                ["Summary"],
                ["Total Rows", rows],
                ["Total Transactions", headers_count],
                ["Total Items", items_count],
            ],
            widths=[20, 25],
            bold_rows=(0, 5),
            first=True,
        )

    if rows == 0:
        path.unlink(missing_ok=True)
        _update(job, status="done", fileName=None, finishedAt=_now())
        return

    _update(job, status="done", fileSize=path.stat().st_size, finishedAt=_now())
    logger.info("Export %s done: %s rows, %s sheet(s), %s bytes", job["id"], rows, sheets, job["fileSize"])
