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
# the sugar of a card: its gula / sugar items (2026-11-05: not the flavour syrups, Less Sugar keeps the butterscotch)
SUGAR = re.compile(r"gula|sugar", re.I)
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


# ---------------------------------------------------------------- sugar in grams per card (migration 086)

SUGAR_CHOICES = ("no sugar", "less sugar", "extra sugar")
SUGAR_LEVEL = {"no sugar": "no", "less sugar": "less", "extra sugar": "extra"}
SUGAR_PCT = {"no sugar": -100, "less sugar": -50, "extra sugar": 50}  # the fallback: the % rules of migration 071


def card_rules() -> dict[tuple[str, str], float]:
    """{(choice key, card code): grams of sugar of one portion with that choice}."""
    try:
        return {(r["name_key"], r["recipe_code"]): _f(r["grams"]) for r in db.fetch(f"SELECT name_key, recipe_code, grams FROM {P}.cc_option_card_rule")}
    except Exception:  # before 086
        return {}


def card_sugar(card: dict) -> float:
    """Grams of sugar of one Normal portion of a card: its gula / sugar items."""
    return sum(_f(ln.get("qty")) for ln in card.get("lines") or [] if ln.get("type") == "item" and SUGAR.search(ln.get("name") or ""))


def _sugar_cards(days: int = 90) -> list[dict]:
    """The cards of the menus sold with a sugar choice (the last `days`), with sugar on the card."""
    since = date.today() - timedelta(days=days)
    parents = {r["parent"] for r in db.fetch(
        f"""WITH br AS ({BRANCH_COMPANY})
            SELECT DISTINCT coalesce(br.company, %(main)s) || ':' || a.parent_id AS parent
              FROM {P}.agg_menu_option_daily a LEFT JOIN br ON br.branch_code = a.branch_code
             WHERE a.sales_date >= %(since)s AND lower(a.option_name) LIKE '%%sugar'""", {"since": since, "main": MAIN_COMPANY})}
    codes = {r["recipe_code"] for r in db.fetch(f"SELECT menu_id, recipe_code FROM {P}.cc_menu_recipe_map") if r["menu_id"] in parents}
    cards = db.fetch(f"SELECT code, name, lines FROM {P}.cc_menu_recipe WHERE code = ANY(%s) ORDER BY name", (sorted(codes),))
    return [c for c in cards if card_sugar(c) > 0]


def _esb_sugar() -> dict[str, dict[str, float]]:
    """{menu name (normalised): {no|less|extra|normal: grams of sugar}} from the ESB BOMs '<menu> (<choice> Sugar)'."""
    out: dict[str, dict[str, float]] = {}
    for r in db.fetch(f"""SELECT b.bom_name, sum(l.qty) FILTER (WHERE l.product_name ~* 'gula|sugar') AS sugar
                            FROM {SCHEMA}.master_bom b JOIN {SCHEMA}.master_bom_lines l ON l.bom_id = b.bom_id
                           WHERE coalesce(b.is_deleted, false) = false AND b.bom_name ~* '\\((no|less|extra|normal) sugar\\)'
                           GROUP BY 1"""):
        m = re.match(r"(.*)\((no|less|extra|normal) sugar\)", r["bom_name"], re.I)
        if m:
            out.setdefault(_norm(m.group(1)), {})[m.group(2).lower()] = _f(r["sugar"])
    return out


def sugar_grams(days: int = 90) -> dict:
    """Variants & Modifiers → Sugar per menu: every card sold with a sugar choice, its Normal sugar and the grams of
    No / Less / Extra with where they came from (esb, pct, check = an ESB figure that did not look right, manual)."""
    try:
        rows = {(r["name_key"], r["recipe_code"]): r for r in db.fetch(f"SELECT * FROM {P}.cc_option_card_rule")}
    except Exception:  # before 086
        rows = {}
    out = []
    for c in _sugar_cards(days):
        normal = card_sugar(c)
        cells = {}
        for k in SUGAR_CHOICES:
            row = rows.get((k, c["code"]))
            cells[SUGAR_LEVEL[k]] = ({"grams": _f(row["grams"]), "source": row["source"], "note": row["note"],
                                      "updatedBy": row["updated_by"], "updatedAt": row["updated_at"].isoformat()} if row
                                     else {"grams": round(normal * (1 + SUGAR_PCT[k] / 100), 3), "source": "rule"})
        out.append({"code": c["code"], "name": c["name"], "normal": round(normal, 3), "choices": cells})
    return {"data": out, "units": "GR"}


def _plausible(level: str, grams: Optional[float], normal: float) -> bool:
    if grams is None:
        return False
    return {"no": grams == 0, "less": 0 < grams < normal, "extra": grams > normal}[level]


