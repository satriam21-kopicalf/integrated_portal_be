"""Sales Calculator by Menu: variants & modifiers and the recipe cards from the ESB BOM (2026-11-05, migration 071).

Choices   a menu line of the POS may carry choices (its ESB packages / extras: Normal / Less / No / Extra Sugar, Extra
          Shot, Ice Cream …). agg_menu_option_daily keeps how many of each choice were sold with each parent menu. A rule
          per choice name (cc_option_rule) says what it changes against the card of the parent menu, which stays the
          standard portion: nothing, a % of what the card uses of a target (sugar = every sugar item of the card, or one
          item) or a quantity of an item per choice. app/menu_costing.py adds these adjustments to the system usage, the
          planning and the COGS.
ESB BOM   the bills of material of ESB (integration_esb.master_bom + master_bom_lines, synced by integrated-esbapi) become
          recipe cards: every BOM line is matched to the item master (kc_master.item by code, its unit through
          kc_master.item_uom), a premix to an active WIP recipe of the same name, the rest kept as an item typed by hand.
"""
import re
from datetime import date, timedelta
from typing import Any, Optional

from psycopg.types.json import Jsonb

from app import database as db
from app.database import SCHEMA
from app.scope import MAIN_COMPANY

P = "integration_portal"
SUGAR = re.compile(r"gula|sugar|syrup|sirup", re.I)
TARGETS = ("sugar", "item")
MODES = ("pct", "qty")
UNIT_FACTOR = {("KG", "GR"): 1000, ("KG", "G"): 1000, ("L", "ML"): 1000, ("LTR", "ML"): 1000, ("LITER", "ML"): 1000}


class OptionError(ValueError):
    """Refused with a message for the user (HTTP 422)."""


def _f(v: Any) -> float:
    return float(v) if v is not None else 0.0


def key_of(name: str) -> str:
    return " ".join(str(name or "").lower().split())


# ---------------------------------------------------------------- rules

def rules() -> dict[str, dict]:
    """{name key: {name, effect, lines, note}} of every choice rule."""
    return {r["name_key"]: {"name": r["name"], "effect": r["effect"], "lines": r["lines"] or [], "note": r["note"],
                            "updatedBy": r["updated_by"], "updatedAt": r["updated_at"].isoformat()}
            for r in db.fetch(f"SELECT * FROM {P}.cc_option_rule ORDER BY name")}


def _clean_lines(lines: list[dict]) -> list[dict]:
    out = []
    for ln in lines or []:
        target, mode = ln.get("target"), ln.get("mode")
        if target not in TARGETS or mode not in MODES:
            raise OptionError("Penyesuaian: target gula / item dan cara persen / qty")
        code = str(ln.get("code") or "").strip() or None
        if target == "item" and not code:
            raise OptionError("Pilih item yang disesuaikan")
        if target == "sugar" and mode == "qty":
            raise OptionError("Gula kartu induk hanya bisa disesuaikan dalam persen; untuk qty pilih itemnya")
        try:
            value = float(ln.get("value"))
        except (TypeError, ValueError):
            raise OptionError("Nilai penyesuaian harus angka") from None
        if mode == "pct" and not -100 <= value <= 500:
            raise OptionError("Persen antara −100 dan 500")
        if value == 0:
            continue
        out.append({"target": target, "code": code if target == "item" else None, "mode": mode, "value": value,
                    **({"name": str(ln.get("name") or code)[:160]} if target == "item" else {})})
    return out


def save_rule(name: str, data: dict, user: dict) -> dict:
    name = " ".join(str(name or "").split())
    if not name:
        raise OptionError("Nama pilihan wajib diisi")
    effect = data.get("effect")
    if effect not in ("none", "adjust", "clear"):
        raise OptionError("Pilih: tidak berpengaruh atau sesuaikan bahan")
    k = key_of(name)
    with db.transaction() as conn:
        if effect == "clear":
            conn.execute(f"DELETE FROM {P}.cc_option_rule WHERE name_key = %s", (k,))
            return {"name": name, "effect": None}
        lines = _clean_lines(data.get("lines")) if effect == "adjust" else []
        if effect == "adjust" and not lines:
            raise OptionError("Isi minimal satu penyesuaian bahan, atau pilih tidak berpengaruh")
        conn.execute(f"""INSERT INTO {P}.cc_option_rule (name_key, name, effect, lines, note, updated_by)
                         VALUES (%s, %s, %s, %s, %s, %s)
                         ON CONFLICT (name_key) DO UPDATE SET name = EXCLUDED.name, effect = EXCLUDED.effect, lines = EXCLUDED.lines,
                         note = EXCLUDED.note, updated_by = EXCLUDED.updated_by, updated_at = now()""",
                     (k, name, effect, Jsonb(lines), (data.get("note") or "").strip()[:300] or None, user.get("full_name") or user["username"]))
    return {"name": name, **rules()[k]}


