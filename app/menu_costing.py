"""Cost Control: Pricelist Items (bahan baku) and Sales Calculator by Menu (migration 056).

Pricelist   the price of every raw material (kc_master.item, STOCK) per base unit from a date (cc_item_price); the price
            of a day is the latest one valid by then. As a reference the average purchase price of the last 90 days
            (ESB goods received, agg_cost_item_period purchase_value / purchase_qty) is shown beside it.
Menu cards  a recipe card per menu as the production calculator: lines of raw materials (base unit) or WIP / premix
            recipes (kc_recipe active version, in grams, exploded into their ingredients per gram of batch); ESB POS
            menus are assigned to a card (cc_menu_recipe_map, by menu id), several menus may share one card.
Report      for a period and branches, from the POS sales per menu (agg_menu_daily):
            1. Material Required Planning: average daily sales × horizon days (+ buffer) exploded into raw materials;
            2. Usage actual vs system: system = sold × cards (exploded), actual = ESB actual usage of the items
               (agg_cost_item_period.actual_qty of the periods starting in the range);
            3. COGS bahan baku: system usage × pricelist; per menu the HPP of a portion, the cost of the sold portions
               and the ratio to the menu's sales.
"""
import math
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from psycopg.types.json import Jsonb

from app import database as db

P = "integration_portal"


class CostingError(ValueError):
    """Refused with a message for the user (HTTP 422)."""


def _f(v: Any) -> float:
    return float(v) if v is not None else 0.0


# ---------------------------------------------------------------- pricelist

def items(search: str = "", category: str = "", on: Optional[date] = None) -> dict:
    """STOCK items of kc_master with their category, base unit, the price valid on `on` (today) and the average
    purchase price of the last 90 days."""
    day = on or date.today()
    rows = db.fetch(
        f"""SELECT i.code, i.name, i.base_uom, c.name AS category,
                   pr.price, pr.valid_from, pr.note,
                   (SELECT sum(a.purchase_value) / nullif(sum(a.purchase_qty), 0) FROM {P}.agg_cost_item_period a
                     WHERE a.product_code = i.code AND a.period_start >= %(since)s AND a.purchase_qty > 0) AS purchase_avg
              FROM kc_master.item i LEFT JOIN kc_master.item_category c ON c.id = i.category_id
              LEFT JOIN LATERAL (SELECT price, valid_from, note FROM {P}.cc_item_price p
                                  WHERE p.item_code = i.code AND p.valid_from <= %(day)s
                                  ORDER BY valid_from DESC LIMIT 1) pr ON true
             WHERE i.is_active AND i.item_type = 'STOCK'
               AND (%(q)s = '' OR i.code ILIKE %(like)s OR i.name ILIKE %(like)s)
               AND (%(cat)s = '' OR c.name = %(cat)s)
             ORDER BY c.name NULLS LAST, i.name""",
        {"day": day, "since": day - timedelta(days=90), "q": search.strip(), "like": f"%{search.strip()}%", "cat": category})
    cats = [r["name"] for r in db.fetch(
        """SELECT DISTINCT c.name FROM kc_master.item i JOIN kc_master.item_category c ON c.id = i.category_id
            WHERE i.is_active AND i.item_type = 'STOCK' ORDER BY 1""")]
    return {"day": day.isoformat(), "categories": cats, "data": [{
        "code": r["code"], "name": r["name"], "unit": r["base_uom"], "category": r["category"],
        "price": _f(r["price"]) if r["price"] is not None else None,
        "validFrom": r["valid_from"].isoformat() if r["valid_from"] else None, "note": r["note"],
        "purchaseAvg": round(_f(r["purchase_avg"]), 4) if r["purchase_avg"] is not None else None} for r in rows]}


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


