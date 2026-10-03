"""Excel export jobs.

An export runs in a background thread: it reads the date range one day at a
time (oldest first), builds the ESB "Sales Recapitulation Detail Report" rows
(app/esb_report.py) and streams them into an .xlsx file (app/xlsx_stream.py)
laid out like the ESB export; a new sheet starts whenever Excel's row limit is
reached. A "Ringkasan" sheet lists gross sales and the deductions per day. Job state lives in a JSON file next to the export so every uvicorn
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
from app.database import SCHEMA, TABLE_TRANSACTIONS
from app.esb_report import (REPORT_COLUMN_WIDTHS, REPORT_HEADERS, TYPE_CONDITIONS, TYPE_LABELS,
                            iter_report_rows)
from app.daily_report import DAILY_COLUMN_WIDTHS, DAILY_HEADERS, daily_rows
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


# report kinds: ESB report title, file name prefix
REPORTS = {
    "detail": ("Sales Recapitulation Detail Report", "Sales_Recapitulation_Detail"),
    "daily": ("Daily Sales Recapitulation Report", "Daily_Sales_Recapitulation"),
}


def _file_name(date_from: date, date_to: date, branch: Optional[str], tx_type: str, report: str) -> str:
    name = f"{REPORTS[report][1]}_{date_from.isoformat()}_to_{date_to.isoformat()}"
    if tx_type != "sales":
        name += f"_{tx_type}"
    if branch:
        name += "_" + re.sub(r"[^A-Za-z0-9]+", "_", branch).strip("_")
    return f"{name}.xlsx"


def create_job(date_from: date, date_to: date, branch: Optional[str], tx_type: str = "sales",
               report: str = "detail") -> dict:
    cleanup_old_exports()
    job = {
        "id": uuid.uuid4().hex,
        "status": "queued",
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "branch": branch,
        "type": tx_type,
        "report": report,
        "totalDays": (date_to - date_from).days + 1,
        "daysDone": 0,
        "currentDate": None,
        "rows": 0,
        "headers": 0,
        "items": 0,
        "sheets": 0,
        "fileName": _file_name(date_from, date_to, branch, tx_type, report),
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


def _fetch_day_headers(day: date, branch: Optional[str], tx_type: str) -> list[dict]:
    where = ["h.sales_date >= %s", "h.sales_date < %s", TYPE_CONDITIONS[tx_type]]
    params: list = [day.isoformat(), (day + timedelta(days=1)).isoformat()]
    if branch:
        where.append("h.branch_code = %s")
        params.append(branch)
    return db.fetch(
        f"SELECT h.sales_num, h.raw_data FROM {TABLE_TRANSACTIONS} h WHERE {' AND '.join(where)} "
        f"ORDER BY h.branch_code, h.sales_date_in, h.sales_num",
        params,
    )


def _branch_name(code: Optional[str]) -> str:
    if not code:
        return "All"
    row = db.fetchrow(f"SELECT branch_name FROM {SCHEMA}.master_branches WHERE branch_code = %s", (code,))
    return row["branch_name"] if row else code


def _preamble(job: dict) -> list[list]:
    """Title rows identical to the ESB export (data header lands on row 11)."""
    d = lambda iso: date.fromisoformat(iso).strftime("%d-%m-%Y")  # noqa: E731
    daily = job.get("report") == "daily"
    return [
        [REPORTS[job.get("report") or "detail"][0]],
        ["PT Yuda Prawira Group"],
        [],
        ["Generated", datetime.now().strftime("%d-%m-%Y %H:%M:%S")],
        ["Period", f"{d(job['dateFrom'])} - {d(job['dateTo'])}"],
        ["Branch", _branch_name(job["branch"])],
        ["Sales Type", TYPE_LABELS[job["type"]]],
        *([["Date Group Mode", "Daily"]] if daily else []),
        ["Generated Username", "Integrated Portal"],
        ["Report File Name", job["fileName"].removesuffix(".xlsx")],
        [],
    ]


def _summary_rows(job: dict) -> list[list]:
    from app.routes.transactions import summarize  # local import: routes import this module

    s = summarize(job["dateFrom"], job["dateTo"], job["branch"])
    header = ["Date", "Gross Subtotal", "Void & Cancelled", "Other Cost (CUPPING, WASTE, ...)",
              "Open Bills", "Sales Subtotal", "Sales Nett Sales", "Sales Transactions"]
    rows = [["Ringkasan Penjualan"], ["Period", f"{job['dateFrom']} - {job['dateTo']}"],
            ["Branch", _branch_name(job["branch"])], [], header]
    for day in s["days"] + [{"date": "TOTAL", **s["totals"]}]:
        rows.append([day["date"], day["gross"]["subtotal"], -day["void"]["subtotal"], -day["other_cost"]["subtotal"],
                     -day["open"]["subtotal"], day["sales"]["subtotal"], day["sales"]["nettSales"],
                     day["sales"]["transactions"]])
    if s["otherCostByMethod"]:
        rows += [[], ["Other Cost per metode", "Subtotal", "Transaksi"]]
        rows += [[m, v["subtotal"], v["transactions"]] for m, v in s["otherCostByMethod"].items()]
    return rows


def _generate(job: dict) -> None:
    if job.get("report") == "daily":
        _generate_daily(job)
        return
    date_from = date.fromisoformat(job["dateFrom"])
    branch = job["branch"]
    tx_type = job.get("type") or "sales"
    path = job_file(job["id"])

    sheet = None
    sheets = 0
    rows = headers_count = 0
    preamble = _preamble(job)

    def preamble_rows(sheet_number: int) -> int:
        # the first sheet also holds the title rows
        return len(preamble) if sheet_number == 1 else 0

    with StreamingXlsxWriter(str(path)) as xlsx:
        for offset in range(job["totalDays"]):
            day = date_from + timedelta(days=offset)
            headers = _fetch_day_headers(day, branch, tx_type)

            batch: list[list] = []
            for row in iter_report_rows(headers):
                # data_rows counts rows already assigned to this sheet (written or batched)
                if sheet is None or sheet.data_rows >= MAX_SHEET_ROWS - preamble_rows(sheets):
                    if sheet is not None:
                        sheet.write_rows(batch)
                        batch = []
                    sheets += 1
                    name = "Report" if sheets == 1 else f"Report ({sheets})"
                    sheet = xlsx.add_sheet(name, widths=REPORT_COLUMN_WIDTHS, header=REPORT_HEADERS,
                                           preamble=preamble if sheets == 1 else ())
                    sheet.data_rows = 0
                batch.append(row)
                sheet.data_rows += 1
                rows += 1
            if batch:
                sheet.write_rows(batch)

            headers_count += len(headers)
            _update(job, daysDone=offset + 1, currentDate=day.isoformat(),
                    rows=rows, headers=headers_count, items=rows, sheets=sheets)

        if rows:
            xlsx.add_static_sheet("Ringkasan", _summary_rows(job), widths=[22, 18, 18, 22, 14, 20, 18, 18],
                                  bold_rows=(0, 4))

    if rows == 0:
        path.unlink(missing_ok=True)
        _update(job, status="done", fileName=None, finishedAt=_now())
        return

    _update(job, status="done", fileSize=path.stat().st_size, finishedAt=_now())
    logger.info("Export %s done: %s rows, %s sheet(s), %s bytes", job["id"], rows, sheets, job["fileSize"])


def _generate_daily(job: dict) -> None:
    """Daily Sales Recapitulation: one row per date and branch (small, one sheet)."""
    date_from = date.fromisoformat(job["dateFrom"])
    tx_type = job.get("type") or "sales"
    path = job_file(job["id"])
    rows = bills = 0
    with StreamingXlsxWriter(str(path)) as xlsx:
        sheet = xlsx.add_sheet("Report", widths=DAILY_COLUMN_WIDTHS, header=DAILY_HEADERS, preamble=_preamble(job))
        for offset in range(job["totalDays"]):
            day = date_from + timedelta(days=offset)
            day_rows = daily_rows(day, job["branch"], tx_type)
            sheet.write_rows(day_rows)
            rows += len(day_rows)
            bills += int(sum(r[3] for r in day_rows))
            _update(job, daysDone=offset + 1, currentDate=day.isoformat(), rows=rows, headers=bills,
                    items=rows, sheets=1)
        if rows:
            xlsx.add_static_sheet("Ringkasan", _summary_rows(job), widths=[22, 18, 18, 22, 14, 20, 18, 18],
                                  bold_rows=(0, 4))
    if rows == 0:
        path.unlink(missing_ok=True)
        _update(job, status="done", fileName=None, finishedAt=_now())
        return
    _update(job, status="done", fileSize=path.stat().st_size, finishedAt=_now())
    logger.info("Daily export %s done: %s rows", job["id"], rows)
