"""Cost Control: Pricelist Items (bahan baku) and Sales Calculator by Menu (migration 056).

Pricelist   the price of every raw material (kc_master.item, STOCK) per base unit from a date (cc_item_price); the price
            of a day is the latest one valid by then. As a reference the average purchase price of the last 90 days
            is shown beside it (ESB purchase invoice lines in the base unit of the item, Σ qty × price / Σ qty).
Menu cards  a recipe card per menu as the production calculator: lines of raw materials (base unit) or WIP / premix
            recipes (kc_recipe active version, in grams, exploded into their ingredients per gram of batch); ESB POS
            menus are assigned to a card (cc_menu_recipe_map, by menu id), several menus may share one card.
Report      for a period and branches, from the POS sales per menu (agg_menu_daily):
            1. Material Required Planning: average daily sales × horizon days (+ buffer) exploded into raw materials,
               less the latest stock of the branches (on_hand) = what to order;
            2. Usage actual vs system over the branches with SO awal and SO akhir (stock_at: the portal stock opname
               from 28 Oct 2026, else the ESB document): system = sold × cards (exploded), actual = SO awal +
               purchases − SO akhir per item;
            3. COGS Material: system usage × pricelist; per menu the HPP of a portion, the cost of the sold portions
               and the ratio to the menu's sales; and the actual COGS (actual usage × pricelist) of the branches with
               both counts against all their sales.
Bundling    a card may hold other cards as lines (type menu: "3 x Es Kopi Calf Premium", 2026-11-06); the report flattens
            them (resolve) so a bundle uses the materials of its menus. A bundle with a card takes over the menus ESB
            records inside it as packages (their quantity sold with that bundle is left out of the menus, no double
            count); a bundle without a card whose contents are recorded as packages is counted through them.
Non-sales   the menus of the ESB bills without a bill number (other cost: CUPPING, WASTE, COMPLIMENT, end product QC
            …, by the payment method that names them) are made too: exploded through the cards into the system usage
            (2026-11-06), apart from the sales; the waste entered in the portal (raw materials, premix) is no menu bill,
            so the two never overlap.
Selling     the list price of every menu from the POS menu master (its active price templates: DI = dine-in, OL =
price       online), beside the HPP of the card as % HPP.
"""
import math
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from psycopg.types.json import Jsonb

from app import database as db
from app.database import SCHEMA
from app import test_branches
from app.scope import COMPANY_NAMES, MAIN_COMPANY
from app.utils import TTLCache

P = "integration_portal"
# the Sales Calculator kept warm (2026-11-10): the menus sold (the heavy part of Assign Menus) a few minutes, a report per
# filter a couple of minutes and only while nothing of the calculator changed (stamp(): prices, cards, mapping, extras,
# variants, goods receipt corrections); the sales and stock opname of the day follow within REPORT_S
MENUS_S, REPORT_S = 600, 120
_cache = TTLCache()


def stamp() -> str:
    """The last change of what the calculator is set up with: a save anywhere makes the cached reports stale."""
    r = db.fetchrow(f"""SELECT concat_ws('|',
          (SELECT max(updated_at)::text || count(*) FROM {P}.cc_item_price), (SELECT max(updated_at)::text || count(*) FROM {P}.cc_menu_recipe),
          (SELECT max(updated_at)::text || count(*) FROM {P}.cc_menu_recipe_map), (SELECT max(updated_at)::text || count(*) FROM {P}.cc_extra_item),
          (SELECT max(updated_at)::text || count(*) FROM {P}.cc_option_rule), (SELECT max(created_at)::text || count(*) FROM {P}.cc_gr_correction),
          (SELECT max(updated_at)::text FROM kc_master.item)) AS s""")
    return r["s"] if r else ""
SKIP_STATUSES = ("Rejected", "Cancelled", "Void")


class CostingError(ValueError):
    """Refused with a message for the user (HTTP 422)."""


def _f(v: Any) -> float:
    return float(v) if v is not None else 0.0


# ---------------------------------------------------------------- pricelist

ITEM_STATUS = {"active": "i.is_active", "inactive": "NOT i.is_active", "all": "true"}


def items(search: str = "", category: str = "", on: Optional[date] = None, status: str = "active") -> dict:
    """STOCK items of kc_master with their category, base unit, the price valid on `on` (today) and the average
    purchase price of the last 90 days. `status` (2026-11-09, List & Pricelist Items): active (the default, what the
    costing uses), inactive or all."""
    if status not in ITEM_STATUS:
        raise CostingError("Status item tidak dikenal")
    day = on or date.today()
    rows = db.fetch(
        f"""SELECT i.code, i.name, i.base_uom, c.name AS category, i.is_active,
                   pr.price, pr.valid_from, pr.note
              FROM kc_master.item i LEFT JOIN kc_master.item_category c ON c.id = i.category_id
              LEFT JOIN LATERAL (SELECT price, valid_from, note FROM {P}.cc_item_price p
                                  WHERE p.item_code = i.code AND p.valid_from <= %(day)s
                                  ORDER BY valid_from DESC LIMIT 1) pr ON true
             WHERE {ITEM_STATUS[status]} AND i.item_type = 'STOCK'
               AND (%(q)s = '' OR i.code ILIKE %(like)s OR i.name ILIKE %(like)s)
               AND (%(cat)s = '' OR c.name = %(cat)s)
             ORDER BY c.name NULLS LAST, i.name""",
        {"day": day, "since": day - timedelta(days=90), "q": search.strip(), "like": f"%{search.strip()}%", "cat": category})
    avg = purchase_avg(day - timedelta(days=90), day)
    hpp = esb_hpp(day)
    cats = [r["name"] for r in db.fetch(
        """SELECT DISTINCT c.name FROM kc_master.item i JOIN kc_master.item_category c ON c.id = i.category_id
            WHERE i.item_type = 'STOCK' ORDER BY 1""")]
    counts = db.fetchrow("""SELECT count(*) FILTER (WHERE is_active) AS active, count(*) FILTER (WHERE NOT is_active) AS inactive
                              FROM kc_master.item WHERE item_type = 'STOCK'""")
    return {"day": day.isoformat(), "categories": cats, "counts": {"active": counts["active"], "inactive": counts["inactive"]}, "data": [{
        "code": r["code"], "name": r["name"], "unit": r["base_uom"], "category": r["category"], "active": r["is_active"],
        "price": _f(r["price"]) if r["price"] is not None else None,
        "validFrom": r["valid_from"].isoformat() if r["valid_from"] else None, "note": r["note"],
        "purchaseAvg": avg.get((r["code"], str(r["base_uom"] or "").upper())),
        "esbHpp": hpp.get(r["code"], (None, 0))[0], "esbHppLocations": hpp.get(r["code"], (None, 0))[1]} for r in rows]}


def set_item_active(code: str, active: bool, user: dict) -> dict:
    """Activate or deactivate an item of the master (2026-11-09): an inactive item leaves the stock opname forms, the
    Pricelist and the item pickers; its history stays."""
    row = db.fetchrow("""UPDATE kc_master.item SET is_active = %s, updated_at = now() WHERE code = %s
                         RETURNING code, name, is_active""", (active, code))
    if not row:
        raise CostingError("Item tidak ditemukan")
    return {"code": row["code"], "name": row["name"], "active": row["is_active"]}


def esb_hpp(day: date) -> dict[str, tuple[float, int]]:
    """{item code: (median ESB HPP of one base unit, outlets)}: the unit cost (end HPP ÷ end qty, base unit) of the latest
    valuation period of every location within 60 days, the median over the locations so that one outlet with an odd
    moving average (stock near zero) does not lead (2026-11-05)."""
    try:
        rows = db.fetch(
            f"""WITH v AS (SELECT DISTINCT ON (location_id, product_id) product_id, end_hpp / end_qty AS unit
                             FROM {SCHEMA}.inventory_valuation
                            WHERE end_qty > 0 AND end_hpp > 0 AND period_end BETWEEN %(a)s AND %(b)s
                            ORDER BY location_id, product_id, period_end DESC)
                SELECT i.code, percentile_cont(0.5) WITHIN GROUP (ORDER BY v.unit) AS med, count(*) AS n
                  FROM v JOIN kc_master.item_esb_map m ON m.esb_id = v.product_id AND m.company_code = %(co)s
                  JOIN kc_master.item i ON i.id = m.item_id GROUP BY i.code""",
            {"a": day - timedelta(days=60), "b": day, "co": MAIN_COMPANY})
    except Exception:  # noqa: BLE001 - the valuation unreachable: the pricelist works without the reference
        return {}
    return {r["code"]: (round(_f(r["med"]), 4), int(r["n"])) for r in rows}