# ---------------------------------------------------------------- choices sold

BRANCH_COMPANY = f"""SELECT DISTINCT ON (branch_code) branch_code, coalesce(company_code, '{MAIN_COMPANY}') AS company
                       FROM {SCHEMA}.master_branches WHERE coalesce(branch_code, '') <> ''
                      ORDER BY branch_code, coalesce(is_deleted, false)"""
ON_DEMAND_DAYS = 7  # the days of a period a request builds itself when the scheduled runs have not yet


def ensure_days(d_from: date, d_to: date) -> list[date]:
    """Builds a few missing days of the period now (latest first); returns the days still missing."""
    from app import aggregates

    missing = aggregates.options_missing(d_from, min(d_to, date.today()))
    for day in missing[:ON_DEMAND_DAYS]:
        try:
            aggregates.refresh_options(day)
        except Exception:  # noqa: BLE001 - the transactions unreachable: the report says what is missing
            break
    return aggregates.options_missing(d_from, min(d_to, date.today()))


def sold(d_from: date, d_to: date, branches: Optional[list[str]]) -> list[dict]:
    """[{parent, option, name, qty}] the choices sold with every parent menu ("<company>:<menu id>")."""
    return [{"parent": r["parent"], "option": r["option"], "name": r["name"], "qty": _f(r["qty"])} for r in db.fetch(
        f"""WITH br AS ({BRANCH_COMPANY})
            SELECT coalesce(br.company, %(main)s) || ':' || a.parent_id AS parent, coalesce(br.company, %(main)s) || ':' || a.option_id AS option,
                   max(a.option_name) AS name, sum(a.qty) AS qty
              FROM {P}.agg_menu_option_daily a LEFT JOIN br ON br.branch_code = a.branch_code
             WHERE a.sales_date BETWEEN %(a)s AND %(b)s AND (%(br)s::text[] IS NULL OR a.branch_code = ANY(%(br)s))
             GROUP BY 1, 2""", {"a": d_from, "b": d_to, "br": branches, "main": MAIN_COMPANY})]


def options(days: int = 90) -> dict:
    """Every choice name sold in the last `days` days: how many, with how many menus (the top ones) and its rule."""
    d_to = date.today()
    d_from = d_to - timedelta(days=days - 1)
    pending = ensure_days(d_to - timedelta(days=6), d_to)
    names: dict[str, dict] = {}
    parents: dict[str, str] = {}
    from app import menu_costing
    for m in menu_costing.menus(days):
        parents[m["id"]] = m["name"]
    mapped = {m["id"] for m in menu_costing.menus(days) if m["recipe"]}
    for s in sold(d_from, d_to, None):
        n = names.setdefault(key_of(s["name"]), {"name": s["name"], "qty": 0.0, "menus": {}, "options": set()})
        n["qty"] += s["qty"]
        n["options"].add(s["option"])
        n["menus"][s["parent"]] = n["menus"].get(s["parent"], 0.0) + s["qty"]
    rule = rules()
    out = []
    for k, n in names.items():
        top = sorted(n["menus"].items(), key=lambda kv: -kv[1])
        out.append({"key": k, "name": n["name"], "qty": n["qty"], "menus": len(top), "ids": len(n["options"]),
                    "withCard": round(sum(q for p, q in top if p in mapped), 3),
                    "top": [{"id": p, "name": parents.get(p, p), "qty": q, "hasCard": p in mapped} for p, q in top[:5]],
                    "rule": rule.get(k)})
    out.sort(key=lambda o: (o["rule"] is not None, -o["qty"]))
    from app import aggregates
    history = aggregates.options_missing(max(d_from, aggregates.OPTION_BACKFILL_FROM), d_to)
    return {"from": d_from.isoformat(), "to": d_to.isoformat(), "data": out, "pendingDays": len(set(history) | set(pending))}


