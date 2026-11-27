"""Building & Renovation (2026-11-27, Building team; page mon.building, migration 162).

The material catalog (group → category → material: spec, detail, unit, price with its date, the store) and the directory
of stores and suppliers. Every price keeps its history (building_price): a price changed is a new row, so an estimate
can say how old the price it uses is (STALE_DAYS). A material or a vendor is not deleted, it is made inactive (an
estimate may still point to it). Projects, RAB and progress come next.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app import database as db

P = "integration_portal"
WIB = ZoneInfo("Asia/Jakarta")
GROUPS = ("Sipil", "MEP", "CCTV, Alarm & Internet", "Alat Kerja", "Furniture", "Lighting")
KINDS = {"STORE": "Toko", "SUPPLIER": "Supplier"}
STALE_DAYS = 90  # a price older than this is shown as "to check"
VENDOR_FIELDS = ("name", "location", "goods", "contact_name", "phone", "maps", "website", "bank", "account_no", "account_name", "note")


class BuildingError(ValueError):
    """Refused with a message for the user (HTTP 422)."""


def _today() -> date:
    return datetime.now(WIB).date()


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def _text(v: Any, n: int = 300) -> Optional[str]:
    s = " ".join(str(v).split()) if v is not None else ""
    return s[:n] or None


def price_of(v: Any) -> Optional[Decimal]:
    """A price typed in (\"Rp 1.250.000\", 1250000, \"1,250,000.50\"): None when empty; pure (tested)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, (int, float, Decimal)):
        d = Decimal(str(v))
    else:
        s = "".join(ch for ch in str(v) if ch.isdigit() or ch in ".,")
        if "," in s and "." in s:  # the last one is the decimal mark
            dec = "," if s.rindex(",") > s.rindex(".") else "."
            s = s.replace("." if dec == "," else ",", "").replace(",", ".")
        elif s.count(".") > 1 or (s.count(".") == 1 and len(s.split(".")[1]) == 3):
            s = s.replace(".", "")  # 1.250.000 / 125.000
        elif s.count(",") > 1 or (s.count(",") == 1 and len(s.split(",")[1]) == 3):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
        try:
            d = Decimal(s)
        except InvalidOperation:
            raise BuildingError("Harga tidak valid") from None
    if d < 0 or d > Decimal("100000000000"):
        raise BuildingError("Harga tidak valid")
    return d.quantize(Decimal("0.01"))


def age_days(price_on: Optional[date], today: Optional[date] = None) -> Optional[int]:
    return None if price_on is None else ((today or _today()) - price_on).days


# ---------------------------------------------------------------- vendors

def _vendor_view(r: dict) -> dict:
    return {"id": r["id"], "kind": r["kind"], "kindLabel": KINDS[r["kind"]], "name": r["name"], "location": r["location"], "goods": r["goods"],
            "contactName": r["contact_name"], "phone": r["phone"], "maps": r["maps"], "website": r["website"], "bank": r["bank"],
            "accountNo": r["account_no"], "accountName": r["account_name"], "note": r["note"], "active": r["is_active"],
            "materials": int(r.get("materials") or 0), "updatedAt": _iso(r["updated_at"]), "updatedBy": r["updated_by"]}


def vendors(include_inactive: bool = False) -> list[dict]:
    rows = db.fetch(f"""SELECT v.*, (SELECT count(*) FROM {P}.building_material m WHERE m.vendor_id = v.id AND m.is_active) AS materials
                          FROM {P}.building_vendor v {'' if include_inactive else 'WHERE v.is_active'} ORDER BY v.kind, v.location NULLS LAST, v.name""")
    return [_vendor_view(r) for r in rows]


def save_vendor(user: dict, vendor_id: Optional[int], body: dict) -> dict:
    kind = str(body.get("kind") or "STORE").upper()
    if kind not in KINDS:
        raise BuildingError("Jenis harus Toko atau Supplier")
    vals = {f: _text(body.get(f), 500 if f in ("maps", "website", "note") else 200) for f in VENDOR_FIELDS}
    if not vals["name"]:
        raise BuildingError("Nama toko / supplier wajib diisi")
    by = user.get("full_name") or user.get("username")
    cols = ("kind",) + VENDOR_FIELDS
    args = [kind] + [vals[f] for f in VENDOR_FIELDS]
    with db.transaction() as conn:
        if vendor_id is None:
            row = conn.execute(f"""INSERT INTO {P}.building_vendor ({', '.join(cols)}, updated_by) VALUES ({', '.join(['%s'] * len(cols))}, %s)
                                   RETURNING id""", (*args, by)).fetchone()
        else:
            row = conn.execute(f"""UPDATE {P}.building_vendor SET {', '.join(f'{c} = %s' for c in cols)}, is_active = %s, updated_at = now(), updated_by = %s
                                   WHERE id = %s RETURNING id""", (*args, bool(body.get("active", True)), by, vendor_id)).fetchone()
            if row is None:
                raise LookupError("Toko / supplier tidak ditemukan")
    return next(v for v in vendors(True) if v["id"] == row["id"])


# ---------------------------------------------------------------- materials

def _material_view(r: dict, today: date) -> dict:
    age = age_days(r["price_on"], today)
    return {"id": r["id"], "group": r["grp"], "category": r["category"], "spec": r["spec"], "detail": r["detail"], "unit": r["unit"],
            "price": float(r["price"]) if r["price"] is not None else None, "priceOn": _iso(r["price_on"]), "priceAge": age,
            "stale": age is not None and age > STALE_DAYS, "vendorId": r["vendor_id"], "vendor": r.get("vendor_name") or r["vendor_text"],
            "vendorPhone": r.get("vendor_phone"), "note": r["note"], "repeat": r["repeat"], "active": r["is_active"],
            "updatedAt": _iso(r["updated_at"]), "updatedBy": r["updated_by"]}


def materials(include_inactive: bool = False) -> dict:
    today = _today()
    rows = db.fetch(f"""SELECT m.*, v.name AS vendor_name, v.phone AS vendor_phone FROM {P}.building_material m
                          LEFT JOIN {P}.building_vendor v ON v.id = m.vendor_id
                         {'' if include_inactive else 'WHERE m.is_active'}
                         ORDER BY array_position(%s::text[], m.grp), m.grp, m.category, m.spec NULLS FIRST, m.detail NULLS FIRST, m.id""", (list(GROUPS),))
    data = [_material_view(r, today) for r in rows]
    return {"data": data, "groups": list(dict.fromkeys([*GROUPS, *(m["group"] for m in data)])), "staleDays": STALE_DAYS}


def save_material(user: dict, material_id: Optional[int], body: dict) -> dict:
    grp, category = _text(body.get("group"), 60), _text(body.get("category"), 80)
    if not grp or not category:
        raise BuildingError("Kelompok dan kategori wajib diisi")
    price = price_of(body.get("price"))
    price_on = date.fromisoformat(str(body["priceOn"])) if body.get("priceOn") else (_today() if price is not None else None)
    if price_on and price_on > _today():
        raise BuildingError("Tanggal harga tidak boleh setelah hari ini")
    vendor_id = int(body["vendorId"]) if body.get("vendorId") else None
    if vendor_id and not db.fetchrow(f"SELECT 1 AS ok FROM {P}.building_vendor WHERE id = %s", (vendor_id,)):
        raise BuildingError("Toko / supplier tidak ditemukan")
    by = user.get("full_name") or user.get("username")
    vals = (grp, category, _text(body.get("spec"), 200), _text(body.get("detail"), 300), _text(body.get("unit"), 30), price, price_on,
            vendor_id, None if vendor_id else _text(body.get("vendor"), 200), _text(body.get("note"), 500), bool(body.get("repeat")))
    with db.transaction() as conn:
        if material_id is None:
            row = conn.execute(f"""INSERT INTO {P}.building_material (grp, category, spec, detail, unit, price, price_on, vendor_id, vendor_text, note, repeat, updated_by)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", (*vals, by)).fetchone()
            old = None
        else:
            old = conn.execute(f"SELECT price, price_on FROM {P}.building_material WHERE id = %s FOR UPDATE", (material_id,)).fetchone()
            if old is None:
                raise LookupError("Material tidak ditemukan")
            row = conn.execute(f"""UPDATE {P}.building_material SET grp = %s, category = %s, spec = %s, detail = %s, unit = %s, price = %s, price_on = %s,
                                          vendor_id = %s, vendor_text = %s, note = %s, repeat = %s, is_active = %s, updated_at = now(), updated_by = %s
                                    WHERE id = %s RETURNING id""", (*vals, bool(body.get("active", True)), by, material_id)).fetchone()
        if price is not None and (old is None or old["price"] != price or old["price_on"] != price_on):
            conn.execute(f"""INSERT INTO {P}.building_price (material_id, price, price_on, vendor_id, source, note, created_by)
                             VALUES (%s, %s, %s, %s, 'MANUAL', %s, %s)""", (row["id"], price, price_on, vendor_id, _text(body.get("priceNote"), 300), by))
    return next(m for m in materials(True)["data"] if m["id"] == row["id"])


def prices(material_id: int) -> list[dict]:
    return [{"price": float(r["price"]), "priceOn": _iso(r["price_on"]), "vendor": r["vendor"], "source": r["source"], "note": r["note"],
             "by": r["created_by"], "at": _iso(r["created_at"])}
            for r in db.fetch(f"""SELECT p.*, v.name AS vendor FROM {P}.building_price p LEFT JOIN {P}.building_vendor v ON v.id = p.vendor_id
                                   WHERE p.material_id = %s ORDER BY p.price_on DESC, p.id DESC""", (material_id,))]


def summary() -> dict:
    r = db.fetchrow(f"""SELECT count(*) FILTER (WHERE is_active) AS materials,
                               count(*) FILTER (WHERE is_active AND price IS NULL) AS no_price,
                               count(*) FILTER (WHERE is_active AND price_on < %s) AS stale,
                               count(DISTINCT category) FILTER (WHERE is_active) AS categories
                          FROM {P}.building_material""", (_today() - timedelta(days=STALE_DAYS),))
    v = db.fetchrow(f"""SELECT count(*) FILTER (WHERE kind = 'STORE' AND is_active) AS stores, count(*) FILTER (WHERE kind = 'SUPPLIER' AND is_active) AS suppliers
                          FROM {P}.building_vendor""")
    return {k: int(x or 0) for k, x in {**r, **v}.items()}
