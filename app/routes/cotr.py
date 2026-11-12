"""Calf On The Road (app/cotr.py, migration 046).

Driver app (fe-calfontheroad), accounts of role driver tied to a Master Driver row:
    GET  /api/cotr/driver/today                 the driver, the menus, today's shift, stock, sales, reloads, requests, recap
    POST /api/cotr/driver/dispatch/confirm      {"counts": {code: cups}, "note"} the check of today's assignment (049):
                                                all there -> accepted, else disputed for the storage
    POST /api/cotr/driver/check-in              {"kind": start|end, "photo", "latitude", "longitude", "accuracyM", "note"}
                                                start also {"signature"} (the stock is the accepted assignment)
                                                end also {"stock", "waste", "cupping": {code: cups}, "cashActual", "cashSpent"}
    POST /api/cotr/driver/sales                 {"lines": [{"code", "qty"}], "payment": cash|qris}
    POST /api/cotr/driver/sales/{id}/void       {"reason"}
    POST /api/cotr/driver/reloads               {"lines": {code: cups}, "note"}
    POST /api/cotr/driver/reloads/{id}/receive  {"counts", "note"}: the check of an approved reload (as the assignment)
    POST /api/cotr/driver/requests              {"kind": a code of Master → Pengajuan, "amount", "note", "photo"}
    POST /api/cotr/driver/area-requests         {"area"} or {"name", "address", "city", "latitude", "longitude"} + {"start",
                                                "end", "note"}: go to an area of the master or a new place (088)
    POST /api/cotr/driver/locations             {"points": [{"at", "lat", "lng", "accuracy", "speed", "heading", "battery"}]}
                                                the GPS of the phone while the shift is open (migration 048)
Every driver action answers the new state of the day (as GET today).

Portal (Monitoring COTR):
    GET  /api/cotr/monitor?day=                 page cotr.check-in: shifts of the day, drivers not started, reloads, requests
    GET  /api/cotr/storage?day=&pool=           page cotr.storage: the drivers of a pool with their assignment and reloads
    PUT  /api/cotr/dispatch                     page cotr.storage: {"day", "driver", "lines", "note"} assign (until accepted)
    POST /api/cotr/dispatch/{id}/resolve        page cotr.storage: {"lines", "note"} the final cups of a difference
    POST /api/cotr/reloads/{id}/decide          page cotr.storage: {"approve", "note", "lines"} the cups sent
    POST /api/cotr/reloads/{id}/resolve         page cotr.storage: {"lines", "note"} the cups received of a difference
    POST /api/cotr/requests/{id}/decide         page cotr.check-in: {"approve", "note"} (a rejection needs the note)
    GET  /api/cotr/masters/{entity}             pools | fleet | fleet-types | menus | boms | drivers (any COTR page; writes:
                                                the page of it); salary, bank and NIK of drivers with page cotr.management only
    PUT/GET /api/cotr/masters/{drivers|fleet}/{code}/image/{photo|qris}   the photo of a driver / armada, the QRIS
    POST /api/cotr/masters/{entity}             create        PUT /api/cotr/masters/{entity}/{code}   update
    DELETE /api/cotr/masters/{entity}/{code}
    GET  /api/cotr/driver-accounts              page cotr.drivers: the accounts of role driver
    POST /api/cotr/drivers/import               page cotr.drivers: {"file": base64 xlsx, "pool", "apply"} preview / save;
                                                the workbook "DATA COTR" (migration 060): every employee, pool by placement
    GET  /api/cotr/locations?day=               page cotr.check-in: the last position of every driver of the day
    GET  /api/cotr/locations/{driver}?day=      page cotr.check-in: the route of one driver
    GET  /api/cotr/production?days=&buffer=     page cotr.production: plan from the sales of the last days
    GET  /api/cotr/usage?dateFrom=&dateTo=&pool=  page cotr.production: theoretical usage per item (sold, waste, cupping
                                                exploded through the recipe cards, migration 051)
    GET  /api/cotr/schedule?dateFrom=&dateTo=&pool=   page cotr.schedule: roster, check ins, attendance, recap (052)
    PUT  /api/cotr/schedule                     page cotr.schedule: {"entries": [{driver, day, status, template, pool, note}]}
    POST /api/cotr/schedule/copy                page cotr.schedule: {"source", "target", "days", "drivers"}
    GET/POST /api/cotr/batches, DELETE /api/cotr/batches/{id}, POST /api/cotr/batches/{id}/send   page cotr.production:
                                                production at a hub (produced / expires on) and its parts sent to pools (062)
    POST /api/cotr/transfers/{id}/receive       page cotr.storage: a part received at the pool
    GET  /api/cotr/pool-stock?pool=&day=        page cotr.storage: the batches of a pool, first expired first
    GET  /api/cotr/fleet/{code}/card            any COTR page: the armada as its label QR shows it, in full (063)
    POST /api/cotr/fleet/{code}/label-token     page cotr.fleet: a new label QR (the printed labels stop opening)
    GET  /api/public/fleet/{token}[/photo|/driver/{code}/photo]   no sign-in: the label QR of an armada, without the
                                                papers, the QRIS and the phone numbers
    GET  /api/cotr/incentives?month=YYYY-MM&scheme=&pool=   page cotr.incentive: incentive per driver (053)
    GET/POST /api/cotr/waste, DELETE /api/cotr/waste/{id}   page cotr.production / cotr.storage: waste and cupping of the
                                                pools / HUBs, per menu (cups) or item (088)
    POST /api/cotr/driver/issues                {"category", "severity", "title", "description", "photo", "latitude", "longitude"} (088)
    GET  /api/cotr/issues?status=&fleet=&pool=&days=, GET /api/cotr/issues/{id}   page cotr.fleet / cotr.check-in (088)
    POST /api/cotr/issues/{id}/step             page cotr.fleet: {"status", "note", "handler", "cost"} handle an issue
    GET  /api/cotr/reconcile?pool=&dateFrom=&dateTo=     page cotr.storage / cotr.production: workbook vs ESB vs app per pool-day
    GET  /api/cotr/pool-ledger?pool=&dateFrom=&dateTo=   page cotr.storage / cotr.production: the sheet "Product" (089)
    PUT  /api/cotr/pool-count                   {"pool", "day", "lines": [{"menu", "qty", "batchNo", "expiresOn"}]} the physical leftover
    PUT  /api/cotr/pool-moves                   {"pool", "day", "kind": transfer|arrived, "lines": {menu: cups}, "note"}
    POST /api/cotr/uploads                      page cotr.production: {"upload", "index", "data"} a part of a big file
    POST /api/cotr/pool-ledger/import           page cotr.production: {"pool", "file" | "upload", "dateFrom", "dateTo", "apply"} the pool
                                                workbook (sheets Product, Report Armada) saved as source 'sheet'
    GET  /api/cotr/pool-available?pool=&day=    what a pool can still assign today per menu
    GET  /api/cotr/batch-codes?pool=&dateFrom=&dateTo=   the production / batch codes counted at the pools (SSDD)
    PUT  /api/cotr/batch-codes/qty              {"pool", "day", "menu", "batchNo", "qty", "reason"} correct the cups of a code
    GET  /api/cotr/esb/summary | /esb/documents?module=&dateFrom=&dateTo=&branch=&q= | /esb/documents/{module}/{num} |
         /esb/lines?module=&...      the ESB company COTR (CALF5, integration_esb_cotr, migration 090)
    GET  /api/cotr/report-armada?pool=&dateFrom=&dateTo=   B / S / C / W / T per driver, cash, QRIS, operational
    GET  /api/cotr/area-requests?dateFrom=&dateTo=&status=&pool=   page cotr.schedule / cotr.check-in (088)
    POST /api/cotr/area-requests/{id}/decide    page cotr.schedule: {"approve", "note", "code", "name", "address", "city"}
    GET  /api/cotr/photos/{check-in|signature|request}/{id}   the team (cotr.check-in) or the driver it belongs to
"""
import base64
import binascii
import re
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import activity, cotr, cotr_esb, cotr_ledger
from app.access import deny, has_page, require_page
from app.pic_checkin import CheckInError
from app.routes.auth import current_user, require_roles
from app.config import get_settings
from app.utils import today
from app.thumbs import image_response

