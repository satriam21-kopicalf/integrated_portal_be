"""Calf On The Road (migration 046): the drivers' day (app/routes/cotr.py, driver app fe-calfontheroad) and the masters
and monitoring of the COTR team (portal pages cotr.*).

A driver account (role "driver") is tied to one row of cotr_driver. Its day is one shift: the start check in (photo,
location, the cups loaded per menu), sales (price of the menu at that moment, cash or QRIS; voided with a reason),
reloads (asked by the driver, approved or rejected by the pool in the portal, received by the driver) and the end
check in, as the "Report Armada" sheet of COTR (migration 047): per menu the cups left, wasted and given for tasting
(cupping), set against the stock the system expects (a difference needs a note), then the cash counted and the cash
spent on the road from it, which give the balance of the day (- short / + over: "Rekap Minus"). The stock on the
fleet is never stored: start stock + reloads received - sales not voided. Costs on the road (fuel, ice, stall …) are
sent with the receipt and approved or rejected in the portal.
"""
import re
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from typing import Any, Iterable, Optional

from psycopg.types.json import Jsonb

from app import database as db
from app.pic_checkin import CheckInError, parse_photo
from app.utils import today

P = "integration_portal"
PAYMENTS = ("cash", "qris")
REQUEST_KINDS = ("fuel", "ice", "stall", "repair", "parking", "other")
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


def received(reload: dict) -> Stock:
    """The cups a received reload added: settled by the storage, else approved, else asked (before migration 049)."""
    return reload.get("received_lines") or reload.get("approved_lines") or reload["lines"]


def check_counts(expected: Stock, counted: Stock) -> tuple[bool, dict[str, int]]:
    """The driver's check of an assignment or a reload: all there, and the difference per menu (found - expected)."""
    diff = difference(counted, expected)
    return not diff, diff


def stock_now(start: Stock, reloads: Iterable[dict], sales: Iterable[dict]) -> Stock:
    """Cups on the fleet: start + reloads received - sales not voided (never below zero)."""
    out = {k: int(v) for k, v in (start or {}).items()}
    for r in reloads:
        if r["status"] == "received":
            for k, v in received(r).items():
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


def left_expected(stock: Stock, waste: Stock, cupping: Stock) -> Stock:
    """What should be left at the end: the stock on the fleet minus what was wasted or given for tasting."""
    keys = set(stock) | set(waste) | set(cupping)
    return {k: stock.get(k, 0) - waste.get(k, 0) - cupping.get(k, 0) for k in sorted(keys)
            if stock.get(k, 0) - waste.get(k, 0) - cupping.get(k, 0)}


def balance(cash_sales: float, cash_spent: Optional[Any], cash_actual: Optional[Any]) -> Optional[float]:
    """Cash counted - (cash sales - cash spent on the road); None before the end of the shift."""
    if cash_actual is None:
        return None
    return float(Decimal(str(cash_actual)) - (Decimal(str(cash_sales)) - Decimal(str(cash_spent or 0))))


def _num(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


def recap(shift: dict, sales: list[dict], reloads: list[dict], requests: list[dict]) -> dict:
    """The figures of a shift: cups, cash and QRIS, voids, reloads received, requests, waste and cupping, the
    difference of the stock and the balance of the cash at the end."""
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
        "reloaded": sum(sum(int(v) for v in received(r).values()) for r in reloads if r["status"] == "received"),
        "requested": float(sum(Decimal(r["amount"]) for r in requests if r["status"] != "rejected")),
        "difference": diff,
        "waste": sum(int(v) for v in (shift.get("end_waste") or {}).values()),
        "cupping": sum(int(v) for v in (shift.get("end_cupping") or {}).values()),
        "cashSpent": float(shift.get("cash_spent") or 0), "cashActual": _num(shift.get("cash_actual")),
        "cashExpected": by_pay["cash"] - float(shift.get("cash_spent") or 0),
        "balance": balance(by_pay["cash"], shift.get("cash_spent"), shift.get("cash_actual")),
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
            "decidedAt": _iso(r["decided_at"]), "decisionNote": r["decision_note"], "receivedAt": _iso(r["received_at"]),
            "approvedLines": r.get("approved_lines") or (r["lines"] if r["status"] != "requested" else None),
            "driverCounts": r.get("driver_counts"), "driverNote": r.get("driver_note"),
            "receivedLines": received(r) if r["status"] == "received" else None, "resolutionNote": r.get("resolution_note")}


