"""Excel / Google Sheets export jobs.

An export runs in a background thread: it reads the date range one day at a
time (oldest first), builds the ESB "Sales Recapitulation Detail Report" rows
(app/esb_report.py) and streams them into an .xlsx file (app/xlsx_stream.py)
laid out like the ESB export; a new sheet starts whenever Excel's row limit is
reached. A "Ringkasan" sheet lists gross sales and the deductions per day.
Format "gsheet" uploads the file to Google Drive as a Google Sheet (app/gsheets.py),
shared with the exporting user and with anyone who has the link. A Google Sheet holds at
most 10 million cells, so a detail report is written in parts of about three days of
all outlets (PART_TARGET_ROWS, cut at a day where possible), each uploaded as soon as it
is complete; several parts go into one Drive folder named after the export.

Each job runs in its own process (app/export_worker.py), not in the web
worker: building a large report is CPU-heavy, and inside a uvicorn worker it
starved the worker's health check, so uvicorn killed the worker mid-export.
At most EXPORT_MAX_CONCURRENT jobs run at once across all workers (file-lock
slots); the others wait as "queued". Job state lives in a JSON file next to the
export, so every worker can report progress and serve the download.
"""
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from app import database as db
from app import gsheets
from app.config import get_settings
from app.database import SCHEMA, TABLE_TRANSACTIONS
from app.esb_report import (REPORT_COLUMN_WIDTHS, REPORT_HEADERS, TYPE_CONDITIONS, TYPE_LABELS,
                            iter_report_rows)
from app.daily_report import DAILY_COLUMN_WIDTHS, DAILY_HEADERS, daily_rows
from app.utils import parse_branches
from app.xlsx_stream import StreamingXlsxWriter

logger = logging.getLogger(__name__)

# Excel allows 1,048,576 rows per sheet, including the header row.
MAX_SHEET_ROWS = 1_048_575
# Google Sheets: 10 million cells per spreadsheet = 217k rows of the 46-column detail report.
# A part closes at the end of the day it passes PART_TARGET_ROWS (~3 days of all outlets,
# ~28 MB, within the Apps Script request limit); PART_MAX_ROWS cuts inside a day if ever needed.
PART_TARGET_ROWS = 150_000
PART_MAX_ROWS = 200_000
# A "running" job that has not reported progress for this long was interrupted
# (e.g. the container restarted).
STALE_AFTER = timedelta(minutes=10)
# a job whose process never started (no pid recorded) within this time is lost
START_TIMEOUT = timedelta(minutes=2)
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
APP_ROOT = Path(__file__).resolve().parent.parent
QUEUE_POLL_SECONDS = 2
QUEUE_HEARTBEAT = timedelta(seconds=30)
DAY_CHUNK_ROWS = 2000


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


def _process_alive(pid: int) -> bool:
    """True while the export process exists and is not a zombie (Linux /proc)."""
    if not Path("/proc").is_dir():
        return True  # no /proc (local Windows development): rely on STALE_AFTER
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    except OSError:
        return True
    return "\nState:\tZ" not in status