router = APIRouter(prefix="/api/cotr", tags=["cotr"])
driver_only = require_roles("driver")
ENTITY_PAGE = {"pools": "cotr.pools", "fleet": "cotr.fleet", "fleet-types": "cotr.fleet", "shift-templates": "cotr.schedule", "incentive-schemes": "cotr.incentive", "menus": "cotr.menu",
               "boms": "cotr.production", "items": "cotr.production", "drivers": "cotr.drivers",
               "areas": "cotr.pools", "accessories": "cotr.fleet", "menu-categories": "cotr.menu", "request-kinds": "cotr.check-in"}  # migration 080: with the pools and the fleet
Entity = Literal["pools", "fleet", "fleet-types", "shift-templates", "incentive-schemes", "menus", "boms", "items", "drivers",
                 "areas", "accessories", "menu-categories", "request-kinds"]
MANAGEMENT = "cotr.management"  # salary, bank and NIK of the drivers (migration 050)


def _call(fn, *args):
    try:
        return fn(*args)
    except CheckInError as e:  # CotrError, or a photo that cannot be read
        raise HTTPException(422, detail=str(e)) from None


def _log(request: Request, user: dict, action: str, summary: str, details: Optional[dict] = None) -> None:
    activity.record(action, user=user, request=request, summary=summary, details=details or {})


# ---------------------------------------------------------------- driver app

class CheckInIn(BaseModel):
    kind: Literal["start", "end"]
    photo: str = Field(min_length=20, max_length=5_000_000)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracyM: Optional[float] = Field(None, ge=0, le=100_000)
    stock: dict[str, int] = Field(default_factory=dict)
    note: Optional[str] = Field(None, max_length=500)
    # end of the shift ("Report Armada"): cups wasted and given for tasting per menu, the cash counted and spent
    waste: dict[str, int] = Field(default_factory=dict)
    cupping: dict[str, int] = Field(default_factory=dict)
    cashActual: Optional[float] = Field(None, ge=0, le=100_000_000)
    cashSpent: Optional[float] = Field(None, ge=0, le=100_000_000)
    signature: Optional[str] = Field(None, max_length=1_000_000)  # start: PNG data URL of the signature pad
    # end, migration 084: the cups not tapped in are booked as recap sales (the QRIS part read from the QRIS mutations)
    recapSales: bool = False
    recapQris: Optional[float] = Field(None, ge=0, le=100_000_000)


class CountsIn(BaseModel):
    counts: dict[str, int] = Field(default_factory=dict)
    note: Optional[str] = Field(None, max_length=500)


