"""Waste (migration 040): the outlets, HUB and PICs enter each wasted item of their branch; Cost Control monitors and
reviews it.

An entry is one item of one branch and business day (today or yesterday, WIB):
  * scope: waste / usage (Usage Management) items of integration_portal.waste_item (docs/Daily SO & Waste.xlsx), or
    premix: an active premix / WIP recipe of the premix calculator (kc_recipe.recipe), counted in grams;
  * the quantity in the item's unit, a waste category (integration_portal.waste_category), a note / reason;
  * a camera photo as evidence (stamped in the browser with the time and the branch, like the stock opname photos).
The person who entered it may delete it while Cost Control has not reviewed it. Cost Control approves or rejects
entries (one by one or many at once) with a note.
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app import database as db
from app.pic_checkin import CheckInError, branch_options, parse_photo
from app.utils import today

T = "integration_portal.waste_entry"
T_ITEM = "integration_portal.waste_item"
T_CAT = "integration_portal.waste_category"
SCOPES = {"waste": "Waste", "usage": "Usage Management", "premix": "Premix"}
STATUSES = ("submitted", "approved", "rejected")
MAX_QTY = Decimal("1000000")
PREMIX = """SELECT code, name, recipe_type FROM kc_recipe.recipe
            WHERE is_active AND recipe_type IN ('PREMIX', 'WIP')
              AND (coalesce(cardinality(facility_types), 0) = 0 OR 'OUTLET' = ANY(facility_types))
            ORDER BY name"""


class WasteError(CheckInError):
    """An entry refused with a message for the user (HTTP 422)."""


def catalog() -> dict:
    """{"items": {scope: [{code, name, unit}]}, "categories": [name]} for the form."""
    items: dict[str, list[dict]] = {s: [] for s in SCOPES}
    for r in db.fetch(f"SELECT scope, code, name, unit FROM {T_ITEM} WHERE is_active ORDER BY name"):
        items[r["scope"]].append({"code": r["code"], "name": r["name"], "unit": r["unit"]})
    try:
        items["premix"] = [{"code": r["code"], "name": r["name"], "unit": "GR", "type": r["recipe_type"]}
                           for r in db.fetch(PREMIX)]
    except Exception:  # noqa: BLE001 - kc_recipe unreachable: the form still works for waste and usage
        items["premix"] = []
    cats = [r["name"] for r in db.fetch(f"SELECT name FROM {T_CAT} WHERE is_active ORDER BY sort, name")]
    return {"items": items, "categories": cats, "scopes": SCOPES}


def _item(scope: str, code: str) -> Optional[dict]:
    return next((i for i in catalog()["items"].get(scope, []) if i["code"] == code), None)


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
    e.scope, e.item_code, e.item_name, e.unit, e.qty, e.category, e.note, e.photo_taken_at, e.latitude, e.longitude,
    e.status, e.review_note, e.reviewed_by, e.reviewed_at, e.created_at"""


def _view(r: dict) -> dict:
    num = lambda v: float(v) if v is not None else None  # noqa: E731
    return {"id": r["id"], "userId": r["user_id"], "username": r["username"], "name": r["full_name"] or r["username"],
            "branchCode": r["branch_code"], "branchName": r["branch_name"], "businessDate": r["business_date"].isoformat(),
            "scope": r["scope"], "itemCode": r["item_code"], "itemName": r["item_name"], "unit": r["unit"],
            "qty": float(r["qty"]), "category": r["category"], "note": r["note"],
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
    out = {"branches": branches, "days": {"from": first.isoformat(), "to": last.isoformat()}, **catalog(), "data": []}
    codes = {b["code"] for b in branches}
    if branch and branch in codes:
        rows = db.fetch(f"SELECT {COLUMNS} FROM {T} e WHERE e.branch_code = %s AND e.business_date = %s "
                        "ORDER BY e.created_at DESC", (branch, day or last))
        out["data"] = [_view(r) for r in rows]
    return out


def create(user: dict, body: dict, device: Optional[str], ip: Optional[str]) -> dict:
    branch = next((b for b in branch_options(user) if b["code"] == body.get("branchCode")), None)
    if branch is None:
        raise WasteError("Cabang tidak tersedia untuk akun Anda")
    first, last = allowed_days()
    day = body.get("businessDate") or last
    if not first <= day <= last:
        raise WasteError("Waste hanya bisa diinput untuk hari ini atau kemarin")
    scope = body.get("scope")
    if scope not in SCOPES:
        raise WasteError("Jenis waste tidak dikenal")
    item = _item(scope, body.get("itemCode") or "")
    if item is None:
        raise WasteError("Item tidak ada di daftar waste")
    qty = _qty(body.get("qty"))
    category = (body.get("category") or "").strip()
    if category not in catalog()["categories"]:
        raise WasteError("Pilih kategori waste")
    if not body.get("photo"):
        raise WasteError("Foto evidence wajib untuk setiap item")
    photo_type, data = parse_photo(body["photo"])
    row = db.fetchrow(
        f"""INSERT INTO {T} (user_id, username, full_name, role, branch_code, branch_name, business_date, scope, item_code,
                item_name, unit, qty, category, note, photo, photo_type, photo_taken_at, latitude, longitude, device, ip)
            VALUES (%(u)s, %(un)s, %(fn)s, %(role)s, %(bc)s, %(bn)s, %(d)s, %(s)s, %(ic)s, %(in)s, %(unit)s, %(q)s, %(c)s,
                %(n)s, %(p)s, %(pt)s, %(ta)s, %(lat)s, %(lng)s, %(dev)s, %(ip)s)
            RETURNING {COLUMNS.replace('e.', '')}""",
        {"u": user["id"], "un": user["username"], "fn": user.get("full_name"), "role": user["role"], "bc": branch["code"],
         "bn": branch["name"], "d": day, "s": scope, "ic": item["code"], "in": item["name"], "unit": item["unit"], "q": qty,
         "c": category, "n": (body.get("note") or "").strip()[:500] or None, "p": data, "pt": photo_type,
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

