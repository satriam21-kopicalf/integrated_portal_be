"""Waste (migration 040): the outlets, HUB and PICs enter each wasted item of their branch; Cost Control monitors and
reviews it.

An entry is one item of one branch and business day (today or yesterday, WIB):
  * scope: waste / usage (Usage Management) items of integration_portal.waste_item (docs/Daily SO & Waste.xlsx), or
    premix: a WIP recipe the stock opname offers for the branch's facility type (active recipe with an active version,
    kc_recipe), entered as in the count: in its count unit (Gram for a weighed premix, Porsi / Pcs …, another unit of
    the recipe can be picked); the grams and the grams of every ingredient (rounded up) are kept (migration 044);
  * the quantity in the item's unit, a waste category (integration_portal.waste_category), a note / reason;
  * a camera photo as evidence (stamped in the browser with the time and the branch, like the stock opname photos).
The person who entered it may delete it while Cost Control has not reviewed it. Cost Control approves or rejects
entries (one by one or many at once) with a note.

Every day (migration 041): Usage Management is a full list, the quantity of every usage item of the branch and day
(usage_count, saved again to correct it); each branch reports its waste daily, an entry or "no waste today" (waste_day),
and daily() tells the monitor which branches have not reported.
"""
import json
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, Optional

from app import database as db
from app.pic_checkin import SCHEMA, CheckInError, branch_options, parse_photo
from app.utils import today

T = "integration_portal.waste_entry"
T_ITEM = "integration_portal.waste_item"
T_CAT = "integration_portal.waste_category"
T_USAGE = "integration_portal.usage_count"
T_DAY = "integration_portal.waste_day"
SCOPES = {"waste": "Waste", "usage": "Usage Management", "premix": "Premix"}
ENTRY_SCOPES = ("waste", "premix")  # usage is the daily list (save_usage)
STATUSES = ("submitted", "approved", "rejected")
MAX_QTY = Decimal("1000000")
# the WIP of the stock opname count (kopicalf_core recipes.count_recipes): active recipe, active version, facility type
PREMIX = """SELECT r.id::text, r.code, r.name, r.recipe_type, r.count_uom, v.id::text AS version_id, v.output_qty_base AS batch
            FROM kc_recipe.recipe r JOIN kc_recipe.recipe_version v ON v.recipe_id = r.id AND v.status = 'ACTIVE'
            WHERE r.is_active AND (coalesce(cardinality(r.facility_types), 0) = 0 OR %(ft)s = ANY(r.facility_types))
            ORDER BY r.name"""
PREMIX_UNITS = """SELECT recipe_id::text, uom, qty_base, per_batch FROM kc_recipe.recipe_unit
                  WHERE recipe_id::text = ANY(%(ids)s) ORDER BY recipe_id, sort, uom"""
PREMIX_LINES = """SELECT l.version_id::text, it.code, coalesce(it.name, l.component_name) AS name, l.is_stock, l.qty_base
                  FROM kc_recipe.recipe_line l LEFT JOIN kc_master.item it ON it.id = l.component_item_id
                  WHERE l.version_id::text = ANY(%(ids)s) ORDER BY l.version_id, l.line_no"""


def _gram(uom: str) -> bool:
    return str(uom or "").strip().lower() == "gram"


