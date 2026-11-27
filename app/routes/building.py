"""Building & Renovation (app/building.py, migration 162), page mon.building.

    GET    /api/building/catalog                     materials, groups, vendors and the summary (?all=1: the inactive too)
    POST   /api/building/materials                   a material {group, category, spec, detail, unit, price, priceOn, vendorId | vendor, note, repeat}
    PUT    /api/building/materials/{id}              the same, plus {active}
    GET    /api/building/materials/{id}/prices       the price history
    POST   /api/building/vendors                     a store / supplier {kind, name, location, goods, contactName, phone, maps, website, bank, accountNo, accountName, note}
    PUT    /api/building/vendors/{id}                the same, plus {active}
"""
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from app import activity
from app import building as bd
from app.access import deny, has_page
from app.routes.auth import current_user

router = APIRouter(tags=["building"])
PAGE = "mon.building"
CAMEL = {"contactName": "contact_name", "accountNo": "account_no", "accountName": "account_name"}


def _need(request: Request, user: dict) -> None:
    if not has_page(user, PAGE):
        deny(request, user)


def _run(fn, *args):
    try:
        return fn(*args)
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from None
    except (bd.BuildingError, ValueError) as e:
        raise HTTPException(422, detail=str(e)) from None


def _body(b: Any) -> dict:
    if not isinstance(b, dict):
        raise HTTPException(422, detail="Data tidak valid")
    return {CAMEL.get(k, k): v for k, v in b.items()}


@router.get("/api/building/catalog")
def catalog(request: Request, all: Optional[int] = None, user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    return {**bd.materials(bool(all)), "vendors": bd.vendors(bool(all)), "summary": bd.summary(), "kinds": bd.KINDS}


@router.post("/api/building/materials", status_code=201)
def add_material(request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    out = _run(bd.save_material, user, None, _body(body))
    activity.record("building.material", user=user, request=request, page="/monitoring/building", summary=f"Menambah material {out['category']} {out['spec'] or ''}".strip())
    return out


@router.put("/api/building/materials/{material_id}")
def edit_material(material_id: int, request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    out = _run(bd.save_material, user, material_id, _body(body))
    activity.record("building.material", user=user, request=request, page="/monitoring/building", summary=f"Mengubah material {out['category']} {out['spec'] or ''}".strip())
    return out


@router.get("/api/building/materials/{material_id}/prices")
def material_prices(material_id: int, request: Request, user: dict = Depends(current_user)) -> list[dict]:
    _need(request, user)
    return bd.prices(material_id)


@router.post("/api/building/vendors", status_code=201)
def add_vendor(request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    out = _run(bd.save_vendor, user, None, _body(body))
    activity.record("building.vendor", user=user, request=request, page="/monitoring/building", summary=f"Menambah {out['kindLabel'].lower()} {out['name']}")
    return out


@router.put("/api/building/vendors/{vendor_id}")
def edit_vendor(vendor_id: int, request: Request, body: Any = Body(...), user: dict = Depends(current_user)) -> dict:
    _need(request, user)
    out = _run(bd.save_vendor, user, vendor_id, _body(body))
    activity.record("building.vendor", user=user, request=request, page="/monitoring/building", summary=f"Mengubah {out['kindLabel'].lower()} {out['name']}")
    return out
