"""Calf On The Road (migration 046): the drivers' day (app/routes/cotr.py, driver app fe-calfontheroad) and the masters
and monitoring of the COTR team (portal pages cotr.*).

A driver account (role "driver") is tied to one row of cotr_driver. Its day is one shift: the start check in (photo,
location, the cups loaded per menu), sales (price of the menu at that moment, cash or QRIS; voided with a reason),
reloads (asked by the driver, approved or rejected by the pool in the portal, received by the driver) and the end
check in (the cups counted, set against the stock the system expects; a difference needs a note). The stock on the
fleet is never stored: start stock + reloads received - sales not voided. Costs on the road (fuel, ice …) are sent
with the receipt and approved or rejected in the portal.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Optional

from psycopg.types.json import Jsonb

from app import database as db
from app.pic_checkin import CheckInError, parse_photo
from app.utils import today

P = "integration_portal"
PAYMENTS = ("cash", "qris")
REQUEST_KINDS = ("fuel", "ice", "repair", "parking", "other")
MAX_QTY = 1000  # cups in one line


class CotrError(CheckInError):
    """Refused with a message for the driver / the team (HTTP 422)."""


# ---------------------------------------------------------------- pure rules (tested without a database)

Stock = dict[str, int]


def clean_stock(value: Any, menus: Iterable[str]) -> Stock:
    """{menu code: cups} of the known menus, positive whole numbers only."""
    known = set(menus)
    out: Stock = {}
    for code, qty in (value or {}).items():
        if code not in known:
            raise CotrError(f"Menu {code} tidak dikenal")
        try:
            n = int(qty)
        except (TypeError, ValueError):
            raise CotrError(f"Jumlah {code} harus angka") from None
        if n < 0 or n > MAX_QTY:
            raise CotrError(f"Jumlah {code} harus 0 sampai {MAX_QTY}")
        if n:
            out[code] = n
    return out


def stock_now(start: Stock, reloads: Iterable[dict], sales: Iterable[dict]) -> Stock:
    """Cups on the fleet: start + reloads received - sales not voided (never below zero)."""
    out = {k: int(v) for k, v in (start or {}).items()}
    for r in reloads:
        if r["status"] == "received":
            for k, v in r["lines"].items():
                out[k] = out.get(k, 0) + int(v)
    for s in sales:
        if not s.get("voided_at"):
            for line in s["lines"]:
                out[line["code"]] = out.get(line["code"], 0) - int(line["qty"])
    return {k: max(0, v) for k, v in out.items() if max(0, v) or k in (start or {})}


def sale_lines(lines: list[dict], menus: dict[str, dict], stock: Stock) -> tuple[list[dict], Decimal]:
    """The lines as sold (name and price of the menu now), refused beyond the stock on the fleet."""
    out, total, seen = [], Decimal(0), set()
    for line in lines:
        code, qty = line.get("code"), int(line.get("qty") or 0)
        menu = menus.get(code)
        if not menu or not menu["is_active"]:
            raise CotrError(f"Menu {code} tidak dijual")
        if code in seen:
            raise CotrError(f"Menu {menu['name']} dua kali")
        seen.add(code)
        if qty <= 0 or qty > MAX_QTY:
            raise CotrError(f"Jumlah {menu['name']} tidak valid")
        if qty > stock.get(code, 0):
            raise CotrError(f"Stok {menu['name']} tinggal {stock.get(code, 0)}")
        price = Decimal(menu["price"])
        out.append({"code": code, "name": menu["name"], "qty": qty, "price": float(price)})
        total += price * qty
    if not out:
        raise CotrError("Pilih minimal satu menu")
    return out, total


def difference(counted: Stock, expected: Stock) -> dict[str, int]:
    """Counted - expected per menu, only where they differ."""
    keys = set(counted) | set(expected)
    return {k: counted.get(k, 0) - expected.get(k, 0) for k in sorted(keys) if counted.get(k, 0) != expected.get(k, 0)}


def recap(shift: dict, sales: list[dict], reloads: list[dict], requests: list[dict]) -> dict:
    """The figures of a shift: cups, cash and QRIS, voids, reloads received, requests, the difference at the end."""
    ok = [s for s in sales if not s.get("voided_at")]
    by_pay = {p: float(sum(Decimal(s["total"]) for s in ok if s["payment"] == p)) for p in PAYMENTS}
    per_menu: dict[str, dict] = {}
    for s in ok:
        for line in s["lines"]:
            m = per_menu.setdefault(line["code"], {"code": line["code"], "name": line["name"], "qty": 0, "total": 0.0})
            m["qty"] += int(line["qty"])
            m["total"] += float(line["price"]) * int(line["qty"])
    diff = difference(shift.get("end_stock") or {}, shift.get("expected_stock") or {}) if shift.get("end_stock") is not None else None
    return {
        "cups": sum(m["qty"] for m in per_menu.values()), "total": sum(by_pay.values()), "cash": by_pay["cash"],
        "qris": by_pay["qris"], "voided": len(sales) - len(ok), "perMenu": sorted(per_menu.values(), key=lambda m: -m["qty"]),
        "reloaded": sum(sum(int(v) for v in r["lines"].values()) for r in reloads if r["status"] == "received"),
        "requested": float(sum(Decimal(r["amount"]) for r in requests if r["status"] != "rejected")),
        "difference": diff,
    }


def explode(boms: dict[str, dict], code: str, qty: float, out: dict[str, dict], depth: int = 0) -> None:
    """Raw materials of `qty` outputs of a BOM; a line that is the output of another BOM is exploded through it."""
    bom = boms.get(code)
    if not bom or depth > 5:
        return
    by_output = {b["output"]: b for b in boms.values()}
    for line in bom["lines"]:
        need = float(line["qty"]) * qty / float(bom["yield_qty"])
        sub = by_output.get(line["item"])
        if sub and sub["code"] != code:
            explode(boms, sub["code"], need, out, depth + 1)
        else:
            cur = out.setdefault(line["item"], {"item": line["item"], "qty": 0.0, "unit": line["unit"]})
            cur["qty"] += need


def plan(menus: list[dict], sales_per_day: dict[str, list[int]], boms: dict[str, dict], buffer_pct: float) -> dict:
    """Production plan: average cups per day + buffer per menu, exploded to raw materials (rounded up)."""
    import math
    rows, materials = [], {}
    for m in menus:
        q = sales_per_day.get(m["code"], [])
        avg = sum(q) / len(q) if q else 0.0
        cups = math.ceil(round(avg * (1 + buffer_pct / 100), 6))  # 100 x 1.1 is 110.00000000000001
        rows.append({"code": m["code"], "name": m["name"], "average": round(avg, 1), "highest": max(q, default=0),
                     "plan": cups, "bom": m.get("bom_code")})
        if m.get("bom_code"):
            explode(boms, m["bom_code"], cups, materials)
    mats = sorted(({**v, "qty": math.ceil(round(v["qty"], 6))} for v in materials.values()), key=lambda v: -v["qty"])
    return {"menus": rows, "materials": mats}


# ---------------------------------------------------------------- reads

def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def menus(active_only: bool = False) -> list[dict]:
    where = "WHERE is_active" if active_only else ""
    return db.fetch(f"SELECT code, name, category, price, bom_code, is_active, sort FROM {P}.cotr_menu {where} ORDER BY sort, name")


def driver_of(user: dict) -> Optional[dict]:
    return db.fetchrow(
        f"""SELECT d.*, p.name AS pool_name, f.type AS fleet_type, f.plate AS fleet_plate
              FROM {P}.cotr_driver d JOIN {P}.cotr_pool p ON p.code = d.pool_code
              LEFT JOIN {P}.cotr_fleet f ON f.code = d.fleet_code WHERE d.user_id = %s""", (user["id"],))


def require_driver(user: dict) -> dict:
    d = driver_of(user)
    if not d:
        raise CotrError("Akun ini belum terhubung ke data driver. Hubungi tim COTR.")
    if d["status"] == "inactive":
        raise CotrError("Driver tidak aktif")
    return d


def shift_of(driver_code: str, day: date) -> Optional[dict]:
    return db.fetchrow(f"SELECT * FROM {P}.cotr_shift WHERE driver_code = %s AND day = %s", (driver_code, day))


def _parts(shift_id: str) -> tuple[list[dict], list[dict]]:
    sales = db.fetch(f"SELECT * FROM {P}.cotr_sale WHERE shift_id = %s ORDER BY at DESC", (shift_id,))
    reloads = db.fetch(f"SELECT * FROM {P}.cotr_reload WHERE shift_id = %s ORDER BY at DESC", (shift_id,))
    return sales, reloads


def _sale_view(s: dict) -> dict:
    return {"id": str(s["id"]), "at": _iso(s["at"]), "lines": s["lines"], "payment": s["payment"], "total": float(s["total"]),
            "voided": {"at": _iso(s["voided_at"]), "reason": s["void_reason"]} if s["voided_at"] else None}


def _reload_view(r: dict) -> dict:
    return {"id": str(r["id"]), "at": _iso(r["at"]), "lines": r["lines"], "note": r["note"], "status": r["status"],
            "decidedAt": _iso(r["decided_at"]), "decisionNote": r["decision_note"], "receivedAt": _iso(r["received_at"])}


def _request_view(r: dict) -> dict:
    return {"id": str(r["id"]), "at": _iso(r["at"]), "kind": r["kind"], "amount": float(r["amount"]), "note": r["note"],
            "status": r["status"], "decidedAt": _iso(r["decided_at"]), "decisionNote": r["decision_note"],
            "driverCode": r["driver_code"]}


def _driver_view(d: dict) -> dict:
    return {"code": d["code"], "name": d["name"], "phone": d["phone"], "pool": d["pool_code"], "poolName": d.get("pool_name"),
            "fleet": d["fleet_code"], "fleetType": d.get("fleet_type"), "fleetPlate": d.get("fleet_plate"), "status": d["status"]}


def today_of(user: dict) -> dict:
    """Everything the driver app shows: the driver, the menus, today's shift with its stock, sales, reloads, requests."""
    d = require_driver(user)
    s = shift_of(d["code"], today())
    sales, reloads = _parts(s["id"]) if s else ([], [])
    reqs = db.fetch(f"SELECT * FROM {P}.cotr_request WHERE driver_code = %s AND at > now() - interval '30 days' ORDER BY at DESC",
                    (d["code"],))
    shift = None
    if s:
        shift = {"id": str(s["id"]), "day": _iso(s["day"]), "status": s["status"], "startedAt": _iso(s["started_at"]),
                 "endedAt": _iso(s["ended_at"]), "startStock": s["start_stock"], "endStock": s["end_stock"],
                 "expectedStock": s["expected_stock"], "endNote": s["end_note"]}
    return {
        "driver": _driver_view(d),
        "menus": [{"code": m["code"], "name": m["name"], "price": float(m["price"])} for m in menus(active_only=True)],
        "shift": shift,
        "stock": stock_now(s["start_stock"], reloads, sales) if s else {},
        "sales": [_sale_view(x) for x in sales],
        "reloads": [_reload_view(x) for x in reloads],
        "requests": [_request_view(x) for x in reqs],
        "recap": recap(s, sales, reloads, [r for r in reqs if s and r["shift_id"] == s["id"]]) if s else None,
    }