class SaleLine(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    qty: int = Field(ge=1, le=cotr.MAX_QTY)


class SaleIn(BaseModel):
    lines: list[SaleLine] = Field(min_length=1, max_length=50)
    payment: Literal["cash", "qris"]


class ReasonIn(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


class ReloadIn(BaseModel):
    lines: dict[str, int]
    note: Optional[str] = Field(None, max_length=300)


class RequestIn(BaseModel):
    kind: str = Field(min_length=1, max_length=40)  # a code of Master → Pengajuan (migration 083)
    amount: float = Field(gt=0, le=100_000_000)
    note: str = Field(min_length=1, max_length=500)
    photo: Optional[str] = Field(None, max_length=5_000_000)  # required unless the kind says otherwise


@router.get("/driver/today")
def driver_today(user: dict = Depends(driver_only)):
    return _call(cotr.today_of, user)


@router.post("/driver/check-in")
def driver_check_in(body: CheckInIn, request: Request, user: dict = Depends(driver_only)):
    out = _call(cotr.check_in, user, body.kind, body.photo, body.latitude, body.longitude, body.accuracyM, body.stock, body.note,
                body.waste, body.cupping, body.cashActual, body.cashSpent, body.signature,
                body.recapSales, body.recapQris)
    _log(request, user, f"cotr.check_in.{body.kind}", f"COTR check {'in' if body.kind == 'start' else 'out'}: {out['driver']['code']}",
         {"stock": body.stock})
    return out


@router.post("/driver/dispatch/confirm")
def driver_confirm(body: CountsIn, request: Request, user: dict = Depends(driver_only)):
    out = _call(cotr.confirm_dispatch, user, body.counts, body.note)
    d = out["dispatch"]
    _log(request, user, "cotr.dispatch.check", f"COTR assignment {'sesuai' if d['status'] == 'accepted' else 'berbeda'}: {out['driver']['code']}")
    return out


@router.post("/driver/sales")
def driver_sell(body: SaleIn, user: dict = Depends(driver_only)):
    return _call(cotr.sell, user, [line.model_dump() for line in body.lines], body.payment)


@router.post("/driver/sales/{sale_id}/void")
def driver_void(sale_id: str, body: ReasonIn, request: Request, user: dict = Depends(driver_only)):
    out = _call(cotr.void_sale, user, sale_id, body.reason)
    _log(request, user, "cotr.sale.void", f"COTR transaksi dibatalkan: {body.reason}", {"saleId": sale_id})
    return out


@router.post("/driver/reloads")
def driver_reload(body: ReloadIn, user: dict = Depends(driver_only)):
    return _call(cotr.request_reload, user, body.lines, body.note)


@router.post("/driver/reloads/{reload_id}/receive")
def driver_receive(reload_id: str, body: Optional[CountsIn] = None, user: dict = Depends(driver_only)):
    return _call(cotr.receive_reload, user, reload_id, body.counts if body else None, body.note if body else None)


@router.post("/driver/requests")
def driver_request(body: RequestIn, request: Request, user: dict = Depends(driver_only)):
    out = _call(cotr.send_request, user, body.kind, body.amount, body.note, body.photo)
    _log(request, user, "cotr.request", f"COTR pengajuan {body.kind}: Rp {body.amount:,.0f}")
    return out


class Point(BaseModel):
    at: datetime
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy: Optional[float] = Field(None, ge=0, le=100_000)
    speed: Optional[float] = Field(None, ge=0, le=200)
    heading: Optional[float] = Field(None, ge=0, le=360)
    battery: Optional[int] = Field(None, ge=0, le=100)


class PointsIn(BaseModel):
    points: list[Point] = Field(min_length=1, max_length=cotr.MAX_POINTS)


@router.post("/driver/locations")
def driver_locations(body: PointsIn, user: dict = Depends(driver_only)):
    return _call(cotr.record_locations, user, [p.model_dump() for p in body.points])


class AreaRequestIn(BaseModel):
    """migration 088: an area of the master (`area`), or a new place (`name` + the GPS there)"""
    area: Optional[str] = Field(None, max_length=40)
    name: Optional[str] = Field(None, max_length=120)
    address: Optional[str] = Field(None, max_length=300)
    city: Optional[str] = Field(None, max_length=80)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    accuracyM: Optional[float] = Field(None, ge=0, le=100_000)
    start: Optional[str] = Field(None, max_length=5)
    end: Optional[str] = Field(None, max_length=5)
    note: Optional[str] = Field(None, max_length=300)


@router.post("/driver/area-requests")
def driver_area_request(body: AreaRequestIn, request: Request, user: dict = Depends(driver_only)):
    out = _call(cotr.send_area_request, user, body.model_dump())
    _log(request, user, "cotr.area-request", f"COTR permintaan area {body.area or body.name}")
    return out


# ---------------------------------------------------------------- portal

class DecisionIn(BaseModel):
    approve: bool
    note: Optional[str] = Field(None, max_length=300)
    lines: Optional[dict[str, int]] = None  # a reload: the cups sent (the ones asked when not given)


class IssueIn(BaseModel):
    """migration 088: an issue of the armada reported by its driver"""
    category: str = Field(min_length=1, max_length=20)
    severity: Literal["low", "medium", "high"] = "medium"
    title: str = Field(min_length=3, max_length=120)
    description: Optional[str] = Field(None, max_length=1000)
    photo: Optional[str] = Field(None, max_length=5_000_000)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)


@router.post("/driver/issues")
def driver_issue(body: IssueIn, request: Request, user: dict = Depends(driver_only)):
    out = _call(cotr.report_issue, user, body.model_dump())
    _log(request, user, "cotr.issue", f"COTR laporan armada ({body.severity}): {body.title}")
    return out


class IssueStepIn(BaseModel):
    status: Literal["open", "in_progress", "resolved", "rejected"]
    note: Optional[str] = Field(None, max_length=1000)
    handler: Optional[str] = Field(None, max_length=120)
    cost: Optional[float] = Field(None, ge=0, le=1_000_000_000)


@router.get("/issues")
def issues(status: Optional[Literal["open", "in_progress", "resolved", "rejected"]] = None, fleet: Optional[str] = None,
           pool: Optional[str] = None, days: int = Query(90, ge=1, le=730), _: dict = Depends(require_page("cotr.fleet", "cotr.check-in"))):
    return cotr.issues(status, fleet, pool, days)


@router.get("/issues/{issue_id}")
def issue_detail(issue_id: str, _: dict = Depends(require_page("cotr.fleet", "cotr.check-in"))):
    return _call(cotr.issue_detail, issue_id)


@router.post("/issues/{issue_id}/step")
def issue_step(issue_id: str, body: IssueStepIn, request: Request, user: dict = Depends(require_page("cotr.fleet"))):
    out = _call(cotr.handle_issue, user, issue_id, body.model_dump())
    _log(request, user, "cotr.issue.step", f"COTR laporan armada {out['fleet']} -> {body.status}: {out['title']}", {"id": issue_id})
    return out


class AreaDecisionIn(BaseModel):
    approve: bool
    note: Optional[str] = Field(None, max_length=300)
    # a new place, as the team corrects it before it becomes an area of the master
    code: Optional[str] = Field(None, max_length=40)
    name: Optional[str] = Field(None, max_length=120)
    address: Optional[str] = Field(None, max_length=300)
    city: Optional[str] = Field(None, max_length=80)


@router.get("/area-requests")
def area_requests(dateFrom: Optional[date] = None, dateTo: Optional[date] = None, status: Optional[Literal["sent", "approved", "rejected"]] = None,
                  pool: Optional[str] = None, _: dict = Depends(require_page("cotr.schedule", "cotr.check-in"))):
    end = dateTo or today() + timedelta(days=7)
    return cotr.area_requests(dateFrom or today() - timedelta(days=14), end, status, pool)


@router.post("/area-requests/{request_id}/decide")
def decide_area_request(request_id: str, body: AreaDecisionIn, request: Request, user: dict = Depends(require_page("cotr.schedule"))):
    out = _call(cotr.decide_area_request, user, request_id, body.approve, body.model_dump())
    _log(request, user, "cotr.area-request.decide", f"COTR permintaan area {'disetujui' if body.approve else 'ditolak'}: {out['areaName']}",
         {"id": request_id, "area": out["area"]})
    return out


class LinesIn(BaseModel):
    lines: dict[str, int]
    note: Optional[str] = Field(None, max_length=300)


class AssignIn(LinesIn):
    day: date
    driver: str = Field(min_length=1, max_length=40)
    cupping: Optional[dict[str, int]] = None  # the part of the lines for tasting (migration 061)


@router.get("/monitor")
def monitor(day: Optional[date] = None, _: dict = Depends(require_page("cotr.check-in"))):
    return cotr.monitor(day or today())


@router.post("/reloads/{reload_id}/decide")
def decide_reload(reload_id: str, body: DecisionIn, request: Request, user: dict = Depends(require_page("cotr.storage"))):
    _call(cotr.decide_reload, user, reload_id, body.approve, body.note, body.lines)
    _log(request, user, "cotr.reload.decide", f"Reload COTR {'disetujui' if body.approve else 'ditolak'}", {"reloadId": reload_id})
    return {"ok": True}


@router.post("/reloads/{reload_id}/resolve")
def resolve_reload(reload_id: str, body: LinesIn, request: Request, user: dict = Depends(require_page("cotr.storage"))):
    _call(cotr.resolve_reload, user, reload_id, body.lines, body.note)
    _log(request, user, "cotr.reload.resolve", "Selisih reload COTR diputuskan", {"reloadId": reload_id, "lines": body.lines})
    return {"ok": True}


@router.get("/storage")
def storage(day: Optional[date] = None, pool: Optional[str] = None, _: dict = Depends(require_page("cotr.storage"))):
    return cotr.storage_day(day or today(), pool)


@router.put("/dispatch")
def assign(body: AssignIn, request: Request, user: dict = Depends(require_page("cotr.storage"))):
    out = _call(cotr.assign, user, body.day, body.driver, body.lines, body.note, body.cupping)
    _log(request, user, "cotr.dispatch.assign", f"Assignment COTR {body.driver} {body.day}", {"lines": body.lines})
    return out


@router.post("/dispatch/{dispatch_id}/resolve")
def resolve_dispatch(dispatch_id: str, body: LinesIn, request: Request, user: dict = Depends(require_page("cotr.storage"))):
    out = _call(cotr.resolve_dispatch, user, dispatch_id, body.lines, body.note)
    _log(request, user, "cotr.dispatch.resolve", f"Selisih assignment COTR {out['driver']} diputuskan", {"lines": body.lines})
    return out


@router.post("/requests/{request_id}/decide")
def decide_request(request_id: str, body: DecisionIn, request: Request, user: dict = Depends(require_page("cotr.check-in"))):
    _call(cotr.decide_request, user, request_id, body.approve, body.note)
    _log(request, user, "cotr.request.decide", f"Pengajuan COTR {'disetujui' if body.approve else 'ditolak'}",
         {"requestId": request_id})
    return {"ok": True}


COTR_PAGES = tuple(sorted(set(ENTITY_PAGE.values()) | {"cotr.check-in", "cotr.storage", "cotr.schedule", "cotr.incentive", MANAGEMENT}))


def _master_user(request: Request, entity: str, user: dict = Depends(current_user)) -> dict:
    """Writes need the page of the master; reading any master needs any COTR page (Master Driver lists the pools …)."""
    if not has_page(user, ENTITY_PAGE[entity]):
        deny(request, user)
    return user


def _cotr_reader(request: Request, user: dict = Depends(current_user)) -> dict:
    if not any(has_page(user, p) for p in COTR_PAGES):
        deny(request, user)
    return user


@router.get("/masters/{entity}")
def master_list(entity: Entity, user: dict = Depends(_cotr_reader)):
    return cotr.master_list(entity, has_page(user, MANAGEMENT))


@router.post("/masters/{entity}", status_code=201)
def master_create(entity: Entity, body: dict, request: Request, user: dict = Depends(_master_user)):
    out = _call(cotr.master_save, user, entity, None, body, has_page(user, MANAGEMENT))
    _log(request, user, "cotr.master.create", f"COTR {entity}: {out.get('code')} dibuat")
    return out


@router.put("/masters/{entity}/{code}")
def master_update(entity: Entity, code: str, body: dict, request: Request, user: dict = Depends(_master_user)):
    out = _call(cotr.master_save, user, entity, code, body, has_page(user, MANAGEMENT))
    _log(request, user, "cotr.master.update", f"COTR {entity}: {code} diubah")
    return out


@router.delete("/masters/{entity}/{code}")
def master_delete(entity: Entity, code: str, request: Request, user: dict = Depends(_master_user)):
    _call(cotr.master_delete, entity, code)
    _log(request, user, "cotr.master.delete", f"COTR {entity}: {code} dihapus")
    return {"ok": True}


class ImageIn(BaseModel):
    image: Optional[str] = Field(None, max_length=5_000_000)  # data URL; null removes the picture


@router.put("/masters/{entity}/{code}/image/{slot}")
def master_image_put(entity: Literal["drivers", "fleet"], code: str, slot: Literal["photo", "qris"], body: ImageIn,
                     request: Request, user: dict = Depends(_master_user)):
    if slot not in cotr.IMAGES[entity]:
        raise HTTPException(404, detail="Gambar tidak dikenal")
    _call(cotr.set_image, user, entity, code, slot, body.image)
    _log(request, user, "cotr.master.image", f"COTR {entity} {code}: {slot} {'diganti' if body.image else 'dihapus'}")
    return {"ok": True}


@router.get("/masters/{entity}/{code}/image/{slot}")
def master_image(entity: Literal["drivers", "fleet"], code: str, slot: Literal["photo", "qris"], w: Optional[int] = Query(None, ge=48, le=1600), user: dict = Depends(_cotr_reader)):
    row = cotr.image(entity, code, slot) if slot in cotr.IMAGES[entity] else None
    if not row or not row["data"]:
        raise HTTPException(404, detail="Gambar tidak ditemukan")
    return image_response(row["data"], row["type"], w, "private, max-age=300")


@router.get("/fleet/{code}/card")
def fleet_card(code: str, _: dict = Depends(_cotr_reader)):
    out = cotr.fleet_card(code, True)
    if out is None:
        raise HTTPException(404, detail="Armada tidak ditemukan")
    return out


@router.post("/fleet/{code}/label-token")
def renew_label_token(code: str, request: Request, user: dict = Depends(require_page("cotr.fleet"))):
    token = _call(cotr.renew_label_token, code)
    _log(request, user, "cotr.fleet.label", f"Label armada {code}: QR baru (label lama tidak berlaku)")
    return {"label_token": token}


@router.get("/employees")
def employees(q: Optional[str] = Query(None, max_length=80), user: dict = Depends(current_user)):
    """User Accounts → COTR (2026-11-12): the employees of Master Driver to make an account for (super admins)."""
    if user.get("role") != "superadmin":
        raise HTTPException(403, detail="Khusus super admin")
    return {"data": cotr.employees(q)}


@router.get("/driver-accounts")
def driver_accounts(_: dict = Depends(require_page("cotr.drivers"))):
    return cotr.driver_accounts()


class ImportIn(BaseModel):
    file: str = Field(min_length=10, max_length=14_000_000)  # base64 of the .xlsx (<= 10 MB)
    pool: Optional[str] = Field(None, max_length=40)  # the "DATA COTR" workbook takes the pool of each placement
    apply: bool = False


@router.post("/drivers/import")
def drivers_import(body: ImportIn, request: Request, user: dict = Depends(require_page("cotr.drivers"))):
    try:
        data = base64.b64decode(re.sub(r"^data:[^,]*,", "", body.file), validate=False)
    except (binascii.Error, ValueError):
        raise HTTPException(422, detail="File tidak dapat dibaca") from None
    team = _call(cotr.parse_team, data)
    if team:  # the workbook "DATA COTR" (migration 060): every employee with its pool, department, bike and armada
        out = _call(cotr.import_team, user, team, body.apply)
        if body.apply:
            _log(request, user, "cotr.team.import", f"Impor tim COTR: {out['new']} baru, {out['update']} diperbarui",
                 {"new": out["new"], "update": out["update"], "skipped": len(out["skipped"])})
        return out
    if not body.pool:
        raise HTTPException(422, detail="Pilih pool untuk file armada")
    rows = _call(cotr.parse_drivers, data)
    if not rows:
        raise HTTPException(422, detail="Tidak ada driver ditemukan: pakai kolom Armada, Nama, HP atau sheet Report Armada")
    out = _call(cotr.import_drivers, user, rows, body.pool, body.apply)
    if body.apply:
        _log(request, user, "cotr.drivers.import", f"Import driver COTR: {out['new']} baru, {out['update']} diubah")
    return out


@router.get("/locations")
def locations(day: Optional[date] = None, _: dict = Depends(require_page("cotr.check-in"))):
    return cotr.positions(day or today())


@router.get("/locations/{driver_code}")
def driver_track(driver_code: str, day: Optional[date] = None, _: dict = Depends(require_page("cotr.check-in"))):
    return cotr.track(driver_code, day or today())


class ScheduleCell(BaseModel):
    driver: str = Field(min_length=1, max_length=40)
    day: date
    status: Optional[Literal["work", "off", "leave", "sick"]] = None  # null clears the cell
    template: Optional[str] = Field(None, max_length=40)
    pool: Optional[str] = Field(None, max_length=40)
    note: Optional[str] = Field(None, max_length=300)
    # several assignments of the day (migration 080); null = keep the slots as they are
    slots: Optional[list["SlotIn"]] = Field(None, max_length=12)


class SlotIn(BaseModel):
    template: Optional[str] = Field(None, max_length=40)
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    area: Optional[str] = Field(None, max_length=40)
    fleet: Optional[str] = Field(None, max_length=40)
    note: Optional[str] = Field(None, max_length=300)


ScheduleCell.model_rebuild()


class ScheduleIn(BaseModel):
    entries: list[ScheduleCell] = Field(min_length=1, max_length=2000)


class ScheduleCopyIn(BaseModel):
    source: date
    target: date
    days: int = Field(7, ge=1, le=31)
    drivers: Optional[list[str]] = None


@router.get("/schedule")
def schedule(dateFrom: Optional[date] = None, dateTo: Optional[date] = None, pool: Optional[str] = None,
             _: dict = Depends(require_page("cotr.schedule"))):
    start = dateFrom or today() - timedelta(days=today().weekday())
    return _call(cotr.schedule, start, dateTo or start + timedelta(days=6), pool)


@router.put("/schedule")
def schedule_save(body: ScheduleIn, request: Request, user: dict = Depends(require_page("cotr.schedule"))):
    n = _call(cotr.schedule_save, user, [e.model_dump() for e in body.entries])
    _log(request, user, "cotr.schedule.save", f"Jadwal COTR: {n} sel disimpan")
    return {"ok": True, "saved": n}


@router.post("/schedule/copy")
def schedule_copy(body: ScheduleCopyIn, request: Request, user: dict = Depends(require_page("cotr.schedule"))):
    n = _call(cotr.schedule_copy, user, body.source, body.target, body.days, body.drivers)
    _log(request, user, "cotr.schedule.copy", f"Jadwal COTR disalin {body.source} → {body.target} ({n} sel)")
    return {"ok": True, "copied": n}


class BatchIn(BaseModel):
    hub: str = Field(min_length=1, max_length=40)
    menu: str = Field(min_length=1, max_length=40)
    qty: int = Field(gt=0, le=100_000)
    producedOn: Optional[date] = None
    expiresOn: Optional[date] = None
    note: Optional[str] = Field(None, max_length=300)


class SendIn(BaseModel):
    pool: str = Field(min_length=1, max_length=40)
    qty: int = Field(gt=0, le=100_000)
    note: Optional[str] = Field(None, max_length=300)


@router.get("/batches")
def batches(dateFrom: Optional[date] = None, dateTo: Optional[date] = None, hub: Optional[str] = None,
            _: dict = Depends(require_page("cotr.production", "cotr.storage"))):
    end = dateTo or today()
    return cotr.batches(dateFrom or end - timedelta(days=13), end, hub)


@router.post("/batches", status_code=201)
def add_batch(body: BatchIn, request: Request, user: dict = Depends(require_page("cotr.production"))):
    out = _call(cotr.add_batch, user, body.model_dump())
    _log(request, user, "cotr.batch.add", f"Produksi COTR {out['batchNo']}: {body.menu} {body.qty}", out)
    return out


@router.delete("/batches/{batch_id}")
def delete_batch(batch_id: str, request: Request, user: dict = Depends(require_page("cotr.production"))):
    _call(cotr.delete_batch, batch_id)
    _log(request, user, "cotr.batch.delete", "Batch produksi COTR dihapus", {"id": batch_id})
    return {"ok": True}


@router.post("/batches/{batch_id}/send")
def send_batch(batch_id: str, body: SendIn, request: Request, user: dict = Depends(require_page("cotr.production"))):
    out = _call(cotr.send_batch, user, batch_id, body.pool, body.qty, body.note)
    _log(request, user, "cotr.batch.send", f"Kiriman batch ke {body.pool}: {body.qty}", {"batch": batch_id, **body.model_dump()})
    return out


@router.post("/transfers/{transfer_id}/receive")
def receive_transfer(transfer_id: str, request: Request, user: dict = Depends(require_page("cotr.storage", "cotr.production"))):
    _call(cotr.receive_transfer, user, transfer_id)
    _log(request, user, "cotr.transfer.receive", "Kiriman produksi diterima pool", {"id": transfer_id})
    return {"ok": True}


class CountLine(BaseModel):
    menu: str = Field(min_length=1, max_length=40)
    qty: int = Field(ge=0, le=100_000)
    batchNo: Optional[str] = Field(None, max_length=40)  # the production / batch code (SSDD)
    expiresOn: Optional[date] = None


class CountIn(BaseModel):
    pool: str = Field(min_length=1, max_length=40)
    day: date
    lines: list[CountLine] = Field(max_length=500)


class MovesIn(BaseModel):
    pool: str = Field(min_length=1, max_length=40)
    day: date
    kind: Literal["transfer", "arrived"]
    lines: dict[str, int]
    note: Optional[str] = Field(None, max_length=300)


@router.get("/pool-ledger")
def pool_ledger(pool: Optional[str] = None, dateFrom: Optional[date] = None, dateTo: Optional[date] = None,
                _: dict = Depends(require_page("cotr.storage", "cotr.production"))):
    end = dateTo or today()
    start = dateFrom or end - timedelta(days=6)
    if (end - start).days > 62:
        raise HTTPException(422, detail="Periode paling lama 62 hari")
    return _call(cotr_ledger.run, pool, start, end) if pool else _call(cotr_ledger.run_all, start, end)


@router.put("/pool-count")
def pool_count(body: CountIn, request: Request, user: dict = Depends(require_page("cotr.storage", "cotr.production"))):
    out = _call(cotr_ledger.save_count, user, body.pool, body.day, [x.model_dump(mode="json") for x in body.lines])
    _log(request, user, "cotr.pool-count", f"COTR sisa aktual {body.pool} {body.day}: {sum(x.qty for x in body.lines)} cup",
         {"pool": body.pool, "day": body.day.isoformat()})
    return out


@router.put("/pool-moves")
def pool_moves(body: MovesIn, request: Request, user: dict = Depends(require_page("cotr.storage", "cotr.production"))):
    out = _call(cotr_ledger.save_moves, user, body.pool, body.day, body.kind, body.lines, body.note)
    _log(request, user, "cotr.pool-moves", f"COTR product {body.kind} {body.pool} {body.day}", {"lines": body.lines})
    return out


class WorkbookIn(BaseModel):
    pool: str = Field(min_length=1, max_length=40)
    file: Optional[str] = Field(None, max_length=4_000_000)  # base64 xlsx (small files)
    upload: Optional[str] = Field(None, pattern=r"^[a-f0-9]{32}$")  # or a file sent in parts (POST /uploads)
    dateFrom: date
    dateTo: Optional[date] = None
    apply: bool = False


class PartIn(BaseModel):
    upload: str = Field(pattern=r"^[a-f0-9]{32}$")
    index: int = Field(ge=0, le=40)
    data: str = Field(min_length=1, max_length=3_500_000)  # base64 of one part


_UPLOADS = Path(get_settings().media_dir) / "cotr-uploads"


def _upload_path(user: dict, upload: str, index: Optional[int] = None) -> Path:
    base = _UPLOADS / f"{user['id']}-{upload}"
    return base / f"{index:03d}" if index is not None else base


@router.post("/uploads")
def upload_part(body: PartIn, user: dict = Depends(require_page("cotr.production"))):
    """A part of a big file (a pool workbook is several MB, more than a request through the portal carries)."""
    try:
        data = base64.b64decode(body.data.split(",")[-1])
    except (binascii.Error, ValueError):
        raise HTTPException(422, detail="File tidak dapat dibaca") from None
    folder = _upload_path(user, body.upload)
    folder.mkdir(parents=True, exist_ok=True)
    for old in _UPLOADS.glob("*"):  # parts older than a day are left over
        if old.is_dir() and old.stat().st_mtime < time.time() - 86400:
            shutil.rmtree(old, ignore_errors=True)
    _upload_path(user, body.upload, body.index).write_bytes(data)
    return {"ok": True}


@router.post("/pool-ledger/import")
def pool_import(body: WorkbookIn, request: Request, user: dict = Depends(require_page("cotr.production"))):
    if body.upload:
        folder = _upload_path(user, body.upload)
        parts = sorted(folder.glob("[0-9][0-9][0-9]")) if folder.is_dir() else []
        if not parts:
            raise HTTPException(422, detail="File belum terkirim")
        data = b"".join(p.read_bytes() for p in parts)
    else:
        try:
            data = base64.b64decode((body.file or "").split(",")[-1])
        except (binascii.Error, ValueError):
            raise HTTPException(422, detail="File tidak dapat dibaca") from None
    out = _call(cotr_ledger.import_workbook, user, body.pool, data, body.dateFrom, body.dateTo, body.apply)
    if body.apply:
        if body.upload:
            shutil.rmtree(_upload_path(user, body.upload), ignore_errors=True)
        _log(request, user, "cotr.pool-import", f"COTR import workbook {body.pool} dari {body.dateFrom}", out)
    return out


class LotIn(BaseModel):
    pool: str = Field(min_length=1, max_length=40)
    day: date
    menu: str = Field(min_length=1, max_length=40)
    batchNo: str = Field(min_length=1, max_length=40)
    qty: int = Field(ge=0, le=100_000)
    reason: str = Field(min_length=3, max_length=300)


@router.put("/batch-codes/qty")
def batch_code_qty(body: LotIn, request: Request, user: dict = Depends(require_page("cotr.storage", "cotr.production"))):
    out = _call(cotr_ledger.adjust_lot, user, body.pool, body.day, body.menu, body.batchNo, body.qty)
    _log(request, user, "cotr.batch-code.adjust", f"COTR kode batch {body.batchNo} {body.pool} {body.day}: {out['old']} -> {out['new']} cup ({body.reason})",
         out | {"reason": body.reason})
    return out


@router.get("/pool-available")
def pool_available(pool: str, day: Optional[date] = None, _: dict = Depends(require_page("cotr.storage", "cotr.production"))):
    """What the pool can still assign to its drivers today, per menu (opening + arrived / transfer − given …)."""
    d = day or today()
    a = _call(cotr_ledger.availability, pool, d)
    menus = {m["code"]: m["name"] for m in cotr.menus(active_only=True)}
    return {"pool": pool, "day": d.isoformat(), "counted": a is not None,
            "rows": [{"menu": c, "name": menus.get(c, c), **v} for c, v in (a or {}).items() if c in menus]}


@router.get("/reconcile")
def reconcile(pool: Optional[str] = None, dateFrom: Optional[date] = None, dateTo: Optional[date] = None,
              _: dict = Depends(require_page("cotr.storage", "cotr.production"))):
    end = dateTo or today()
    start = dateFrom or end.replace(day=1)
    if start > end or (end - start).days > 62:
        raise HTTPException(422, detail="Periode paling lama 62 hari")
    return cotr_ledger.reconcile(pool or None, start, end)


@router.get("/batch-codes")
def batch_codes(pool: Optional[str] = None, dateFrom: Optional[date] = None, dateTo: Optional[date] = None,
                _: dict = Depends(require_page("cotr.storage", "cotr.production"))):
    end = dateTo or today()
    start = dateFrom or end.replace(day=1)
    if (end - start).days > 92:
        raise HTTPException(422, detail="Periode paling lama 92 hari")
    return cotr_ledger.batch_codes(pool, start, end)


# ---------------------------------------------------------------- the ESB company COTR (migration 090)

@router.get("/esb/summary")
def esb_summary(_: dict = Depends(require_page("cotr.production", "cotr.storage"))):
    return cotr_esb.summary()


@router.get("/esb/documents")
def esb_documents(module: str, dateFrom: Optional[date] = None, dateTo: Optional[date] = None, branch: Optional[str] = None,
                  q: Optional[str] = Query(None, max_length=80), _: dict = Depends(require_page("cotr.production", "cotr.storage"))):
    end = dateTo or today()
    start = dateFrom or end.replace(day=1)
    if (end - start).days > 400:
        raise HTTPException(422, detail="Periode paling lama 400 hari")
    return _call(cotr_esb.documents, module, start, end, branch, q)


class ApplyIn(BaseModel):
    apply: bool = False


@router.post("/esb/sync-recipes")
def esb_sync_recipes(body: ApplyIn, request: Request, user: dict = Depends(require_page("cotr.production"))):
    """The items and recipe cards made the same as ESB COTR (checked first with apply false)."""
    out = _call(cotr_esb.sync_recipes, user, body.apply)
    if body.apply:
        _log(request, user, "cotr.recipes.esb", f"COTR resep & item disamakan dengan ESB: {out['cards']} kartu, {out['items']} item", out)
    return out


@router.get("/esb/lines")
def esb_lines(module: str, dateFrom: Optional[date] = None, dateTo: Optional[date] = None, branch: Optional[str] = None,
              _: dict = Depends(require_page("cotr.production", "cotr.storage"))):
    end = dateTo or today()
    start = dateFrom or end.replace(day=1)
    if (end - start).days > 400:
        raise HTTPException(422, detail="Periode paling lama 400 hari")
    return {"data": _call(cotr_esb.lines, module, start, end, branch)}


@router.get("/esb/documents/{module}/{doc_num}")
def esb_document(module: str, doc_num: str, _: dict = Depends(require_page("cotr.production", "cotr.storage"))):
    return _call(cotr_esb.document, module, doc_num)


@router.get("/report-armada")
def report_armada(pool: Optional[str] = None, dateFrom: Optional[date] = None, dateTo: Optional[date] = None,
                  _: dict = Depends(require_page("cotr.storage", "cotr.production", "cotr.check-in"))):
    end = dateTo or today()
    start = dateFrom or end.replace(day=1)
    if (end - start).days > 62:
        raise HTTPException(422, detail="Periode paling lama 62 hari")
    return cotr_ledger.report_armada(pool, start, end)


@router.get("/pool-stock")
def pool_stock(pool: Optional[str] = None, day: Optional[date] = None, _: dict = Depends(require_page("cotr.storage", "cotr.production"))):
    return cotr.pool_stock(pool, day or today())


@router.get("/incentives")
def incentives(month: Optional[str] = None, scheme: Optional[str] = None, pool: Optional[str] = None,
               _: dict = Depends(require_page("cotr.incentive"))):
    try:
        day = date.fromisoformat(f"{month}-01") if month else today().replace(day=1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Bulan harus YYYY-MM")
    return _call(cotr.incentives, day, scheme, pool)


# ---------------------------------------------------------------- pay adjustments and payslips (migration 092)

class PayAdjustmentIn(BaseModel):
    driver: str = Field(min_length=1, max_length=40)
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    kind: Literal["bonus", "kasbon", "minus", "warning", "other"]
    amount: float = Field(gt=0, le=1_000_000_000)
    note: Optional[str] = Field(None, max_length=300)


def _month(value: str) -> date:
    return date.fromisoformat(f"{value}-01")


@router.get("/pay-adjustments")
def pay_adjustments(month: str, driver: Optional[str] = None, _: dict = Depends(require_page("cotr.incentive"))):
    return {"data": cotr.pay_adjustment_list(_month(month), driver)}


@router.post("/pay-adjustments", status_code=201)
def add_pay_adjustment(body: PayAdjustmentIn, request: Request, user: dict = Depends(require_page("cotr.incentive"))):
    out = _call(cotr.add_pay_adjustment, user, body.driver, _month(body.month), body.kind, body.amount, body.note)
    _log(request, user, "cotr.pay-adjustment", f"COTR {body.kind} {body.driver} {body.month}: Rp {body.amount:,.0f}", body.model_dump())
    return out


@router.delete("/pay-adjustments/{adjustment_id}")
def delete_pay_adjustment(adjustment_id: int, request: Request, user: dict = Depends(require_page("cotr.incentive"))):
    _call(cotr.delete_pay_adjustment, adjustment_id)
    _log(request, user, "cotr.pay-adjustment.delete", f"COTR hapus komponen gaji #{adjustment_id}")
    return {"ok": True}


class PublishIn(BaseModel):
    on: bool


@router.post("/payslips/{month}/publish")
def publish_payslips(month: str, body: PublishIn, request: Request, user: dict = Depends(require_page("cotr.incentive"))):
    cotr.publish_payslips(user, _month(month), body.on)
    _log(request, user, "cotr.payslips.publish", f"COTR slip gaji {month} {'diterbitkan' if body.on else 'ditarik'}")
    return {"ok": True}


@router.get("/payslips/{month}/{driver}")
def payslip_of(month: str, driver: str, _: dict = Depends(require_page("cotr.incentive"))):
    out = cotr.payslip(driver, _month(month), published_only=False)
    if not out:
        raise HTTPException(404, detail="Karyawan tidak ditemukan")
    return out


@router.get("/driver/payslips")
def driver_payslips(user: dict = Depends(driver_only)):
    """The COTR app: the months published and their payslips of the employee signed in."""
    d = cotr.require_driver(user)
    return {"data": [s for s in (cotr.payslip(d["code"], _month(m)) for m in cotr.payslip_months(d["code"])) if s]}


@router.get("/usage")
def usage(dateFrom: Optional[date] = None, dateTo: Optional[date] = None, pool: Optional[str] = None,
          _: dict = Depends(require_page("cotr.production"))):
    end = dateTo or today()
    return cotr.usage(dateFrom or end - timedelta(days=6), end, pool)


@router.get("/production")
def production(days: int = Query(7, ge=1, le=60), buffer: float = Query(10, ge=0, le=100),
               _: dict = Depends(require_page("cotr.production"))):
    return cotr.production(days, buffer)


class WasteIn(BaseModel):
    day: Optional[date] = None
    pool_code: str = Field(min_length=1, max_length=40)
    item: str = Field("", max_length=120)  # a menu (menu_code) names itself
    qty: float = Field(gt=0)
    unit: str = Field("cup", min_length=1, max_length=10)
    reason: str = Field(min_length=1, max_length=300)
    # migration 081: a waste of the production (with its batch) and the item of the master it deducts
    kind: Literal["storage", "production", "cupping"] = "storage"  # migration 088: cupping at the pool / HUB
    menu_code: Optional[str] = Field(None, max_length=40)  # migration 088: cups of a menu
    batch_id: Optional[str] = Field(None, max_length=40)
    item_code: Optional[str] = Field(None, max_length=40)


@router.get("/waste")
def waste_list(dateFrom: Optional[date] = None, dateTo: Optional[date] = None, pool: Optional[str] = None,
               _: dict = Depends(require_page("cotr.production", "cotr.storage"))):
    end = dateTo or today()
    return cotr.waste_list(dateFrom or end - timedelta(days=30), end, pool)


@router.post("/waste", status_code=201)
def waste_add(body: WasteIn, user: dict = Depends(require_page("cotr.production", "cotr.storage"))):
    return _call(cotr.waste_add, user, body.model_dump())


@router.delete("/waste/{waste_id}")
def waste_delete(waste_id: str, _: dict = Depends(require_page("cotr.production", "cotr.storage"))):
    cotr.waste_delete(waste_id)
    return {"ok": True}


@router.get("/photos/{kind}/{row_id}")
def photo(kind: Literal["check-in", "signature", "request", "issue"], row_id: str, request: Request, w: Optional[int] = Query(None, ge=48, le=1600), user: dict = Depends(current_user)):
    if not has_page(user, "cotr.check-in") and not (kind == "issue" and has_page(user, "cotr.fleet")):
        d = cotr.driver_of(user) if user["role"] == "driver" else None
        if not d or cotr.photo_owner(kind, row_id) != d["code"]:
            deny(request, user)
    row = cotr.photo(kind, row_id)
    if not row or not row["photo"]:
        raise HTTPException(404, detail="Foto tidak ditemukan")
    return image_response(row["photo"], row["photo_type"], w, "private, max-age=86400")


# ---------------------------------------------------------------- label QR of an armada (no sign-in, migration 063)

public_router = APIRouter()


def _fleet_of(token: str) -> str:
    row = cotr.fleet_by_token(token)
    if not row:
        raise HTTPException(404, detail="Label tidak dikenal atau sudah diganti")
    return row["code"]


@public_router.get("/api/public/fleet/{token}")
def public_fleet(token: str):
    return cotr.fleet_card(_fleet_of(token), False)


@public_router.get("/api/public/fleet/{token}/photo")
def public_fleet_photo(token: str, w: Optional[int] = Query(None, ge=48, le=1600)):
    row = cotr.image("fleet", _fleet_of(token), "photo")
    if not row or not row["data"]:
        raise HTTPException(404, detail="Foto tidak ada")
    return image_response(row["data"], row["type"], w, "public, max-age=300")


@public_router.get("/api/public/fleet/{token}/driver/{driver}/photo")
def public_driver_photo(token: str, driver: str, w: Optional[int] = Query(None, ge=48, le=1600)):
    code = _fleet_of(token)
    d = cotr.db.fetchrow(f"SELECT 1 FROM {cotr.P}.cotr_driver WHERE code = %s AND fleet_code = %s AND status <> 'inactive'", (driver, code))
    row = cotr.image("drivers", driver, "photo") if d else None
    if not row or not row["data"]:
        raise HTTPException(404, detail="Foto tidak ada")
    return image_response(row["data"], row["type"], w, "public, max-age=300")


class AccessoryIn(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    qty: int = Field(1, ge=1, le=1000)
    note: Optional[str] = Field(None, max_length=300)
    assignedOn: Optional[date] = None  # the day it is used from (2026-11-09)


@router.get("/fleet-accessories")
def fleet_accessories(fleet: Optional[str] = None, _: dict = Depends(require_page("cotr.fleet"))):
    return {"data": _call(cotr.fleet_accessories, fleet)}


@router.post("/fleet/{fleet}/accessories")
def fleet_assign(fleet: str, body: AccessoryIn, request: Request, user: dict = Depends(require_page("cotr.fleet"))):
    out = _call(cotr.fleet_assign, user, fleet, body.code.upper(), body.qty, body.note, body.assignedOn)
    _log(request, user, "cotr.fleet.accessory", f"Accessories {body.code} ke fleet {fleet}")
    return {"data": out}


@router.delete("/fleet-accessories/{assignment_id}")
def fleet_unassign(assignment_id: int, request: Request, user: dict = Depends(require_page("cotr.fleet"))):
    _call(cotr.fleet_unassign, user, assignment_id)
    _log(request, user, "cotr.fleet.accessory", f"Accessories dilepas ({assignment_id})")
    return {"ok": True}


@router.get("/accessory-usage")
def accessory_usage(dateFrom: Optional[date] = None, dateTo: Optional[date] = None, _: dict = Depends(require_page("cotr.fleet"))):
    end = dateTo or today()
    return {"data": _call(cotr.accessory_usage, dateFrom or end - timedelta(days=30), end)}


# ---------------------------------------------------------------- storage & production stock (migration 081)

class BatchWasteIn(BaseModel):
    qty: int = Field(ge=0, le=100_000)
    reason: Optional[str] = Field(None, max_length=300)


class ReceiptIn(BaseModel):
    pool: str = Field(min_length=1, max_length=40)
    day: Optional[date] = None
    item: str = Field(min_length=1, max_length=40)
    qty: float = Field(gt=0, le=10_000_000)
    unit: Optional[str] = Field(None, max_length=20)
    supplier: Optional[str] = Field(None, max_length=120)
    docNo: Optional[str] = Field(None, max_length=60)
    note: Optional[str] = Field(None, max_length=300)


class CountIn(BaseModel):
    id: Optional[str] = Field(None, max_length=40)
    pool: str = Field(min_length=1, max_length=40)
    day: Optional[date] = None
    lines: dict = Field(default_factory=dict)
    note: Optional[str] = Field(None, max_length=300)
    submit: bool = False


@router.post("/batches/{batch_id}/waste")
def batch_waste(batch_id: str, body: BatchWasteIn, request: Request, user: dict = Depends(require_page("cotr.production"))):
    out = _call(cotr.batch_waste, user, batch_id, body.qty, body.reason)
    _log(request, user, "cotr.batch.waste", f"Waste produksi {out['batchNo']}: {body.qty} cup")
    return out


@router.get("/stock")
def stock(pool: Optional[str] = None, day: Optional[date] = None, _: dict = Depends(require_page("cotr.production", "cotr.storage"))):
    return _call(cotr.stock, pool, day) if pool else _call(cotr.stock_all, day)


@router.get("/receipts")
def receipts(pool: Optional[str] = None, dateFrom: Optional[date] = None, dateTo: Optional[date] = None,
             _: dict = Depends(require_page("cotr.production", "cotr.storage"))):
    end = dateTo or today()
    return {"data": _call(cotr.receipts, pool, dateFrom or end - timedelta(days=30), end)}


@router.post("/receipts", status_code=201)
def receipt_add(body: ReceiptIn, request: Request, user: dict = Depends(require_page("cotr.production", "cotr.storage"))):
    out = _call(cotr.receipt_add, user, body.model_dump())
    _log(request, user, "cotr.receipt.add", f"Barang masuk {body.pool}: {body.item} {body.qty}")
    return out


@router.delete("/receipts/{receipt_id}")
def receipt_delete(receipt_id: str, request: Request, user: dict = Depends(require_page("cotr.production", "cotr.storage"))):
    _call(cotr.receipt_delete, receipt_id)
    _log(request, user, "cotr.receipt.delete", "Barang masuk dihapus", {"id": receipt_id})
    return {"ok": True}


@router.get("/counts")
def counts(pool: Optional[str] = None, _: dict = Depends(require_page("cotr.production", "cotr.storage"))):
    return {"data": _call(cotr.counts, pool)}


@router.get("/counts/{count_id}")
def count_detail(count_id: str, _: dict = Depends(require_page("cotr.production", "cotr.storage"))):
    return _call(cotr.count_detail, count_id)


@router.post("/counts")
def count_save(body: CountIn, request: Request, user: dict = Depends(require_page("cotr.production", "cotr.storage"))):
    out = _call(cotr.count_save, user, body.model_dump())
    _log(request, user, "cotr.count.save", f"Stock opname COTR {body.pool} {out['day']}: {len(out['lines'])} item")
    return out


@router.post("/counts/{count_id}/approve")
def count_approve(count_id: str, request: Request, user: dict = Depends(require_page("cotr.production"))):
    out = _call(cotr.count_approve, user, count_id)
    _log(request, user, "cotr.count.approve", f"Stock opname COTR {out['pool']} {out['day']} di-approve")
    return out
