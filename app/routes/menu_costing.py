"""Cost Control: Pricelist Items (page cc.pricelist) and Sales Calculator by Menu (page cc.sales-calc), app/menu_costing.py.

    GET    /api/cost-control/pricelist?search=&category=&date=   items (bahan baku) with the price valid on the date
    GET    /api/cost-control/pricelist/{code}/history           the prices of an item
    PUT    /api/cost-control/pricelist                          {"rows": [{code, price | null, validFrom, note}]}
    GET    /api/cost-control/sales-calc/recipes                 the menu cards with their menus and their list prices
    POST   /api/cost-control/sales-calc/recipes                 create   PUT /.../recipes/{code} update (menus too)
    DELETE /api/cost-control/sales-calc/recipes/{code}
    GET    /api/cost-control/sales-calc/menus?days=             ESB POS menus sold lately and their card
    GET    /api/cost-control/sales-calc/menus/{id}/package      what ESB records inside a menu (bundling contents)
    POST   /api/cost-control/sales-calc/auto-cards            {"dry"}: a card for every menu without one (ESB BOM, bundles)
    GET    /api/cost-control/cogs/outlets?dateFrom&dateTo&branch   page cc.cogs: COGS & Usage per outlet (app/outlet_cogs.py)
    GET    /api/cost-control/sales-calc/wips                    WIP / premix recipes (production calculator)
    GET/POST /api/cost-control/sales-calc/extras, PUT/DELETE /.../extras/{id}   items outside the cards (cable ties,
                                                                plastic …) per bill / portion / outlet-day (migration 057)
    GET    /api/cost-control/sales-calc/report?dateFrom&dateTo&branch&horizon&buffer
                                                                MRP, usage actual vs system, COGS Material
    GET    /api/cost-control/sales-calc/options?days=           the choices sold with the menus (Less Sugar, Extra Shot …)
    PUT    /api/cost-control/sales-calc/options/rule            {"name", "effect": none | adjust | clear, "lines", "note"}
    GET    /api/cost-control/sales-calc/esb-boms?search=        the ESB bills of material and the POS menus of their name
    GET    /api/cost-control/sales-calc/esb-boms/{id}           the card a BOM makes (matched lines and what was not)
    POST   /api/cost-control/sales-calc/esb-boms/{id}/import    {"code"?, "name"?, "menus"?: [{id, name}]} the card
A role "user" given a page sees only its own branches (app/scope.py).
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import activity, menu_costing as mc, menu_options as mo
from app.access import require_page
from app.scope import NO_BRANCH, scoped_branch
from app.utils import parse_branches, today

router = APIRouter(prefix="/api/cost-control", tags=["cost-control"])
PRICE, CALC = "cc.pricelist", "cc.sales-calc"


def _refuse(exc: Exception):
    raise HTTPException(status_code=422, detail=str(exc))


class PriceRow(BaseModel):
    code: str = Field(min_length=1, max_length=60)
    price: Optional[float] = Field(None, ge=0, le=1_000_000_000)
    validFrom: Optional[date] = None
    note: Optional[str] = Field(None, max_length=300)


class PricesIn(BaseModel):
    rows: list[PriceRow] = Field(min_length=1, max_length=2000)


@router.get("/pricelist")
def pricelist(search: str = "", category: str = "", day: Optional[date] = Query(None, alias="date"),
              _: dict = Depends(require_page(PRICE, CALC))) -> dict:
    return mc.items(search, category, day)


@router.get("/pricelist/{code}/history")
def price_history(code: str, _: dict = Depends(require_page(PRICE))) -> dict:
    return {"data": mc.price_history(code)}


@router.put("/pricelist")
def set_prices(body: PricesIn, request: Request, user: dict = Depends(require_page(PRICE))) -> dict:
    try:
        n = mc.set_prices([r.model_dump() for r in body.rows], user)
    except mc.CostingError as exc:
        _refuse(exc)
    activity.record("cc.pricelist", user=user, request=request, page="/cost-control/pricelist",
                    summary=f"Pricelist bahan baku: {n} harga disimpan", details={"codes": [r.code for r in body.rows][:50]})
    return {"saved": n}


@router.get("/sales-calc/recipes")
def recipes(_: dict = Depends(require_page(CALC))) -> dict:
    return {"data": mc.recipes(with_prices=True)}


def _save(key: Optional[str], body: dict, request: Request, user: dict) -> dict:
    try:
        out = mc.save_recipe(key, body, user)
    except mc.CostingError as exc:
        _refuse(exc)
    activity.record("cc.menu-recipe", user=user, request=request, page="/cost-control/sales-calculator",
                    summary=f"Resep menu {out['code']} {'dibuat' if key is None else 'diubah'}",
                    details={"code": out["code"], "menus": len(out["menus"])})
    return out


@router.post("/sales-calc/recipes", status_code=201)
def create_recipe(body: dict, request: Request, user: dict = Depends(require_page(CALC))) -> dict:
    return _save(None, body, request, user)


@router.put("/sales-calc/recipes/{code}")
def update_recipe(code: str, body: dict, request: Request, user: dict = Depends(require_page(CALC))) -> dict:
    return _save(code, body, request, user)


@router.delete("/sales-calc/recipes/{code}")
def delete_recipe(code: str, request: Request, user: dict = Depends(require_page(CALC))) -> dict:
    if not mc.delete_recipe(code):
        raise HTTPException(404, detail="Resep tidak ditemukan")
    activity.record("cc.menu-recipe", user=user, request=request, page="/cost-control/sales-calculator",
                    summary=f"Resep menu {code} dihapus", details={"code": code})
    return {"ok": True}


@router.get("/sales-calc/menus")
def menus(days: int = Query(90, ge=7, le=365), _: dict = Depends(require_page(CALC))) -> dict:
    return {"data": mc.menus(days)}


@router.get("/sales-calc/menus/{menu_id}/package")
def package(menu_id: str, _: dict = Depends(require_page(CALC))) -> dict:
    try:
        return mc.package_contents(menu_id)
    except mc.CostingError as exc:
        _refuse(exc)


@router.post("/sales-calc/auto-cards")
def auto_cards(body: dict, request: Request, user: dict = Depends(require_page(CALC))) -> dict:
    """A card for every menu sold lately without one: the ESB BOM of the menu, a bundle from its package contents."""
    dry = bool(body.get("dry", True))
    out = mo.auto_cards(user, dry=dry)
    if not dry:
        activity.record("cc.menu-recipe", user=user, request=request, page="/cost-control/sales-calculator",
                        summary=f"Kartu resep otomatis: {len(out['created'])} dari BOM, {len(out['bundles'])} bundling, "
                                f"{len(out['assigned'])} menu dipasang, {len(out['skipped'])} dilewati",
                        details={k: out[k] for k in ("created", "bundles", "assigned")})
    return out


@router.get("/sales-calc/wips")
def wips(_: dict = Depends(require_page(CALC))) -> dict:
    return {"data": list(mc.wips().values())}


@router.get("/sales-calc/extras")
def extras(_: dict = Depends(require_page(CALC))) -> dict:
    return {"data": mc.extras()}


def _save_extra(extra_id: Optional[int], body: dict, request: Request, user: dict) -> dict:
    try:
        out = mc.save_extra(extra_id, body, user)
    except mc.CostingError as exc:
        _refuse(exc)
    activity.record("cc.extra-item", user=user, request=request, page="/cost-control/sales-calculator",
                    summary=f"Item tambahan {out['name']} {'ditambah' if extra_id is None else 'diubah'}", details=out)
    return out


@router.post("/sales-calc/extras", status_code=201)
def create_extra(body: dict, request: Request, user: dict = Depends(require_page(CALC))) -> dict:
    return _save_extra(None, body, request, user)


@router.put("/sales-calc/extras/{extra_id}")
def update_extra(extra_id: int, body: dict, request: Request, user: dict = Depends(require_page(CALC))) -> dict:
    return _save_extra(extra_id, body, request, user)


@router.delete("/sales-calc/extras/{extra_id}")
def delete_extra(extra_id: int, request: Request, user: dict = Depends(require_page(CALC))) -> dict:
    if not mc.delete_extra(extra_id):
        raise HTTPException(404, detail="Item tambahan tidak ditemukan")
    activity.record("cc.extra-item", user=user, request=request, page="/cost-control/sales-calculator",
                    summary=f"Item tambahan #{extra_id} dihapus", details={"id": extra_id})
    return {"ok": True}


@router.get("/sales-calc/report")
def report(dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None), branch: Optional[str] = None,
           horizon: int = Query(7, ge=1, le=60), buffer: float = Query(10, ge=0, le=200),
           _: dict = Depends(require_page(CALC))) -> dict:
    end = dateTo or today()
    start = dateFrom or end - timedelta(days=6)
    if start > end or (end - start).days > 92:
        raise HTTPException(422, detail="Periode 1 hari sampai 3 bulan")
    scoped = scoped_branch(branch)
    branches = None if scoped is None else ([] if scoped == NO_BRANCH else parse_branches(scoped))
    return mc.report(start, end, branches, horizon, buffer)


@router.get("/cogs/outlets")
def cogs_outlets(dateFrom: Optional[date] = Query(None), dateTo: Optional[date] = Query(None), branch: Optional[str] = None,
                 _: dict = Depends(require_page("cc.cogs"))) -> dict:
    """COGS & Usage per outlet between two stock opnames (app/outlet_cogs.py)."""
    from app import outlet_cogs
    end = dateTo or today()
    start = dateFrom or end - timedelta(days=6)
    if start > end or (end - start).days > 92:
        raise HTTPException(422, detail="Periode 1 hari sampai 3 bulan")
    scoped = scoped_branch(branch)
    branches = None if scoped is None else ([] if scoped == NO_BRANCH else parse_branches(scoped))
    return outlet_cogs.outlets(start, end, branches)


# ---------------------------------------------------------------- variants & modifiers, ESB BOM (migration 071)

@router.get("/sales-calc/options")
def options(days: int = Query(90, ge=7, le=180), _: dict = Depends(require_page(CALC))) -> dict:
    return mo.options(days)


@router.put("/sales-calc/options/rule")
def save_option_rule(body: dict, request: Request, user: dict = Depends(require_page(CALC))) -> dict:
    try:
        out = mo.save_rule(str(body.get("name") or ""), body, user)
    except mo.OptionError as exc:
        _refuse(exc)
    activity.record("cc.menu-option", user=user, request=request, page="/cost-control/sales-calculator",
                    summary=f"Aturan pilihan {out['name']}: {out.get('effect') or 'dihapus'}", details=out)
    return out


@router.get("/sales-calc/esb-boms")
def esb_boms(search: str = Query("", max_length=80), _: dict = Depends(require_page(CALC))) -> dict:
    return {"data": mo.boms(search)}


@router.get("/sales-calc/esb-boms/{bom_id}")
def esb_bom(bom_id: str, _: dict = Depends(require_page(CALC))) -> dict:
    try:
        return mo.bom_card(bom_id)
    except mo.OptionError as exc:
        _refuse(exc)


@router.post("/sales-calc/esb-boms/{bom_id}/import")
def import_bom(bom_id: str, body: dict, request: Request, user: dict = Depends(require_page(CALC))) -> dict:
    try:
        out = mo.import_bom(bom_id, body, user)
    except (mo.OptionError, mc.CostingError) as exc:
        _refuse(exc)
    activity.record("cc.menu-recipe", user=user, request=request, page="/cost-control/sales-calculator",
                    summary=f"Resep menu {out['code']} dari BOM ESB", details={"code": out["code"], "bom": bom_id, "menus": len(out["menus"])})
    return out