def purchase_avg(d_from: date, d_to: date) -> dict[tuple[str, str], float]:
    """{(product code, UOM): average price} of the ESB purchase invoice lines of the period (Σ qty × price / Σ qty);
    a purchase in another unit (a pack) is left out of the base-unit price."""
    try:
        rows = db.fetch(
            f"""SELECT l.product_code, upper(coalesce(l.uom_name, '')) AS uom, sum(l.qty * l.price) AS value, sum(l.qty) AS qty
                  FROM {SCHEMA}.erp_documents d
                  JOIN {SCHEMA}.erp_document_lines l ON l.module = d.module AND l.doc_num = d.doc_num AND l.line_type = 'detail'
                 WHERE d.module = 'purchase_invoice' AND d.doc_date BETWEEN %s AND %s AND d.deleted_at IS NULL
                   AND d.status_name <> ALL(%s) AND l.product_code IS NOT NULL AND l.qty > 0
                 GROUP BY 1, 2""", (d_from, d_to, list(SKIP_STATUSES)))
    except Exception:  # noqa: BLE001 - the ESB documents unreachable: the pricelist still works without the reference
        return {}
    return {(r["product_code"], r["uom"]): round(_f(r["value"]) / _f(r["qty"]), 4) for r in rows if _f(r["qty"])}


def price_history(code: str) -> list[dict]:
    return [{"validFrom": r["valid_from"].isoformat(), "price": _f(r["price"]), "note": r["note"], "by": r["updated_by"],
             "at": r["updated_at"].isoformat()} for r in db.fetch(
        f"SELECT * FROM {P}.cc_item_price WHERE item_code = %s ORDER BY valid_from DESC", (code,))]


def set_prices(rows: list[dict], user: dict) -> int:
    """[{code, price, validFrom, note}] (price null removes the price of that date)."""
    known = {r["code"] for r in db.fetch("SELECT code FROM kc_master.item WHERE code = ANY(%s)",
                                         ([str(r.get("code")) for r in rows],))}
    by = user.get("full_name") or user["username"]
    n = 0
    with db.transaction() as conn:
        for r in rows:
            code = str(r.get("code") or "")
            if code not in known:
                raise CostingError(f"Item tidak dikenal: {code}")
            day = r.get("validFrom") or date.today()
            if r.get("price") is None:
                conn.execute(f"DELETE FROM {P}.cc_item_price WHERE item_code = %s AND valid_from = %s", (code, day))
            else:
                price = Decimal(str(r["price"]))
                if price < 0:
                    raise CostingError("Harga tidak boleh negatif")
                conn.execute(
                    f"""INSERT INTO {P}.cc_item_price (item_code, valid_from, price, note, updated_by) VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (item_code, valid_from) DO UPDATE SET price = EXCLUDED.price, note = EXCLUDED.note,
                            updated_by = EXCLUDED.updated_by, updated_at = now()""",
                    (code, day, price, (r.get("note") or "").strip()[:300] or None, by))
            n += 1
    return n


def prices_on(day: date, codes: Optional[list[str]] = None, later: bool = True) -> dict[str, float]:
    """{item: Pricelist price valid on `day`}. 2026-11-12: an item whose first price was set after `day` takes that first
    price (`later`): AIR NEW got its price on 12 Nov and every menu with water lost its HPP for the days before."""
    return {r["item_code"]: _f(r["price"]) for r in db.fetch(
        f"""SELECT DISTINCT ON (item_code) item_code, price FROM {P}.cc_item_price
             WHERE (%s OR valid_from <= %s) AND (%s::text[] IS NULL OR item_code = ANY(%s))
             ORDER BY item_code, (valid_from <= %s) DESC, CASE WHEN valid_from <= %s THEN valid_from END DESC NULLS LAST,
                      valid_from""", (later, day, codes, codes, day, day))}


# ---------------------------------------------------------------- WIP and menu cards

def wips() -> dict[str, dict]:
    """Every active WIP / premix recipe (active version): {code: {name, components: [{code, name, unit, perGram}]}}."""
    out: dict[str, dict] = {}
    try:
        rows = db.fetch(
            """SELECT r.code, r.name, v.output_qty_base AS batch, it.code AS item, coalesce(it.name, l.component_name) AS iname,
                      it.base_uom, l.is_stock, l.qty_base
                 FROM kc_recipe.recipe r JOIN kc_recipe.recipe_version v ON v.recipe_id = r.id AND v.status = 'ACTIVE'
                 JOIN kc_recipe.recipe_line l ON l.version_id = v.id LEFT JOIN kc_master.item it ON it.id = l.component_item_id
                WHERE r.is_active ORDER BY r.name, l.line_no""")
    except Exception:  # noqa: BLE001 - kc_recipe unreachable: the cards with raw materials only still work
        return out
    for r in rows:
        w = out.setdefault(r["code"], {"code": r["code"], "name": r["name"], "components": []})
        batch = _f(r["batch"]) or 1.0
        w["components"].append({"code": r["item"], "name": r["iname"], "unit": r["base_uom"] or "GR",
                                "isStock": r["is_stock"], "perGram": _f(r["qty_base"]) / batch})
    return out


def recipes(with_prices: bool = False) -> list[dict]:
    maps: dict[str, list[dict]] = {}
    for m in db.fetch(f"SELECT menu_id, menu_name, recipe_code FROM {P}.cc_menu_recipe_map ORDER BY menu_name"):
        maps.setdefault(m["recipe_code"], []).append({"id": m["menu_id"], "name": m["menu_name"]})
    if with_prices:  # the list prices of the menus of every card (the cards page: % HPP)
        prices = menu_prices([m["id"] for ms in maps.values() for m in ms])
        for ms in maps.values():
            for m in ms:
                m["prices"] = prices.get(m["id"], [])
    return [{"code": r["code"], "name": r["name"], "category": r["category"], "lines": r["lines"] or [], "note": r["note"],
             "isActive": r["is_active"], "menus": maps.get(r["code"], []), "updatedBy": r["updated_by"],
             "updatedAt": r["updated_at"].isoformat()}
            for r in db.fetch(f"SELECT * FROM {P}.cc_menu_recipe ORDER BY category NULLS LAST, name")]


def _lines(lines: list[dict]) -> list[dict]:
    out = []
    for ln in lines or []:
        kind, code = ln.get("type"), str(ln.get("code") or "").strip()
        if not code:
            continue
        if kind not in ("item", "wip", "other", "menu"):
            raise CostingError("Baris resep harus bahan baku / packaging, WIP, item lain atau menu (bundling)")
        qty = _f(ln.get("qty"))
        if qty <= 0:
            raise CostingError(f"Qty {ln.get('name') or code} harus lebih dari 0")
        line = {"type": kind, "code": code, "name": str(ln.get("name") or code)[:160], "qty": qty,
                "unit": "GR" if kind == "wip" else "PORSI" if kind == "menu" else str(ln.get("unit") or "")[:20]}
        if kind == "other":  # a free item: its own unit and price (not in kc_master)
            price = ln.get("price")
            line["price"] = _f(price) if price not in (None, "") else None
            if line["price"] is not None and line["price"] < 0:
                raise CostingError("Harga tidak boleh negatif")
        out.append(line)
    return out