def premix_recipes(facility_type: str = "OUTLET") -> list[dict]:
    """The WIP / premix of the stock opname for a facility type: units in grams (Gram always offered), the count unit
    (recipe count_uom when it is one of its units, else Gram) and the grams of every ingredient per gram of WIP."""
    recipes = db.fetch(PREMIX, {"ft": facility_type})
    if not recipes:
        return []
    units: dict[str, list[dict]] = {}
    for u in db.fetch(PREMIX_UNITS, {"ids": [r["id"] for r in recipes]}):
        units.setdefault(u["recipe_id"], []).append(u)
    lines: dict[str, list[dict]] = {}
    for ln in db.fetch(PREMIX_LINES, {"ids": [r["version_id"] for r in recipes]}):
        lines.setdefault(ln["version_id"], []).append(ln)
    out = []
    for r in recipes:
        batch = Decimal(str(r["batch"]))
        us = [{"uom": u["uom"], "grams": 1.0 if _gram(u["uom"]) else float(
            Decimal(str(u["qty_base"])) if u["qty_base"] is not None else batch / Decimal(str(u["per_batch"])))}
            for u in units.get(r["id"], [])]
        if not any(_gram(u["uom"]) for u in us):
            us.append({"uom": "Gram", "grams": 1.0})
        want = (r["count_uom"] or "").lower()
        count_uom = next((u["uom"] for u in us if u["uom"].lower() == want), "Gram")
        out.append({"code": r["code"], "name": r["name"], "unit": count_uom, "type": r["recipe_type"], "units": us,
                    "components": [{"code": ln["code"], "name": ln["name"], "isStock": ln["is_stock"],
                                    "perGram": float(Decimal(str(ln["qty_base"])) / batch)}
                                   for ln in lines.get(r["version_id"], [])]})
    return out


def facility_type(branch_code: str) -> str:
    """OUTLET / HUB … of a branch (ESB branch through facility_esb_map, or a kopicalf facility code); OUTLET if unknown."""
    try:
        rows = db.fetch(
            f"""SELECT f.facility_type FROM {SCHEMA}.master_branches b
                JOIN kc_master.facility_esb_map m ON m.company_code = b.company_code AND m.esb_id = b.branch_id::text
                JOIN kc_master.facility f ON f.id = m.facility_id WHERE b.branch_code = %(c)s
                UNION ALL SELECT facility_type FROM kc_master.facility WHERE code = %(c)s""", {"c": branch_code})
    except Exception:  # noqa: BLE001
        return "OUTLET"
    return next((r["facility_type"] for r in rows if r.get("facility_type")), "OUTLET")


def explode(item: dict, uom: str, qty: Decimal) -> tuple[str, Decimal, list[dict]]:
    """(unit, grams of WIP, grams per ingredient rounded up as the premix calculator) of `qty` `uom` of a premix."""
    units = item.get("units") or [{"uom": item["unit"], "grams": 1.0}]
    unit = next((u for u in units if u["uom"].lower() == str(uom or item["unit"]).lower()), None)
    if unit is None:
        raise WasteError(f"Satuan {uom} tidak ada untuk {item['name']}")
    grams = qty * Decimal(str(unit["grams"]))

    def up(v: Decimal) -> Decimal:
        return (v - Decimal("0.001")).quantize(Decimal(1), rounding=ROUND_CEILING) if v > 0 else Decimal(0)
    comps = [{"code": c.get("code"), "name": c["name"], "isStock": c.get("isStock", True),
              "grams": float(up(grams * Decimal(str(c["perGram"]))))} for c in item.get("components") or []]
    return unit["uom"], grams.quantize(Decimal("0.001")), comps


class WasteError(CheckInError):
    """An entry refused with a message for the user (HTTP 422)."""


def catalog(facility: str = "OUTLET") -> dict:
    """{"items": {scope: [{code, name, unit}]}, "categories": [name]} for the form (premix of the facility type)."""
    items: dict[str, list[dict]] = {s: [] for s in SCOPES}
    for r in db.fetch(f"SELECT scope, code, name, unit FROM {T_ITEM} WHERE is_active ORDER BY name"):
        items[r["scope"]].append({"code": r["code"], "name": r["name"], "unit": r["unit"]})
    try:
        items["premix"] = premix_recipes(facility)
    except Exception:  # noqa: BLE001 - kc_recipe unreachable: the form still works for waste and usage
        items["premix"] = []
    cats = [r["name"] for r in db.fetch(f"SELECT name FROM {T_CAT} WHERE is_active ORDER BY sort, name")]
    return {"items": items, "categories": cats, "scopes": SCOPES}


def _item(scope: str, code: str, facility: str = "OUTLET") -> Optional[dict]:
    return next((i for i in catalog(facility)["items"].get(scope, []) if i["code"] == code), None)


