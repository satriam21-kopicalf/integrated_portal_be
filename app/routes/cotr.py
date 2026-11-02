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
    POST /api/cotr/driver/requests              {"kind": fuel|ice|stall|repair|parking|other, "amount", "note", "photo"}
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
    GET  /api/cotr/incentives?month=YYYY-MM&scheme=&pool=   page cotr.incentive: incentive per driver (053)
    GET/POST /api/cotr/waste, DELETE /api/cotr/waste/{id}   page cotr.production
    GET  /api/cotr/photos/{check-in|signature|request}/{id}   the team (cotr.check-in) or the driver it belongs to
"""
import base64
import binascii
import re
from datetime import date, datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import activity, cotr
from app.access import deny, has_page, require_page
from app.pic_checkin import CheckInError
from app.routes.auth import current_user, require_roles
from app.utils import today

router = APIRouter(prefix="/api/cotr", tags=["cotr"])
driver_only = require_roles("driver")
ENTITY_PAGE = {"pools": "cotr.pools", "fleet": "cotr.fleet", "fleet-types": "cotr.fleet", "shift-templates": "cotr.schedule", "incentive-schemes": "cotr.incentive", "menus": "cotr.menu",
               "boms": "cotr.production", "items": "cotr.production", "drivers": "cotr.drivers"}
Entity = Literal["pools", "fleet", "fleet-types", "shift-templates", "incentive-schemes", "menus", "boms", "items", "drivers"]
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
    kind: Literal["fuel", "ice", "stall", "repair", "parking", "other"]
    amount: float = Field(gt=0, le=100_000_000)
    note: str = Field(min_length=1, max_length=500)
    photo: str = Field(min_length=20, max_length=5_000_000)


@router.get("/driver/today")
def driver_today(user: dict = Depends(driver_only)):
    return _call(cotr.today_of, user)


@router.post("/driver/check-in")
def driver_check_in(body: CheckInIn, request: Request, user: dict = Depends(driver_only)):
    out = _call(cotr.check_in, user, body.kind, body.photo, body.latitude, body.longitude, body.accuracyM, body.stock, body.note,
                body.waste, body.cupping, body.cashActual, body.cashSpent, body.signature)
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


# ---------------------------------------------------------------- portal

class DecisionIn(BaseModel):
    approve: bool
    note: Optional[str] = Field(None, max_length=300)
    lines: Optional[dict[str, int]] = None  # a reload: the cups sent (the ones asked when not given)


class LinesIn(BaseModel):
    lines: dict[str, int]
    note: Optional[str] = Field(None, max_length=300)


class AssignIn(LinesIn):
    day: date
    driver: str = Field(min_length=1, max_length=40)


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
    out = _call(cotr.assign, user, body.day, body.driver, body.lines, body.note)
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
def master_image(entity: Literal["drivers", "fleet"], code: str, slot: Literal["photo", "qris"], user: dict = Depends(_cotr_reader)):
    row = cotr.image(entity, code, slot) if slot in cotr.IMAGES[entity] else None
    if not row or not row["data"]:
        raise HTTPException(404, detail="Gambar tidak ditemukan")
    return Response(bytes(row["data"]), media_type=row["type"], headers={"Cache-Control": "private, max-age=300"})


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


@router.get("/incentives")
def incentives(month: Optional[str] = None, scheme: Optional[str] = None, pool: Optional[str] = None,
               _: dict = Depends(require_page("cotr.incentive"))):
    try:
        day = date.fromisoformat(f"{month}-01") if month else today().replace(day=1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Bulan harus YYYY-MM")
    return _call(cotr.incentives, day, scheme, pool)


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
    item: str = Field(min_length=1, max_length=120)
    qty: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=10)
    reason: str = Field(min_length=1, max_length=300)


@router.get("/waste")
def waste_list(dateFrom: Optional[date] = None, dateTo: Optional[date] = None, _: dict = Depends(require_page("cotr.production"))):
    end = dateTo or today()
    return cotr.waste_list(dateFrom or end - timedelta(days=30), end)


@router.post("/waste", status_code=201)
def waste_add(body: WasteIn, user: dict = Depends(require_page("cotr.production"))):
    return _call(cotr.waste_add, user, body.model_dump())


@router.delete("/waste/{waste_id}")
def waste_delete(waste_id: str, _: dict = Depends(require_page("cotr.production"))):
    cotr.waste_delete(waste_id)
    return {"ok": True}


@router.get("/photos/{kind}/{row_id}")
def photo(kind: Literal["check-in", "signature", "request"], row_id: str, request: Request, user: dict = Depends(current_user)):
    if not has_page(user, "cotr.check-in"):
        d = cotr.driver_of(user) if user["role"] == "driver" else None
        if not d or cotr.photo_owner(kind, row_id) != d["code"]:
            deny(request, user)
    row = cotr.photo(kind, row_id)
    if not row or not row["photo"]:
        raise HTTPException(404, detail="Foto tidak ditemukan")
    return Response(bytes(row["photo"]), media_type=row["photo_type"], headers={"Cache-Control": "private, max-age=86400"})