def save_recipe(key: Optional[str], data: dict, user: dict) -> dict:
    code = str(data.get("code") or key or "").strip().upper()
    name = " ".join(str(data.get("name") or "").split())
    if not code or not name:
        raise CostingError("Kode dan nama resep wajib diisi")
    lines = _lines(data.get("lines"))
    if any(ln["type"] == "menu" for ln in lines):  # a bundle: its cards exist and never hold the bundle itself
        graph = {r["code"]: [ln["code"] for ln in r["lines"] or [] if ln.get("type") == "menu"]
                 for r in db.fetch(f"SELECT code, lines FROM {P}.cc_menu_recipe") if r["code"] != key}
        graph[code] = [ln["code"] for ln in lines if ln["type"] == "menu"]
        for ln in lines:
            if ln["type"] == "menu" and ln["code"] not in graph:
                raise CostingError(f"Kartu {ln['code']} tidak ditemukan")
        seen, todo = set(), list(graph[code])
        while todo:
            c = todo.pop()
            if c == code:
                raise CostingError("Bundling tidak boleh berisi kartunya sendiri")
            if c not in seen:
                seen.add(c)
                todo.extend(graph.get(c, []))
    row = (code, name, (data.get("category") or "").strip() or None, Jsonb(lines),
           (data.get("note") or "").strip() or None, bool(data.get("isActive", True)), user.get("full_name") or user["username"])
    with db.transaction() as conn:
        if key is None:
            if conn.execute(f"SELECT 1 FROM {P}.cc_menu_recipe WHERE code = %s", (code,)).fetchone():
                raise CostingError("Kode resep sudah dipakai")
            conn.execute(f"""INSERT INTO {P}.cc_menu_recipe (code, name, category, lines, note, is_active, updated_by)
                             VALUES (%s, %s, %s, %s, %s, %s, %s)""", row)
        else:
            cur = conn.execute(f"""UPDATE {P}.cc_menu_recipe SET code = %s, name = %s, category = %s, lines = %s, note = %s,
                                   is_active = %s, updated_by = %s, updated_at = now() WHERE code = %s""", (*row, key))
            if cur.rowcount == 0:
                raise CostingError("Resep tidak ditemukan")
        if "menus" in data:
            conn.execute(f"DELETE FROM {P}.cc_menu_recipe_map WHERE recipe_code = %s", (code,))
            for m in data.get("menus") or []:
                conn.execute(f"""INSERT INTO {P}.cc_menu_recipe_map (menu_id, menu_name, recipe_code, updated_by)
                                 VALUES (%s, %s, %s, %s) ON CONFLICT (menu_id) DO UPDATE SET recipe_code = EXCLUDED.recipe_code,
                                 menu_name = EXCLUDED.menu_name, updated_by = EXCLUDED.updated_by, updated_at = now()""",
                             (str(m["id"]), str(m.get("name") or m["id"])[:200], code, row[-1]))
    return next(r for r in recipes() if r["code"] == code)


def resolve(cards: dict[str, dict]) -> dict[str, dict]:
    """The cards with their menu lines (bundling) replaced by the lines of those cards x the portions; a bundle is
    marked. A card missing or a loop (refused when saved) is left out."""
    def flat(code: str, seen: frozenset) -> list[dict]:
        out = []
        for ln in cards[code].get("lines") or []:
            if ln.get("type") != "menu":
                out.append(ln)
            elif ln["code"] in cards and ln["code"] not in seen:
                out.extend({**sub, "qty": sub["qty"] * ln["qty"]} for sub in flat(ln["code"], seen | {ln["code"]}))
        return out
    return {code: {**c, "lines": flat(code, frozenset({code})), "bundle": any(ln.get("type") == "menu" for ln in c.get("lines") or [])}
            for code, c in cards.items()}


def _channel(template: str) -> str:
    t = template.strip().upper()
    return "dine-in" if t.startswith("DI") else "online" if t.startswith(("OL", "ONLINE")) else "other"


CHANNEL_ORDER = {"dine-in": 0, "online": 1, "other": 2}


def menu_prices(ids: list[str]) -> dict[str, list[dict]]:
    """{"<company>:<menu id>": [{channel, price, min, max}]} the list prices of the menus from the POS menu master: the
    active price templates per channel (dine-in DI..., online OL..., other), the most used price of a channel with its
    range; a price under Rp 100 (a placeholder template) is left out. No template: the price of the menu."""
    if not ids:
        return {}
    try:
        rows = db.fetch(
            f"""SELECT company_code || ':' || menu_id AS id, price, raw_data->'menuTemplates' AS templates
                  FROM {SCHEMA}.master_pos_menu WHERE company_code || ':' || menu_id = ANY(%s)""", (sorted(set(ids)),))
    except Exception:  # noqa: BLE001 - the menu master unreachable: the cards without prices
        return {}
    out: dict[str, list[dict]] = {}
    for r in rows:
        per: dict[str, list[float]] = {}
        for t in r["templates"] or []:
            test = "TEST" in str(t.get("menuTemplateName") or "").upper()  # ONLINE-TESTING …: no selling price
            price = _f(t.get("price")) if str(t.get("flagActive")) == "1" and not test else 0.0
            if price >= 100:
                per.setdefault(_channel(str(t.get("menuTemplateName") or "")), []).append(price)
        if not per and r["price"] is not None and _f(r["price"]) >= 100:
            per["other"] = [_f(r["price"])]
        out[r["id"]] = [{"channel": ch, "price": max(set(ps), key=lambda x: (ps.count(x), x)), "min": min(ps), "max": max(ps)}
                        for ch, ps in sorted(per.items(), key=lambda kv: CHANNEL_ORDER[kv[0]])]
    return out


def package_contents(menu_id: str) -> dict:
    """What ESB records inside a menu ("<company>:<menu id>"): its package groups with their quantity and the menus to
    choose from, each with the card it uses; a group of one menu is a fixed content of the bundle."""
    company, _, mid = menu_id.partition(":")
    row = db.fetchrow(f"SELECT menu_name, raw_data FROM {SCHEMA}.master_pos_menu WHERE company_code = %s AND menu_id = %s",
                      (company, mid))
    if not row:
        raise CostingError("Menu tidak ada di master menu ESB")
    groups = ((row["raw_data"] or {}).get("menuPackages") or {}).get("menuGroup") or []
    ids = [f"{company}:{m.get('menuID')}" for g in groups for m in g.get("menus") or []]
    cards = {r["menu_id"]: r["recipe_code"] for r in db.fetch(
        f"SELECT menu_id, recipe_code FROM {P}.cc_menu_recipe_map WHERE menu_id = ANY(%s)", (ids,))} if ids else {}
    out = []
    for g in groups:
        menus_ = [{"id": f"{company}:{m.get('menuID')}", "name": m.get("menuName") or str(m.get("menuID")),
                   "card": cards.get(f"{company}:{m.get('menuID')}")}
                  for m in g.get("menus") or [] if str(m.get("flagActive", 1)) == "1"]
        if menus_:
            out.append({"name": g.get("menuGroupName") or "", "min": _f(g.get("minQty")), "max": _f(g.get("maxQty")), "menus": menus_})
    return {"id": menu_id, "name": row["menu_name"], "groups": out}


def delete_recipe(code: str) -> bool:
    with db.transaction() as conn:
        return conn.execute(f"DELETE FROM {P}.cc_menu_recipe WHERE code = %s", (code,)).rowcount > 0


# the ESB company of every branch: an ESB menu id is unique within a company only (Kopi Calf menu 1 = Es Kopi Calf
# Premium, Wasgee Tea has a menu 1 too), so a menu is "<company>:<menu id>" (migration 058)
BRANCH_COMPANY = f"""SELECT DISTINCT ON (branch_code) branch_code, coalesce(company_code, '{MAIN_COMPANY}') AS company
                       FROM {SCHEMA}.master_branches WHERE coalesce(branch_code, '') <> ''
                      ORDER BY branch_code, coalesce(is_deleted, false)"""


def menus(days: int = 90) -> list[dict]:
    """Every menu of the POS transactions (menus, packages and extras, from the monthly rollup of every month, as the
    sales pages), with its brand (ESB company of the branches that sold it), the quantity of the last `days` days, the
    last day it sold and the card assigned to it. Most sold first, then by name."""
    rows = _cache.get(f"menus:{days}:{date.today()}")
    if rows is None:
        rows = _menus_sold(days)
        _cache.set(f"menus:{days}:{date.today()}", rows, MENUS_S)
    mapped = {r["menu_id"]: r["recipe_code"] for r in db.fetch(f"SELECT menu_id, recipe_code FROM {P}.cc_menu_recipe_map")}
    out = [{"id": r["menu_id"], "name": r["name"] or r["menu_id"], "category": r["category"] or "",
            "detail": r["detail"] or "", "kinds": sorted((r["kinds"] or "menu").split(",")),
            "companies": [{"code": c, "name": COMPANY_NAMES.get(c, c)} for c in sorted((r["companies"] or MAIN_COMPANY).split(","))],
            "qty": _f(r["qty"]), "lastSold": (r["last_day"] or r["last_month"]).isoformat() if (r["last_day"] or r["last_month"]) else None,
            "recipe": mapped.get(r["menu_id"])} for r in rows]
    out.sort(key=lambda m: (-m["qty"], m["name"].lower()))
    return out