def prices_on(day: date, codes: Optional[list[str]] = None) -> dict[str, float]:
    return {r["item_code"]: _f(r["price"]) for r in db.fetch(
        f"""SELECT DISTINCT ON (item_code) item_code, price FROM {P}.cc_item_price
             WHERE valid_from <= %s AND (%s::text[] IS NULL OR item_code = ANY(%s))
             ORDER BY item_code, valid_from DESC""", (day, codes, codes))}


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


def recipes() -> list[dict]:
    maps: dict[str, list[dict]] = {}
    for m in db.fetch(f"SELECT menu_id, menu_name, recipe_code FROM {P}.cc_menu_recipe_map ORDER BY menu_name"):
        maps.setdefault(m["recipe_code"], []).append({"id": m["menu_id"], "name": m["menu_name"]})
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
        if kind not in ("item", "wip"):
            raise CostingError("Baris resep harus bahan baku atau WIP")
        qty = _f(ln.get("qty"))
        if qty <= 0:
            raise CostingError(f"Qty {ln.get('name') or code} harus lebih dari 0")
        out.append({"type": kind, "code": code, "name": str(ln.get("name") or code)[:160], "qty": qty,
                    "unit": "GR" if kind == "wip" else str(ln.get("unit") or "")[:20]})
    return out