# ---------------------------------------------------------------- the driver's actions

def _menu_map() -> dict[str, dict]:
    return {m["code"]: m for m in menus()}


def _open_shift(d: dict) -> dict:
    s = shift_of(d["code"], today())
    if not s or s["status"] != "open":
        raise CotrError("Belum ada shift yang berjalan. Check in dulu.")
    return s


def check_in(user: dict, kind: str, photo: str, lat: float, lng: float, accuracy: Optional[float], stock: dict,
             note: Optional[str]) -> dict:
    d = require_driver(user)
    ptype, pdata = parse_photo(photo)
    counted = clean_stock(stock, _menu_map())
    note = (note or "").strip() or None
    with db.transaction() as conn:
        s = conn.execute(f"SELECT * FROM {P}.cotr_shift WHERE driver_code = %s AND day = %s FOR UPDATE", (d["code"], today())).fetchone()
        if kind == "start":
            if s:
                raise CotrError("Shift hari ini sudah dimulai")
            if not counted:
                raise CotrError("Isi produk yang dibawa")
            s = conn.execute(
                f"""INSERT INTO {P}.cotr_shift (driver_code, day, pool_code, fleet_code, start_stock)
                    VALUES (%s, %s, %s, %s, %s) RETURNING *""",
                (d["code"], today(), d["pool_code"], d["fleet_code"], Jsonb(counted))).fetchone()
        else:
            if not s or s["status"] != "open":
                raise CotrError("Belum ada shift yang berjalan")
            sales = conn.execute(f"SELECT * FROM {P}.cotr_sale WHERE shift_id = %s", (s["id"],)).fetchall()
            reloads = conn.execute(f"SELECT * FROM {P}.cotr_reload WHERE shift_id = %s", (s["id"],)).fetchall()
            expected = stock_now(s["start_stock"], reloads, sales)
            if difference(counted, expected) and not note:
                raise CotrError("Ada selisih dengan stok sistem: tulis penjelasannya di catatan")
            conn.execute(
                f"""UPDATE {P}.cotr_shift SET status = 'closed', ended_at = now(), end_stock = %s, expected_stock = %s,
                        end_note = %s WHERE id = %s""", (Jsonb(counted), Jsonb(expected), note, s["id"]))
        conn.execute(
            f"""INSERT INTO {P}.cotr_check_in (shift_id, kind, latitude, longitude, accuracy_m, photo, photo_type, stock, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (s["id"], kind, lat, lng, accuracy, pdata, ptype, Jsonb(counted), note))
    return today_of(user)


def sell(user: dict, lines: list[dict], payment: str) -> dict:
    if payment not in PAYMENTS:
        raise CotrError("Pilih Tunai atau QRIS")
    d = require_driver(user)
    with db.transaction() as conn:
        s = conn.execute(f"SELECT * FROM {P}.cotr_shift WHERE driver_code = %s AND day = %s FOR UPDATE", (d["code"], today())).fetchone()
        if not s or s["status"] != "open":
            raise CotrError("Belum ada shift yang berjalan. Check in dulu.")
        sales = conn.execute(f"SELECT * FROM {P}.cotr_sale WHERE shift_id = %s", (s["id"],)).fetchall()
        reloads = conn.execute(f"SELECT * FROM {P}.cotr_reload WHERE shift_id = %s", (s["id"],)).fetchall()
        sold, total = sale_lines(lines, _menu_map(), stock_now(s["start_stock"], reloads, sales))
        conn.execute(f"INSERT INTO {P}.cotr_sale (shift_id, lines, payment, total) VALUES (%s, %s, %s, %s)",
                     (s["id"], Jsonb(sold), payment, total))
    return today_of(user)


def void_sale(user: dict, sale_id: str, reason: str) -> dict:
    reason = (reason or "").strip()
    if not reason:
        raise CotrError("Tulis alasan pembatalan")
    d = require_driver(user)
    s = _open_shift(d)
    row = db.fetchrow(f"""UPDATE {P}.cotr_sale SET voided_at = now(), void_reason = %s
                           WHERE id = %s AND shift_id = %s AND voided_at IS NULL RETURNING id""", (reason, sale_id, s["id"]))
    if not row:
        raise CotrError("Transaksi tidak ditemukan atau sudah dibatalkan")
    return today_of(user)


def request_reload(user: dict, lines: dict, note: Optional[str]) -> dict:
    d = require_driver(user)
    s = _open_shift(d)
    asked = clean_stock(lines, _menu_map())
    if not asked:
        raise CotrError("Isi jumlah yang diajukan")
    db.fetchrow(f"INSERT INTO {P}.cotr_reload (shift_id, lines, note) VALUES (%s, %s, %s) RETURNING id",
                (s["id"], Jsonb(asked), (note or "").strip() or None))
    return today_of(user)


def receive_reload(user: dict, reload_id: str) -> dict:
    d = require_driver(user)
    s = _open_shift(d)
    row = db.fetchrow(f"""UPDATE {P}.cotr_reload SET status = 'received', received_at = now()
                           WHERE id = %s AND shift_id = %s AND status = 'approved' RETURNING id""", (reload_id, s["id"]))
    if not row:
        raise CotrError("Reload belum disetujui pool atau sudah diterima")
    return today_of(user)


def send_request(user: dict, kind: str, amount: Any, note: str, photo: str) -> dict:
    if kind not in REQUEST_KINDS:
        raise CotrError("Jenis pengajuan tidak dikenal")
    try:
        value = Decimal(str(amount))
    except Exception:
        raise CotrError("Nominal tidak valid") from None
    if value <= 0 or value > Decimal(100_000_000):
        raise CotrError("Nominal tidak valid")
    if not (note or "").strip():
        raise CotrError("Tulis keterangan")
    d = require_driver(user)
    ptype, pdata = parse_photo(photo)
    s = shift_of(d["code"], today())
    db.fetchrow(f"""INSERT INTO {P}.cotr_request (driver_code, shift_id, kind, amount, note, photo, photo_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (d["code"], s["id"] if s else None, kind, value, note.strip(), pdata, ptype))
    return today_of(user)


# ---------------------------------------------------------------- the portal: masters

# entity -> (table, key column, columns written from the payload, ordering)
MASTERS: dict[str, tuple[str, str, tuple[str, ...], str]] = {
    "pools": ("cotr_pool", "code", ("code", "name", "kind", "parent_code", "city", "address", "storage", "is_active"), "kind, name"),
    "fleet": ("cotr_fleet", "code", ("code", "type", "plate", "pool_code", "capacity", "condition", "last_service", "is_active"), "code"),
    "menus": ("cotr_menu", "code", ("code", "name", "category", "price", "bom_code", "is_active", "sort"), "sort, name"),
    "boms": ("cotr_bom", "code", ("code", "name", "output", "yield_qty", "yield_unit", "lines"), "name"),
    "drivers": ("cotr_driver", "code", ("code", "name", "phone", "pool_code", "fleet_code", "status", "joined_on", "user_id"), "code"),
}


def _plain(row: dict) -> dict:
    return {k: (float(v) if isinstance(v, Decimal) else _iso(v)) for k, v in row.items()
            if k not in ("updated_by",)}


def master_list(entity: str) -> list[dict]:
    table, _, _, order = MASTERS[entity]
    if entity == "drivers":
        rows = db.fetch(f"""SELECT d.*, u.username AS account FROM {P}.cotr_driver d
                              LEFT JOIN {P}.user_account u ON u.id = d.user_id ORDER BY d.code""")
    else:
        rows = db.fetch(f"SELECT * FROM {P}.{table} ORDER BY {order}")
    out = [_plain(r) for r in rows]
    for r in out:
        if "user_id" in r and r["user_id"] is not None:
            r["user_id"] = str(r["user_id"])
    return out


def _value(col: str, v: Any) -> Any:
    if v == "":
        v = None
    if col == "lines":
        lines = [{"item": str(x.get("item", "")).strip(), "qty": float(x.get("qty") or 0), "unit": str(x.get("unit") or "")}
                 for x in (v or []) if str(x.get("item", "")).strip()]
        if any(x["qty"] <= 0 for x in lines):
            raise CotrError("Qty komponen harus lebih dari 0")
        return Jsonb(lines)
    if col == "storage":
        return [s.strip() for s in (v.split(",") if isinstance(v, str) else (v or [])) if str(s).strip()]
    return v


def master_save(user: dict, entity: str, key: Optional[str], data: dict) -> dict:
    """Create (key None) or update one master row; the code may change (references follow it)."""
    table, pk, cols, _ = MASTERS[entity]
    row = {c: _value(c, data.get(c)) for c in cols if c in data}
    if not (row.get(pk) or key):
        raise CotrError("Kode wajib diisi")
    if "code" in row and row["code"]:
        row["code"] = str(row["code"]).strip().upper()
    names = list(row)
    try:
        if key is None:
            sql = (f"INSERT INTO {P}.{table} ({', '.join(names)}, updated_by) VALUES ({', '.join(['%s'] * len(names))}, %s) "
                   f"RETURNING *")
            saved = db.fetchrow(sql, (*row.values(), user["id"]))
        else:
            sets = ", ".join(f"{n} = %s" for n in names)
            saved = db.fetchrow(f"UPDATE {P}.{table} SET {sets}, updated_at = now(), updated_by = %s WHERE {pk} = %s RETURNING *",
                                (*row.values(), user["id"], key))
    except Exception as e:  # unique / foreign key / check violations come back as a message
        raise CotrError(_db_message(e)) from None
    if not saved:
        raise CotrError("Data tidak ditemukan")
    return _plain(saved)


def master_delete(entity: str, key: str) -> None:
    table, pk, _, _ = MASTERS[entity]
    try:
        db.fetchrow(f"DELETE FROM {P}.{table} WHERE {pk} = %s RETURNING {pk}", (key,))
    except Exception as e:
        raise CotrError(_db_message(e)) from None


def _db_message(e: Exception) -> str:
    text = str(e)
    if "unique" in text or "duplicate key" in text:
        return "Kode sudah dipakai"
    if "foreign key" in text:
        return "Masih dipakai data lain, atau referensi (pool / armada / BOM / akun) tidak ada"
    if "check constraint" in text:
        return "Isian tidak valid"
    return "Data tidak dapat disimpan"


def driver_accounts() -> list[dict]:
    """Accounts of role driver, with the driver each one is tied to (Master Driver: the account select)."""
    return [{"id": str(r["id"]), "username": r["username"], "name": r["full_name"], "driver": r["driver"]}
            for r in db.fetch(f"""SELECT u.id, u.username, u.full_name, d.code AS driver FROM {P}.user_account u
                                    LEFT JOIN {P}.cotr_driver d ON d.user_id = u.id
                                   WHERE u.role = 'driver' AND u.is_active ORDER BY u.username""")]


def waste_list(day_from: date, day_to: date) -> list[dict]:
    return [_plain(r) | {"id": str(r["id"])} for r in db.fetch(
        f"SELECT * FROM {P}.cotr_waste WHERE day BETWEEN %s AND %s ORDER BY day DESC, created_at DESC", (day_from, day_to))]


def waste_add(user: dict, data: dict) -> dict:
    try:
        r = db.fetchrow(f"""INSERT INTO {P}.cotr_waste (day, pool_code, item, qty, unit, reason, created_by)
                            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                        (data.get("day") or today(), data.get("pool_code"), (data.get("item") or "").strip(), data.get("qty"),
                         data.get("unit"), (data.get("reason") or "").strip(), user["id"]))
    except Exception as e:
        raise CotrError(_db_message(e)) from None
    return _plain(r) | {"id": str(r["id"])}


def waste_delete(waste_id: str) -> None:
    db.fetchrow(f"DELETE FROM {P}.cotr_waste WHERE id = %s RETURNING id", (waste_id,))


# ---------------------------------------------------------------- the portal: monitoring

def monitor(day: date) -> dict:
    """The shifts of a day (every driver, with its check ins, stock, recap), the reloads and requests to decide."""
    drivers = {d["code"]: d for d in db.fetch(
        f"""SELECT d.*, p.name AS pool_name FROM {P}.cotr_driver d JOIN {P}.cotr_pool p ON p.code = d.pool_code ORDER BY d.code""")}
    shifts = db.fetch(f"SELECT * FROM {P}.cotr_shift WHERE day = %s", (day,))
    ids = [s["id"] for s in shifts]
    checkins = db.fetch(f"""SELECT id, shift_id, kind, at, latitude, longitude, accuracy_m, stock, note, photo IS NOT NULL AS has_photo
                              FROM {P}.cotr_check_in WHERE shift_id = ANY(%s) ORDER BY at""", (ids,)) if ids else []
    sales = db.fetch(f"SELECT * FROM {P}.cotr_sale WHERE shift_id = ANY(%s)", (ids,)) if ids else []
    reloads = db.fetch(f"SELECT * FROM {P}.cotr_reload WHERE shift_id = ANY(%s) ORDER BY at", (ids,)) if ids else []
    reqs = db.fetch(f"SELECT * FROM {P}.cotr_request WHERE at::date = %s OR status = 'sent' ORDER BY at", (day,))
    by = lambda rows, s: [r for r in rows if r["shift_id"] == s["id"]]  # noqa: E731
    out = []
    for s in shifts:
        d = drivers.get(s["driver_code"], {})
        sal, rel = by(sales, s), by(reloads, s)
        out.append({
            "id": str(s["id"]), "driver": s["driver_code"], "driverName": d.get("name"), "pool": s["pool_code"],
            "poolName": d.get("pool_name"), "fleet": s["fleet_code"], "status": s["status"],
            "startedAt": _iso(s["started_at"]), "endedAt": _iso(s["ended_at"]), "startStock": s["start_stock"],
            "stock": stock_now(s["start_stock"], rel, sal), "endNote": s["end_note"],
            "checkIns": [{"id": str(c["id"]), "kind": c["kind"], "at": _iso(c["at"]),
                          "lat": float(c["latitude"]) if c["latitude"] is not None else None,
                          "lng": float(c["longitude"]) if c["longitude"] is not None else None,
                          "stock": c["stock"], "note": c["note"], "hasPhoto": c["has_photo"]} for c in by(checkins, s)],
            "recap": recap(s, sal, rel, [r for r in reqs if r["shift_id"] == s["id"]]),
        })
    started = {s["driver_code"] for s in shifts}
    return {
        "day": day.isoformat(), "shifts": out,
        "notStarted": [{"code": d["code"], "name": d["name"], "pool": d["pool_code"]}
                       for d in drivers.values() if d["status"] == "active" and d["code"] not in started],
        "reloads": [_reload_view(r) | {"driver": next((s["driver_code"] for s in shifts if s["id"] == r["shift_id"]), None)}
                    for r in reloads],
        "requests": [_request_view(r) | {"driverName": drivers.get(r["driver_code"], {}).get("name")} for r in reqs],
    }


def decide_reload(user: dict, reload_id: str, approve: bool, note: Optional[str]) -> None:
    row = db.fetchrow(f"""UPDATE {P}.cotr_reload SET status = %s, decided_at = now(), decided_by = %s, decision_note = %s
                           WHERE id = %s AND status = 'requested' RETURNING id""",
                      ("approved" if approve else "rejected", user["id"], (note or "").strip() or None, reload_id))
    if not row:
        raise CotrError("Reload sudah diputuskan")


def decide_request(user: dict, request_id: str, approve: bool, note: Optional[str]) -> None:
    if not approve and not (note or "").strip():
        raise CotrError("Tulis alasan penolakan")
    row = db.fetchrow(f"""UPDATE {P}.cotr_request SET status = %s, decided_at = now(), decided_by = %s, decision_note = %s
                           WHERE id = %s AND status = 'sent' RETURNING id""",
                      ("approved" if approve else "rejected", user["id"], (note or "").strip() or None, request_id))
    if not row:
        raise CotrError("Pengajuan sudah diputuskan")


def photo(kind: str, row_id: str) -> Optional[dict]:
    table = {"check-in": "cotr_check_in", "request": "cotr_request"}[kind]
    return db.fetchrow(f"SELECT photo, photo_type FROM {P}.{table} WHERE id = %s", (row_id,))


def photo_owner(kind: str, row_id: str) -> Optional[str]:
    """The driver code a photo belongs to (a driver sees only its own)."""
    if kind == "request":
        r = db.fetchrow(f"SELECT driver_code FROM {P}.cotr_request WHERE id = %s", (row_id,))
    else:
        r = db.fetchrow(f"""SELECT s.driver_code FROM {P}.cotr_check_in c JOIN {P}.cotr_shift s ON s.id = c.shift_id
                             WHERE c.id = %s""", (row_id,))
    return r["driver_code"] if r else None


def production(days: int, buffer_pct: float) -> dict:
    """The production plan from the sales of the last `days` days (cups per menu and day, every driver)."""
    rows = db.fetch(
        f"""SELECT sh.day, l->>'code' AS code, sum((l->>'qty')::int) AS qty
              FROM {P}.cotr_sale s JOIN {P}.cotr_shift sh ON sh.id = s.shift_id, jsonb_array_elements(s.lines) l
             WHERE s.voided_at IS NULL AND sh.day >= current_date - %s AND sh.day < current_date
             GROUP BY 1, 2""", (days,))
    per: dict[str, list[int]] = {}
    seen_days = sorted({r["day"] for r in rows})
    for code in {r["code"] for r in rows}:
        per[code] = [int(next((r["qty"] for r in rows if r["code"] == code and r["day"] == d), 0)) for d in seen_days]
    boms = {b["code"]: b for b in db.fetch(f"SELECT * FROM {P}.cotr_bom")}
    return plan([m for m in menus(active_only=True)], per, boms, buffer_pct) | {"days": len(seen_days)}