def _dispatch_view(x: Optional[dict]) -> Optional[dict]:
    if not x:
        return None
    return {"id": str(x["id"]), "day": _iso(x["day"]), "driver": x["driver_code"], "pool": x["pool_code"], "lines": x["lines"],
            "note": x["note"], "status": x["status"], "assignedAt": _iso(x["assigned_at"]), "driverCounts": x["driver_counts"],
            "driverNote": x["driver_note"], "checkedAt": _iso(x["checked_at"]), "finalLines": x["final_lines"],
            "resolvedAt": _iso(x["resolved_at"]), "resolutionNote": x["resolution_note"]}


def dispatch_of(driver_code: str, day: date) -> Optional[dict]:
    return db.fetchrow(f"SELECT * FROM {P}.cotr_dispatch WHERE driver_code = %s AND day = %s", (driver_code, day))


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
                 "expectedStock": s["expected_stock"], "endNote": s["end_note"], "endWaste": s.get("end_waste"),
                 "endCupping": s.get("end_cupping")}
    return {
        "driver": _driver_view(d),
        "menus": [{"code": m["code"], "name": m["name"], "price": float(m["price"])} for m in menus(active_only=True)],
        "shift": shift,
        "dispatch": _dispatch_view(dispatch_of(d["code"], today())),
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


def confirm_dispatch(user: dict, counts: dict, note: Optional[str]) -> dict:
    """The driver's check of today's assignment: all there -> accepted (the start stock); else the counts found and a
    note go to the storage (disputed), which settles the final cups."""
    d = require_driver(user)
    found = clean_stock(counts, _menu_map())
    note = (note or "").strip() or None
    with db.transaction() as conn:
        x = conn.execute(f"SELECT * FROM {P}.cotr_dispatch WHERE driver_code = %s AND day = %s FOR UPDATE", (d["code"], today())).fetchone()
        if not x:
            raise CotrError("Belum ada assignment produk dari storage")
        if x["status"] != "assigned":
            raise CotrError("Assignment sudah dicek")
        ok, _ = check_counts(x["lines"], found)
        if not ok and not note:
            raise CotrError("Ada perbedaan jumlah: tulis keterangannya untuk storage")
        if ok:
            conn.execute(f"""UPDATE {P}.cotr_dispatch SET status = 'accepted', checked_at = now(), final_lines = lines,
                                 driver_note = %s WHERE id = %s""", (note, x["id"]))
        else:
            conn.execute(f"""UPDATE {P}.cotr_dispatch SET status = 'disputed', checked_at = now(), driver_counts = %s,
                                 driver_note = %s WHERE id = %s""", (Jsonb(found), note, x["id"]))
    return today_of(user)


def check_in(user: dict, kind: str, photo: str, lat: float, lng: float, accuracy: Optional[float], stock: dict,
             note: Optional[str], waste: Optional[dict] = None, cupping: Optional[dict] = None,
             cash_actual: Optional[float] = None, cash_spent: Optional[float] = None, signature: Optional[str] = None) -> dict:
    d = require_driver(user)
    ptype, pdata = parse_photo(photo)
    menu_codes = _menu_map()
    counted = clean_stock(stock, menu_codes)
    wasted, tasted = clean_stock(waste, menu_codes), clean_stock(cupping, menu_codes)
    note = (note or "").strip() or None
    sign_type, sign = parse_photo(signature) if signature else (None, None)
    with db.transaction() as conn:
        s = conn.execute(f"SELECT * FROM {P}.cotr_shift WHERE driver_code = %s AND day = %s FOR UPDATE", (d["code"], today())).fetchone()
        if kind == "start":
            if s:
                raise CotrError("Shift hari ini sudah dimulai")
            # the start stock is the assignment of the storage, checked by the driver (migration 049)
            x = conn.execute(f"SELECT * FROM {P}.cotr_dispatch WHERE driver_code = %s AND day = %s", (d["code"], today())).fetchone()
            if not x or x["status"] != "accepted":
                raise CotrError("Cek dulu produk yang di-assign storage sampai sesuai")
            if not sign:
                raise CotrError("Tanda tangan wajib diisi")
            counted = x["final_lines"]
            s = conn.execute(
                f"""INSERT INTO {P}.cotr_shift (driver_code, day, pool_code, fleet_code, start_stock, dispatch_id)
                    VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                (d["code"], today(), d["pool_code"], d["fleet_code"], Jsonb(counted), x["id"])).fetchone()
        else:
            if not s or s["status"] != "open":
                raise CotrError("Belum ada shift yang berjalan")
            sales = conn.execute(f"SELECT * FROM {P}.cotr_sale WHERE shift_id = %s", (s["id"],)).fetchall()
            reloads = conn.execute(f"SELECT * FROM {P}.cotr_reload WHERE shift_id = %s", (s["id"],)).fetchall()
            if cash_actual is None or cash_actual < 0:
                raise CotrError("Isi uang tunai yang dihitung (actual cash)")
            if cash_spent is not None and cash_spent < 0:
                raise CotrError("Operasional tidak valid")
            on_fleet = stock_now(s["start_stock"], reloads, sales)
            if any(wasted.get(k, 0) + tasted.get(k, 0) > on_fleet.get(k, 0) for k in set(wasted) | set(tasted)):
                raise CotrError("Wasting + cupping melebihi stok di armada")
            expected = left_expected(on_fleet, wasted, tasted)
            if difference(counted, expected) and not note:
                raise CotrError("Ada selisih dengan stok sistem: tulis penjelasannya di catatan")
            conn.execute(
                f"""UPDATE {P}.cotr_shift SET status = 'closed', ended_at = now(), end_stock = %s, expected_stock = %s,
                        end_note = %s, end_waste = %s, end_cupping = %s, cash_actual = %s, cash_spent = %s
                     WHERE id = %s""",
                (Jsonb(counted), Jsonb(expected), note, Jsonb(wasted), Jsonb(tasted), cash_actual, cash_spent or 0, s["id"]))
        conn.execute(
            f"""INSERT INTO {P}.cotr_check_in (shift_id, kind, latitude, longitude, accuracy_m, photo, photo_type, stock, note,
                    signature, signature_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (s["id"], kind, lat, lng, accuracy, pdata, ptype, Jsonb(counted), note, sign, sign_type))
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


def receive_reload(user: dict, reload_id: str, counts: Optional[dict] = None, note: Optional[str] = None) -> dict:
    """The driver's check of an approved reload: all there -> received (the approved cups join the stock); else the
    counts found and a note go to the storage (disputed), which settles the cups received."""
    d = require_driver(user)
    s = _open_shift(d)
    note = (note or "").strip() or None
    with db.transaction() as conn:
        r = conn.execute(f"SELECT * FROM {P}.cotr_reload WHERE id = %s AND shift_id = %s FOR UPDATE", (reload_id, s["id"])).fetchone()
        if not r or r["status"] != "approved":
            raise CotrError("Reload belum disetujui storage atau sudah diterima")
        approved = r["approved_lines"] or r["lines"]
        found = clean_stock(counts, _menu_map()) if counts is not None else approved
        ok, _ = check_counts(approved, found)
        if not ok and not note:
            raise CotrError("Ada perbedaan jumlah: tulis keterangannya untuk storage")
        if ok:
            conn.execute(f"""UPDATE {P}.cotr_reload SET status = 'received', received_at = now(), received_lines = %s,
                                 driver_note = %s WHERE id = %s""", (Jsonb(approved), note, r["id"]))
        else:
            conn.execute(f"""UPDATE {P}.cotr_reload SET status = 'disputed', driver_counts = %s, driver_note = %s
                              WHERE id = %s""", (Jsonb(found), note, r["id"]))
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
    "pools": ("cotr_pool", "code", ("code", "name", "kind", "parent_code", "city", "address", "storage", "is_active",
                                    "latitude", "longitude", "radius_m", "pic_name", "phone", "open_time", "close_time", "note"),
              "kind, name"),
    "fleet": ("cotr_fleet", "code", ("code", "name", "type", "plate", "pool_code", "capacity", "condition", "last_service",
                                     "is_active", "brand", "model", "frame_no", "acquired_on", "qris_bank", "qris_nmid",
                                     "qris_merchant", "note"), "code"),
    "fleet-types": ("cotr_fleet_type", "code", ("code", "name", "sort"), "sort, name"),
    "menus": ("cotr_menu", "code", ("code", "name", "category", "price", "bom_code", "is_active", "sort"), "sort, name"),
    "boms": ("cotr_bom", "code", ("code", "name", "output", "yield_qty", "yield_unit", "lines", "kind", "units", "note"),
             "kind DESC, name"),
    "items": ("cotr_item", "code", ("code", "name", "category", "unit", "pack_unit", "pack_qty", "cost", "is_stock",
                                    "is_active", "note"), "category, name"),
    "drivers": ("cotr_driver", "code", ("code", "name", "phone", "pool_code", "fleet_code", "status", "joined_on", "user_id",
                                        "nik", "birth_place", "birth_date", "gender", "address", "emergency_name",
                                        "emergency_phone", "employment_status", "position", "contract_end", "salary_monthly",
                                        "allowance_monthly", "bank_name", "bank_account", "bank_holder", "note"), "code"),
}
# read and written only by superadmins and the COTR management (page cotr.management, migration 050)
SENSITIVE = {"drivers": ("nik", "salary_monthly", "allowance_monthly", "bank_name", "bank_account", "bank_holder")}
# pictures of a master row: slot -> (column of the bytes, column of the type)
IMAGES = {"drivers": {"photo": ("photo", "photo_type")},
          "fleet": {"photo": ("photo", "photo_type"), "qris": ("qris_image", "qris_image_type")}}
_BYTES = {"photo", "photo_type", "qris_image", "qris_image_type", "updated_by"}
UPPER_CODES = {"pools", "fleet", "menus", "boms", "drivers", "items"}  # fleet types keep their code as typed (sepeda_listrik)


def _plain(row: dict) -> dict:
    out = {k: (float(v) if isinstance(v, Decimal) else _iso(v)) for k, v in row.items() if k not in _BYTES}
    for k in ("open_time", "close_time"):
        if out.get(k):
            out[k] = str(out[k])[:5]
    if "photo" in row:
        out["has_photo"] = row["photo"] is not None
    if "qris_image" in row:
        out["has_qris"] = row["qris_image"] is not None
    return out


def _hide(entity: str, row: dict, sensitive: bool) -> dict:
    if not sensitive:
        for k in SENSITIVE.get(entity, ()):
            row.pop(k, None)
    return row


def master_list(entity: str, sensitive: bool = False) -> list[dict]:
    table, _, _, order = MASTERS[entity]
    if entity == "drivers":
        rows = db.fetch(f"""SELECT d.*, u.username AS account FROM {P}.cotr_driver d
                              LEFT JOIN {P}.user_account u ON u.id = d.user_id ORDER BY d.code""")
    else:
        rows = db.fetch(f"SELECT * FROM {P}.{table} ORDER BY {order}")
    out = [_hide(entity, _plain(r), sensitive) for r in rows]
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
    if col == "units":  # the containers of a premix: [{name, grams}]
        units = [{"name": str(u.get("name", "")).strip(), "grams": float(u.get("grams") or 0)} for u in (v or []) if str(u.get("name", "")).strip()]
        if any(u["grams"] <= 0 for u in units):
            raise CotrError("Gram per satuan harus lebih dari 0")
        return Jsonb(units)
    return v


def master_save(user: dict, entity: str, key: Optional[str], data: dict, sensitive: bool = False) -> dict:
    """Create (key None) or update one master row; the code may change (references follow it). Without the COTR
    management page the salary, bank and NIK of a driver are left as they are."""
    table, pk, cols, _ = MASTERS[entity]
    hidden = set() if sensitive else set(SENSITIVE.get(entity, ()))
    row = {c: _value(c, data.get(c)) for c in cols if c in data and c not in hidden}
    if not (row.get(pk) or key):
        raise CotrError("Kode wajib diisi")
    if row.get("code") and entity in UPPER_CODES:
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
    return _hide(entity, _plain(saved), sensitive)


def set_image(user: dict, entity: str, code: str, slot: str, data_url: Optional[str]) -> None:
    """Puts (or removes, data_url None) the picture of a master row: driver photo, armada photo or its QRIS."""
    data_col, type_col = IMAGES[entity][slot]
    table, pk, _, _ = MASTERS[entity]
    kind, data = parse_photo(data_url) if data_url else (None, None)
    row = db.fetchrow(f"""UPDATE {P}.{table} SET {data_col} = %s, {type_col} = %s, updated_at = now(), updated_by = %s
                           WHERE {pk} = %s RETURNING {pk}""", (data, kind, user["id"], code))
    if not row:
        raise CotrError("Data tidak ditemukan")


def image(entity: str, code: str, slot: str) -> Optional[dict]:
    data_col, type_col = IMAGES[entity][slot]
    table, pk, _, _ = MASTERS[entity]
    return db.fetchrow(f"SELECT {data_col} AS data, {type_col} AS type FROM {P}.{table} WHERE {pk} = %s", (code,))


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
    checkins = db.fetch(f"""SELECT id, shift_id, kind, at, latitude, longitude, accuracy_m, stock, note, photo IS NOT NULL AS has_photo,
                                   signature IS NOT NULL AS has_signature
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
                          "stock": c["stock"], "note": c["note"], "hasPhoto": c["has_photo"],
                          "hasSignature": c["has_signature"]} for c in by(checkins, s)],
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


def decide_reload(user: dict, reload_id: str, approve: bool, note: Optional[str], lines: Optional[dict] = None) -> None:
    """The storage approves a reload with the cups it sends (the ones asked when not given; fewer when short) or
    rejects it (a reason is required)."""
    note = (note or "").strip() or None
    if not approve and not note:
        raise CotrError("Tulis alasan penolakan")
    sent = clean_stock(lines, _menu_map()) if lines is not None else None
    if approve and sent is not None and not sent:
        raise CotrError("Isi jumlah yang dikirim")
    row = db.fetchrow(f"""UPDATE {P}.cotr_reload SET status = %s, decided_at = now(), decided_by = %s, decision_note = %s,
                              approved_lines = CASE WHEN %s THEN COALESCE(%s, lines) END
                           WHERE id = %s AND status = 'requested' RETURNING id""",
                      ("approved" if approve else "rejected", user["id"], note, approve, Jsonb(sent) if sent else None, reload_id))
    if not row:
        raise CotrError("Reload sudah diputuskan")


def resolve_reload(user: dict, reload_id: str, lines: dict, note: Optional[str]) -> None:
    """The storage settles a reload the driver found different: the cups that join the stock of the fleet."""
    final = clean_stock(lines, _menu_map())
    row = db.fetchrow(f"""UPDATE {P}.cotr_reload SET status = 'received', received_at = now(), received_lines = %s,
                              resolved_at = now(), resolved_by = %s, resolution_note = %s
                           WHERE id = %s AND status = 'disputed' RETURNING id""",
                      (Jsonb(final), user["id"], (note or "").strip() or None, reload_id))
    if not row:
        raise CotrError("Reload tidak dalam status perbedaan")


# ---------------------------------------------------------------- the storage: assignment of the products (049)

def storage_day(day: date, pool: Optional[str]) -> dict:
    """The drivers of a pool for the day with their assignment, the shift, and the reloads to decide or settle."""
    where, args = ("AND d.pool_code = %s", (pool,)) if pool else ("", ())
    drivers = db.fetch(f"""SELECT d.code, d.name, d.pool_code, d.fleet_code, d.status FROM {P}.cotr_driver d
                            WHERE d.status <> 'inactive' {where} ORDER BY d.fleet_code NULLS LAST, d.code""", args)
    codes = [d["code"] for d in drivers]
    disp = {x["driver_code"]: x for x in db.fetch(f"SELECT * FROM {P}.cotr_dispatch WHERE day = %s AND driver_code = ANY(%s)", (day, codes))}
    shifts = {s["driver_code"]: s for s in db.fetch(f"SELECT * FROM {P}.cotr_shift WHERE day = %s AND driver_code = ANY(%s)", (day, codes))}
    ids = [s["id"] for s in shifts.values()]
    reloads = db.fetch(f"""SELECT r.*, s.driver_code FROM {P}.cotr_reload r JOIN {P}.cotr_shift s ON s.id = r.shift_id
                            WHERE r.shift_id = ANY(%s) ORDER BY r.at""", (ids,)) if ids else []
    prev = {x["driver_code"]: x["final_lines"] or x["lines"] for x in db.fetch(
        f"""SELECT DISTINCT ON (driver_code) driver_code, lines, final_lines FROM {P}.cotr_dispatch
             WHERE day < %s AND driver_code = ANY(%s) ORDER BY driver_code, day DESC""", (day, codes))}
    rows = []
    for d in drivers:
        s = shifts.get(d["code"])
        rows.append({"code": d["code"], "name": d["name"], "pool": d["pool_code"], "fleet": d["fleet_code"], "status": d["status"],
                     "dispatch": _dispatch_view(disp.get(d["code"])), "previous": prev.get(d["code"]),
                     "shift": s["status"] if s else None})
    return {"day": day.isoformat(), "drivers": rows,
            "reloads": [_reload_view(r) | {"driver": r["driver_code"]} for r in reloads]}


def assign(user: dict, day: date, driver_code: str, lines: dict, note: Optional[str]) -> dict:
    """The storage assigns the cups of the day to a driver; changeable until the driver has accepted them."""
    given = clean_stock(lines, _menu_map())
    if not given:
        raise CotrError("Isi minimal satu produk")
    d = db.fetchrow(f"SELECT code, pool_code FROM {P}.cotr_driver WHERE code = %s", (driver_code,))
    if not d:
        raise CotrError("Driver tidak ditemukan")
    row = db.fetchrow(
        f"""INSERT INTO {P}.cotr_dispatch (day, driver_code, pool_code, lines, note, assigned_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (day, driver_code) DO UPDATE SET lines = EXCLUDED.lines, note = EXCLUDED.note, status = 'assigned',
                assigned_at = now(), assigned_by = EXCLUDED.assigned_by, driver_counts = NULL, driver_note = NULL, checked_at = NULL
              WHERE {P}.cotr_dispatch.status <> 'accepted'
            RETURNING *""",
        (day, driver_code, d["pool_code"], Jsonb(given), (note or "").strip() or None, user["id"]))
    if not row:
        raise CotrError("Assignment sudah diterima driver dan tidak dapat diubah")
    return _dispatch_view(row)


def resolve_dispatch(user: dict, dispatch_id: str, lines: dict, note: Optional[str]) -> dict:
    """The storage settles an assignment the driver found different: the final cups, the start stock of the shift."""
    final = clean_stock(lines, _menu_map())
    if not final:
        raise CotrError("Isi jumlah final")
    row = db.fetchrow(f"""UPDATE {P}.cotr_dispatch SET status = 'accepted', final_lines = %s, resolved_at = now(),
                              resolved_by = %s, resolution_note = %s
                           WHERE id = %s AND status = 'disputed' RETURNING *""",
                      (Jsonb(final), user["id"], (note or "").strip() or None, dispatch_id))
    if not row:
        raise CotrError("Assignment tidak dalam status perbedaan")
    return _dispatch_view(row)


def decide_request(user: dict, request_id: str, approve: bool, note: Optional[str]) -> None:
    if not approve and not (note or "").strip():
        raise CotrError("Tulis alasan penolakan")
    row = db.fetchrow(f"""UPDATE {P}.cotr_request SET status = %s, decided_at = now(), decided_by = %s, decision_note = %s
                           WHERE id = %s AND status = 'sent' RETURNING id""",
                      ("approved" if approve else "rejected", user["id"], (note or "").strip() or None, request_id))
    if not row:
        raise CotrError("Pengajuan sudah diputuskan")


def photo(kind: str, row_id: str) -> Optional[dict]:
    """The photo of a check in or a request, or the signature of a check in."""
    if kind == "signature":
        return db.fetchrow(f"SELECT signature AS photo, signature_type AS photo_type FROM {P}.cotr_check_in WHERE id = %s", (row_id,))
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


# ---------------------------------------------------------------- import of the drivers (Excel)

_ARMADA = re.compile(r"^\s*(BNI|BRI)\s*0*(\d+)\b[\s./-]*(.*)$", re.I)
TEMPLATE = ("armada", "nama", "hp")  # the columns of the template sheet (first row), in any order


def armada_code(bank: str, number: str) -> str:
    """BNI 1 / BNI01 / bni 01 -> "BNI 01" (two digits at least, as the armada of migration 047)."""
    return f"{bank.upper()} {int(number):02d}"


def _template_rows(rows, cols: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        def cell(k: str) -> str:
            i = cols[k]
            return str(r[i]).strip() if i is not None and i < len(r) and r[i] is not None else ""
        m = _ARMADA.match(cell("armada"))
        if m and cell("nama"):
            out.setdefault(armada_code(m.group(1), m.group(2)), {"name": " ".join(cell("nama").split()).title(), "phone": cell("hp")})
    return out


def _report_rows(rows) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        for v in r[:3]:
            if not isinstance(v, str):
                continue
            m = _ARMADA.match(" ".join(v.split()))
            name = m.group(3).strip(" /") if m else ""
            if m and name and not re.search(r"pesen|customer|total", name, re.I):
                out.setdefault(armada_code(m.group(1), m.group(2)), {"name": name.title(), "phone": ""})
    return out


def parse_drivers(data: bytes) -> list[dict]:
    """Drivers of an Excel file: a sheet with the columns Armada, Nama, HP (template), else every cell like
    "BNI 01 BIYAN" of the "Report Armada" sheet. One row per armada (the first one wins)."""
    import openpyxl

    try:
        wb = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise CotrError("File bukan Excel (.xlsx) yang valid") from None
    out: dict[str, dict] = {}
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        head = [str(v or "").strip().lower() for v in next(rows, ())]
        cols = {name: next((i for i, h in enumerate(head) if h.startswith(name)), None) for name in TEMPLATE}
        found = _template_rows(rows, cols) if cols["armada"] is not None and cols["nama"] is not None else _report_rows(rows)
        for k, v in found.items():
            out.setdefault(k, v)
    return [{"armada": k, **v} for k, v in sorted(out.items())]


def import_drivers(user: dict, rows: list[dict], pool_code: str, apply: bool) -> dict:
    """Preview (apply False) or save the drivers: one per armada (DRV-BNI01), new ones created, the name / phone of
    the known ones updated; an armada that does not exist yet is created in the pool."""
    if not db.fetchrow(f"SELECT 1 FROM {P}.cotr_pool WHERE code = %s", (pool_code,)):
        raise CotrError("Pool tidak ditemukan")
    fleet = {r["code"] for r in db.fetch(f"SELECT code FROM {P}.cotr_fleet")}
    known = {r["fleet_code"]: r for r in db.fetch(
        f"SELECT code, name, phone, fleet_code FROM {P}.cotr_driver WHERE fleet_code IS NOT NULL")}
    plan = []
    for r in rows:
        k = known.get(r["armada"])
        same = k and k["name"] == r["name"] and (not r["phone"] or k["phone"] == r["phone"])
        plan.append({**r, "code": k["code"] if k else "DRV-" + r["armada"].replace(" ", ""),
                     "action": "new" if not k else ("same" if same else "update"),
                     "newArmada": r["armada"] not in fleet, "current": k["name"] if k else None})
    if apply:
        with db.transaction() as conn:
            for p in plan:
                if p["newArmada"]:
                    conn.execute(f"""INSERT INTO {P}.cotr_fleet (code, type, pool_code, updated_by) VALUES (%s, 'sepeda', %s, %s)
                                     ON CONFLICT (code) DO NOTHING""", (p["armada"], pool_code, user["id"]))
                if p["action"] == "new":
                    conn.execute(f"""INSERT INTO {P}.cotr_driver (code, name, phone, pool_code, fleet_code, updated_by)
                                     VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (code) DO NOTHING""",
                                 (p["code"], p["name"], p["phone"], pool_code, p["armada"], user["id"]))
                elif p["action"] == "update":
                    conn.execute(f"""UPDATE {P}.cotr_driver SET name = %s, phone = COALESCE(NULLIF(%s, ''), phone),
                                     updated_at = now(), updated_by = %s WHERE code = %s""",
                                 (p["name"], p["phone"], user["id"], p["code"]))
    return {"rows": plan, "new": sum(p["action"] == "new" for p in plan), "update": sum(p["action"] == "update" for p in plan),
            "same": sum(p["action"] == "same" for p in plan), "newArmada": sum(p["newArmada"] for p in plan), "applied": apply}


# ---------------------------------------------------------------- where the drivers are (migration 048)

MIN_GAP_S = 10  # positions closer in time are dropped (the app sends every ~30 s)
MAX_POINTS = 120  # one batch


def thin(points: list[dict], last_at: Optional[datetime]) -> list[dict]:
    """The positions to keep: in time order, valid coordinates, at least MIN_GAP_S after the previous one kept."""
    out, prev = [], last_at
    for p in sorted(points, key=lambda x: x["at"]):
        if not (-90 <= p["lat"] <= 90 and -180 <= p["lng"] <= 180) or (p["lat"] == 0 and p["lng"] == 0):
            continue
        if prev is not None and (p["at"] - prev).total_seconds() < MIN_GAP_S:
            continue
        out.append(p)
        prev = p["at"]
    return out


def record_locations(user: dict, points: list[dict]) -> dict:
    """Positions sent by the driver app while its shift is open (a batch kept on the phone while offline too)."""
    d = require_driver(user)
    s = shift_of(d["code"], today())
    if not s or s["status"] != "open":
        return {"kept": 0, "tracking": False}
    last = db.fetchrow(f"SELECT max(at) AS at FROM {P}.cotr_location WHERE driver_code = %s", (d["code"],))
    kept = thin(points[:MAX_POINTS], last["at"] if last else None)
    if kept:
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    f"""INSERT INTO {P}.cotr_location (driver_code, shift_id, at, latitude, longitude, accuracy_m, speed_ms,
                            heading, battery) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [(d["code"], s["id"], p["at"], p["lat"], p["lng"], p.get("accuracy"), p.get("speed"), p.get("heading"),
                      p.get("battery")) for p in kept])
    return {"kept": len(kept), "tracking": True}


def _point(r: dict) -> dict:
    return {"at": _iso(r["at"]), "lat": float(r["latitude"]), "lng": float(r["longitude"]),
            "accuracy": _num(r["accuracy_m"]), "speed": _num(r["speed_ms"]), "battery": r["battery"]}


def positions(day: date) -> list[dict]:
    """The last position of every driver with a shift on the day, with the status of the shift."""
    rows = db.fetch(
        f"""SELECT DISTINCT ON (l.driver_code) l.*, d.name, d.fleet_code, s.status
              FROM {P}.cotr_location l JOIN {P}.cotr_shift s ON s.id = l.shift_id
              JOIN {P}.cotr_driver d ON d.code = l.driver_code
             WHERE s.day = %s ORDER BY l.driver_code, l.at DESC""", (day,))
    return [{"driver": r["driver_code"], "name": r["name"], "fleet": r["fleet_code"], "shift": r["status"], **_point(r)}
            for r in rows]


def track(driver_code: str, day: date) -> list[dict]:
    """Every position of one driver on the day, in time order (the route)."""
    return [_point(r) for r in db.fetch(
        f"""SELECT l.* FROM {P}.cotr_location l JOIN {P}.cotr_shift s ON s.id = l.shift_id
             WHERE l.driver_code = %s AND s.day = %s ORDER BY l.at""", (driver_code, day))]


# ---------------------------------------------------------------- theoretical usage (migration 051)

def usage(d_from: date, d_to: date, pool: Optional[str]) -> dict:
    """What the drivers used in theory: the cups sold (not voided), wasted and given for tasting at the end of the
    shifts, exploded through the recipe card of each menu (premix down to its raw materials) into items."""
    where, args = ("AND sh.pool_code = %s", (pool,)) if pool else ("", ())
    sold = db.fetch(
        f"""SELECT l->>'code' AS code, sum((l->>'qty')::int) AS qty FROM {P}.cotr_sale s
              JOIN {P}.cotr_shift sh ON sh.id = s.shift_id, jsonb_array_elements(s.lines) l
             WHERE s.voided_at IS NULL AND sh.day BETWEEN %s AND %s {where} GROUP BY 1""", (d_from, d_to, *args))
    ends = db.fetch(f"""SELECT end_waste, end_cupping FROM {P}.cotr_shift sh
                         WHERE sh.day BETWEEN %s AND %s AND sh.status = 'closed' {where}""", (d_from, d_to, *args))
    cups: dict[str, dict[str, float]] = {}
    for r in sold:
        cups.setdefault(r["code"], {"sold": 0, "waste": 0, "cupping": 0})["sold"] += int(r["qty"])
    for e in ends:
        for src, col in (("waste", "end_waste"), ("cupping", "end_cupping")):
            for code, n in (e[col] or {}).items():
                cups.setdefault(code, {"sold": 0, "waste": 0, "cupping": 0})[src] += int(n)
    menus_by = {m["code"]: m for m in menus()}
    boms = {b["code"]: b for b in db.fetch(f"SELECT * FROM {P}.cotr_bom")}
    items = {i["code"]: i for i in db.fetch(f"SELECT * FROM {P}.cotr_item")}
    out: dict[str, dict] = {}
    menu_rows, no_recipe = [], []
    for code, c in sorted(cups.items()):
        m = menus_by.get(code)
        menu_rows.append({"code": code, "name": m["name"] if m else code, **c, "bom": m["bom_code"] if m else None})
        if not m or not m["bom_code"]:
            no_recipe.append(code)
            continue
        for src in ("sold", "waste", "cupping"):
            if not c[src]:
                continue
            part: dict[str, dict] = {}
            explode(boms, m["bom_code"], c[src], part)
            for k, v in part.items():
                it = items.get(k)
                row = out.setdefault(k, {"item": k, "name": it["name"] if it else k, "unit": it["unit"] if it else v["unit"],
                                         "category": it["category"] if it else None, "sold": 0.0, "waste": 0.0, "cupping": 0.0,
                                         "cost": float(it["cost"]) if it and it["cost"] is not None else None})
                row[src] += v["qty"]
    import math
    rows = []
    for r in out.values():
        r["total"] = r["sold"] + r["waste"] + r["cupping"]
        for k in ("sold", "waste", "cupping", "total"):
            r[k] = math.ceil(round(r[k], 6))
        r["value"] = round(r["total"] * r["cost"], 2) if r["cost"] is not None else None
        rows.append(r)
    rows.sort(key=lambda r: (r["category"] or "", -r["total"]))
    return {"from": d_from.isoformat(), "to": d_to.isoformat(), "menus": menu_rows, "items": rows, "noRecipe": no_recipe}