def save_recipe(key: Optional[str], data: dict, user: dict) -> dict:
    code = str(data.get("code") or key or "").strip().upper()
    name = " ".join(str(data.get("name") or "").split())
    if not code or not name:
        raise CostingError("Kode dan nama resep wajib diisi")
    row = (code, name, (data.get("category") or "").strip() or None, Jsonb(_lines(data.get("lines"))),
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


def delete_recipe(code: str) -> bool:
    with db.transaction() as conn:
        return conn.execute(f"DELETE FROM {P}.cc_menu_recipe WHERE code = %s", (code,)).rowcount > 0


def menus(days: int = 90) -> list[dict]:
    """The ESB POS menus sold in the last `days` days with their quantity and the card assigned to them."""
    since = date.today() - timedelta(days=days)
    return [{"id": r["menu_id"], "name": r["name"], "category": r["category"], "qty": _f(r["qty"]), "recipe": r["recipe_code"]}
            for r in db.fetch(
        f"""SELECT a.menu_id, max(a.menu_name) AS name, max(a.category) AS category, sum(a.qty) AS qty, m.recipe_code
              FROM {P}.agg_menu_daily a LEFT JOIN {P}.cc_menu_recipe_map m ON m.menu_id = a.menu_id
             WHERE a.sales_date >= %s GROUP BY a.menu_id, m.recipe_code ORDER BY sum(a.qty) DESC""", (since,))]


def explode(card: dict, portions: float, wip: dict[str, dict], out: dict[str, dict]) -> None:
    """Raw materials of `portions` portions of a card: item lines in their unit, WIP lines (grams) per ingredient."""
    for ln in card.get("lines") or []:
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


def report(d_from: date, d_to: date, branches: Optional[list[str]], horizon: int = 7, buffer_pct: float = 10) -> dict:
    days = (d_to - d_from).days + 1
    sold = db.fetch(
        f"""SELECT a.menu_id, max(a.menu_name) AS name, max(a.category) AS category, sum(a.qty) AS qty,
                   sum(a.subtotal - a.discount) AS sales
              FROM {P}.agg_menu_daily a WHERE a.sales_date BETWEEN %(a)s AND %(b)s
               AND (%(br)s::text[] IS NULL OR a.branch_code = ANY(%(br)s)) GROUP BY a.menu_id ORDER BY 4 DESC""",
        {"a": d_from, "b": d_to, "br": branches})
    cards = {r["code"]: r for r in recipes() if r["isActive"]}
    mapped = {m["id"]: code for code, c in cards.items() for m in c["menus"]}
    wip = wips()
    system: dict[str, dict] = {}
    plan: dict[str, dict] = {}
    per_menu, unmapped = [], []
    for s in sold:
        qty = _f(s["qty"])
        code = mapped.get(s["menu_id"])
        if not code:
            unmapped.append({"id": s["menu_id"], "name": s["name"], "category": s["category"], "qty": qty, "sales": _f(s["sales"])})
            continue
        explode(cards[code], qty, wip, system)
        explode(cards[code], qty / days * horizon * (1 + buffer_pct / 100), wip, plan)
        per_menu.append({"id": s["menu_id"], "name": s["name"], "recipe": code, "recipeName": cards[code]["name"],
                         "qty": qty, "sales": _f(s["sales"])})
    codes = [k for k in system if not k.startswith(("wip:", "n:"))]
    price = prices_on(d_to, codes or None)
    # HPP of a portion of every card used
    hpp: dict[str, Optional[float]] = {}
    for code in {m["recipe"] for m in per_menu}:
        one: dict[str, dict] = {}
        explode(cards[code], 1, wip, one)
        missing = [v for v in one.values() if v.get("code") and v.get("isStock", True) and v["code"] not in price]
        hpp[code] = None if missing else sum(v["qty"] * price.get(v["code"] or "", 0.0) for v in one.values())
    for m in per_menu:
        unit = hpp.get(m["recipe"])
        m["hpp"] = round(unit, 2) if unit is not None else None
        m["cogs"] = round(unit * m["qty"], 2) if unit is not None else None
        m["ratio"] = round(m["cogs"] / m["sales"], 6) if m["cogs"] is not None and m["sales"] else None
    actual = {r["product_code"]: r for r in db.fetch(
        f"""SELECT product_code, max(product_name) AS name, max(base_unit) AS unit, sum(actual_qty) AS actual,
                   sum(theoretical_qty) AS esb_theoretical
              FROM {P}.agg_cost_item_period WHERE period_start BETWEEN %(a)s AND %(b)s
               AND (%(br)s::text[] IS NULL OR branch_code = ANY(%(br)s)) AND product_code IS NOT NULL GROUP BY 1""",
        {"a": d_from, "b": d_to, "br": branches})}
    usage = []
    for k, v in sorted(system.items(), key=lambda kv: kv[1]["name"]):
        a = actual.get(v["code"] or "")
        p = price.get(v["code"] or "")
        usage.append({"code": v["code"], "name": v["name"], "unit": v["unit"], "isStock": v.get("isStock", True),
                      "system": round(v["qty"], 3), "actual": round(_f(a["actual"]), 3) if a else None,
                      "variance": round(_f(a["actual"]) - v["qty"], 3) if a else None,
                      "price": p, "systemValue": round(v["qty"] * p, 2) if p is not None else None,
                      "actualValue": round(_f(a["actual"]) * p, 2) if a and p is not None else None})
    mrp = [{"code": v["code"], "name": v["name"], "unit": v["unit"], "isStock": v.get("isStock", True),
            "qty": math.ceil(round(v["qty"], 6)), "value": round(math.ceil(round(v["qty"], 6)) * price[v["code"]], 2)
            if v["code"] in price else None} for v in sorted(plan.values(), key=lambda x: x["name"])]
    sales = sum(m["sales"] for m in per_menu)
    cogs = sum(m["cogs"] or 0 for m in per_menu)
    return {"period": {"from": d_from.isoformat(), "to": d_to.isoformat(), "days": days, "horizon": horizon, "buffer": buffer_pct},
            "menus": per_menu, "unmapped": unmapped, "usage": usage, "mrp": mrp,
            "total": {"sales": sales, "cogs": cogs, "ratio": round(cogs / sales, 6) if sales else None,
                      "menusMapped": len(per_menu), "menusUnmapped": len(unmapped),
                      "unpriced": sum(1 for m in per_menu if m["hpp"] is None),
                      "systemValue": sum(u["systemValue"] or 0 for u in usage),
                      "actualValue": sum(u["actualValue"] or 0 for u in usage)}}