def _qty(value: Any) -> Decimal:
    try:
        q = Decimal(str(value).replace(",", ".")).quantize(Decimal("0.001"))
    except (InvalidOperation, ValueError):
        raise WasteError("Jumlah harus angka") from None
    if q <= 0 or q > MAX_QTY:
        raise WasteError("Jumlah harus lebih dari 0")
    return q


def allowed_days() -> tuple[date, date]:
    """Waste is entered for today or, late, for yesterday (WIB)."""
    t = today()
    return t - timedelta(days=1), t


COLUMNS = """e.id::text, e.user_id::text, e.username, e.full_name, e.role, e.branch_code, e.branch_name, e.business_date,
    e.scope, e.item_code, e.item_name, e.unit, e.qty, e.category, e.note, e.production_date, e.expired_date, e.qty_base, e.exploded, e.photo_taken_at, e.latitude, e.longitude,
    e.status, e.review_note, e.reviewed_by, e.reviewed_at, e.created_at"""


def _view(r: dict) -> dict:
    num = lambda v: float(v) if v is not None else None  # noqa: E731
    return {"id": r["id"], "userId": r["user_id"], "username": r["username"], "name": r["full_name"] or r["username"],
            "branchCode": r["branch_code"], "branchName": r["branch_name"], "businessDate": r["business_date"].isoformat(),
            "scope": r["scope"], "itemCode": r["item_code"], "itemName": r["item_name"], "unit": r["unit"],
            "qty": float(r["qty"]), "category": r["category"], "note": r["note"],
            "productionDate": r["production_date"].isoformat() if r.get("production_date") else None,
            "expiredDate": r["expired_date"].isoformat() if r.get("expired_date") else None,
            "grams": num(r.get("qty_base")), "ingredients": r.get("exploded") or None,
            "photoTakenAt": r["photo_taken_at"].isoformat() if r["photo_taken_at"] else None,
            "latitude": num(r["latitude"]), "longitude": num(r["longitude"]), "status": r["status"],
            "review": None if r["status"] == "submitted" else {
                "note": r["review_note"], "by": r["reviewed_by"],
                "at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None},
            "createdAt": r["created_at"].isoformat()}


# ---------------------------------------------------------------- input

def form(user: dict, branch: Optional[str], day: Optional[date]) -> dict:
    branches = [{"code": b["code"], "name": b["name"]} for b in branch_options(user)]
    first, last = allowed_days()
    codes = {b["code"] for b in branches}
    out = {"branches": branches, "days": {"from": first.isoformat(), "to": last.isoformat()},
           **catalog(facility_type(branch) if branch and branch in codes else "OUTLET"), "data": []}
    if branch and branch in codes:
        rows = db.fetch(f"SELECT {COLUMNS} FROM {T} e WHERE e.branch_code = %s AND e.business_date = %s "
                        "ORDER BY e.created_at DESC", (branch, day or last))
        out["data"] = [_view(r) for r in rows]
        d = day or last
        saved = db.fetch(f"""SELECT item_code, qty, category, note, photo IS NOT NULL AS has_photo FROM {T_USAGE}
                             WHERE branch_code = %s AND business_date = %s""", (branch, d))
        out["usage"] = {r["item_code"]: float(r["qty"]) for r in saved}
        out["usageDetail"] = {r["item_code"]: {"qty": float(r["qty"]), "category": r.get("category"), "note": r.get("note"),
                                               "hasPhoto": bool(r.get("has_photo"))} for r in saved}
        flag = db.fetchrow(f"SELECT no_waste, full_name, username, updated_at FROM {T_DAY} "
                           "WHERE branch_code = %s AND business_date = %s", (branch, d))
        out["noWaste"] = None if not flag or not flag["no_waste"] else {
            "by": flag["full_name"] or flag["username"], "at": flag["updated_at"].isoformat()}
    return out


def _branch_day(user: dict, body: dict) -> tuple[dict, date]:
    branch = next((b for b in branch_options(user) if b["code"] == body.get("branchCode")), None)
    if branch is None:
        raise WasteError("Cabang tidak tersedia untuk akun Anda")
    first, last = allowed_days()
    day = body.get("businessDate") or last
    if not first <= day <= last:
        raise WasteError("Waste hanya bisa diinput untuk hari ini atau kemarin")
    return branch, day


def save_usage(user: dict, body: dict) -> dict:
    """Usage Management lines of a branch and day: {itemCode: {qty, category, note, photo, takenAt, latitude,
    longitude}} (migration 045: category and photo required, the photo kept when saved again without one); an empty
    quantity (null) removes the item."""
    branch, day = _branch_day(user, body)
    items = {i["code"]: i for i in catalog()["items"]["usage"]}
    cats = catalog()["categories"]
    lines = body.get("lines") or {}
    unknown = [c for c in lines if c not in items]
    if unknown:
        raise WasteError(f"Item tidak ada di daftar Usage Management: {unknown[0]}")
    keep, drop = [], []
    for code, line in lines.items():
        line = line if isinstance(line, dict) else {"qty": line}
        value, name = line.get("qty"), items[code]["name"]
        if value is None or str(value).strip() == "":
            drop.append(code)
            continue
        try:
            q = Decimal(str(value).replace(",", ".")).quantize(Decimal("0.001"))
        except (InvalidOperation, ValueError):
            raise WasteError(f"Jumlah {name} harus angka") from None
        if q < 0 or q > MAX_QTY:
            raise WasteError(f"Jumlah {name} tidak valid")
        category = (line.get("category") or "").strip()
        if category not in cats:
            raise WasteError(f"Pilih kategori untuk {name}")
        photo = parse_photo(line["photo"]) if line.get("photo") else None
        keep.append((code, q, category, (line.get("note") or "").strip()[:500] or None, photo, line))
    if keep and any(k[4] is None for k in keep):
        have = {r["item_code"] for r in db.fetch(
            f"SELECT item_code FROM {T_USAGE} WHERE branch_code = %s AND business_date = %s AND photo IS NOT NULL",
            (branch["code"], day))}
        missing = [items[k[0]]["name"] for k in keep if k[4] is None and k[0] not in have]
        if missing:
            raise WasteError(f"Foto evidence wajib untuk {missing[0]}")
    with db.transaction() as conn:
        if drop:
            conn.execute(f"DELETE FROM {T_USAGE} WHERE branch_code = %s AND business_date = %s AND item_code = ANY(%s)",
                         (branch["code"], day, drop))
        for code, q, category, note, photo, line in keep:
            i = items[code]
            pt, data = photo if photo else (None, None)
            conn.execute(
                f"""INSERT INTO {T_USAGE} (branch_code, branch_name, business_date, item_code, item_name, unit, qty,
                        category, note, photo, photo_type, photo_taken_at, latitude, longitude,
                        user_id, username, full_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (branch_code, business_date, item_code) DO UPDATE SET qty = EXCLUDED.qty,
                        category = EXCLUDED.category, note = EXCLUDED.note,
                        photo = coalesce(EXCLUDED.photo, {T_USAGE}.photo),
                        photo_type = coalesce(EXCLUDED.photo_type, {T_USAGE}.photo_type),
                        photo_taken_at = CASE WHEN EXCLUDED.photo IS NULL THEN {T_USAGE}.photo_taken_at ELSE EXCLUDED.photo_taken_at END,
                        latitude = CASE WHEN EXCLUDED.photo IS NULL THEN {T_USAGE}.latitude ELSE EXCLUDED.latitude END,
                        longitude = CASE WHEN EXCLUDED.photo IS NULL THEN {T_USAGE}.longitude ELSE EXCLUDED.longitude END,
                        user_id = EXCLUDED.user_id, username = EXCLUDED.username, full_name = EXCLUDED.full_name,
                        updated_at = now()""",
                (branch["code"], branch["name"], day, code, i["name"], i["unit"], q, category, note, data, pt,
                 line.get("takenAt") if photo else None, line.get("latitude") if photo else None,
                 line.get("longitude") if photo else None, user["id"], user["username"], user.get("full_name")))
    return {"branchCode": branch["code"], "branchName": branch["name"], "businessDate": day.isoformat(),
            "saved": len(keep), "removed": len(drop)}


def usage_photo(branch_code: str, day: date, item_code: str) -> Optional[dict]:
    return db.fetchrow(f"""SELECT branch_code, photo, photo_type FROM {T_USAGE} WHERE branch_code = %s
                           AND business_date = %s AND item_code = %s AND photo IS NOT NULL""", (branch_code, day, item_code))


def set_no_waste(user: dict, body: dict) -> dict:
    """"No waste today" for a branch and day (noWaste false withdraws it)."""
    branch, day = _branch_day(user, body)
    on = bool(body.get("noWaste", True))
    with db.transaction() as conn:
        if on:
            conn.execute(
                f"""INSERT INTO {T_DAY} (branch_code, business_date, branch_name, no_waste, user_id, username, full_name)
                    VALUES (%s, %s, %s, true, %s, %s, %s)
                    ON CONFLICT (branch_code, business_date) DO UPDATE SET no_waste = true, user_id = EXCLUDED.user_id,
                        username = EXCLUDED.username, full_name = EXCLUDED.full_name, updated_at = now()""",
                (branch["code"], day, branch["name"], user["id"], user["username"], user.get("full_name")))
        else:
            conn.execute(f"DELETE FROM {T_DAY} WHERE branch_code = %s AND business_date = %s", (branch["code"], day))
    return {"branchCode": branch["code"], "branchName": branch["name"], "businessDate": day.isoformat(), "noWaste": on}


def create(user: dict, body: dict, device: Optional[str], ip: Optional[str]) -> dict:
    branch, day = _branch_day(user, body)
    scope = body.get("scope")
    if scope not in ENTRY_SCOPES:
        raise WasteError("Jenis waste tidak dikenal")
    facility = facility_type(branch["code"]) if scope == "premix" else "OUTLET"
    item = _item(scope, body.get("itemCode") or "", facility)
    if item is None:
        raise WasteError("Item tidak ada di daftar waste")
    qty = _qty(body.get("qty"))
    unit, grams, comps = (explode(item, body.get("uom"), qty) if scope == "premix" else (item["unit"], None, None))
    category = (body.get("category") or "").strip()
    if category not in catalog(facility)["categories"]:
        raise WasteError("Pilih kategori waste")
    if not body.get("photo"):
        raise WasteError("Foto evidence wajib untuk setiap item")
    made, expires = body.get("productionDate"), body.get("expiredDate")
    if made and expires and expires < made:
        raise WasteError("Expired date harus setelah production date")
    photo_type, data = parse_photo(body["photo"])
    row = db.fetchrow(
        f"""INSERT INTO {T} (user_id, username, full_name, role, branch_code, branch_name, business_date, scope, item_code,
                item_name, unit, qty, qty_base, exploded, category, note, production_date, expired_date, photo, photo_type, photo_taken_at, latitude, longitude, device, ip)
            VALUES (%(u)s, %(un)s, %(fn)s, %(role)s, %(bc)s, %(bn)s, %(d)s, %(s)s, %(ic)s, %(in)s, %(unit)s, %(q)s, %(g)s, %(x)s::jsonb, %(c)s,
                %(n)s, %(pd)s, %(ed)s, %(p)s, %(pt)s, %(ta)s, %(lat)s, %(lng)s, %(dev)s, %(ip)s)
            RETURNING {COLUMNS.replace('e.', '')}""",
        {"u": user["id"], "un": user["username"], "fn": user.get("full_name"), "role": user["role"], "bc": branch["code"],
         "bn": branch["name"], "d": day, "s": scope, "ic": item["code"], "in": item["name"], "unit": unit, "q": qty, "g": grams,
         "x": json.dumps(comps) if comps is not None else None,
         "c": category, "n": (body.get("note") or "").strip()[:500] or None, "pd": made, "ed": expires, "p": data, "pt": photo_type,
         "ta": body.get("takenAt"), "lat": body.get("latitude"), "lng": body.get("longitude"),
         "dev": (device or "")[:300] or None, "ip": ip})
    return _view(row)


def delete(user: dict, entry_id: str) -> Optional[dict]:
    """The author removes an entry Cost Control has not reviewed yet."""
    return db.fetchrow(f"""DELETE FROM {T} WHERE id::text = %s AND user_id = %s AND status = 'submitted'
                           RETURNING id::text, branch_name, item_name, qty, unit""", (entry_id, user["id"]))


# ---------------------------------------------------------------- monitoring & review

def monitor(date_from: date, date_to: date, branches: Optional[list[str]], scope: str = "", category: str = "",
            status: str = "", user_id: str = "", limit: int = 2000) -> dict:
    where = ["e.business_date BETWEEN %(a)s AND %(b)s"]
    params: dict[str, Any] = {"a": date_from, "b": date_to, "lim": limit}
    if branches is not None:
        where.append("e.branch_code = ANY(%(br)s)")
        params["br"] = branches
    for key, value, allowed in (("scope", scope, SCOPES), ("status", status, STATUSES)):
        if value and value in allowed:
            where.append(f"e.{key} = %({key})s")
            params[key] = value
    if category:
        where.append("e.category = %(cat)s")
        params["cat"] = category
    if user_id:
        where.append("e.user_id::text = %(u)s")
        params["u"] = user_id
    cond = " AND ".join(where)
    rows = db.fetch(f"SELECT {COLUMNS} FROM {T} e WHERE {cond} ORDER BY e.business_date DESC, e.created_at DESC "
                    "LIMIT %(lim)s", params)
    summary = db.fetchrow(
        f"""SELECT count(*)::int AS entries, count(DISTINCT e.branch_code)::int AS branches,
                   count(DISTINCT e.item_code)::int AS items, count(DISTINCT e.user_id)::int AS people,
                   count(*) FILTER (WHERE e.status = 'submitted')::int AS submitted,
                   count(*) FILTER (WHERE e.status = 'approved')::int AS approved,
                   count(*) FILTER (WHERE e.status = 'rejected')::int AS rejected
            FROM {T} e WHERE {cond}""", params)
    by_item = db.fetch(
        f"""SELECT e.scope, e.item_code AS code, max(e.item_name) AS name, e.unit, sum(e.qty)::float AS qty,
                   count(*)::int AS entries, count(DISTINCT e.branch_code)::int AS branches
            FROM {T} e WHERE {cond} AND e.status <> 'rejected' GROUP BY e.scope, e.item_code, e.unit
            ORDER BY count(*) DESC, 3 LIMIT 300""", params)
    by_branch = db.fetch(
        f"""SELECT e.branch_code AS code, max(e.branch_name) AS name, count(*)::int AS entries,
                   count(*) FILTER (WHERE e.status = 'submitted')::int AS submitted,
                   count(DISTINCT e.business_date)::int AS days, max(e.created_at) AS last_at
            FROM {T} e WHERE {cond} GROUP BY e.branch_code ORDER BY 3 DESC, 2""", params)
    by_category = db.fetch(
        f"""SELECT e.category AS name, count(*)::int AS entries FROM {T} e WHERE {cond} AND e.status <> 'rejected'
            GROUP BY e.category ORDER BY 2 DESC, 1""", params)
    for b in by_branch:
        b["last_at"] = b["last_at"].isoformat() if b["last_at"] else None
    return {"data": [_view(r) for r in rows], "summary": summary, "byItem": by_item, "byBranch": by_branch,
            "byCategory": by_category, "truncated": len(rows) >= limit}


def review(ids: list[str], status: str, note: Optional[str], user: dict, branches: Optional[tuple]) -> list[dict]:
    """Approve / reject entries in the reviewer's branches; returns the rows changed."""
    return db.fetch(
        f"""UPDATE {T} SET status = %(s)s, review_note = %(n)s, reviewed_by = %(by)s, reviewed_at = now()
            WHERE id::text = ANY(%(ids)s) AND (%(all)s OR branch_code = ANY(%(br)s))
            RETURNING id::text, branch_name, item_name""",
        {"s": status, "n": (note or "").strip()[:500] or None, "by": user.get("full_name") or user["username"],
         "ids": ids, "all": branches is None, "br": list(branches or [])})


def photo_of(entry_id: str) -> Optional[dict]:
    return db.fetchrow(f"SELECT user_id::text, branch_code, photo, photo_type FROM {T} WHERE id::text = %s", (entry_id,))



def daily(date_from: date, date_to: date, branches: Optional[list[str]]) -> dict:
    """Per branch and day: waste entries, "no waste today", usage items filled; the branches that have not reported."""
    every = branch_options({}, every=True)
    if branches is not None:
        every = [b for b in every if b["code"] in set(branches)]
    p = {"a": date_from, "b": date_to, "br": [b["code"] for b in every]}
    days: dict[tuple, dict] = {}

    def at(code, d):
        return days.setdefault((code, d), {"entries": 0, "noWaste": False, "usage": 0})
    for r in db.fetch(f"""SELECT branch_code, business_date, count(*)::int AS n FROM {T} WHERE business_date
                          BETWEEN %(a)s AND %(b)s AND branch_code = ANY(%(br)s) GROUP BY 1, 2""", p):
        at(r["branch_code"], r["business_date"])["entries"] = r["n"]
    for r in db.fetch(f"""SELECT branch_code, business_date FROM {T_DAY} WHERE no_waste AND business_date
                          BETWEEN %(a)s AND %(b)s AND branch_code = ANY(%(br)s)""", p):
        at(r["branch_code"], r["business_date"])["noWaste"] = True
    for r in db.fetch(f"""SELECT branch_code, business_date, count(*)::int AS n FROM {T_USAGE} WHERE business_date
                          BETWEEN %(a)s AND %(b)s AND branch_code = ANY(%(br)s) GROUP BY 1, 2""", p):
        at(r["branch_code"], r["business_date"])["usage"] = r["n"]
    last = min(date_to, today())
    span = [date_from + timedelta(days=i) for i in range((last - date_from).days + 1)] if last >= date_from else []
    rows, missing = [], []
    for b in every:
        cells = []
        for d in span:
            c = days.get((b["code"], d), {"entries": 0, "noWaste": False, "usage": 0})
            cells.append({"date": d.isoformat(), **c, "reported": c["entries"] > 0 or c["noWaste"]})
        reported = sum(1 for c in cells if c["reported"])
        rows.append({"code": b["code"], "name": b["name"], "days": cells, "reported": reported,
                     "missing": len(cells) - reported, "usageDays": sum(1 for c in cells if c["usage"])})
        if cells and not cells[-1]["reported"]:
            missing.append({"code": b["code"], "name": b["name"]})
    return {"dates": [d.isoformat() for d in span], "branches": rows, "missingLast": missing,
            "lastDate": span[-1].isoformat() if span else None}


def usage_report(date_from: date, date_to: date, branches: Optional[list[str]]) -> list[dict]:
    """The Usage Management quantities of the period (latest first)."""
    where = "business_date BETWEEN %(a)s AND %(b)s" + ("" if branches is None else " AND branch_code = ANY(%(br)s)")
    rows = db.fetch(f"""SELECT branch_code, branch_name, business_date, item_code, item_name, unit, qty::float AS qty,
                               category, note, photo IS NOT NULL AS has_photo,
                               coalesce(full_name, username) AS by, updated_at FROM {T_USAGE} WHERE {where}
                        ORDER BY business_date DESC, branch_name, item_name LIMIT 5000""",
                    {"a": date_from, "b": date_to, "br": branches or []})
    return [{"branchCode": r["branch_code"], "branchName": r["branch_name"], "businessDate": r["business_date"].isoformat(),
             "itemCode": r["item_code"], "itemName": r["item_name"], "unit": r["unit"], "qty": r["qty"], "by": r["by"],
             "category": r.get("category"), "note": r.get("note"), "hasPhoto": bool(r.get("has_photo")),
             "updatedAt": r["updated_at"].isoformat()} for r in rows]