def fill_sugar(user: dict, days: int = 90) -> dict:
    """The grams of every card sold with a sugar choice: from its ESB BOM '<menu> (<choice> Sugar)' when it looks right
    (No = 0, Less below Normal, Extra above), else the % rule (an ESB figure that did not look right is marked check).
    A row Cost Control set (manual) stays."""
    esb = _esb_sugar()
    by = user.get("full_name") or user["username"]
    n = {"esb": 0, "pct": 0, "check": 0, "kept": 0}
    with db.transaction() as conn:
        manual = {(r["name_key"], r["recipe_code"]) for r in conn.execute(
            f"SELECT name_key, recipe_code FROM {P}.cc_option_card_rule WHERE source = 'manual'").fetchall()}
        for c in _sugar_cards(days):
            normal = card_sugar(c)
            e = esb.get(_norm(c["name"]), {})
            for k in SUGAR_CHOICES:
                if (k, c["code"]) in manual:
                    n["kept"] += 1
                    continue
                level = SUGAR_LEVEL[k]
                v = e.get(level)
                if _plausible(level, v, normal):
                    grams, src, note = v, "esb", None
                else:
                    grams = round(normal * (1 + SUGAR_PCT[k] / 100), 3)
                    src = "pct" if v is None else "check"
                    note = None if v is None else f"ESB: {v:g} g (Normal {normal:g} g) — perlu dicek"
                conn.execute(f"""INSERT INTO {P}.cc_option_card_rule (name_key, recipe_code, grams, source, note, updated_by)
                                 VALUES (%s, %s, %s, %s, %s, %s)
                                 ON CONFLICT (name_key, recipe_code) DO UPDATE SET grams = EXCLUDED.grams, source = EXCLUDED.source,
                                 note = EXCLUDED.note, updated_by = EXCLUDED.updated_by, updated_at = now()""",
                             (k, c["code"], grams, src, note, by))
                n[src] += 1
    return {"filled": n, **sugar_grams(days)}


def save_sugar(changes: list[dict], user: dict) -> dict:
    """changes: [{card, level: no|less|extra, grams}] — grams None: back to the % rule (the row goes)."""
    by = user.get("full_name") or user["username"]
    keys = {v: k for k, v in SUGAR_LEVEL.items()}
    with db.transaction() as conn:
        for c in changes:
            k = keys.get(c.get("level"))
            code = str(c.get("card") or "")
            if not k or not code:
                raise OptionError("Pilih menu dan tingkat gula")
            if c.get("grams") is None:
                conn.execute(f"DELETE FROM {P}.cc_option_card_rule WHERE name_key = %s AND recipe_code = %s", (k, code))
                continue
            grams = _f(c["grams"])
            if grams < 0 or grams > 5000:
                raise OptionError("Gramasi 0 sampai 5.000 gram")
            conn.execute(f"""INSERT INTO {P}.cc_option_card_rule (name_key, recipe_code, grams, source, note, updated_by)
                             VALUES (%s, %s, %s, 'manual', NULL, %s)
                             ON CONFLICT (name_key, recipe_code) DO UPDATE SET grams = EXCLUDED.grams, source = 'manual', note = NULL,
                             updated_by = EXCLUDED.updated_by, updated_at = now()""", (k, code, grams, by))
    return {"saved": len(changes)}


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
    grams = card_rules()  # migration 086: the sugar of a choice in grams per card
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
        g = grams.get((key_of(c["name"]), code))
        sugar = [(k, v) for k, v in one.items() if SUGAR.search(v["name"] or "")]
        total = sum(v["qty"] for _, v in sugar)
        if g is not None and total > 0:
            # the grams set for this card: the card's sugar moves to them, each sugar item by its share
            for k, v in sugar:
                rows.setdefault(k, {key: v[key] for key in v if key != "qty"})
                d = per_menu.setdefault(c["parent"], {})
                d[k] = d.get(k, 0.0) + (g - total) * (v["qty"] / total) * c["qty"]
            continue
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


# ---------------------------------------------------------------- recipe cards made for every menu (2026-11-06)

def _fixed_contents(groups: list[dict]) -> Optional[list[tuple[str, str, float]]]:
    """[(menu id, name, portions)] of a bundle whose package groups are fixed: a group of one menu (its min quantity)
    or of n menus taken n (one of each); None when a group is a choice (pick 2 of 5 …)."""
    out = []
    for g in groups:
        menus = [m for m in g.get("menus") or [] if str(m.get("flagActive", 1)) == "1"] or (g.get("menus") or [])
        n, need = len(menus), _f(g.get("minQty"))
        if n == 1 and need > 0:
            out.append((str(menus[0].get("menuID")), menus[0].get("menuName") or "", need))
        elif n > 1 and need == n:
            out.extend((str(m.get("menuID")), m.get("menuName") or "", 1.0) for m in menus)
        else:
            return None
    return out or None


