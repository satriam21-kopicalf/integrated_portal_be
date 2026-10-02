"""Excel export job tests (database stubbed, jobs run inline, see conftest.py)."""
import io
import json
from datetime import datetime, timedelta, timezone

import openpyxl

from app import exports
from app.config import get_settings


def _start(client, **body):
    res = client.post("/api/exports", json=body)
    assert res.status_code == 202, res.text
    return res.json()


def _workbook(client, job):
    res = client.get(job["downloadUrl"])
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert job["fileName"] in res.headers["content-disposition"]
    assert "content-encoding" not in res.headers  # not gzipped
    return openpyxl.load_workbook(io.BytesIO(res.content), read_only=True)


def test_export_job_writes_all_days_newest_first(client):
    job = _start(client, dateFrom="2026-09-29", dateTo="2026-09-30")
    status = client.get(f"/api/exports/{job['id']}").json()
    assert status["status"] == "done"
    assert status["totalDays"] == 2 and status["daysDone"] == 2
    # S-002 (1 item) + S-001 (no items) + S-000 (2 items)
    assert (status["rows"], status["headers"], status["items"], status["sheets"]) == (4, 3, 3, 1)
    assert status["fileName"] == "ESB_Sales_2026-09-29_to_2026-09-30.xlsx"
    assert status["downloadUrl"] == f"/api/exports/{job['id']}/download"

    wb = _workbook(client, status)
    assert wb.sheetnames == ["Summary", "Transactions"]
    # read-only mode drops trailing empty cells, so pad rows back to 44 columns
    rows = [tuple(r) + (None,) * (44 - len(r)) for r in wb["Transactions"].iter_rows(values_only=True)]
    assert len(rows) == 5 and all(len(r) == 44 for r in rows)
    assert rows[0][0] == "Sales Number"
    assert [r[0] for r in rows[1:]] == ["S-002", "S-001", "S-000", "S-000"]
    assert rows[1][35] == "Latte" and rows[1][38] == 2
    assert rows[2][35] is None  # header without items -> blank item columns
    summary = {r[0]: r[1] for r in wb["Summary"].iter_rows(values_only=True) if len(r) == 2}
    assert summary["Total Rows"] == 4 and summary["Total Transactions"] == 3
    assert summary["Period"] == "2026-09-29 - 2026-09-30"


def test_export_splits_into_multiple_sheets(client, monkeypatch):
    monkeypatch.setattr(exports, "MAX_SHEET_ROWS", 2)
    job = client.get(f"/api/exports/{_start(client, dateFrom='2026-09-29', dateTo='2026-09-30')['id']}").json()
    assert job["sheets"] == 2
    wb = _workbook(client, job)
    assert wb.sheetnames == ["Summary", "Transactions", "Transactions (2)"]
    assert [len(list(wb[n].iter_rows())) for n in wb.sheetnames[1:]] == [3, 3]  # header + 2 rows each


def test_export_branch_filter_and_file_name(client):
    job = client.get(f"/api/exports/{_start(client, dateFrom='2026-09-29', dateTo='2026-09-30', branch='Calf B')['id']}").json()
    assert job["rows"] == 1 and job["headers"] == 1
    assert job["fileName"] == "ESB_Sales_2026-09-29_to_2026-09-30_Calf_B.xlsx"


def test_export_without_data_has_no_file(client):
    job = client.get(f"/api/exports/{_start(client, dateFrom='2026-01-01', dateTo='2026-01-02')['id']}").json()
    assert job["status"] == "done" and job["rows"] == 0
    assert job["fileName"] is None and job["downloadUrl"] is None
    assert client.get(f"/api/exports/{job['id']}/download").status_code == 409


def test_export_absolute_download_url(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "public_base_url", "https://portal-api.example.com/")
    job = _start(client, dateFrom="2026-09-30", dateTo="2026-09-30")
    assert job["downloadUrl"] == f"https://portal-api.example.com/api/exports/{job['id']}/download"


def test_export_default_range_when_no_dates(client):
    job = _start(client)
    assert job["totalDays"] == get_settings().default_days + 1


def test_export_validation(client):
    assert client.post("/api/exports", json={"dateFrom": "30-09-2026", "dateTo": "2026-09-30"}).status_code == 422
    res = client.post("/api/exports", json={"dateFrom": "2026-10-01", "dateTo": "2026-09-30"})
    assert res.status_code == 422 and "error" in res.json()


def test_export_failure_is_reported(client, fake_db):
    fake_db.fail = True
    job = client.get(f"/api/exports/{_start(client, dateFrom='2026-09-30', dateTo='2026-09-30')['id']}").json()
    assert job["status"] == "error" and "database is down" in job["error"]
    assert not exports.job_file(job["id"]).exists()


def test_unknown_or_invalid_job_id(client):
    assert client.get("/api/exports/" + "0" * 32).status_code == 404
    assert client.get("/api/exports/../../etc/passwd").status_code == 404
    assert client.get("/api/exports/not-a-job/download").status_code == 404


def test_stale_running_job_reported_as_error(client):
    job_id = "a" * 32
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    state = {"id": job_id, "status": "running", "updatedAt": old, "fileName": "x.xlsx"}
    (exports._export_dir() / f"{job_id}.json").write_text(json.dumps(state), encoding="utf-8")
    assert client.get(f"/api/exports/{job_id}").json()["status"] == "error"