def adjustments(choice_rows: list[dict], cards: dict, mapped: dict, wip: dict, rule: dict, explode) -> tuple[dict, dict, list]:
    """The materials the choices change: ({parent menu: {material key: qty}}, {material key: row}, unruled choices).
    `explode(card, portions, wip, out)` is menu_costing.explode."""
    per_menu: dict[str, dict[str, float]] = {}
    rows: dict[str, dict] = {}
    unruled: dict[str, dict] = {}
    one_cache: dict[str, dict] = {}
    item_info = _items({ln["code"] for r in rule.values() for ln in r["lines"] if ln.get("code")})
    for c in choice_rows:
        code = mapped.get(c["parent"])
        if not code or code not in cards:
            continue  # the parent menu has no card: the menu itself is listed as without a card
        r = rule.get(key_of(c["name"]))
        if r is None:
            u = unruled.setdefault(key_of(c["name"]), {"name": c["name"], "qty": 0.0})
            u["qty"] += c["qty"]
            continue
        if r["effect"] != "adjust":
            continue
        if code not in one_cache:
            one: dict[str, dict] = {}
            explode(cards[code], 1, wip, one)
            one_cache[code] = one
        one = one_cache[code]
        for ln in r["lines"]:
            if ln["mode"] == "pct":
                hits = [(k, v) for k, v in one.items() if (SUGAR.search(v["name"] or "") if ln["target"] == "sugar" else v.get("code") == ln["code"])]
                for k, v in hits:
                    rows.setdefault(k, {key: v[key] for key in v if key != "qty"})
                    d = per_menu.setdefault(c["parent"], {})
                    d[k] = d.get(k, 0.0) + v["qty"] * ln["value"] / 100 * c["qty"]
            else:
                info = item_info.get(ln["code"], {"name": ln.get("name") or ln["code"], "unit": ""})
                rows.setdefault(ln["code"], {"code": ln["code"], "name": info["name"], "unit": info["unit"]})
                d = per_menu.setdefault(c["parent"], {})
                d[ln["code"]] = d.get(ln["code"], 0.0) + ln["value"] * c["qty"]
    return per_menu, rows, sorted(unruled.values(), key=lambda u: -u["qty"])


def _items(codes: set[str]) -> dict[str, dict]:
    if not codes:
        return {}
    return {r["code"]: {"name": r["name"], "unit": r["base_uom"] or ""} for r in db.fetch(
        "SELECT code, name, base_uom FROM kc_master.item WHERE code = ANY(%s)", (sorted(codes),))}


# ---------------------------------------------------------------- recipe cards from the ESB BOM

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def boms(search: str = "") -> list[dict]:
    """The ESB bills of material (active), with their lines count and the POS menus sold under the same name."""
    rows = db.fetch(
        f"""SELECT b.bom_id, b.bom_code, b.bom_name, b.product_id, b.product_name, b.bom_type,
                   (SELECT count(*) FROM {SCHEMA}.master_bom_lines l WHERE l.bom_id = b.bom_id) AS lines
              FROM {SCHEMA}.master_bom b
             WHERE NOT coalesce(b.is_deleted, false) AND coalesce(b.is_active, true)
               AND (%(q)s = '' OR b.bom_name ILIKE %(like)s OR b.product_name ILIKE %(like)s OR b.bom_code ILIKE %(like)s)
             ORDER BY b.product_name, b.bom_name""", {"q": search.strip(), "like": f"%{search.strip()}%"})
    from app import menu_costing
    by_name: dict[str, list[dict]] = {}
    for m in menu_costing.menus(90):
        by_name.setdefault(_norm(m["name"]), []).append(m)
    out = []
    for r in rows:
        menus = by_name.get(_norm(r["product_name"]), []) + [m for m in by_name.get(_norm(r["bom_name"]), [])
                                                             if _norm(r["bom_name"]) != _norm(r["product_name"])]
        out.append({"id": r["bom_id"], "code": r["bom_code"], "name": r["bom_name"] or r["product_name"], "product": r["product_name"],
                    "type": r["bom_type"], "lines": r["lines"],
                    "menus": [{"id": m["id"], "name": m["name"], "qty": m["qty"], "recipe": m["recipe"]} for m in menus]})
    return out


def _to_unit(qty: float, have: str, want: str) -> Optional[float]:
    have, want = (have or "").upper(), (want or "").upper()
    if not have or not want or have == want:
        return qty
    if (have, want) in UNIT_FACTOR:
        return qty * UNIT_FACTOR[(have, want)]
    if (want, have) in UNIT_FACTOR:
        return qty / UNIT_FACTOR[(want, have)]
    return None