def _menus_sold(days: int) -> list[dict]:
    """The menus of the POS transactions with their brand, quantity of the last `days` days and last day sold."""
    since = date.today() - timedelta(days=days)
    rows = db.fetch(
        f"""WITH br AS ({BRANCH_COMPANY}),
                 every AS (SELECT coalesce(br.company, %(main)s) || ':' || a.menu_id AS menu_id, max(a.menu_name) AS name,
                                  max(a.category) AS category, max(a.category_detail) AS detail,
                                  string_agg(DISTINCT a.kind, ',') AS kinds, max(coalesce(br.company, %(main)s)) AS companies,
                                  max(a.month) AS last_month
                             FROM {P}.agg_menu_monthly a LEFT JOIN br ON br.branch_code = a.branch_code
                            WHERE a.menu_id <> '?' GROUP BY 1),
                 recent AS (SELECT coalesce(br.company, %(main)s) || ':' || a.menu_id AS menu_id, sum(a.qty) AS qty,
                                   max(a.sales_date) AS last_day
                              FROM {P}.agg_menu_daily a LEFT JOIN br ON br.branch_code = a.branch_code
                             WHERE a.sales_date >= %(since)s AND a.menu_id <> '?' GROUP BY 1)
            SELECT coalesce(e.menu_id, r.menu_id) AS menu_id, e.name, e.category, e.detail, e.kinds, e.companies, e.last_month,
                   r.qty, r.last_day
              FROM every e FULL JOIN recent r ON r.menu_id = e.menu_id""",
        {"since": since, "main": MAIN_COMPANY})
    missing = [r["menu_id"] for r in rows if r["name"] is None]
    if missing:  # sold this month, not rolled up yet: the names from the daily rows
        names = {r["menu_id"]: r for r in db.fetch(
            f"""WITH br AS ({BRANCH_COMPANY})
                SELECT coalesce(br.company, %s) || ':' || a.menu_id AS menu_id, max(a.menu_name) AS name, max(a.category) AS category,
                       max(a.category_detail) AS detail, string_agg(DISTINCT a.kind, ',') AS kinds,
                       max(coalesce(br.company, %s)) AS companies
                  FROM {P}.agg_menu_daily a LEFT JOIN br ON br.branch_code = a.branch_code
                 WHERE coalesce(br.company, %s) || ':' || a.menu_id = ANY(%s) GROUP BY 1""",
            (MAIN_COMPANY, MAIN_COMPANY, MAIN_COMPANY, missing))}
        for r in rows:
            if r["name"] is None and r["menu_id"] in names:
                r.update({k: names[r["menu_id"]][k] for k in ("name", "category", "detail", "kinds", "companies")})
    return rows


def explode(card: dict, portions: float, wip: dict[str, dict], out: dict[str, dict]) -> None:
    """Raw materials of `portions` portions of a card: item lines in their unit, WIP lines (grams) per ingredient."""
    for ln in card.get("lines") or []:
        if ln["type"] == "other":
            row = out.setdefault(f"o:{ln['name'].lower()}", {"code": None, "name": ln["name"], "unit": ln.get("unit") or "",
                                                             "price": ln.get("price")})
            row["qty"] = row.get("qty", 0.0) + ln["qty"] * portions
            continue
        if ln["type"] == "item":
            row = out.setdefault(ln["code"], {"code": ln["code"], "name": ln["name"], "unit": ln.get("unit") or ""})
            row["qty"] = row.get("qty", 0.0) + ln["qty"] * portions
            continue
        w = wip.get(ln["code"])
        if not w:
            row = out.setdefault(f"wip:{ln['code']}", {"code": None, "name": f"{ln['name']} (WIP tanpa resep aktif)", "unit": "GR"})
            row["qty"] = row.get("qty", 0.0) + ln["qty"] * portions
            continue
        for c in w["components"]:
            key = c["code"] or f"n:{c['name']}"
            row = out.setdefault(key, {"code": c["code"], "name": c["name"], "unit": c["unit"], "isStock": c["isStock"]})
            row["qty"] = row.get("qty", 0.0) + ln["qty"] * portions * c["perGram"]


BASES = ("bill", "portion", "day")


def extras() -> list[dict]:
    return [{"id": r["id"], "code": r["item_code"], "name": r["name"], "unit": r["unit"],
             "price": _f(r["price"]) if r["price"] is not None else None, "basis": r["basis"], "qty": _f(r["qty"]),
             "note": r["note"], "isActive": r["is_active"]}
            for r in db.fetch(f"SELECT * FROM {P}.cc_extra_item ORDER BY is_active DESC, name")]