def auto_cards(user: dict, dry: bool = False, days: int = 90) -> dict:
    """A recipe card for every menu sold lately without one: from the ESB BOM linked to the menu in the POS menu master
    (menus sharing a BOM share its card), and for a bundle without a BOM a card holding the cards of its fixed package
    contents. Cards already made are kept; dry: only what would be done."""
    from app import menu_costing
    menus = [m for m in menu_costing.menus(days) if not m["recipe"] and "menu" in m["kinds"]]
    cards = {r["code"]: r for r in menu_costing.recipes()}
    mapped = {m["id"]: code for code, c in cards.items() for m in c["menus"]}
    ids = [m["id"] for m in menus]
    master = {r["id"]: r for r in db.fetch(
        f"""SELECT company_code || ':' || menu_id AS id, company_code, menu_name, raw_data->>'bomID' AS bom,
                   raw_data->'menuPackages'->'menuGroup' AS groups
              FROM {SCHEMA}.master_pos_menu WHERE company_code || ':' || menu_id = ANY(%s)""", (ids,))} if ids else {}
    bom_codes = {str(r["bom_id"]): r["bom_code"] for r in db.fetch(
        f"SELECT bom_id, bom_code FROM {SCHEMA}.master_bom WHERE bom_id::text = ANY(%s)",
        ([str(r["bom"]) for r in master.values() if r["bom"] and r["bom"] != "0"],))} if master else {}
    by_bom: dict[str, list[dict]] = {}
    bundles, skipped = [], []
    for m in menus:
        r = master.get(m["id"])
        if not r:
            skipped.append({"id": m["id"], "name": m["name"], "reason": "tidak ada di master menu ESB"})
        elif r["bom"] and r["bom"] != "0" and r["bom"] in bom_codes:
            by_bom.setdefault(r["bom"], []).append(m)
        elif r["groups"]:
            bundles.append((m, r))
        else:
            skipped.append({"id": m["id"], "name": m["name"], "reason": "tanpa BOM dan tanpa isi package di ESB"})
    # the menus inside the bundles get their card too, sold on their own lately or not
    inside = {f"{r['company_code']}:{mid}": n for _, r in bundles for mid, n, _ in (_fixed_contents(r["groups"] or []) or [])}
    extra = [i for i in inside if i not in mapped and not any(i == x["id"] for ms in by_bom.values() for x in ms)]
    if extra:
        for r in db.fetch(f"""SELECT company_code || ':' || menu_id AS id, menu_name, raw_data->>'bomID' AS bom,
                                     (SELECT bom_code FROM {SCHEMA}.master_bom b WHERE b.bom_id::text = raw_data->>'bomID') AS bom_code
                                FROM {SCHEMA}.master_pos_menu WHERE company_code || ':' || menu_id = ANY(%s)""", (extra,)):
            if r["bom"] and r["bom"] != "0" and r["bom_code"] is not None:
                bom_codes[r["bom"]] = r["bom_code"]
                by_bom.setdefault(r["bom"], []).append({"id": r["id"], "name": r["menu_name"]})
    created, assigned = [], []
    for bom_id, ms in by_bom.items():
        code = f"BOM-{bom_codes[bom_id] or bom_id}".upper()[:40]
        menus_of = [{"id": x["id"], "name": x["name"]} for x in ms]
        if code in cards:
            assigned.extend(x["name"] for x in ms)
            if not dry:
                c = cards[code]
                menu_costing.save_recipe(code, {**c, "menus": c["menus"] + menus_of}, user)
        else:
            try:
                if not dry:
                    cards[code] = import_bom(bom_id, {"code": code, "menus": menus_of}, user)
                else:
                    cards[code] = {"code": code, "name": ms[0]["name"], "menus": menus_of, "lines": []}
                created.append({"code": code, "name": cards[code]["name"], "menus": [x["name"] for x in ms]})
            except (OptionError, menu_costing.CostingError) as exc:
                skipped.extend({"id": x["id"], "name": x["name"], "reason": str(exc)} for x in ms)
                continue
        for x in ms:
            mapped[x["id"]] = code
    made_bundles = []
    for m, r in bundles:
        company = r["company_code"]
        contents = _fixed_contents(r["groups"] or [])
        if contents is None:
            skipped.append({"id": m["id"], "name": m["name"], "reason": "isi package berupa pilihan, isi kartunya manual"})
            continue
        missing = [n for mid, n, _ in contents if f"{company}:{mid}" not in mapped]
        if missing:
            skipped.append({"id": m["id"], "name": m["name"], "reason": "menu isi belum punya kartu: " + ", ".join(sorted(set(missing)))})
            continue
        lines: dict[str, dict] = {}
        for mid, n, q in contents:
            code = mapped[f"{company}:{mid}"]
            ln = lines.setdefault(code, {"type": "menu", "code": code, "name": cards[code]["name"] if code in cards else n, "qty": 0.0})
            ln["qty"] += q
        code = f"BUNDLE-{m['id'].split(':')[-1]}" + ("" if company == MAIN_COMPANY else f"-{company}")
        if not dry:
            menu_costing.save_recipe(code if code in cards else None, {
                "code": code, "name": m["name"], "category": "Bundling", "lines": list(lines.values()),
                "note": "Dari isi package menu ESB", "isActive": True, "menus": [{"id": m["id"], "name": m["name"]}]}, user)
        made_bundles.append({"code": code, "name": m["name"], "contents": [f"{ln['qty']:g} × {ln['name']}" for ln in lines.values()]})
    return {"dry": dry, "created": created, "assigned": assigned, "bundles": made_bundles, "skipped": skipped,
            "menus": len(menus)}