def bom_card(bom_id: str) -> dict:
    """The card a BOM makes: its lines matched to the item master / the WIP recipes, and what could not be matched."""
    head = db.fetchrow(f"SELECT bom_id, bom_code, bom_name, product_name FROM {SCHEMA}.master_bom WHERE bom_id = %s", (bom_id,))
    if not head:
        raise OptionError("BOM tidak ditemukan")
    lines = db.fetch(f"""SELECT line_no, product_code, product_name, uom_name, qty, last_hpp FROM {SCHEMA}.master_bom_lines
                          WHERE bom_id = %s ORDER BY line_no""", (bom_id,))
    codes = sorted({str(ln["product_code"]) for ln in lines if ln["product_code"]})
    items = {r["code"]: r for r in db.fetch(
        """SELECT i.code, i.name, upper(coalesce(i.base_uom, '')) AS base,
                  coalesce(jsonb_object_agg(upper(u.uom), u.factor) FILTER (WHERE u.uom IS NOT NULL), '{}') AS units
             FROM kc_master.item i LEFT JOIN kc_master.item_uom u ON u.item_id = i.id AND u.is_active
            WHERE i.code = ANY(%s) GROUP BY i.code, i.name, i.base_uom""", (codes,))} if codes else {}
    from app import menu_costing
    wip_by_name = {_norm(w["name"]): w for w in menu_costing.wips().values()}
    out, notes = [], []
    for ln in lines:
        qty, uom = _f(ln["qty"]), str(ln["uom_name"] or "").upper()
        if qty <= 0:
            continue
        it = items.get(str(ln["product_code"] or ""))
        w = wip_by_name.get(_norm(ln["product_name"]))
        # a premix of the BOM is its WIP recipe (grams), so its ingredients are counted as the Production Calculator does
        if w and (not it or "premix" in _norm(ln["product_name"])):
            grams = _to_unit(qty, uom, "GR")
            if grams is not None:
                out.append({"type": "wip", "code": w["code"], "name": w["name"], "qty": round(grams, 4), "unit": "GR", "from": ln["product_name"]})
                continue
        if it:
            base = it["base"]
            units = {k: float(v) for k, v in (it["units"] or {}).items()}
            q = qty * units[uom] if uom in units and uom != base else _to_unit(qty, uom, base)
            if q is None:
                notes.append(f"{ln['product_name']}: satuan {uom} belum bisa diubah ke {base} (master item)")
                q = qty
            out.append({"type": "item", "code": it["code"], "name": it["name"], "qty": round(q, 4), "unit": base, "from": ln["product_name"]})
            continue
        price = _f(ln["last_hpp"]) or None
        out.append({"type": "other", "code": str(ln["product_code"] or ln["product_name"])[:40], "name": ln["product_name"] or "?",
                    "qty": round(qty, 4), "unit": uom, "price": price, "from": ln["product_name"]})
        notes.append(f"{ln['product_name']}: tidak ada di master item, disimpan sebagai item lain")
    return {"bom": {"id": head["bom_id"], "code": head["bom_code"], "name": head["bom_name"] or head["product_name"]},
            "lines": out, "notes": notes}


def import_bom(bom_id: str, data: dict, user: dict) -> dict:
    """A recipe card from the BOM (the lines as bom_card), with the POS menus picked; an existing card of that code is
    replaced (its menus stay unless others are given)."""
    from app import menu_costing
    card = bom_card(bom_id)
    if not card["lines"]:
        raise OptionError("BOM tidak punya baris bahan")
    code = str(data.get("code") or f"BOM-{card['bom']['code'] or bom_id}").strip().upper()[:40]
    name = " ".join(str(data.get("name") or card["bom"]["name"]).split())[:160]
    exists = any(r["code"] == code for r in menu_costing.recipes())
    body = {"code": code, "name": name, "category": data.get("category") or "ESB BOM",
            "lines": [{k: v for k, v in ln.items() if k != "from"} for ln in card["lines"]],
            "note": f"Dari BOM ESB {card['bom']['code'] or bom_id} ({card['bom']['name']})", "isActive": True}
    if data.get("menus") is not None:
        body["menus"] = data["menus"]
    return menu_costing.save_recipe(code if exists else None, body, user)