def save_extra(extra_id: Optional[int], data: dict, user: dict) -> dict:
    code = (data.get("code") or "").strip() or None
    name = " ".join(str(data.get("name") or "").split())
    unit = str(data.get("unit") or "").strip()
    if data.get("basis") not in BASES:
        raise CostingError("Dasar hitung: per bill, per porsi atau per hari")
    if _f(data.get("qty")) <= 0:
        raise CostingError("Qty harus lebih dari 0")
    if code:
        it = db.fetchrow("SELECT name, base_uom FROM kc_master.item WHERE code = %s", (code,))
        if not it:
            raise CostingError(f"Item tidak dikenal: {code}")
        name, unit = name or it["name"], it["base_uom"]
    if not name or not unit:
        raise CostingError("Nama dan satuan item wajib diisi")
    price = data.get("price")
    row = (code, name[:160], unit[:20], None if code or price in (None, "") else _f(price), data["basis"], _f(data["qty"]),
           (data.get("note") or "").strip() or None, bool(data.get("isActive", True)), user.get("full_name") or user["username"])
    with db.transaction() as conn:
        if extra_id is None:
            new = conn.execute(f"""INSERT INTO {P}.cc_extra_item (item_code, name, unit, price, basis, qty, note, is_active, updated_by)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", row).fetchone()
            extra_id = new["id"]
        elif conn.execute(f"""UPDATE {P}.cc_extra_item SET item_code = %s, name = %s, unit = %s, price = %s, basis = %s, qty = %s,
                              note = %s, is_active = %s, updated_by = %s, updated_at = now() WHERE id = %s""",
                          (*row, extra_id)).rowcount == 0:
            raise CostingError("Item tambahan tidak ditemukan")
    return next(e for e in extras() if e["id"] == extra_id)


def delete_extra(extra_id: int) -> bool:
    with db.transaction() as conn:
        return conn.execute(f"DELETE FROM {P}.cc_extra_item WHERE id = %s", (extra_id,)).rowcount > 0


def non_sales(d_from: date, d_to: date, branches: Optional[list[str]]) -> list[dict]:
    """[{reason, menu_id, name, qty, bills}] the menus of the other-cost bills of ESB (finished, no bill number) by their
    payment method (CUPPING, WASTE, COMPLIMENT …), straight from the transactions (the rollups keep the sales only)."""
    from app.aggregates import PAY_METHOD
    from app.esb_report import TYPE_CONDITIONS, active_line_sql
    from app.database import TABLE_TRANSACTIONS
    return db.fetch(
        f"""SELECT {PAY_METHOD} AS reason, coalesce(h.company_code, %(main)s) || ':' || (m->>'menuID') AS menu_id,
                   max(m->>'menuName') AS name, sum(coalesce(nullif(m->>'qty', '')::numeric, 0)) AS qty,
                   count(DISTINCT h.sales_num)::int AS bills
              FROM {TABLE_TRANSACTIONS} h, jsonb_array_elements(coalesce(h.raw_data->'salesMenus', '[]'::jsonb)) m
             WHERE h.sales_date >= %(a)s::date AND h.sales_date < %(b)s::date + 1 AND {TYPE_CONDITIONS['other_cost']}
               AND {active_line_sql('m')} AND coalesce(m->>'menuID', '') <> ''
               AND (%(br)s::text[] IS NULL OR h.branch_code = ANY(%(br)s))
             GROUP BY 1, 2""", {"a": d_from, "b": d_to, "br": branches, "main": MAIN_COMPANY})


def non_sales_by_branch(d_from: date, d_to: date, branches: list[str]) -> list[dict]:
    """[{branch_code, sales_date, menu_id, qty}] the menus of the other-cost bills per branch and day (running stock)."""
    from app.esb_report import TYPE_CONDITIONS, active_line_sql
    from app.database import TABLE_TRANSACTIONS
    return db.fetch(
        f"""SELECT h.branch_code, (h.sales_date AT TIME ZONE 'UTC')::date AS sales_date,  -- the business day, as the rollups
                   coalesce(h.company_code, %(main)s) || ':' || (m->>'menuID') AS menu_id,
                   sum(coalesce(nullif(m->>'qty', '')::numeric, 0)) AS qty
              FROM {TABLE_TRANSACTIONS} h, jsonb_array_elements(coalesce(h.raw_data->'salesMenus', '[]'::jsonb)) m
             WHERE h.sales_date >= %(a)s::date AND h.sales_date < %(b)s::date + 1 AND {TYPE_CONDITIONS['other_cost']}
               AND {active_line_sql('m')} AND coalesce(m->>'menuID', '') <> '' AND h.branch_code = ANY(%(br)s)
             GROUP BY 1, 2, 3""", {"a": d_from, "b": d_to, "br": branches, "main": MAIN_COMPANY})


def _sold(d_from: date, d_to: date, branches: Optional[list[str]], cards: dict, mapped: dict, wip: dict, horizon: int,
          buffer_pct: float) -> dict:
    """The menus sold by the branches exploded through the cards (system usage, the plan) with the additional items."""
    branches = test_branches.real(branches)  # a test branch sells what its source branch sells
    days = (d_to - d_from).days + 1
    sold = db.fetch(
        f"""WITH br AS ({BRANCH_COMPANY})
            SELECT coalesce(br.company, %(main)s) || ':' || a.menu_id AS menu_id, max(a.menu_name) AS name,
                   max(a.category) AS category, sum(a.qty) AS qty,
                   sum(a.subtotal - a.discount) AS sales
              FROM {P}.agg_menu_daily a LEFT JOIN br ON br.branch_code = a.branch_code
             WHERE a.sales_date BETWEEN %(a)s AND %(b)s AND a.menu_id <> '?'
               AND (%(br)s::text[] IS NULL OR a.branch_code = ANY(%(br)s)) GROUP BY 1 ORDER BY 4 DESC""",
        {"a": d_from, "b": d_to, "br": branches, "main": MAIN_COMPANY})
    system: dict[str, dict] = {}
    plan: dict[str, dict] = {}
    per_menu, unmapped = [], []
    from app import menu_options
    try:
        pending = menu_options.ensure_days(d_from, d_to)
        choice_rows, rule = menu_options.sold(d_from, d_to, branches), menu_options.rules()
    except Exception:  # noqa: BLE001 - no choices data yet: the cards alone
        pending, choice_rows, rule = [], [], {}
    # a bundle with a card holds its menus: what ESB records inside it as packages is no menu sold on its own (2026-11-06)
    bundles = {m for m, code in mapped.items() if cards[code].get("bundle")}
    inside: dict[str, float] = {}
    for c in choice_rows:
        if c["parent"] in bundles:
            inside[c["option"]] = inside.get(c["option"], 0.0) + c["qty"]
    choice_rows = [c for c in choice_rows if c["parent"] not in bundles]
    counted_inside = {c["parent"] for c in choice_rows if c["option"] in mapped}
    # a choice (Less Sugar, Gelas Kaca …) is no menu without a card: its rule changes the card of its menu (2026-11-05)
    choice_ids = {c["option"] for c in choice_rows}
    choice_sales = 0.0
    for s in sold:
        qty = _f(s["qty"]) - inside.get(s["menu_id"], 0.0)
        if s["menu_id"] in inside and qty <= 1e-9:  # sold only inside bundles with a card
            choice_sales += _f(s["sales"])
            continue
        code = mapped.get(s["menu_id"])
        if not code and s["menu_id"] in choice_ids:
            choice_sales += _f(s["sales"])
            continue
        if not code:  # a bundle whose contents ESB records as packages with a card is counted through them
            unmapped.append({"id": s["menu_id"], "name": s["name"], "category": s["category"], "qty": qty, "sales": _f(s["sales"]),
                             "viaPackages": s["menu_id"] in counted_inside})
            continue
        explode(cards[code], qty, wip, system)
        explode(cards[code], qty / days * horizon * (1 + buffer_pct / 100), wip, plan)
        per_menu.append({"id": s["menu_id"], "name": s["name"], "recipe": code, "recipeName": cards[code]["name"],
                         "qty": qty, "sales": _f(s["sales"])})
    # the choices sold with the menus (Less / No Sugar, Extra Shot …): their rules change the card of the parent menu
    option_of, option_rows, unruled = menu_options.adjustments(choice_rows, cards, mapped, wip, rule, explode)
    for deltas in option_of.values():
        for k, q in deltas.items():
            for target, amount in ((system, q), (plan, q / days * horizon * (1 + buffer_pct / 100))):
                row = target.setdefault(k, {**option_rows[k], "qty": 0.0})
                row["qty"] = row.get("qty", 0.0) + amount
    # the menus made without a sale (ESB other-cost bills: cupping, waste, compliment, QC …): their materials are used too
    reasons: dict[str, dict] = {}
    try:
        given = non_sales(d_from, d_to, branches)
    except Exception:  # noqa: BLE001 - the transactions unreachable: the sales alone
        given = []
    for g in given:
        r = reasons.setdefault(g["reason"], {"reason": g["reason"], "bills": 0, "portions": 0.0, "unmapped": 0.0, "menus": {}})
        qty = _f(g["qty"])
        r["bills"] += g["bills"]
        r["portions"] += qty
        code = mapped.get(g["menu_id"])
        if not code:
            r["unmapped"] += qty
            continue
        explode(cards[code], qty, wip, system)
        explode(cards[code], qty / days * horizon * (1 + buffer_pct / 100), wip, plan)
        r["menus"][code] = r["menus"].get(code, 0.0) + qty
    # items outside the cards: per bill, per portion sold (menus with a card) or per day of an outlet with sales
    base = db.fetchrow(
        f"""SELECT coalesce(sum(bills) FILTER (WHERE tx_type = 'sales'), 0) AS bills,
                   count(DISTINCT (branch_code, sales_date)) FILTER (WHERE tx_type = 'sales' AND subtotal > 0) AS days
              FROM {P}.agg_sales_daily WHERE sales_date BETWEEN %(a)s AND %(b)s
               AND (%(br)s::text[] IS NULL OR branch_code = ANY(%(br)s))""", {"a": d_from, "b": d_to, "br": branches}) or {}
    counts = {"bill": _f(base.get("bills")), "portion": sum(m["qty"] for m in per_menu), "day": _f(base.get("days"))}
    extra_rows = []
    for e in (x for x in extras() if x["isActive"]):
        used = e["qty"] * counts[e["basis"]]
        key = e["code"] or f"o:{e['name'].lower()}"
        for target, amount in ((system, used), (plan, used / days * horizon * (1 + buffer_pct / 100))):
            row = target.setdefault(key, {"code": e["code"], "name": e["name"], "unit": e["unit"], "price": e["price"], "extra": True})
            row["qty"] = row.get("qty", 0.0) + amount
        extra_rows.append({**e, "count": counts[e["basis"]], "used": round(used, 3)})
    return {"nonSales": list(reasons.values()), "system": system, "plan": plan, "menus": per_menu, "unmapped": unmapped, "extras": extra_rows, "counts": counts,
            "options": option_of, "unruled": unruled, "optionsPending": len(pending),
            "allSales": sum(m["sales"] for m in per_menu) + sum(m["sales"] for m in unmapped) + choice_sales,
            "choiceSales": choice_sales}


def _delta_cost(key: str, qty: float, price: dict[str, float]) -> Optional[float]:
    """The cost of what a choice changes in one material (None while its price is missing; unpriced items count 0)."""
    if key.startswith(("wip:", "n:", "o:")):
        return 0.0
    p = price.get(key)
    return None if p is None else qty * p


def report(d_from: date, d_to: date, branches: Optional[list[str]], horizon: int = 7, buffer_pct: float = 10) -> dict:
    """The calculator of a period (see _report), kept REPORT_S seconds per filter while nothing of the setup changed."""
    key = f"report:{d_from}:{d_to}:{','.join(sorted(branches)) if branches is not None else '*'}:{horizon}:{buffer_pct}:{stamp()}"
    hit = _cache.get(key)
    if hit is None:
        hit = _report(d_from, d_to, branches, horizon, buffer_pct)
        _cache.set(key, hit, REPORT_S)
    return hit


def _report(d_from: date, d_to: date, branches: Optional[list[str]], horizon: int = 7, buffer_pct: float = 10) -> dict:
    days = (d_to - d_from).days + 1
    cards = {k: c for k, c in resolve({r["code"]: r for r in recipes()}).items() if c["isActive"]}  # a bundle may hold an inactive card
    mapped = {m["id"]: code for code, c in cards.items() for m in c["menus"]}
    wip = wips()
    got = _sold(d_from, d_to, branches, cards, mapped, wip, horizon, buffer_pct)
    system, plan, per_menu, extra_rows = got["system"], got["plan"], got["menus"], got["extras"]
    codes = [k for k in system if not k.startswith(("wip:", "n:", "o:"))]
    price = prices_on(d_to, codes or None)
    # an item without a Pricelist price valid by then takes the ESB HPP (as the waste value, 2026-11-06): a price set
    # later in the Pricelist must not drop the menus using it out of the COGS
    hpp_esb = esb_hpp(d_to) if any(c not in price for c in codes) else {}
    fallback = sorted(c for c in codes if c not in price and c in hpp_esb)
    price.update({c: hpp_esb[c][0] for c in fallback})
    # HPP of a portion of every card used
    hpp: dict[str, Optional[float]] = {}
    def cost(v: dict) -> Optional[float]:
        if v.get("code"):
            return price.get(v["code"])
        return v.get("price") if v.get("price") is not None else (0.0 if not v.get("isStock", True) else None)
    for code in {m["recipe"] for m in per_menu}:
        one: dict[str, dict] = {}
        explode(cards[code], 1, wip, one)
        missing = [v for v in one.values() if (v.get("code") or "price" in v) and cost(v) is None]
        hpp[code] = None if missing else sum(v["qty"] * (cost(v) or 0.0) for v in one.values())
    for code in {c for r in got["nonSales"] for c in r["menus"]} - set(hpp):  # cards made only without a sale
        one = {}
        explode(cards[code], 1, wip, one)
        hpp[code] = None if any((v.get("code") or "price" in v) and cost(v) is None for v in one.values())             else sum(v["qty"] * (cost(v) or 0.0) for v in one.values())
    non_sales = [{"reason": r["reason"], "bills": r["bills"], "portions": round(r["portions"], 3), "unmapped": round(r["unmapped"], 3),
                  "cost": round(sum(q * (hpp.get(c) or 0.0) for c, q in r["menus"].items()), 2),
                  "unpriced": sum(1 for c in r["menus"] if hpp.get(c) is None)}
                 for r in sorted(got["nonSales"], key=lambda x: -x["portions"])]
    for m in per_menu:
        unit = hpp.get(m["recipe"])
        # what the choices sold with the menu change (Less Sugar saves sugar, Extra Shot adds espresso …)
        deltas = got["options"].get(m["id"], {})
        extra = [_delta_cost(k, q, price) for k, q in deltas.items()]
        m["optionCogs"] = round(sum(x for x in extra if x is not None), 2) if deltas else 0.0
        m["hpp"] = round(unit, 2) if unit is not None else None
        m["cogs"] = round(unit * m["qty"] + m["optionCogs"], 2) if unit is not None and None not in extra else None
        m["ratio"] = round(m["cogs"] / m["sales"], 6) if m["cogs"] is not None and m["sales"] else None

    def unit_price(v: dict) -> Optional[float]:
        return price.get(v["code"]) if v["code"] else v.get("price")

    # usage actual vs system: both over the branches with SO awal and SO akhir (portal counts, else ESB documents)
    units = {v["code"]: str(v["unit"] or "").upper() for v in system.values() if v.get("code")}
    actual = actual_usage(d_from, d_to, branches, units)
    covered = actual["covered"]
    same = covered and (branches is not None and sorted(branches) == covered)
    scope = got if same or not covered else _sold(d_from, d_to, covered, cards, mapped, wip, horizon, buffer_pct)
    usage = []
    for k, v in sorted(scope["system"].items(), key=lambda kv: kv[1]["name"]):
        a = actual["items"].get(v["code"] or "") if covered else None
        p = unit_price(v)
        usage.append({"code": v["code"], "name": v["name"], "unit": v["unit"], "isStock": v.get("isStock", True), "extra": v.get("extra", False),
                      "system": round(v["qty"], 3),
                      "soStart": round(a["start"], 3) if a else None, "purchased": round(a["bought"], 3) if a else None,
                      "soEnd": round(a["end"], 3) if a else None, "actual": round(a["actual"], 3) if a else None,
                      "purchasedOther": [{"uom": k, "qty": round(q, 3)} for k, q in sorted(a["skipped"].items())] if a else [],
                      "moves": {k: round(q, 3) for k, q in a["moves"].items()} if a else {},
                      "variance": round(a["actual"] - v["qty"], 3) if a else None,
                      "price": p, "systemValue": round(v["qty"] * p, 2) if p is not None else None,
                      "actualValue": round(a["actual"] * p, 2) if a and p is not None else None})
    anomalies = count_anomalies(actual.get("perBranch", []), {u["code"]: u for u in usage if u["code"]}) if covered else []
    # SO coverage (2026-11-06): the stock items the cards use that the counts of both ends hold; an item missing from the
    # SO lists has no actual usage, so its system value stays out of the actual COGS
    stock_rows = [u for u in usage if u["code"] and u["isStock"] and not u["extra"] and u["systemValue"]]
    counted = [u for u in stock_rows if u["actual"] is not None]
    coverage = {"items": len(stock_rows), "counted": len(counted),
                "value": round(sum(u["systemValue"] for u in stock_rows), 2),
                "countedValue": round(sum(u["systemValue"] for u in counted), 2),
                "missing": [{"code": u["code"], "name": u["name"], "systemValue": u["systemValue"]}
                            for u in sorted(stock_rows, key=lambda x: -x["systemValue"]) if u["actual"] is None][:30]} if covered else None
    # waste recorded (not rejected) by the branches with both counts: the part of actual − system it explains (2026-11-06)
    waste_value = None
    if covered:
        try:
            from app import waste
            w = waste.with_values(db.fetch(f"""SELECT {waste.LIGHT} FROM {waste.T} WHERE business_date BETWEEN %s AND %s
                                                 AND branch_code = ANY(%s) AND status <> 'rejected'""", (d_from, d_to, covered)))
            waste_value = round(sum(v["value"] or 0 for v in w), 2)
        except Exception:  # noqa: BLE001 - no waste data: the difference stays unexplained
            waste_value = None
    # material required planning: the plan less the latest stock of the branches
    plan_units = {v["code"]: str(v["unit"] or "").upper() for v in plan.values() if v.get("code")}
    try:
        have = on_hand(branches, plan_units)
    except Exception:  # noqa: BLE001 - no stock data: the gross requirement only
        have = {"items": {}, "branches": 0, "from": None, "to": None}
    mrp = []
    for v in sorted(plan.values(), key=lambda x: x["name"]):
        need = math.ceil(round(v["qty"], 6))
        stock = have["items"].get(v["code"] or "") if v.get("code") else None
        order = max(0, math.ceil(round(need - stock, 6))) if stock is not None else need
        p = unit_price(v)
        mrp.append({"code": v["code"], "name": v["name"], "unit": v["unit"], "isStock": v.get("isStock", True), "extra": v.get("extra", False),
                    "qty": need, "onHand": round(stock, 3) if stock is not None else None, "toOrder": order,
                    "value": round(need * p, 2) if p is not None else None, "orderValue": round(order * p, 2) if p is not None else None})
    extra_cost = sum((e["used"] * (unit_price({"code": e["code"], "price": e["price"]}) or 0.0)) for e in extra_rows)
    non_sales_cost = sum(r["cost"] for r in non_sales)  # made and given away: a cost without a sale
    sales = sum(m["sales"] for m in per_menu)
    cogs = sum(m["cogs"] or 0 for m in per_menu)
    actual_cogs = sum(u["actualValue"] or 0 for u in usage) if covered else None
    return {"period": {"from": d_from.isoformat(), "to": d_to.isoformat(), "days": days, "horizon": horizon, "buffer": buffer_pct},
            "menus": per_menu, "unmapped": got["unmapped"], "usage": usage, "nonSales": non_sales,
            "options": {"unruled": got["unruled"], "pendingDays": got["optionsPending"]}, "mrp": mrp, "extras": extra_rows, "counts": got["counts"],
            "coverage": coverage, "anomalies": anomalies,
            "stock": {"branches": actual["branches"], "covered": covered, "onHand": {**{k: have[k] for k in ("branches", "from", "to")}, "running": bool(have.get("running"))}},
            "total": {"sales": sales, "cogs": cogs + extra_cost + non_sales_cost, "menuCogs": cogs, "extraCogs": round(extra_cost, 2),
                      "ratio": round((cogs + extra_cost + non_sales_cost) / sales, 6) if sales else None,
                      "menusMapped": len(per_menu), "menusUnmapped": sum(1 for u in got["unmapped"] if not u["viaPackages"]),
                      "menusViaPackages": sum(1 for u in got["unmapped"] if u["viaPackages"]),
                      "allSales": round(got["allSales"], 2), "choiceSales": round(got["choiceSales"], 2),
                      "unpriced": sum(1 for m in per_menu if m["hpp"] is None),
                      "nonSalesCost": round(non_sales_cost, 2),
                      "priceFallback": len(fallback), "priceMissing": sum(1 for c in codes if c not in price),
                      "systemValue": sum(u["systemValue"] or 0 for u in usage),
                      "actualValue": actual_cogs or 0,
                      "actualCogs": round(actual_cogs, 2) if actual_cogs is not None else None,
                      "actualSales": scope["allSales"] if covered else None,
                      "actualRatio": round(actual_cogs / scope["allSales"], 6) if covered and scope["allSales"] else None,
                      "branchesCovered": len(covered),
                      "wasteValue": waste_value,
                      "unexplained": round(actual_cogs - sum(u["systemValue"] or 0 for u in usage if u["actual"] is not None) - (waste_value or 0), 2)
                      if covered and actual_cogs is not None else None}}


# ---------------------------------------------------------------- stock: SO awal / SO akhir (2026-11-04)
# The stock of a branch on a day comes from its stock opname counted in the portal (kopicalf core kc_inventory, the
# current line of every item = after a Cost Control correction, qty_base + qty_wip in the item base unit), counted
# before the outlet opens: a count dated D is the stock at the start of D. Before the portal counts (the Weekly SO of
# 28 Oct 2026) or for a branch without one, the ESB stock opname document closest to the day (as Outlet ratio): its
# quantity at the end of its day.

PORTAL_SO_FROM = date(2026, 10, 28)
LIVE_COUNTS = ("SUBMITTED", "RECOUNT_REQUESTED", "APPROVED", "EXPORTED")
UNIT_FACTOR = {("KG", "GR"): 1000, ("KG", "G"): 1000, ("L", "ML"): 1000, ("LTR", "ML"): 1000, ("LITER", "ML"): 1000}


def to_unit(qty: float, have: Optional[str], want: Optional[str]) -> Optional[float]:
    """qty in `have` converted to `want` (same unit, KG ↔ GR, L ↔ ML); None when they cannot be compared."""
    have, want = (have or "").upper(), (want or "").upper()
    if not want or not have or have == want:
        return qty
    if (have, want) in UNIT_FACTOR:
        return qty * UNIT_FACTOR[(have, want)]
    if (want, have) in UNIT_FACTOR:
        return qty / UNIT_FACTOR[(want, have)]
    return None


def portal_counts(day: date, branches: Optional[list[str]], back: int = 1, ahead: int = 1) -> dict[str, dict]:
    """{branch: {source, date, refs, items: {code: (qty, unit)}, esb: {code: (unit, factor)}}}: the portal counts of the
    branch on the business day closest to `day` (within -back..+ahead days, every count of that day together)."""
    lo, hi = max(day - timedelta(days=back), PORTAL_SO_FROM), day + timedelta(days=ahead)
    if hi < lo:
        return {}
    rows = db.fetch(
        f"""SELECT coalesce(b.branch_code, fa.code) AS branch_code, c.business_date, c.number, it.code, upper(coalesce(l.base_uom, '')) AS unit,
                   upper(coalesce(l.esb_uom, '')) AS esb_uom, coalesce(l.esb_factor, 1) AS esb_factor,
                   sum(coalesce(l.qty_base, 0) + coalesce(l.qty_wip, 0)) AS qty
              FROM kc_inventory.stock_count c
              JOIN kc_master.facility fa ON fa.id = c.facility_id
              LEFT JOIN kc_master.facility_esb_map m ON m.facility_id = c.facility_id
              LEFT JOIN {SCHEMA}.master_branches b ON b.company_code = m.company_code AND b.branch_id::text = m.esb_id
              JOIN kc_inventory.stock_count_line l ON l.count_id = c.id AND l.is_current
                   AND (l.qty_base IS NOT NULL OR coalesce(l.qty_wip, 0) > 0)
              JOIN kc_master.item it ON it.id = l.item_id
             WHERE c.status = ANY(%(live)s) AND c.business_date BETWEEN %(lo)s AND %(hi)s
               AND (b.branch_code IS NOT NULL OR fa.code = ANY(%(test)s))
               AND (%(br)s::text[] IS NULL OR coalesce(b.branch_code, fa.code) = ANY(%(br)s))
             GROUP BY 1, 2, 3, 4, 5, 6, 7""",
        {"live": list(LIVE_COUNTS), "lo": lo, "hi": hi, "br": branches, "test": list(test_branches.sources())})
    if branches is None:  # every branch: the test branches stay out of the real figures (app/test_branches.py)
        rows = [r for r in rows if not test_branches.is_test(r["branch_code"])]
    best: dict[str, date] = {}
    for r in rows:
        d = r["business_date"]
        cur = best.get(r["branch_code"])
        if cur is None or abs((d - day).days) < abs((cur - day).days) or (abs((d - day).days) == abs((cur - day).days) and d < cur):
            best[r["branch_code"]] = d
    out: dict[str, dict] = {}
    for r in rows:
        if r["business_date"] != best[r["branch_code"]]:
            continue
        b = out.setdefault(r["branch_code"], {"source": "portal", "date": r["business_date"].isoformat(), "refs": set(), "items": {}, "esb": {}})
        b["refs"].add(r["number"])
        qty, _ = b["items"].get(r["code"], (0.0, r["unit"]))
        b["items"][r["code"]] = (qty + _f(r["qty"]), r["unit"])
        if r["esb_uom"]:
            b["esb"][r["code"]] = (r["esb_uom"], _f(r["esb_factor"]) or 1.0)
    for b in out.values():
        b["refs"] = sorted(b["refs"])
    return out


def esb_counts(day: date, branches: Optional[list[str]]) -> dict[str, dict]:
    """The same from the ESB stock opname document of every branch closest to `day` (app/ratio.py so_values)."""
    from app import ratio

    docs = ratio.so_values(day, branches)
    if not docs:
        return {}
    by_doc = {v["doc"]: b for b, v in docs.items()}
    out = {b: {"source": "esb", "date": v["date"], "refs": [v["doc"]], "items": {}, "esb": {}} for b, v in docs.items()}
    for r in db.fetch(
            f"""SELECT doc_num, product_code, upper(coalesce(uom_name, '')) AS uom, sum(qty) AS qty
                  FROM {SCHEMA}.erp_document_lines
                 WHERE module = 'stock_opname' AND line_type = 'detail' AND doc_num = ANY(%s) AND product_code IS NOT NULL
                 GROUP BY 1, 2, 3""", (list(by_doc),)):
        items = out[by_doc[r["doc_num"]]]["items"]
        qty, _ = items.get(r["product_code"], (0.0, r["uom"]))
        items[r["product_code"]] = (qty + _f(r["qty"]), r["uom"])
    return out


def stock_at(portal_day: date, esb_day: date, branches: Optional[list[str]], back: int = 1) -> dict[str, dict]:
    """The stock of every branch: its portal count of `portal_day` (start of the day), else its ESB document of
    `esb_day` (end of the day)."""
    try:
        esb = esb_counts(esb_day, branches)
    except Exception:  # noqa: BLE001 - the ESB documents unreachable: the portal counts alone
        esb = {}
    try:
        portal = portal_counts(portal_day, branches, back=back)
    except Exception:  # noqa: BLE001 - kopicalf core unreachable: the ESB documents alone
        portal = {}
    return {**esb, **portal}


def item_units(codes: list[str]) -> dict[str, tuple[str, dict[str, float]]]:
    """{item code: (base unit, {UOM: base units in one})} of kc_master (the packs of an item, e.g. PCS = 500 GR)."""
    out: dict[str, tuple[str, dict[str, float]]] = {}
    if not codes:
        return out
    for r in db.fetch("""SELECT i.code, upper(coalesce(i.base_uom, '')) AS base, upper(u.uom) AS uom, u.factor
                           FROM kc_master.item i JOIN kc_master.item_uom u ON u.item_id = i.id
                          WHERE i.code = ANY(%s) AND u.is_active""", (codes,)):
        out.setdefault(r["code"], (r["base"], {}))[1][r["uom"]] = _f(r["factor"])
    return out


def _bought(rows: dict[tuple[str, str], float], code: str, unit: str, esb: Optional[tuple[str, float]],
            packs: Optional[tuple[str, dict[str, float]]] = None, skipped: Optional[dict[str, float]] = None) -> float:
    """What a branch bought of an item, in `unit`: purchases in that unit, in a unit it converts to (KG → GR), in a pack
    of the item master (kc_master.item_uom, × its factor) or in the ESB unit of the portal count list (× its factor).
    A purchase in any other unit is left out and added to `skipped` ({UOM: qty})."""
    total = 0.0
    for (c, uom), qty in rows.items():
        if c != code:
            continue
        v = to_unit(qty, uom, unit)
        if v is None and packs and uom in packs[1]:
            v = to_unit(qty * packs[1][uom], packs[0], unit)
        if v is None and esb and uom == esb[0]:
            v = qty * esb[1]
        if v is None:
            if skipped is not None:
                skipped[uom] = skipped.get(uom, 0.0) + qty
            continue
        total += v
    return total


def actual_usage(d_from: date, d_to: date, branches: Optional[list[str]], units: dict[str, str]) -> dict:
    """SO awal + the moves in and out (goods received, outlet purchases, production, deliveries, returns;
    app/stock_moves.py) − SO akhir per item (in the unit of the recipe cards, `units`) over the branches that have
    both counts: SO awal = the stock at the start of d_from, SO akhir = the stock at the end of d_to (stock_at).
    {items: {code: {start, bought, end, actual, skipped: {UOM: qty}}}, branches: [{code, start, end}], covered: [codes]}."""
    try:
        start = stock_at(d_from, d_from - timedelta(days=1), branches)
        end = stock_at(d_to + timedelta(days=1), d_to, branches)
        covered = sorted(b for b in start if b in end)
        from app import stock_moves
        bought = stock_moves.moves(d_from, d_to, covered) if covered else {}
        try:
            packs = item_units(sorted(units))
        except Exception:  # noqa: BLE001 - no item master: purchases in the unit of the card only
            packs = {}
    except Exception:  # noqa: BLE001 - no stock data: the report shows the system usage only
        return {"items": {}, "branches": [], "covered": []}
    items: dict[str, dict] = {}
    per_branch: list[dict] = []  # one row per branch × item, for the anomalies of the counts
    for b in covered:
        a, z = start[b], end[b]
        for code, unit in units.items():
            if code not in a["items"] and code not in z["items"]:
                continue
            qa, ua = a["items"].get(code, (0.0, unit))
            qz, uz = z["items"].get(code, (0.0, unit))
            va, vz = to_unit(qa, ua, unit), to_unit(qz, uz, unit)
            if va is None or vz is None:
                continue  # counted in a unit that cannot be compared with the card
            row = items.setdefault(code, {"start": 0.0, "bought": 0.0, "end": 0.0, "skipped": {}, "moves": {}})
            esb = a["esb"].get(code) or z["esb"].get(code)
            here: dict[str, float] = {}
            for kind, rows in bought.get(b, {}).items():  # every move in or out (app/stock_moves.py), signed
                got = _bought(rows, code, unit, esb, packs.get(code), row["skipped"])
                if got:
                    here[kind] = got
                    row["moves"][kind] = row["moves"].get(kind, 0.0) + got
                    row["bought"] += got
            row["start"] += va
            row["end"] += vz
            moved = sum(here.values())
            per_branch.append({"branch": b, "code": code, "unit": unit, "start": va, "moves": moved,
                               "in": sum(q for q in here.values() if q > 0), "end": vz, "actual": va + moved - vz,
                               "refs": {"start": a["refs"], "end": z["refs"], "startSource": a["source"], "endSource": z["source"]}})
    for row in items.values():
        row["actual"] = row["start"] + row["bought"] - row["end"]

    def side(v: dict) -> dict:
        return {"source": v["source"], "date": v["date"], "refs": v["refs"]}
    every = sorted(set(start) | set(end))
    return {"items": items, "covered": covered, "perBranch": per_branch,
            "branches": [{"code": b, "start": side(start[b]) if b in start else None, "end": side(end[b]) if b in end else None} for b in every]}


ANOMALY_JUMP = 5        # a count this many times the other end and what came in
ANOMALY_MIN_RP = 50_000  # below this value impact an odd row is left out


def count_anomalies(rows: list[dict], usage: dict[str, dict], limit: int = 150) -> list[dict]:
    """The branch × item rows whose counts look wrong (2026-11-06), the largest value first, for Cost Control to check
    and correct the stock opname: negative usage (more at the end than at the start plus what came in), a count many
    times the other end (a unit or a digit typed wrong), a usage many times what the branch could hold."""
    from app.pic_checkin import _names
    out = []
    for r in rows:
        price = (usage.get(r["code"]) or {}).get("price")
        if not price:
            continue
        start, end, came, actual = r["start"], r["end"], r["in"], r["actual"]
        reasons = []
        if actual < -0.01 * max(start, end, 1):
            reasons.append("negative")  # the end count is more than the start plus what came in
        if start > ANOMALY_JUMP * (end + came) and start - end - came > 0:
            reasons.append("startHigh")  # SO awal far above SO akhir and what came in
        if end > ANOMALY_JUMP * (start + came) and end - start > 0:
            reasons.append("endHigh")
        if not reasons:
            continue
        impact = abs(actual) * price if "negative" in reasons else abs(start - end) * price
        if impact < ANOMALY_MIN_RP:
            continue
        out.append({**r, "name": usage[r["code"]]["name"], "price": price, "reasons": reasons, "impact": round(impact, 2)})
    out.sort(key=lambda x: -x["impact"])
    names = _names(sorted({x["branch"] for x in out[:limit]}))
    return [{**x, "branchName": names.get(x["branch"], x["branch"]), **{k: round(x[k], 3) for k in ("start", "moves", "in", "end", "actual")}}
            for x in out[:limit]]


def on_hand(branches: Optional[list[str]], units: dict[str, str]) -> dict:
    """The stock of the branches now, per item in the unit of the cards: what the Material Required Planning has already.
    The running stock (app/running_stock.py: the latest SO + every move − waste − usage since, 2026-11-07); a branch
    without a portal count: its ESB document around yesterday."""
    today = date.today()
    try:
        from app import running_stock
        run = running_stock.running(branches)
    except Exception:  # noqa: BLE001 - no running stock: the counts alone
        run = {}
    if run:
        items: dict[str, float] = {}
        for v in run.values():
            for code, unit in units.items():
                it = v["items"].get(code)
                q = to_unit(it["estimate"], it["unit"], unit) if it else None
                if q is not None:
                    items[code] = items.get(code, 0.0) + max(q, 0.0)  # an estimate below zero has nothing to give
        dates = sorted({v["so"]["date"] for v in run.values()})
        return {"items": items, "branches": len(run), "from": dates[0], "to": dates[-1], "running": True}
    snap = stock_at(today, today - timedelta(days=1), branches, back=8)
    items: dict[str, float] = {}
    for v in snap.values():
        for code, unit in units.items():
            if code in v["items"]:
                q, u = v["items"][code]
                q = to_unit(q, u, unit)
                if q is not None:
                    items[code] = items.get(code, 0.0) + q
    dates = sorted({v["date"] for v in snap.values()})
    return {"items": items, "branches": len(snap), "from": dates[0] if dates else None, "to": dates[-1] if dates else None}