def read_job(job_id: str) -> Optional[dict]:
    if not JOB_ID_RE.match(job_id):
        return None
    try:
        job = json.loads(_state_file(job_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if job["status"] in ("queued", "running"):
        now = datetime.now(timezone.utc)
        updated = datetime.fromisoformat(job["updatedAt"])
        if job.get("pid") and not _process_alive(job["pid"]):
            job.update(status="error", error="Export terhenti (proses export berhenti). Silakan export ulang.")
        elif not job.get("pid") and now - datetime.fromisoformat(job.get("createdAt") or job["updatedAt"]) > START_TIMEOUT:
            job.update(status="error", error="Export tidak dapat dimulai. Silakan export ulang.")
        elif now - updated > STALE_AFTER:
            job.update(status="error", error="Export terhenti (server restart). Silakan export ulang.")
    return job


def cleanup_old_exports() -> None:
    cutoff = datetime.now().timestamp() - get_settings().export_ttl_hours * 3600
    for path in _export_dir().iterdir():
        if path.name.startswith("."):  # slot lock files
            continue
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
    codes = parse_branches(branch)
    if len(codes) == 1:
        name += "_" + re.sub(r"[^A-Za-z0-9]+", "_", codes[0]).strip("_")
    elif codes:
        name += f"_{len(codes)}_branches"
    return f"{name}.xlsx"


def list_jobs(owner: str, limit: int = 20) -> list[dict]:
    """The owner's recent jobs (newest first), with the same staleness checks as read_job."""
    jobs = []
    for path in sorted(_export_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            owner_of = json.loads(path.read_text(encoding="utf-8")).get("owner")
        except (OSError, json.JSONDecodeError):
            continue
        if owner_of == owner:
            job = read_job(path.stem)
            if job:
                jobs.append(job)
        if len(jobs) >= limit:
            break
    return jobs


FORMATS = ("xlsx", "gsheet")


def create_job(date_from: date, date_to: date, branch: Optional[str], tx_type: str = "sales",
               report: str = "detail", owner: Optional[dict] = None, fmt: str = "xlsx") -> dict:
    cleanup_old_exports()
    job = {
        "id": uuid.uuid4().hex,
        "owner": str(owner["id"]) if owner else None,
        "ownerName": owner["username"] if owner else None,
        "ownerRole": owner["role"] if owner else None,
        "status": "queued",
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "branch": branch,
        "type": tx_type,
        "report": report,
        "format": fmt,
        "ownerEmail": owner.get("email") if owner else None,
        "phase": None,  # "upload" while a Google Sheet is uploaded
        "uploadPct": None,
        "sheetUrl": None,
        "sheetSharedWith": None,
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
    _launch(job)
    return job


def _launch(job: dict) -> None:
    """Start the job in its own process (tests replace this to run inline)."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.export_worker", job["id"]],
        cwd=str(APP_ROOT),
        start_new_session=True,  # not killed together with the web worker
    )
    # reap the child when it ends so it does not linger as a zombie
    threading.Thread(target=proc.wait, name=f"export-reaper-{job['id'][:8]}", daemon=True).start()


def _acquire_slot(job: dict):
    """Block until one of the EXPORT_MAX_CONCURRENT slots is free (job stays "queued").

    Slots are lock files; the OS releases the lock when the process ends.
    """
    try:
        import fcntl
    except ImportError:  # Windows development: no limit
        return None
    slots = max(1, get_settings().export_max_concurrent)
    last_beat = datetime.now(timezone.utc)
    while True:
        for i in range(slots):
            handle = open(_export_dir() / f".slot-{i}.lock", "w")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except BlockingIOError:
                handle.close()
        if datetime.now(timezone.utc) - last_beat > QUEUE_HEARTBEAT:
            _update(job, status="queued")  # keeps the job from looking stale
            last_beat = datetime.now(timezone.utc)
        time.sleep(QUEUE_POLL_SECONDS)


def run_job(job_id: str) -> None:
    """Entry point of the export process (app/export_worker.py)."""
    job = json.loads(_state_file(job_id).read_text(encoding="utf-8"))
    _update(job, pid=os.getpid())
    _run(job)


def _run(job: dict) -> None:
    slot = _acquire_slot(job)
    try:
        _update(job, status="running")
        if job.get("format") == "gsheet" and job.get("report") != "daily":
            _generate_sheet_parts(job)
        else:
            _generate(job)
            if job.get("format") == "gsheet" and job.get("fileName"):
                _to_google_sheet(job)
        _log(job, "export.done", "ok", f"Export selesai: {job.get('fileName') or 'tidak ada data'}"
             + (" (Google Sheets)" if job.get("sheetUrl") else ""))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Export %s failed", job["id"])
        job_file(job["id"]).unlink(missing_ok=True)
        for part in _export_dir().glob(f"{job['id']}.part*.xlsx"):
            part.unlink(missing_ok=True)
        _update(job, status="error", error=str(exc), phase=None, finishedAt=_now())
        _log(job, "export.failed", "failed", f"Export gagal: {exc}"[:300])
    finally:
        if slot is not None:
            slot.close()


def job_details(job: dict) -> dict:
    """What the activity log keeps of an export job."""
    keys = ("id", "report", "type", "format", "dateFrom", "dateTo", "branch", "totalDays", "rows", "headers", "items",
            "fileName", "fileSize", "sheetUrl", "sheetSharedWith", "sheetLinkAccess", "sheetParts", "createdAt",
            "finishedAt", "error")
    out = {k: job.get(k) for k in keys}
    out["branches"] = parse_branches(job.get("branch")) or "all"
    return out


def _log(job: dict, action: str, status: str, summary: str) -> None:
    from app import activity  # local import: keeps the export process start light
    owner = {"id": job["owner"], "username": job.get("ownerName"), "role": job.get("ownerRole")} if job.get("owner") else None
    activity.record(action, user=owner, status=status, page="/sales", summary=summary, details=job_details(job))


def _day_headers(day: date, branch: Optional[str], tx_type: str) -> Iterator[dict]:
    """Sales of one day in report order, read in chunks."""
    where = ["h.sales_date >= %s", "h.sales_date < %s", TYPE_CONDITIONS[tx_type]]
    params: list = [day.isoformat(), (day + timedelta(days=1)).isoformat()]
    if branch:
        where.append("h.branch_code = ANY(%s)")
        params.append(parse_branches(branch))
    return db.stream(
        f"SELECT h.sales_num, h.raw_data FROM {TABLE_TRANSACTIONS} h WHERE {' AND '.join(where)} "
        f"ORDER BY h.branch_code, h.sales_date_in, h.sales_num",
        params,
        DAY_CHUNK_ROWS,
    )


def _branch_name(code: Optional[str]) -> str:
    if not code:
        return "All"
    codes = parse_branches(code)
    rows = db.fetch(
        f"SELECT DISTINCT ON (branch_code) branch_code, branch_name FROM {SCHEMA}.master_branches "
        "WHERE branch_code = ANY(%s) ORDER BY branch_code, COALESCE(is_deleted, false)", (codes,))
    names = {r["branch_code"]: r["branch_name"] for r in rows}
    return ", ".join(names.get(c, c) for c in codes)


def _preamble(job: dict, date_from: Optional[str] = None, date_to: Optional[str] = None,
              name: Optional[str] = None) -> list[list]:
    """Title rows identical to the ESB export (data header lands on row 11); a part of a
    Google Sheets export shows its own period and name."""
    d = lambda iso: date.fromisoformat(iso).strftime("%d-%m-%Y")  # noqa: E731
    daily = job.get("report") == "daily"
    return [
        [REPORTS[job.get("report") or "detail"][0]],
        ["PT Yuda Prawira Group"],
        [],
        ["Generated", datetime.now().strftime("%d-%m-%Y %H:%M:%S")],
        ["Period", f"{d(date_from or job['dateFrom'])} - {d(date_to or job['dateTo'])}"],
        ["Branch", _branch_name(job["branch"])],
        ["Sales Type", TYPE_LABELS[job["type"]]],
        *([["Date Group Mode", "Daily"]] if daily else []),
        ["Generated Username", job.get("ownerName") or "Integrated Portal"],  # who exported it
        ["Report File Name", name or job["fileName"].removesuffix(".xlsx")],
        [],
    ]


def _summary_rows(job: dict, date_from: Optional[str] = None, date_to: Optional[str] = None) -> list[list]:
    from app.routes.transactions import summarize  # local import: routes import this module

    date_from, date_to = date_from or job["dateFrom"], date_to or job["dateTo"]
    s = summarize(date_from, date_to, job["branch"])
    header = ["Date", "Gross Subtotal", "Void & Cancelled", "Other Cost (CUPPING, WASTE, ...)",
              "Open Bills", "Sales Subtotal", "Sales Nett Sales", "Sales Transactions"]
    rows = [["Ringkasan Penjualan"], ["Period", f"{date_from} - {date_to}"],
            ["Branch", _branch_name(job["branch"])], [], header]
    for day in s["days"] + [{"date": "TOTAL", **s["totals"]}]:
        rows.append([day["date"], day["gross"]["subtotal"], -day["void"]["subtotal"], -day["other_cost"]["subtotal"],
                     -day["open"]["subtotal"], day["sales"]["subtotal"], day["sales"]["nettSales"],
                     day["sales"]["transactions"]])
    if s["otherCostByMethod"]:
        rows += [[], ["Other Cost per metode", "Subtotal", "Transaksi"]]
        rows += [[m, v["subtotal"], v["transactions"]] for m, v in s["otherCostByMethod"].items()]
    return rows


def _upload(job: dict, path: Path, title: str, folder: Optional[str] = None, part: Optional[int] = None) -> dict:
    """Upload one .xlsx as a Google Sheet; a heartbeat keeps the job from looking stale meanwhile."""
    stop = threading.Event()

    def beat():
        while not stop.wait(60):
            _write_job(job)

    threading.Thread(target=beat, name=f"export-upload-{job['id'][:8]}", daemon=True).start()
    try:
        _update(job, phase="upload", uploadPct=0, uploadPart=part)
        return gsheets.upload_as_sheet(str(path), title, job.get("ownerEmail"),
                                       lambda pct: _update(job, uploadPct=round(pct * 100)), folder=folder)
    finally:
        stop.set()


def _to_google_sheet(job: dict) -> None:
    """Upload the finished .xlsx (one file: daily report) as a Google Sheet."""
    sheet = _upload(job, job_file(job["id"]), job["fileName"].removesuffix(".xlsx"))
    _update(job, status="done", phase=None, uploadPct=100, sheetUrl=sheet["url"], sheetSharedWith=sheet["sharedWith"],
            sheetLinkAccess=sheet.get("linkAccess"), finishedAt=_now())


def _part_file(job_id: str, number: int) -> Path:
    return _export_dir() / f"{job_id}.part{number}.xlsx"


def _generate_sheet_parts(job: dict) -> None:
    """Detail report for Google Sheets: written in parts that each fit in one Google Sheet,
    every part uploaded as soon as it is complete. One part = one sheet (and the .xlsx stays
    downloadable); several = sheets in one Drive folder named after the export."""
    date_from = date.fromisoformat(job["dateFrom"])
    branch, tx_type = job["branch"], job.get("type") or "sales"
    base = job["fileName"].removesuffix(".xlsx")
    folder = f"{base} ({datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=7))):%Y-%m-%d %H.%M})"
    parts: list[dict] = []
    cur: Optional[dict] = None
    rows = headers_count = 0
    short = lambda d: d.strftime("%d %b %Y")  # noqa: E731

    def open_part(day: date) -> dict:
        number = len(parts) + 1
        xlsx = StreamingXlsxWriter(str(_part_file(job["id"], number)))
        part = {"number": number, "from": day, "to": day, "rows": 0, "xlsx": xlsx, "batch": []}
        part["sheet"] = xlsx.add_sheet("Report", widths=REPORT_COLUMN_WIDTHS, header=REPORT_HEADERS,
                                       preamble=_preamble(job, name=f"{base} - part {number}"))
        return part

    def close_part(part: dict, last: bool) -> None:
        if part["batch"]:
            part["sheet"].write_rows(part["batch"])
            part["batch"] = []
        part["xlsx"].add_static_sheet("Ringkasan", _summary_rows(job, part["from"].isoformat(), part["to"].isoformat()),
                                      widths=[22, 18, 18, 22, 14, 20, 18, 18], bold_rows=(0, 4))
        part["xlsx"].close()
        path = _part_file(job["id"], part["number"])
        single = last and not parts
        title = base if single else f"{base} - part {part['number']} ({short(part['from'])} - {short(part['to'])})"
        sheet = _upload(job, path, title, folder=None if single else folder, part=part["number"])
        parts.append({"part": part["number"], "dateFrom": part["from"].isoformat(), "dateTo": part["to"].isoformat(),
                      "rows": part["rows"], "url": sheet["url"], "folderUrl": sheet.get("folderUrl"),
                      "linkAccess": sheet.get("linkAccess"), "sharedWith": sheet.get("sharedWith")})
        if single:
            os.replace(path, job_file(job["id"]))  # the one part is the whole report: keep it downloadable
        else:
            path.unlink(missing_ok=True)
        _update(job, phase=None, uploadPart=None, sheetParts=parts, sheets=len(parts))

    try:
        for offset in range(job["totalDays"]):
            day = date_from + timedelta(days=offset)
            if cur is not None and cur["rows"] >= PART_TARGET_ROWS:
                close_part(cur, last=False)
                cur = None
            day_headers = 0

            def counted(headers):
                nonlocal day_headers
                for h in headers:
                    day_headers += 1
                    yield h

            for row in iter_report_rows(counted(_day_headers(day, branch, tx_type))):
                if cur is None:
                    cur = open_part(day)
                elif cur["rows"] >= PART_MAX_ROWS:  # a single day too big for one sheet: cut inside it
                    close_part(cur, last=False)
                    cur = open_part(day)
                cur["batch"].append(row)
                cur["rows"] += 1
                cur["to"] = day
                rows += 1
                if len(cur["batch"]) >= DAY_CHUNK_ROWS:
                    cur["sheet"].write_rows(cur["batch"])
                    cur["batch"] = []
            headers_count += day_headers
            _update(job, daysDone=offset + 1, currentDate=day.isoformat(), rows=rows, headers=headers_count, items=rows)
        if cur is not None:
            close_part(cur, last=True)
            cur = None
    finally:
        if cur is not None:  # failed half way: close the open file so it can be removed
            try:
                cur["xlsx"].close()
            except Exception:  # noqa: BLE001
                pass

    if rows == 0:
        _update(job, status="done", fileName=None, finishedAt=_now())
        return
    single = len(parts) == 1
    whole = job_file(job["id"])
    _update(job, status="done", phase=None, uploadPct=100,
            sheetUrl=parts[0]["url"] if single else (parts[0].get("folderUrl") or parts[0]["url"]),
            sheetFolderUrl=None if single else parts[0].get("folderUrl"),
            sheetSharedWith=parts[0].get("sharedWith"), sheetLinkAccess=parts[0].get("linkAccess"),
            fileSize=whole.stat().st_size if whole.exists() else None, downloadable=whole.exists(), finishedAt=_now())
    logger.info("Export %s to Google Sheets done: %s rows in %s part(s)", job["id"], rows, len(parts))


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
            day_headers = 0

            def counted(rows):
                nonlocal day_headers
                for h in rows:
                    day_headers += 1
                    yield h

            batch: list[list] = []
            for row in iter_report_rows(counted(_day_headers(day, branch, tx_type))):
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

            headers_count += day_headers
            _update(job, daysDone=offset + 1, currentDate=day.isoformat(),
                    rows=rows, headers=headers_count, items=rows, sheets=sheets)

        if rows:
            xlsx.add_static_sheet("Ringkasan", _summary_rows(job), widths=[22, 18, 18, 22, 14, 20, 18, 18],
                                  bold_rows=(0, 4))

    if rows == 0:
        path.unlink(missing_ok=True)
        _update(job, status="done", fileName=None, finishedAt=_now())
        return

    # a Google Sheet job stays "running" until the upload is done
    _update(job, status="running" if job.get("format") == "gsheet" else "done", fileSize=path.stat().st_size,
            finishedAt=None if job.get("format") == "gsheet" else _now())
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
    _update(job, status="running" if job.get("format") == "gsheet" else "done", fileSize=path.stat().st_size,
            finishedAt=None if job.get("format") == "gsheet" else _now())
    logger.info("Daily export %s done: %s rows", job["id"], rows)
