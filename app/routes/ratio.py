"""Outlet ratio (Cost Control page "Rasio Outlet", app/ratio.py).

    GET  /api/cost-control/ratio?dateFrom=&dateTo=&branch=        per outlet + total
    GET  /api/cost-control/ratio/export?dateFrom=&dateTo=&branch=  the same as an Excel file (columns of the workbook)
    PUT  /api/cost-control/ratio/so-override  {"branchCode", "date", "value" | null, "note"}   corrected SO value
    PUT  /api/cost-control/ratio/mdr          {"branchCode", "month", "pct" | null, "note"}    MDR of a month

A role "user" given this page sees only its own branches (app/scope.py).
"""
import os
import tempfile
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app import activity, ratio
from app.access import require_page
from app.scope import NO_BRANCH, scoped_branch
from app.utils import parse_branches, today
from app.xlsx_stream import StreamingXlsxWriter

router = APIRouter(prefix="/api/cost-control/ratio", tags=["cost-control"], dependencies=[Depends(require_page("cc.ratio"))])


def _range(date_from: Optional[str], date_to: Optional[str]) -> tuple[date, date]:
    end = date.fromisoformat(date_to) if date_to else today()
    start = date.fromisoformat(date_from) if date_from else end.replace(day=1)
    if start > end:
        raise ValueError("dateFrom must not be after dateTo")
    if (end - start).days > 92:
        raise ValueError("At most 3 months at a time")
    return start, end


def _codes(branch: Optional[str]) -> Optional[list[str]]:
    scoped = scoped_branch(branch)
    if scoped is None:
        return None
    return [] if scoped == NO_BRANCH else parse_branches(scoped)


@router.get("")
def get_ratio(dateFrom: Optional[str] = Query(None), dateTo: Optional[str] = Query(None), branch: Optional[str] = Query(None)):
    try:
        start, end = _range(dateFrom, dateTo)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return ratio.ratio(start, end, _codes(branch))


COLUMNS = [
    ("Outlet", "branchName", 34), ("Kode", "branchCode", 10), ("Gross sales", "gross", 16), ("Nett sales ESB", "nett", 16),
    ("MDR %", "mdrPct", 8), ("Pendapatan", "pendapatan", 16), ("Pembelian BB", "bb", 16), ("Pembelian Espresso", "espresso", 16),
    ("Pembelian Dimsum", "dimsum", 16), ("COGS value", "cogs", 16), ("% Espresso thd COGS", "espressoShare", 10),
    ("BB ratio", "bbRatio", 10), ("Espresso ratio", "espressoRatio", 10), ("COGS ratio (gross)", "cogsRatioGross", 10),
    ("COGS ratio (net)", "cogsRatioNet", 10), ("SO awal", "soStart", 16), ("SO akhir", "soEnd", 16), ("Usage", "usage", 16),
    ("Usage ratio (gross)", "usageRatioGross", 10), ("Usage ratio (net)", "usageRatioNet", 10),
    ("Delta COGS - usage (net)", "delta", 10), ("Non sales", "nonSales", 14), ("Item journal", "itemJournal", 14),
    ("Pembelian lain", "otherPurchases", 14),
]


def _cell(row: dict, key: str):
    v = row.get(key)
    if isinstance(v, dict):  # SO: the value used
        v = v.get("value")
    return v


@router.get("/export")
def export_ratio(request: Request, dateFrom: Optional[str] = Query(None), dateTo: Optional[str] = Query(None),
                 branch: Optional[str] = Query(None)):
    try:
        start, end = _range(dateFrom, dateTo)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    data = ratio.ratio(start, end, _codes(branch))
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    rows = [[f"Rasio Outlet {start.isoformat()} – {end.isoformat()} (SO awal {data['period']['soStart']}, SO akhir {data['period']['soEnd']})"], [],
            [c[0] for c in COLUMNS]]
    rows += [[_cell(o, c[1]) for c in COLUMNS] for o in data["outlets"]]
    t = data["total"]
    rows.append(["Total", "", t["gross"], t["nett"], None, None, t["bb"], t["espresso"], t["dimsum"], t["cogs"], t["espressoShare"],
                 t["bbRatio"], t["espressoRatio"], t["cogsRatioGross"], t["cogsRatioNet"], t["soStart"], t["soEnd"], t["usage"],
                 t["usageRatioGross"], t["usageRatioNet"], None, t["nonSales"], t["itemJournal"], t["otherPurchases"]])
    with StreamingXlsxWriter(path) as xlsx:
        xlsx.add_static_sheet("Rasio Outlet", rows, widths=[c[2] for c in COLUMNS], bold_rows=(0, 2, len(rows) - 1))
    user = getattr(request.state, "user", None)
    activity.record("export.ratio", user=user, request=request, summary=f"Ekspor Rasio Outlet {start} – {end}",
                    details={"from": start.isoformat(), "to": end.isoformat(), "outlets": len(data["outlets"])})
    return FileResponse(path, filename=f"Rasio Outlet {start.isoformat()} - {end.isoformat()}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        background=BackgroundTask(os.remove, path))


class SoOverride(BaseModel):
    branchCode: str = Field(min_length=1, max_length=32)
    date: date
    value: Optional[Decimal] = Field(default=None, ge=0)
    note: str = Field(default="", max_length=300)


@router.put("/so-override")
def put_so_override(body: SoOverride, request: Request):
    user = request.state.user
    if _codes(body.branchCode) == []:
        return JSONResponse({"error": "Cabang tidak dapat diakses"}, status_code=403)
    if body.value is not None and not body.note.strip():
        return JSONResponse({"error": "Tulis alasan koreksi SO", "field": "note"}, status_code=422)
    ratio.set_so_override(body.branchCode, body.date, body.value, body.note, str(user["id"]))
    activity.record("cost_control.so_override", user=user, request=request,
                    summary=f"Koreksi SO {body.branchCode} {body.date}: {body.value if body.value is not None else 'dihapus'}",
                    details={"branch": body.branchCode, "date": body.date.isoformat(),
                             "value": str(body.value) if body.value is not None else None, "note": body.note})
    return {"ok": True}


class MdrIn(BaseModel):
    branchCode: str = Field(min_length=1, max_length=32)
    month: date
    pct: Optional[Decimal] = Field(default=None, ge=0, lt=100)
    note: Optional[str] = Field(default=None, max_length=300)


@router.put("/mdr")
def put_mdr(body: MdrIn, request: Request):
    user = request.state.user
    if _codes(body.branchCode) == []:
        return JSONResponse({"error": "Cabang tidak dapat diakses"}, status_code=403)
    ratio.set_mdr(body.branchCode, body.month.replace(day=1), body.pct, body.note, str(user["id"]))
    activity.record("cost_control.mdr", user=user, request=request,
                    summary=f"MDR {body.branchCode} {body.month:%Y-%m}: {body.pct if body.pct is not None else 'dihapus'}",
                    details={"branch": body.branchCode, "month": body.month.isoformat(),
                             "pct": str(body.pct) if body.pct is not None else None})
    return {"ok": True}
