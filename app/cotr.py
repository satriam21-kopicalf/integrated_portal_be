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
import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from typing import Any, Iterable, Optional

from psycopg.types.json import Jsonb

from app import database as db
from app.pic_checkin import CheckInError, parse_photo
from app.utils import today

P = "integration_portal"
PAYMENTS = ("cash", "qris")
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


def unrecorded(on_fleet: Stock, counted: Stock, waste: Stock, cupping: Stock) -> Stock:
    """The cups gone from the fleet that no transaction, waste or cupping explains: sold but not tapped in (recap at
    the pool, migration 084). More left than expected is not a sale: it stays a difference."""
    expected = left_expected(on_fleet, waste, cupping)
    return {k: expected[k] - counted.get(k, 0) for k in sorted(expected) if expected[k] - counted.get(k, 0) > 0}


def recap_sales(gone: Stock, menus: dict[str, dict], qris: Optional[Any]) -> list[tuple[str, list[dict], Decimal]]:
    """The recap sales of the unrecorded cups at the menu price: (payment, lines, total) — the QRIS part as read from the
    QRIS mutations (without lines), the rest cash with the lines, so the cups are counted once."""
    lines, total = [], Decimal(0)
    for code, qty in gone.items():
        m = menus.get(code)
        if not m:
            continue
        price = Decimal(m["price"])
        lines.append({"code": code, "name": m["name"], "qty": qty, "price": float(price)})
        total += price * qty
    q = Decimal(str(qris or 0))
    if q < 0 or q > total:
        raise CotrError(f"QRIS rekap tidak boleh lebih dari total penjualan rekap Rp {int(total):,}".replace(",", "."))
    if not lines:
        return []
    out = [("cash", lines, total - q)]
    if q > 0:
        out.append(("qris", [], q))
    return out


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
        "qris": by_pay["qris"], "voided": len(sales) - len(ok),
        # migration 084: what was booked at the pool from the stock (not tapped in during the shift)
        "recapCups": sum(int(x["qty"]) for t in ok if t.get("source") == "recap" for x in t["lines"]),
        "recapTotal": float(sum(Decimal(t["total"]) for t in ok if t.get("source") == "recap")), "perMenu": sorted(per_menu.values(), key=lambda m: -m["qty"]),
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
    return db.fetch(f"""SELECT code, name, category, price, bom_code, is_active, sort, shelf_life_days, esb_aliases
                          FROM {P}.cotr_menu {where} ORDER BY sort, name""")


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
            "source": s.get("source") or "live",
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
            "resolvedAt": _iso(x["resolved_at"]), "resolutionNote": x["resolution_note"], "cupping": x.get("cupping") or {}}


def dispatch_of(driver_code: str, day: date) -> Optional[dict]:
    return db.fetchrow(f"SELECT * FROM {P}.cotr_dispatch WHERE driver_code = %s AND day = %s", (driver_code, day))


def _request_view(r: dict) -> dict:
    return {"id": str(r["id"]), "at": _iso(r["at"]), "kind": r["kind"], "amount": float(r["amount"]), "note": r["note"],
            "status": r["status"], "decidedAt": _iso(r["decided_at"]), "decisionNote": r["decision_note"],
            "driverCode": r["driver_code"], "hasPhoto": r.get("photo") is not None}


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
        # migration 083: the kinds of a request, from Master → Pengajuan
        "requestKinds": [{"code": k["code"], "name": k["name"], "description": k["description"], "needsPhoto": k["needs_photo"],
                          "maxAmount": float(k["max_amount"]) if k["max_amount"] is not None else None, "active": k["is_active"]}
                         for k in request_kinds(active_only=False)],  # the inactive ones still name the older requests
        "recap": recap(s, sales, reloads, [r for r in reqs if s and r["shift_id"] == s["id"]]) if s else None,
        # 2026-11-08: where the roster sends the driver today and the accessories on the fleet
        "slots": [{"start": r["start_time"].strftime("%H:%M"), "end": r["end_time"].strftime("%H:%M"), "area": r["area"], "address": r["address"],
                   "lat": float(r["latitude"]) if r["latitude"] is not None else None, "lng": float(r["longitude"]) if r["longitude"] is not None else None}
                  for r in db.fetch(f"""SELECT s.start_time, s.end_time, a.name AS area, a.address, a.latitude, a.longitude FROM {P}.cotr_schedule_slot s
                                         LEFT JOIN {P}.cotr_area a ON a.code = s.area_code WHERE s.driver_code = %s AND s.day = %s ORDER BY s.start_time""",
                                     (d["code"], today()))],
        "accessories": [{"name": r["name"], "qty": r["qty"]} for r in db.fetch(
            f"""SELECT a.name, fa.qty FROM {P}.cotr_fleet_accessory fa JOIN {P}.cotr_accessory a ON a.code = fa.accessory_code
                 WHERE fa.fleet_code = %s AND fa.removed_on IS NULL ORDER BY a.name""", (d["fleet_code"],))] if d.get("fleet_code") else [],
        # migration 088: the areas of Master → Area to ask for (of the driver's pool first) and the requests of 14 days
        "areas": [{"code": r["code"], "name": r["name"], "address": r["address"], "city": r["city"], "pool": r["pool_code"],
                   "lat": float(r["latitude"]) if r["latitude"] is not None else None, "lng": float(r["longitude"]) if r["longitude"] is not None else None,
                   "radius": r["radius_m"]}
                  for r in db.fetch(f"""SELECT * FROM {P}.cotr_area WHERE is_active
                                         ORDER BY (pool_code IS NOT DISTINCT FROM %s) DESC, name""", (d["pool_code"],))],
        "issues": [_issue_view(r) for r in db.fetch(
            f"""{_ISSUES} WHERE i.driver_code = %s AND (i.status IN ('open', 'in_progress') OR i.created_at > now() - interval '30 days')
                 ORDER BY i.created_at DESC""", (d["code"],))],
        "areaRequests": [_area_request_view(r) for r in db.fetch(
            f"{_AREA_REQUESTS} WHERE q.driver_code = %s AND q.created_at > now() - interval '14 days' ORDER BY q.created_at DESC", (d["code"],))],
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
             cash_actual: Optional[float] = None, cash_spent: Optional[float] = None, signature: Optional[str] = None,
             recap: bool = False, recap_qris: Optional[float] = None) -> dict:
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
            if d["fleet_code"]:  # the accessories on the fleet now go with this shift (migration 080)
                conn.execute(f"""INSERT INTO {P}.cotr_shift_accessory (shift_id, accessory_code, fleet_code, qty)
                                 SELECT %s, accessory_code, fleet_code, qty FROM {P}.cotr_fleet_accessory
                                  WHERE fleet_code = %s AND removed_on IS NULL ON CONFLICT DO NOTHING""", (s["id"], d["fleet_code"]))
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
            if recap:  # migration 084: the cups not tapped in are booked as recap sales, then nothing is left unexplained
                for payment, lines, total in recap_sales(unrecorded(on_fleet, counted, wasted, tasted), menu_codes, recap_qris):
                    conn.execute(f"INSERT INTO {P}.cotr_sale (shift_id, lines, payment, total, source) VALUES (%s, %s, %s, %s, 'recap')",
                                 (s["id"], Jsonb(lines), payment, total))
                sales = conn.execute(f"SELECT * FROM {P}.cotr_sale WHERE shift_id = %s", (s["id"],)).fetchall()
                on_fleet = stock_now(s["start_stock"], reloads, sales)
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


def request_kinds(active_only: bool = True) -> list[dict]:
    """The kinds of a request (master cotr_request_kind, migration 083), in their order."""
    return db.fetch(f"SELECT * FROM {P}.cotr_request_kind {'WHERE is_active' if active_only else ''} ORDER BY sort, name")


def send_request(user: dict, kind: str, amount: Any, note: str, photo: str) -> dict:
    k = next((x for x in request_kinds() if x["code"] == kind), None)
    if not k:
        raise CotrError("Jenis pengajuan tidak dikenal")
    try:
        value = Decimal(str(amount))
    except Exception:
        raise CotrError("Nominal tidak valid") from None
    if value <= 0 or value > Decimal(100_000_000):
        raise CotrError("Nominal tidak valid")
    if k["max_amount"] is not None and value > k["max_amount"]:
        raise CotrError(f"Nominal {k['name']} paling banyak Rp {int(k['max_amount']):,}".replace(",", "."))
    if not (note or "").strip():
        raise CotrError("Tulis keterangan")
    d = require_driver(user)
    if not photo and k["needs_photo"]:
        raise CotrError("Lampirkan foto nota")
    ptype, pdata = parse_photo(photo) if photo else (None, None)
    s = shift_of(d["code"], today())
    db.fetchrow(f"""INSERT INTO {P}.cotr_request (driver_code, shift_id, kind, amount, note, photo, photo_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (d["code"], s["id"] if s else None, kind, value, note.strip(), pdata, ptype))
    return today_of(user)


# ---------------------------------------------------------------- area requests (migration 088)

def _hm(v: Any) -> Optional[str]:
    return v.strftime("%H:%M") if v else None


def _area_request_view(r: dict) -> dict:
    return {"id": str(r["id"]), "driver": r["driver_code"], "driverName": r.get("driver_name"), "pool": r.get("pool_code"),
            "day": _iso(r["day"]), "area": r["area_code"], "areaName": r.get("area_name") or r["name"],
            "areaAddress": r.get("area_address") or r["address"], "isNew": r["name"] is not None,
            "name": r["name"], "address": r["address"], "city": r["city"],
            "lat": float(r["latitude"]) if r["latitude"] is not None else None, "lng": float(r["longitude"]) if r["longitude"] is not None else None,
            "accuracy": float(r["accuracy_m"]) if r["accuracy_m"] is not None else None,
            "start": _hm(r["start_time"]), "end": _hm(r["end_time"]), "note": r["note"], "status": r["status"],
            "decidedAt": _iso(r["decided_at"]), "decisionNote": r["decision_note"], "decidedBy": r.get("decided_name"), "at": _iso(r["created_at"])}


_AREA_REQUESTS = f"""SELECT q.*, d.name AS driver_name, d.pool_code, a.name AS area_name, a.address AS area_address,
                            u.full_name AS decided_name
                       FROM {P}.cotr_area_request q JOIN {P}.cotr_driver d ON d.code = q.driver_code
                       LEFT JOIN {P}.cotr_area a ON a.code = q.area_code LEFT JOIN {P}.user_account u ON u.id = q.decided_by"""


def _clock(v: Any) -> Optional[str]:
    v = (v or "").strip()
    if not v:
        return None
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
        raise CotrError("Jam tidak valid (HH:MM)")
    return v


def send_area_request(user: dict, data: dict) -> dict:
    """The driver asks to go to an area of the master, or to a new place (its name, the address and the GPS there)."""
    d = require_driver(user)
    area = (data.get("area") or "").strip() or None
    name = (data.get("name") or "").strip() or None
    start, end = _clock(data.get("start")), _clock(data.get("end"))
    if start and end and end <= start:
        raise CotrError("Jam selesai harus setelah jam mulai")
    lat, lng = data.get("latitude"), data.get("longitude")
    if area:
        if not db.fetchrow(f"SELECT 1 FROM {P}.cotr_area WHERE code = %s AND is_active", (area,)):
            raise CotrError("Area tidak ditemukan")
        name = None
    elif not name:
        raise CotrError("Pilih area atau isi nama tempat baru")
    elif lat is None or lng is None:
        raise CotrError("Ambil lokasi saat ini untuk tempat baru")
    with db.transaction() as conn:
        pending = conn.execute(f"SELECT count(*) AS n FROM {P}.cotr_area_request WHERE driver_code = %s AND status = 'sent'",
                               (d["code"],)).fetchone()["n"]
        if pending >= 5:
            raise CotrError("Masih ada 5 permintaan area menunggu keputusan")
        conn.execute(f"""INSERT INTO {P}.cotr_area_request (driver_code, day, area_code, name, address, city, latitude, longitude,
                             accuracy_m, start_time, end_time, note)
                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                     (d["code"], data.get("day") or today(), area, name, (data.get("address") or "").strip() or None,
                      (data.get("city") or "").strip() or None, lat, lng, data.get("accuracyM"), start, end,
                      (data.get("note") or "").strip() or None))
    return today_of(user)


def area_requests(d_from: date, d_to: date, status: Optional[str] = None, pool: Optional[str] = None) -> list[dict]:
    rows = db.fetch(f"""{_AREA_REQUESTS} WHERE q.day BETWEEN %s AND %s AND (%s::text IS NULL OR q.status = %s)
                         AND (%s::text IS NULL OR d.pool_code = %s) ORDER BY (q.status = 'sent') DESC, q.created_at DESC""",
                    (d_from, d_to, status, status, pool, pool))
    return [_area_request_view(r) for r in rows]


def decide_area_request(user: dict, request_id: str, approve: bool, data: dict) -> dict:
    """Approved: a new place becomes an area of the master (the name / address / code as the team corrects them), and the
    roster of that day sends the driver there — the slot running at the start time takes the area, else a slot is added
    for the times asked. Rejected: a reason is needed."""
    note = (data.get("note") or "").strip() or None
    if not approve and not note:
        raise CotrError("Tulis alasan penolakan")
    with db.transaction() as conn:
        q = conn.execute(f"SELECT * FROM {P}.cotr_area_request WHERE id::text = %s FOR UPDATE", (request_id,)).fetchone()
        if not q:
            raise CotrError("Permintaan area tidak ditemukan")
        if q["status"] != "sent":
            raise CotrError("Permintaan area sudah diputuskan")
        area = q["area_code"]
        if approve and not area:
            d = conn.execute(f"SELECT pool_code FROM {P}.cotr_driver WHERE code = %s", (q["driver_code"],)).fetchone()
            code = ((data.get("code") or "").strip() or f"AREA-R{str(q['id'])[:6]}").upper()
            if conn.execute(f"SELECT 1 FROM {P}.cotr_area WHERE code = %s", (code,)).fetchone():
                raise CotrError(f"Kode area {code} sudah dipakai")
            conn.execute(f"""INSERT INTO {P}.cotr_area (code, name, address, city, latitude, longitude, pool_code, note, updated_by)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                         (code, (data.get("name") or "").strip() or q["name"], (data.get("address") or "").strip() or q["address"] or "",
                          (data.get("city") or "").strip() or q["city"] or "", q["latitude"], q["longitude"], d["pool_code"] if d else None,
                          f"Dari permintaan driver {q['driver_code']}", user["id"]))
            area = code
        if approve and q["start_time"]:
            slot = conn.execute(f"""SELECT id FROM {P}.cotr_schedule_slot WHERE driver_code = %s AND day = %s
                                     AND start_time <= %s AND end_time > %s ORDER BY start_time LIMIT 1""",
                                (q["driver_code"], q["day"], q["start_time"], q["start_time"])).fetchone()
            if slot:
                conn.execute(f"UPDATE {P}.cotr_schedule_slot SET area_code = %s, updated_at = now(), updated_by = %s WHERE id = %s",
                             (area, user["id"], slot["id"]))
            elif q["end_time"]:
                conn.execute(f"""INSERT INTO {P}.cotr_schedule_slot (driver_code, day, start_time, end_time, area_code, fleet_code, note, updated_by)
                                 SELECT code, %s, %s, %s, %s, fleet_code, %s, %s FROM {P}.cotr_driver WHERE code = %s""",
                             (q["day"], q["start_time"], q["end_time"], area, "Permintaan driver", user["id"], q["driver_code"]))
        conn.execute(f"""UPDATE {P}.cotr_area_request SET status = %s, area_code = %s, decided_by = %s, decided_at = now(),
                             decision_note = %s WHERE id = %s""",
                     ("approved" if approve else "rejected", area, user["id"], note, q["id"]))
    return _area_request_view(db.fetchrow(f"{_AREA_REQUESTS} WHERE q.id = %s", (q["id"],)))


# ---------------------------------------------------------------- fleet issues (migration 088)

ISSUE_CATEGORIES = ("brake", "battery", "tyre", "engine", "electrical", "body", "cooler", "accessory", "other")
ISSUE_STEPS = {"open": ("in_progress", "resolved", "rejected"), "in_progress": ("resolved", "rejected", "open"),
               "resolved": ("in_progress",), "rejected": ("open",)}

_ISSUES = f"""SELECT i.id, i.fleet_code, i.driver_code, i.category, i.severity, i.title, i.description, i.photo IS NOT NULL AS has_photo,
                     i.latitude, i.longitude, i.status, i.handler, i.resolution_note, i.cost, i.resolved_at, i.updated_at, i.created_at,
                     f.name AS fleet_name, f.plate, f.pool_code, d.name AS driver_name
                FROM {P}.cotr_fleet_issue i JOIN {P}.cotr_fleet f ON f.code = i.fleet_code
                LEFT JOIN {P}.cotr_driver d ON d.code = i.driver_code"""


def _issue_view(r: dict, log: Optional[list] = None) -> dict:
    out = {"id": str(r["id"]), "fleet": r["fleet_code"], "fleetName": r["fleet_name"], "plate": r["plate"], "pool": r["pool_code"],
           "driver": r["driver_code"], "driverName": r["driver_name"], "category": r["category"], "severity": r["severity"],
           "title": r["title"], "description": r["description"], "hasPhoto": r["has_photo"],
           "lat": float(r["latitude"]) if r["latitude"] is not None else None, "lng": float(r["longitude"]) if r["longitude"] is not None else None,
           "status": r["status"], "handler": r["handler"], "resolutionNote": r["resolution_note"],
           "cost": float(r["cost"]) if r["cost"] is not None else None, "resolvedAt": _iso(r["resolved_at"]),
           "updatedAt": _iso(r["updated_at"]), "at": _iso(r["created_at"])}
    if log is not None:
        out["log"] = [{"status": x["status"], "note": x["note"], "by": x["by_name"], "at": _iso(x["created_at"])} for x in log]
    return out


def report_issue(user: dict, data: dict) -> dict:
    """The driver reports an issue of the armada it drives (its fleet in the master), with a photo and where it is."""
    d = require_driver(user)
    if not d.get("fleet_code"):
        raise CotrError("Anda belum terhubung ke armada")
    if data.get("category") not in ISSUE_CATEGORIES:
        raise CotrError("Pilih jenis kendala")
    if data.get("severity") not in ("low", "medium", "high"):
        raise CotrError("Pilih tingkat kendala")
    title = (data.get("title") or "").strip()
    if len(title) < 3:
        raise CotrError("Tulis kendalanya")
    ptype, pdata = parse_photo(data["photo"]) if data.get("photo") else (None, None)
    s = shift_of(d["code"], today())
    with db.transaction() as conn:
        n = conn.execute(f"SELECT count(*) AS n FROM {P}.cotr_fleet_issue WHERE driver_code = %s AND status = 'open'", (d["code"],)).fetchone()["n"]
        if n >= 10:
            raise CotrError("Masih ada 10 laporan armada yang belum ditangani")
        r = conn.execute(f"""INSERT INTO {P}.cotr_fleet_issue (fleet_code, driver_code, shift_id, category, severity, title, description,
                                 photo, photo_type, latitude, longitude, updated_by)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                         (d["fleet_code"], d["code"], s["id"] if s else None, data["category"], data["severity"], title,
                          (data.get("description") or "").strip(), pdata, ptype, data.get("latitude"), data.get("longitude"),
                          user["id"])).fetchone()
        conn.execute(f"INSERT INTO {P}.cotr_fleet_issue_log (issue_id, status, note, by_name, by_user) VALUES (%s, 'open', %s, %s, %s)",
                     (r["id"], "Dilaporkan driver", d["name"], user["id"]))
    return today_of(user)


def issues(status: Optional[str] = None, fleet: Optional[str] = None, pool: Optional[str] = None, days: int = 90) -> list[dict]:
    """The issues still open or in progress, and the ones closed in the last days; the urgent first."""
    rows = db.fetch(f"""{_ISSUES} WHERE (%s::text IS NULL OR i.status = %s) AND (%s::text IS NULL OR i.fleet_code = %s)
                          AND (%s::text IS NULL OR f.pool_code = %s)
                          AND (i.status IN ('open', 'in_progress') OR i.created_at > now() - make_interval(days => %s))
                        ORDER BY (i.status IN ('open', 'in_progress')) DESC, (i.severity = 'high') DESC, i.created_at DESC""",
                    (status, status, fleet, fleet, pool, pool, days))
    return [_issue_view(r) for r in rows]


def issue_detail(issue_id: str) -> dict:
    r = db.fetchrow(f"{_ISSUES} WHERE i.id::text = %s", (issue_id,))
    if not r:
        raise CotrError("Laporan tidak ditemukan")
    return _issue_view(r, db.fetch(f"SELECT * FROM {P}.cotr_fleet_issue_log WHERE issue_id = %s ORDER BY created_at", (r["id"],)))


def handle_issue(user: dict, issue_id: str, data: dict) -> dict:
    """The management moves an issue: in progress (who handles it), resolved (what was done, the cost), rejected (why),
    or opened again; every step is logged with its note."""
    to, note = data.get("status"), (data.get("note") or "").strip() or None
    with db.transaction() as conn:
        i = conn.execute(f"SELECT * FROM {P}.cotr_fleet_issue WHERE id::text = %s FOR UPDATE", (issue_id,)).fetchone()
        if not i:
            raise CotrError("Laporan tidak ditemukan")
        if to not in ISSUE_STEPS.get(i["status"], ()):
            raise CotrError("Status tidak dapat diubah ke sana")
        if to in ("resolved", "rejected") and not note:
            raise CotrError("Tulis apa yang dilakukan" if to == "resolved" else "Tulis alasan penolakan")
        handler = (data.get("handler") or "").strip() or i["handler"]
        cost = data.get("cost") if data.get("cost") is not None else i["cost"]
        if cost is not None and float(cost) < 0:
            raise CotrError("Biaya tidak valid")
        closed = to in ("resolved", "rejected")
        conn.execute(f"""UPDATE {P}.cotr_fleet_issue SET status = %s, handler = %s, cost = %s,
                             resolution_note = CASE WHEN %s THEN %s ELSE resolution_note END,
                             resolved_at = CASE WHEN %s THEN now() END, updated_by = %s, updated_at = now() WHERE id = %s""",
                     (to, handler, cost, closed, note, closed, user["id"], i["id"]))
        parts = [note, f"Ditangani: {handler}" if handler and handler != i["handler"] else None]
        conn.execute(f"INSERT INTO {P}.cotr_fleet_issue_log (issue_id, status, note, by_name, by_user) VALUES (%s, %s, %s, %s, %s)",
                     (i["id"], to, " · ".join(x for x in parts if x) or None,
                      user.get("full_name") or user.get("username"), user["id"]))
    return issue_detail(issue_id)


# ---------------------------------------------------------------- the portal: masters

# entity -> (table, key column, columns written from the payload, ordering)
MASTERS: dict[str, tuple[str, str, tuple[str, ...], str]] = {
    "pools": ("cotr_pool", "code", ("code", "name", "kind", "parent_code", "city", "address", "storage", "is_active",
                                    "latitude", "longitude", "radius_m", "pic_name", "phone", "open_time", "close_time", "note",
                                    "is_production", "esb_branch_code"),
              "kind, name"),
    "fleet": ("cotr_fleet", "code", ("code", "name", "type", "plate", "pool_code", "capacity", "condition", "last_service",
                                     "is_active", "brand", "model", "frame_no", "acquired_on", "qris_bank", "qris_nmid",
                                     "qris_merchant", "note", "asset_no", "color", "year_made", "battery_no", "stnk_no",
                                     "stnk_expires", "insurance_no", "insurance_expires", "odometer_km", "next_service",
                                     "gps_id"), "code"),
    "fleet-types": ("cotr_fleet_type", "code", ("code", "name", "sort"), "sort, name"),
    # migration 080: the selling / standby places of the drivers and the accessories of the fleet
    "areas": ("cotr_area", "code", ("code", "name", "address", "district", "city", "province", "postal_code", "latitude",
                                    "longitude", "radius_m", "pool_code", "note", "is_active"), "name"),
    "accessories": ("cotr_accessory", "code", ("code", "name", "category", "unit", "serial_no", "condition", "note",
                                               "is_active"), "category, name"),
    "incentive-schemes": ("cotr_incentive_scheme", "code", ("code", "name", "valid_from", "tiers", "product_bonus",
                                                            "late_penalty", "absent_penalty", "min_attendance_pct",
                                                            "is_active", "note"), "valid_from DESC, code"),
    "shift-templates": ("cotr_shift_template", "code", ("code", "name", "start_time", "end_time", "break_min", "grace_min",
                                                        "color", "is_active", "sort", "pay_rate"), "sort, start_time"),
    "request-kinds": ("cotr_request_kind", "code", ("code", "name", "description", "max_amount", "needs_photo", "sort", "is_active"),
                      "sort, name"),  # migration 083
    "menu-categories": ("cotr_menu_category", "code", ("code", "name", "sort", "is_active"), "sort, name"),
    "menus": ("cotr_menu", "code", ("code", "name", "category", "price", "bom_code", "is_active", "sort", "shelf_life_days", "esb_aliases"), "sort, name"),
    "boms": ("cotr_bom", "code", ("code", "name", "output", "yield_qty", "yield_unit", "lines", "kind", "units", "note"),
             "kind DESC, name"),
    "items": ("cotr_item", "code", ("code", "name", "category", "unit", "pack_unit", "pack_qty", "cost", "is_stock",
                                    "is_active", "note"), "category, name"),
    "drivers": ("cotr_driver", "code", ("code", "name", "phone", "pool_code", "fleet_code", "status", "joined_on", "user_id",
                                        "nik", "birth_place", "birth_date", "gender", "address", "emergency_name",
                                        "emergency_phone", "employment_status", "position", "contract_end", "salary_monthly",
                                        "allowance_monthly", "bank_name", "bank_account", "bank_holder", "note",
                                        "employee_id", "nickname", "department", "bike_code"), "code"),
}
# read and written only by superadmins and the COTR management (page cotr.management, migration 050)
SENSITIVE = {"drivers": ("nik", "salary_monthly", "allowance_monthly", "bank_name", "bank_account", "bank_holder")}
# pictures of a master row: slot -> (column of the bytes, column of the type)
IMAGES = {"drivers": {"photo": ("photo", "photo_type")},
          "fleet": {"photo": ("photo", "photo_type"), "qris": ("qris_image", "qris_image_type")}}
_BYTES = {"photo", "photo_type", "qris_image", "qris_image_type", "updated_by"}
UPPER_CODES = {"menu-categories", "pools", "fleet", "menus", "boms", "drivers", "items", "shift-templates", "incentive-schemes", "areas", "accessories"}  # fleet types keep their code as typed (sepeda_listrik)


def _plain(row: dict) -> dict:
    out = {k: (float(v) if isinstance(v, Decimal) else _iso(v)) for k, v in row.items() if k not in _BYTES}
    for k in ("open_time", "close_time", "start_time", "end_time"):
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
    if col == "esb_aliases":  # migration 095: the ESB / workbook names of a menu, lower case, spaces collapsed
        return sorted({" ".join(str(s).lower().split()) for s in (v.splitlines() if isinstance(v, str) else (v or [])) if str(s).strip()})
    if col == "storage":
        return [s.strip() for s in (v.split(",") if isinstance(v, str) else (v or [])) if str(s).strip()]
    if col == "tiers":  # [{from, to | null, rate}] sorted, not overlapping
        tiers = sorted(({"from": int(t.get("from") or 0), "to": int(t["to"]) if t.get("to") not in (None, "") else None,
                         "rate": float(t.get("rate") or 0)} for t in (v or [])), key=lambda t: t["from"])
        for a, b in zip(tiers, tiers[1:]):
            if a["to"] is None or a["to"] > b["from"]:
                raise CotrError("Tier insentif tumpang tindih")
        if any(t["to"] is not None and t["to"] <= t["from"] for t in tiers) or any(t["rate"] < 0 for t in tiers):
            raise CotrError("Tier insentif tidak valid")
        return Jsonb(tiers)
    if col == "product_bonus":
        return Jsonb([{"menu": str(b.get("menu", "")).strip().upper(), "rate": float(b.get("rate") or 0)}
                      for b in (v or []) if str(b.get("menu", "")).strip()])
    if col == "valid_from" and v:
        return str(v)[:7] + "-01"
    if col == "units":  # the containers of a premix: [{name, grams}]
        units = [{"name": str(u.get("name", "")).strip(), "grams": float(u.get("grams") or 0)} for u in (v or []) if str(u.get("name", "")).strip()]
        if any(u["grams"] <= 0 for u in units):
            raise CotrError("Gram per satuan harus lebih dari 0")
        return Jsonb(units)
    return v


_NOT_NULL: dict[str, set[str]] = {}


def _not_null_text(table: str) -> set[str]:
    """The text columns of a master table that may not be null (cached for the process)."""
    if table not in _NOT_NULL:
        _NOT_NULL[table] = {r["column_name"] for r in db.fetch(
            """SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s
                AND is_nullable = 'NO' AND data_type IN ('text', 'character varying')""", (P, table))}
    return _NOT_NULL[table]


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
    # a text field left empty in a column that may not be null (address, city …) is stored empty, not null (2026-11-09)
    for c in _not_null_text(table):
        if c in row and row[c] is None:
            row[c] = ""
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


DEFAULT_POOL = "DEFAULT"  # migration 082


def master_delete(entity: str, key: str) -> None:
    table, pk, _, _ = MASTERS[entity]
    if entity == "pools" and key == DEFAULT_POOL:
        raise CotrError("Pool Default tidak dapat dihapus")
    try:
        with db.transaction() as conn:
            if entity == "pools":  # its employees and fleet go to the Pool Default (2026-11-09), the pools under it lose their parent
                conn.execute(f"UPDATE {P}.cotr_driver SET pool_code = %s, updated_at = now() WHERE pool_code = %s", (DEFAULT_POOL, key))
                conn.execute(f"UPDATE {P}.cotr_fleet SET pool_code = %s, updated_at = now() WHERE pool_code = %s", (DEFAULT_POOL, key))
                conn.execute(f"UPDATE {P}.cotr_pool SET parent_code = NULL WHERE parent_code = %s", (key,))
            if entity == "menu-categories":
                name = conn.execute(f"SELECT name FROM {P}.cotr_menu_category WHERE code = %s", (key,)).fetchone()
                if name and conn.execute(f"SELECT 1 FROM {P}.cotr_menu WHERE category = %s LIMIT 1", (name["name"],)).fetchone():
                    raise CotrError("Kategori masih dipakai menu; ganti kategori menunya dulu")
            conn.execute(f"DELETE FROM {P}.{table} WHERE {pk} = %s", (key,))
    except CotrError:
        raise
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
    if "not-null" in text or "null value" in text:
        col = text.split('column "')[1].split('"')[0] if 'column "' in text else ""
        return f"Isian wajib belum diisi{f' ({col})' if col else ''}"
    return "Data tidak dapat disimpan"


def link_account(user_id: str, driver_code: Optional[str]) -> None:
    """User Accounts (2026-11-12): the account belongs to this employee of Master Driver (None = to none); an employee
    has one account, so the one it had before is untied."""
    with db.transaction() as conn:
        if driver_code:
            d = conn.execute(f"SELECT code, user_id FROM {P}.cotr_driver WHERE code = %s FOR UPDATE", (driver_code,)).fetchone()
            if not d:
                raise CotrError("Karyawan COTR tidak ditemukan")
        conn.execute(f"UPDATE {P}.cotr_driver SET user_id = NULL WHERE user_id = %s AND code IS DISTINCT FROM %s", (user_id, driver_code))
        if driver_code:
            conn.execute(f"UPDATE {P}.cotr_driver SET user_id = %s WHERE code = %s", (user_id, driver_code))


def own_pool(user: dict) -> Optional[str]:
    """#16 (2026-11-12): the pool / HUB a COTR account is bound to — its employee's pool when it is not a superadmin
    and has no Storage & Production (the COTR management chooses any pool); None = free to choose."""
    from app.access import has_page
    if not user.get("id") or user.get("role") == "superadmin" or has_page(user, "cotr.production"):
        return None
    d = driver_of_account(str(user["id"]))
    return d["pool"] if d and d.get("pool") else None


def driver_of_account(user_id: str) -> Optional[dict]:
    r = db.fetchrow(f"""SELECT d.code, d.name, d.nickname, d.pool_code, d.department, p.name AS pool_name FROM {P}.cotr_driver d
                         LEFT JOIN {P}.cotr_pool p ON p.code = d.pool_code WHERE d.user_id = %s""", (user_id,))
    return {"code": r["code"], "name": r["name"], "nickname": r["nickname"], "pool": r["pool_code"], "poolName": r["pool_name"],
            "department": r["department"]} if r else None


def employees(q: Optional[str] = None) -> list[dict]:
    """The employees of Master Driver to pick an account for (User Accounts → COTR), with the account they have."""
    like = f"%{q.strip()}%" if q and q.strip() else None
    rows = db.fetch(f"""SELECT d.code, d.name, d.nickname, d.phone, d.pool_code, p.name AS pool_name, d.department, d.position,
                               d.employee_id, u.username
                          FROM {P}.cotr_driver d LEFT JOIN {P}.cotr_pool p ON p.code = d.pool_code
                          LEFT JOIN {P}.user_account u ON u.id = d.user_id
                         WHERE d.status <> 'inactive' AND (%s::text IS NULL OR d.name ILIKE %s OR d.nickname ILIKE %s OR d.code ILIKE %s)
                         ORDER BY p.name NULLS LAST, d.department, d.name LIMIT 400""", (like, like, like, like))
    return [{"code": r["code"], "name": r["name"], "nickname": r["nickname"], "phone": r["phone"], "pool": r["pool_code"],
             "poolName": r["pool_name"], "department": r["department"], "position": r["position"], "username": r["username"]} for r in rows]


def driver_accounts() -> list[dict]:
    """Accounts of role driver, with the driver each one is tied to (Master Driver: the account select)."""
    return [{"id": str(r["id"]), "username": r["username"], "name": r["full_name"], "driver": r["driver"]}
            for r in db.fetch(f"""SELECT u.id, u.username, u.full_name, d.code AS driver FROM {P}.user_account u
                                    LEFT JOIN {P}.cotr_driver d ON d.user_id = u.id
                                   WHERE u.role = 'driver' AND u.is_active ORDER BY u.username""")]


def waste_list(day_from: date, day_to: date, pool: Optional[str] = None) -> list[dict]:
    """Waste and cupping of the pools / HUBs (migration 088: per menu too), with who recorded it."""
    return [_plain(r) | {"id": str(r["id"])} for r in db.fetch(
        f"""SELECT w.*, u.full_name AS by_name FROM {P}.cotr_waste w LEFT JOIN {P}.user_account u ON u.id = w.created_by
             WHERE w.day BETWEEN %s AND %s AND (%s::text IS NULL OR w.pool_code = %s)
             ORDER BY w.day DESC, w.created_at DESC""", (day_from, day_to, pool, pool))]


def waste_add(user: dict, data: dict) -> dict:
    """A waste (storage / production) or a cupping of a pool / HUB: a menu in cups (migration 088), a stock item (deducted
    from the running stock) or another thing typed."""
    kind = data.get("kind") or "storage"
    menu = (data.get("menu_code") or "").strip() or None
    code = None if menu else (data.get("item_code") or "").strip() or None
    item, unit = (data.get("item") or code or "").strip(), data.get("unit")
    if menu:
        m = _menu_map().get(menu)
        if not m:
            raise CotrError("Menu tidak ditemukan")
        item, unit = m["name"], "cup"
    if not data.get("pool_code"):
        raise CotrError("Pilih pool / HUB")
    if not item:
        raise CotrError("Pilih menu atau item")
    if not (data.get("reason") or "").strip():
        raise CotrError("Isi keterangan cupping" if kind == "cupping" else "Isi alasan waste")
    try:
        r = db.fetchrow(f"""INSERT INTO {P}.cotr_waste (day, pool_code, item, qty, unit, reason, created_by, kind, batch_id, item_code, menu_code)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                        (data.get("day") or today(), data.get("pool_code"), item, data.get("qty"), unit,
                         (data.get("reason") or "").strip(), user["id"], kind, data.get("batch_id") or None, code, menu))
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
    # 2026-11-08: the deductions of a shift (cupping and waste, per menu at its price), what the fleet carried and where
    # the roster sent the driver
    menu = _menu_map()
    dispatch = {x["id"]: x for x in db.fetch(f"SELECT id, cupping FROM {P}.cotr_dispatch WHERE day = %s", (day,))}
    acc: dict = {}
    for r in (db.fetch(f"""SELECT sa.shift_id, a.name, sa.qty FROM {P}.cotr_shift_accessory sa
                            JOIN {P}.cotr_accessory a ON a.code = sa.accessory_code WHERE sa.shift_id = ANY(%s)""", (ids,)) if ids else []):
        acc.setdefault(r["shift_id"], []).append({"name": r["name"], "qty": r["qty"]})
    slots: dict = {}
    for r in db.fetch(f"""SELECT s.driver_code, s.start_time, s.end_time, a.name AS area FROM {P}.cotr_schedule_slot s
                           LEFT JOIN {P}.cotr_area a ON a.code = s.area_code WHERE s.day = %s ORDER BY s.start_time""", (day,)):
        slots.setdefault(r["driver_code"], []).append({"start": r["start_time"].strftime("%H:%M"), "end": r["end_time"].strftime("%H:%M"), "area": r["area"]})

    def deductions(sh: dict) -> dict:
        assigned = (dispatch.get(sh.get("dispatch_id")) or {}).get("cupping") or {}
        rows = []
        for kind, src in (("cupping", sh.get("end_cupping") or {}), ("waste", sh.get("end_waste") or {})):
            for code, q in src.items():
                q = int(q)
                if q:
                    price = float((menu.get(code) or {}).get("price") or 0)
                    rows.append({"kind": kind, "code": code, "name": (menu.get(code) or {}).get("name") or code, "qty": q, "price": price, "value": price * q,
                                 "assigned": int(assigned.get(code, 0)) if kind == "cupping" else None})
        for code, q in assigned.items():  # cupping assigned by the storage, not reported yet at the end
            if int(q) and not any(r["kind"] == "cupping" and r["code"] == code for r in rows):
                price = float((menu.get(code) or {}).get("price") or 0)
                rows.append({"kind": "cupping", "code": code, "name": (menu.get(code) or {}).get("name") or code, "qty": 0, "price": price, "value": 0.0, "assigned": int(q)})
        return {"lines": rows, "cups": sum(r["qty"] for r in rows), "value": sum(r["value"] for r in rows),
                "cupping": sum(r["value"] for r in rows if r["kind"] == "cupping"), "waste": sum(r["value"] for r in rows if r["kind"] == "waste")}
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
            "deductions": deductions(s), "accessories": acc.get(s["id"], []), "slots": slots.get(s["driver_code"], []),
            "phone": d.get("phone"),
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
    # the drivers only: the team of the workbook DATA COTR (migration 060) also holds the storage, stockist … staff
    drivers = db.fetch(f"""SELECT d.code, d.name, d.nickname, d.pool_code, d.fleet_code, d.status FROM {P}.cotr_driver d
                            WHERE d.status <> 'inactive' AND (d.department IS NULL OR d.department ILIKE 'armada%%') {where}
                            ORDER BY d.fleet_code NULLS LAST, d.code""", args)
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
        rows.append({"code": d["code"], "name": d["name"], "nickname": d["nickname"], "pool": d["pool_code"], "fleet": d["fleet_code"],
                     "status": d["status"], "dispatch": _dispatch_view(disp.get(d["code"])), "previous": prev.get(d["code"]),
                     "shift": s["status"] if s else None})
    advice = suggestions(day, codes)
    for r in rows:
        r["advice"] = advice.get(r["code"])
    return {"day": day.isoformat(), "drivers": rows,
            "reloads": [_reload_view(r) | {"driver": r["driver_code"]} for r in reloads]}


def assign(user: dict, day: date, driver_code: str, lines: dict, note: Optional[str], cupping: Optional[dict] = None) -> dict:
    """The storage assigns the cups of the day to a driver; changeable until the driver has accepted them. `lines` is
    what the driver carries (the start stock); `cupping` (migration 061) the part of it for tasting, at most the lines."""
    menus = _menu_map()
    given = clean_stock(lines, menus)
    if not given:
        raise CotrError("Isi minimal satu produk")
    taste = clean_stock(cupping or {}, menus)
    if any(n > given.get(k, 0) for k, n in taste.items()):
        raise CotrError("Cupping tidak boleh melebihi produk yang dibawa")
    d = db.fetchrow(f"SELECT code, pool_code FROM {P}.cotr_driver WHERE code = %s", (driver_code,))
    if not d:
        raise CotrError("Driver tidak ditemukan")
    from app import cotr_ledger  # migration 089: never more than the pool has, once it has an opening stock
    cotr_ledger.check_assignment(d["pool_code"], day, driver_code, given)
    row = db.fetchrow(
        f"""INSERT INTO {P}.cotr_dispatch (day, driver_code, pool_code, lines, note, assigned_by, cupping)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (day, driver_code) DO UPDATE SET lines = EXCLUDED.lines, note = EXCLUDED.note, status = 'assigned',
                cupping = EXCLUDED.cupping,
                assigned_at = now(), assigned_by = EXCLUDED.assigned_by, driver_counts = NULL, driver_note = NULL, checked_at = NULL
              WHERE {P}.cotr_dispatch.status <> 'accepted'
            RETURNING *""",
        (day, driver_code, d["pool_code"], Jsonb(given), (note or "").strip() or None, user["id"], Jsonb(taste)))
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
    table = {"check-in": "cotr_check_in", "request": "cotr_request", "issue": "cotr_fleet_issue"}[kind]
    return db.fetchrow(f"SELECT photo, photo_type FROM {P}.{table} WHERE id = %s", (row_id,))


def photo_owner(kind: str, row_id: str) -> Optional[str]:
    """The driver code a photo belongs to (a driver sees only its own)."""
    if kind in ("request", "issue"):
        table = "cotr_request" if kind == "request" else "cotr_fleet_issue"
        r = db.fetchrow(f"SELECT driver_code FROM {P}.{table} WHERE id::text = %s", (row_id,))
    else:
        r = db.fetchrow(f"""SELECT s.driver_code FROM {P}.cotr_check_in c JOIN {P}.cotr_shift s ON s.id = c.shift_id
                             WHERE c.id = %s""", (row_id,))
    return r["driver_code"] if r else None


def _esb_days(d0: date, d1: date, kinds: tuple[str, ...], pool: Optional[str] = None) -> list[dict]:
    """The ESB company COTR per pool day where the workbooks have none (migration 090): rows {day, code, kind, qty}."""
    from app import cotr_esb
    sheet = {(r["pool_code"], r["day"]) for r in db.fetch(
        f"SELECT DISTINCT pool_code, day FROM {P}.cotr_pool_move WHERE source = 'sheet' AND day BETWEEN %s AND %s", (d0, d1))}
    out = []
    for p, days in cotr_esb.figures(d0, d1).items():
        if pool and p != pool:
            continue
        for d, menus_ in days.items():
            if (p, d) in sheet:
                continue
            for code, f in menus_.items():
                out += [{"day": d, "code": code, "kind": k, "qty": f[k]} for k in kinds if f.get(k)]
    return out


def production(days: int, buffer_pct: float) -> dict:
    """The production plan from the sales of the last `days` days (cups per menu and day, every driver)."""
    rows = db.fetch(
        f"""SELECT sh.day, l->>'code' AS code, sum((l->>'qty')::int) AS qty
              FROM {P}.cotr_sale s JOIN {P}.cotr_shift sh ON sh.id = s.shift_id, jsonb_array_elements(s.lines) l
             WHERE s.voided_at IS NULL AND sh.day >= current_date - %s AND sh.day < current_date
             GROUP BY 1, 2""", (days,))
    # migration 089: a pool day taken from the workbooks counts its Product Terjual (the app has no sales there)
    rows = list(rows) + db.fetch(
        f"""SELECT day, menu_code AS code, sum(qty) AS qty FROM {P}.cotr_pool_move
             WHERE source = 'sheet' AND kind = 'sold' AND day >= current_date - %s AND day < current_date GROUP BY 1, 2""", (days,))
    rows += [r for r in _esb_days(today() - timedelta(days=days), today() - timedelta(days=1), ("sold",))]
    merged: dict[tuple, int] = {}
    for r in rows:
        merged[(r["day"], r["code"])] = merged.get((r["day"], r["code"]), 0) + int(r["qty"])
    rows = [{"day": d, "code": c, "qty": q} for (d, c), q in merged.items()]
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
    # migration 089: the pool days imported from the workbooks (Product Terjual, Wasting, Cupping)
    sw, sa = ("AND pool_code = %s", (pool,)) if pool else ("", ())
    for r in db.fetch(f"""SELECT menu_code, kind, sum(qty) AS qty FROM {P}.cotr_pool_move
                           WHERE source = 'sheet' AND kind IN ('sold', 'waste', 'cupping') AND day BETWEEN %s AND %s {sw} GROUP BY 1, 2""",
                      (d_from, d_to, *sa)):
        cups.setdefault(r["menu_code"], {"sold": 0, "waste": 0, "cupping": 0})[r["kind"]] += int(r["qty"])
    # migration 090: the ESB company COTR (simple sales, cupping / waste journals) where the workbooks have no day
    for r in _esb_days(d_from, d_to, ("sold", "waste", "cupping"), pool):
        cups.setdefault(r["code"], {"sold": 0, "waste": 0, "cupping": 0})[r["kind"]] += r["qty"]
    # migration 088: the cups wasted or given for tasting at the pools / HUBs
    pw, pa = ("AND pool_code = %s", (pool,)) if pool else ("", ())
    for r in db.fetch(f"""SELECT menu_code, kind, sum(qty) AS qty FROM {P}.cotr_waste
                           WHERE menu_code IS NOT NULL AND day BETWEEN %s AND %s {pw} GROUP BY 1, 2""", (d_from, d_to, *pa)):
        src = "cupping" if r["kind"] == "cupping" else "waste"
        cups.setdefault(r["menu_code"], {"sold": 0, "waste": 0, "cupping": 0})[src] += float(r["qty"])
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


# ---------------------------------------------------------------- master schedule (migration 052)

STATUSES = ("work", "off", "leave", "sick")


def _minutes(t: Any) -> int:
    h, m = str(t)[:5].split(":")
    return int(h) * 60 + int(m)


def attendance(entry: Optional[dict], template: Optional[dict], started: Optional[str], ended: Optional[str]) -> str:
    """The attendance of one driver on one day: the roster against the check in (times "HH:MM" in Jakarta).
    work + check in -> on_time / late (after start + grace); work without -> absent (planned while the day is to come
    is decided by the caller); no roster but a check in -> unscheduled; off / leave / sick -> the status."""
    if entry is None or entry["status"] != "work":
        if started:
            return "unscheduled"
        return entry["status"] if entry else "none"
    if not started:
        return "absent"
    if template and _minutes(started) > _minutes(template["start_time"]) + int(template["grace_min"] or 0):
        return "late"
    return "on_time"


def schedule(d_from: date, d_to: date, pool: Optional[str]) -> dict:
    """The roster of a period with the check ins of the drivers and a recap per driver (for the incentives)."""
    if (d_to - d_from).days > 62:
        raise CotrError("Rentang jadwal maksimal 62 hari")
    templates = {t["code"]: _plain(t) for t in db.fetch(f"SELECT * FROM {P}.cotr_shift_template ORDER BY sort")}
    drivers = db.fetch(f"""SELECT code, name, nickname, department, pool_code, fleet_code, status FROM {P}.cotr_driver
                            WHERE status <> 'inactive' OR code IN (SELECT driver_code FROM {P}.cotr_schedule
                                                                    WHERE day BETWEEN %s AND %s)
                            ORDER BY pool_code NULLS LAST, name""", (d_from, d_to))
    if pool:
        drivers = [d for d in drivers if d["pool_code"] == pool]
    entries = {(r["driver_code"], r["day"]): r for r in db.fetch(
        f"SELECT * FROM {P}.cotr_schedule WHERE day BETWEEN %s AND %s", (d_from, d_to))}
    slots: dict[tuple, list[dict]] = {}
    for r in db.fetch(f"""SELECT s.*, a.name AS area_name FROM {P}.cotr_schedule_slot s LEFT JOIN {P}.cotr_area a ON a.code = s.area_code
                           WHERE s.day BETWEEN %s AND %s ORDER BY s.start_time""", (d_from, d_to)):
        slots.setdefault((r["driver_code"], r["day"]), []).append(
            {"id": r["id"], "template": r["template_code"], "start": r["start_time"].strftime("%H:%M"), "end": r["end_time"].strftime("%H:%M"),
             "area": r["area_code"], "areaName": r["area_name"], "fleet": r["fleet_code"], "note": r["note"]})
    shifts = {(r["driver_code"], r["day"]): r for r in db.fetch(
        f"""SELECT driver_code, day, to_char(started_at AT TIME ZONE 'Asia/Jakarta', 'HH24:MI') AS started,
                   to_char(ended_at AT TIME ZONE 'Asia/Jakarta', 'HH24:MI') AS ended, status
              FROM {P}.cotr_shift WHERE day BETWEEN %s AND %s""", (d_from, d_to))}
    now = today()
    cells, recap = [], []
    for d in drivers:
        tally = {k: 0 for k in ("scheduled", "worked", "on_time", "late", "absent", "off", "leave", "sick", "unscheduled")}
        pay = 0.0  # the day rate of the shift of every day worked (migration 060: N / H / LS of the payroll sheets)
        day = d_from
        while day <= d_to:
            e, sh = entries.get((d["code"], day)), shifts.get((d["code"], day))
            t = templates.get(e["template_code"]) if e and e["template_code"] else None
            state = attendance(e, t, sh["started"] if sh else None, sh["ended"] if sh else None)
            if state == "absent" and day >= now:
                state = "planned"
            if e or sh:
                cells.append({"driver": d["code"], "day": day.isoformat(), "status": e["status"] if e else None,
                              "template": e["template_code"] if e else None, "pool": e["pool_code"] if e else None,
                              "note": e["note"] if e else None, "started": sh["started"] if sh else None,
                              "ended": sh["ended"] if sh else None, "attendance": state, "slots": slots.get((d["code"], day), [])})
            if e and e["status"] == "work":
                tally["scheduled"] += 1
            if sh:
                tally["worked"] += 1
                if t and t.get("pay_rate") is not None:
                    pay += float(t["pay_rate"])
            if state in tally:
                tally[state] += 1
            day = date.fromordinal(day.toordinal() + 1)
        recap.append({"driver": d["code"], **tally, "pay": pay})
    return {"from": d_from.isoformat(), "to": d_to.isoformat(), "templates": list(templates.values()),
            "drivers": [{k: d[k] for k in ("code", "name", "nickname", "department", "pool_code", "fleet_code", "status")} for d in drivers],
            "cells": cells, "recap": recap}


def schedule_save(user: dict, entries: list[dict]) -> int:
    """Upsert roster cells; a cell with status null is cleared."""
    n = 0
    with db.transaction() as conn, conn.cursor() as cur:
        for e in entries:
            status = e.get("status")
            if status is None:
                cur.execute(f"DELETE FROM {P}.cotr_schedule WHERE driver_code = %s AND day = %s", (e["driver"], e["day"]))
                cur.execute(f"DELETE FROM {P}.cotr_schedule_slot WHERE driver_code = %s AND day = %s", (e["driver"], e["day"]))
                n += 1
                continue
            if status not in STATUSES:
                raise CotrError(f"Status jadwal tidak dikenal: {status}")
            template = e.get("template") if status == "work" else None
            if status == "work" and not template:
                raise CotrError("Jadwal kerja butuh shift")
            given = e.get("slots") if status == "work" else []
            if given:  # several assignments in the day (migration 080): the first one is the day's shift
                given = sorted(given, key=lambda x: x["start"])
                template = template or given[0].get("template")
                if not template:
                    raise CotrError("Slot pertama butuh template shift")
            cur.execute(
                f"""INSERT INTO {P}.cotr_schedule (driver_code, day, status, template_code, pool_code, note, updated_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (driver_code, day) DO UPDATE SET status = EXCLUDED.status,
                        template_code = EXCLUDED.template_code, pool_code = EXCLUDED.pool_code, note = EXCLUDED.note,
                        updated_at = now(), updated_by = EXCLUDED.updated_by""",
                (e["driver"], e["day"], status, template, e.get("pool") or None, e.get("note") or None, user["id"]))
            if given is not None:
                cur.execute(f"DELETE FROM {P}.cotr_schedule_slot WHERE driver_code = %s AND day = %s", (e["driver"], e["day"]))
                for x in given:
                    if x["end"] <= x["start"]:
                        raise CotrError(f"Jam selesai slot {x['start']} harus setelah jam mulai")
                    cur.execute(
                        f"""INSERT INTO {P}.cotr_schedule_slot (driver_code, day, template_code, start_time, end_time, area_code,
                                fleet_code, note, updated_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (e["driver"], e["day"], x.get("template") or None, x["start"], x["end"], x.get("area") or None,
                         x.get("fleet") or None, x.get("note") or None, user["id"]))
                for a, b in zip(given, given[1:]):
                    if b["start"] < a["end"]:
                        raise CotrError(f"Slot {a['start']}–{a['end']} dan {b['start']}–{b['end']} bertumpuk")
            n += 1
    return n


def schedule_copy(user: dict, src: date, dst: date, days: int, drivers: Optional[list[str]]) -> int:
    """Copy the roster of `days` days from `src` to `dst` (e.g. last week to this week), overwriting the target."""
    if not 1 <= days <= 31:
        raise CotrError("Jumlah hari salin 1–31")
    offset = dst.toordinal() - src.toordinal()
    if offset == 0:
        raise CotrError("Tanggal asal dan tujuan sama")
    where, args = ("AND driver_code = ANY(%s)", (drivers,)) if drivers else ("", ())
    with db.transaction() as conn, conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {P}.cotr_schedule (driver_code, day, status, template_code, pool_code, note, updated_by)
                SELECT driver_code, day + %s, status, template_code, pool_code, note, %s FROM {P}.cotr_schedule
                 WHERE day >= %s AND day < %s {where}
                ON CONFLICT (driver_code, day) DO UPDATE SET status = EXCLUDED.status,
                    template_code = EXCLUDED.template_code, pool_code = EXCLUDED.pool_code, note = EXCLUDED.note,
                    updated_at = now(), updated_by = EXCLUDED.updated_by""",
            (offset, user["id"], src, date.fromordinal(src.toordinal() + days), *args))
        n = cur.rowcount
        end = date.fromordinal(src.toordinal() + days)
        cur.execute(f"""DELETE FROM {P}.cotr_schedule_slot WHERE day >= %s AND day < %s {where}""",
                    (date.fromordinal(dst.toordinal()), date.fromordinal(dst.toordinal() + days), *args))
        cur.execute(
            f"""INSERT INTO {P}.cotr_schedule_slot (driver_code, day, template_code, start_time, end_time, area_code, fleet_code, note, updated_by)
                SELECT driver_code, day + %s, template_code, start_time, end_time, area_code, fleet_code, note, %s
                  FROM {P}.cotr_schedule_slot WHERE day >= %s AND day < %s {where}""", (offset, user["id"], src, end, *args))
        return n


# ---------------------------------------------------------------- incentives (migration 053)

def tier_pay(cups: int, tiers: list[dict]) -> list[dict]:
    """The cups of every segment of the tiers and their pay: 2600 cups on the default = 500 × 500 + 600 × 1000."""
    out = []
    for t in tiers:
        top = cups if t.get("to") is None else min(cups, int(t["to"]))
        n = max(0, top - int(t["from"]))
        out.append({"from": int(t["from"]), "to": t.get("to"), "rate": float(t["rate"]), "cups": n, "pay": n * float(t["rate"])})
    return out


def incentive(cups: int, by_menu: dict[str, int], att: dict, scheme: dict) -> dict:
    """The incentive of one driver for one month: tiers + product bonus − late / absent deductions (never below 0);
    nothing when the attendance (worked / scheduled) is under the minimum of the scheme."""
    tiers = tier_pay(cups, scheme.get("tiers") or [])
    bonus = [{"menu": b["menu"], "rate": float(b["rate"]), "cups": int(by_menu.get(b["menu"], 0)),
              "pay": int(by_menu.get(b["menu"], 0)) * float(b["rate"])} for b in scheme.get("product_bonus") or []]
    gross = sum(t["pay"] for t in tiers) + sum(b["pay"] for b in bonus)
    deduction = att.get("late", 0) * float(scheme.get("late_penalty") or 0) + att.get("absent", 0) * float(scheme.get("absent_penalty") or 0)
    scheduled = att.get("scheduled", 0)
    pct = round(att.get("worked", 0) * 100 / scheduled, 1) if scheduled else None
    minimum = float(scheme.get("min_attendance_pct") or 0)
    eligible = not minimum or (pct is not None and pct >= minimum)
    total = max(0.0, gross - deduction) if eligible else 0.0
    return {"cups": cups, "tiers": tiers, "bonus": bonus, "gross": gross, "deduction": deduction,
            "attendancePct": pct, "eligible": eligible, "total": total}


def scheme_for(month: date, code: Optional[str]) -> dict:
    if code:
        row = db.fetchrow(f"SELECT * FROM {P}.cotr_incentive_scheme WHERE code = %s", (code,))
    else:
        row = db.fetchrow(f"""SELECT * FROM {P}.cotr_incentive_scheme WHERE is_active AND valid_from <= %s
                               ORDER BY valid_from DESC LIMIT 1""", (month,))
    if not row:
        raise CotrError("Belum ada skema insentif untuk bulan ini")
    return _plain(row)


def incentives(month: date, code: Optional[str], pool: Optional[str]) -> dict:
    """The incentives of every driver for a month: cups sold (not voided) per menu, attendance of Master Schedule."""
    start = month.replace(day=1)
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    last = date.fromordinal(end.toordinal() - 1)
    scheme = scheme_for(start, code)
    sold = db.fetch(f"""SELECT sh.driver_code, l->>'code' AS code, sum((l->>'qty')::int) AS qty FROM {P}.cotr_sale s
                          JOIN {P}.cotr_shift sh ON sh.id = s.shift_id, jsonb_array_elements(s.lines) l
                         WHERE s.voided_at IS NULL AND sh.day >= %s AND sh.day < %s GROUP BY 1, 2""", (start, end))
    per: dict[str, dict[str, int]] = {}
    for r in sold:
        per.setdefault(r["driver_code"], {})[r["code"]] = int(r["qty"])
    # migration 089 (2026-11-12): the days of the Report Armada sheets where the driver has no shift in the app count too
    app_days = {(r["driver_code"], r["day"]) for r in db.fetch(
        f"SELECT driver_code, day FROM {P}.cotr_shift WHERE day >= %s AND day < %s", (start, end))}
    sheet_days: dict[str, int] = {}
    for r in db.fetch(f"""SELECT driver_code, day, lines FROM {P}.cotr_report_day
                           WHERE driver_code IS NOT NULL AND day >= %s AND day < %s""", (start, end)):
        if (r["driver_code"], r["day"]) in app_days or not r["lines"]:
            continue
        sheet_days[r["driver_code"]] = sheet_days.get(r["driver_code"], 0) + 1
        for k, x in r["lines"].items():
            per.setdefault(r["driver_code"], {})[k] = per.get(r["driver_code"], {}).get(k, 0) + int(x.get("t") or 0)
    roster = schedule(start, last, pool) if (last - start).days <= 62 else {"drivers": [], "recap": []}
    att = {r["driver"]: r for r in roster["recap"]}
    adjust = pay_adjustments(start)
    rows = []
    for d in roster["drivers"]:
        if not str(d.get("department") or "armada").lower().startswith("armada"):
            continue  # the incentive is of the armada; storage, stockist, CK … are paid otherwise (2026-11-12)
        by_menu = per.get(d["code"], {})
        a = dict(att.get(d["code"], {}))
        if sheet_days.get(d["code"]):
            a["worked"] = a.get("worked", 0) + sheet_days[d["code"]]
            a["scheduled"] = max(a.get("scheduled", 0), a["worked"])
        row = {"driver": d["code"], "name": d["name"], "pool": d["pool_code"],
               "attendance": {k: a.get(k, 0) for k in ("scheduled", "worked", "late", "absent")},
               **incentive(sum(by_menu.values()), by_menu, a, scheme)}
        adj = adjust.get(d["code"], {})
        row["adjustments"] = {k: adj.get(k, 0.0) for k in PAY_KINDS}
        row["takeHome"] = row["total"] + adj.get("bonus", 0.0) - sum(adj.get(k, 0.0) for k in PAY_DEDUCTIONS)
        rows.append(row)
    rows.sort(key=lambda r: -r["total"])
    return {"month": start.isoformat()[:7], "scheme": scheme, "rows": rows, "published": is_published(start),
            "total": sum(r["total"] for r in rows), "cups": sum(r["cups"] for r in rows),
            "takeHome": sum(r["takeHome"] for r in rows)}


# ---------------------------------------------------------------- pay adjustments and payslips (migration 092)

PAY_KINDS = ("bonus", "kasbon", "minus", "warning", "other")
PAY_DEDUCTIONS = ("kasbon", "minus", "warning", "other")


def pay_adjustments(month: date, driver: Optional[str] = None) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for r in db.fetch(f"""SELECT driver_code, kind, sum(amount) AS amount FROM {P}.cotr_pay_adjustment
                           WHERE month = %s AND (%s::text IS NULL OR driver_code = %s) GROUP BY 1, 2""", (month.replace(day=1), driver, driver)):
        out.setdefault(r["driver_code"], {})[r["kind"]] = float(r["amount"])
    return out


def pay_adjustment_list(month: date, driver: Optional[str] = None) -> list[dict]:
    return [{"id": r["id"], "driver": r["driver_code"], "kind": r["kind"], "amount": float(r["amount"]), "note": r["note"],
             "by": r["by_name"], "at": _iso(r["created_at"])}
            for r in db.fetch(f"""SELECT a.*, u.full_name AS by_name FROM {P}.cotr_pay_adjustment a LEFT JOIN {P}.user_account u ON u.id = a.created_by
                                   WHERE a.month = %s AND (%s::text IS NULL OR a.driver_code = %s) ORDER BY a.created_at""",
                                (month.replace(day=1), driver, driver))]


def add_pay_adjustment(user: dict, driver: str, month: date, kind: str, amount: float, note: Optional[str]) -> dict:
    if kind not in PAY_KINDS:
        raise CotrError("Jenis tidak dikenal")
    if not amount or amount <= 0:
        raise CotrError("Nominal harus lebih dari 0")
    if kind != "bonus" and not (note or "").strip():
        raise CotrError("Tulis keterangan potongan")
    if is_published(month):
        raise CotrError("Slip bulan ini sudah diterbitkan: batalkan penerbitan dulu untuk mengubah")
    try:
        r = db.fetchrow(f"""INSERT INTO {P}.cotr_pay_adjustment (driver_code, month, kind, amount, note, created_by)
                            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                        (driver, month.replace(day=1), kind, amount, (note or "").strip() or None, user["id"]))
    except Exception as e:
        raise CotrError(_db_message(e)) from None
    return {"id": r["id"]}


def delete_pay_adjustment(adjustment_id: int) -> None:
    r = db.fetchrow(f"SELECT month FROM {P}.cotr_pay_adjustment WHERE id = %s", (adjustment_id,))
    if r and is_published(r["month"]):
        raise CotrError("Slip bulan ini sudah diterbitkan: batalkan penerbitan dulu untuk mengubah")
    db.fetchrow(f"DELETE FROM {P}.cotr_pay_adjustment WHERE id = %s RETURNING id", (adjustment_id,))


def is_published(month: date) -> bool:
    return bool(db.fetchrow(f"SELECT 1 FROM {P}.cotr_payslip_month WHERE month = %s", (month.replace(day=1),)))


def publish_payslips(user: dict, month: date, on: bool) -> None:
    if on:
        db.fetchrow(f"""INSERT INTO {P}.cotr_payslip_month (month, published_by) VALUES (%s, %s)
                        ON CONFLICT (month) DO UPDATE SET published_at = now(), published_by = EXCLUDED.published_by RETURNING month""",
                    (month.replace(day=1), user["id"]))
    else:
        db.fetchrow(f"DELETE FROM {P}.cotr_payslip_month WHERE month = %s RETURNING month", (month.replace(day=1),))


def payslip(driver_code: str, month: date, published_only: bool = True) -> Optional[dict]:
    """The payslip of an employee for a month: base salary and allowance of Master Driver, the incentive, the bonus and
    every deduction with its note, and the take home pay. Shown to the employee once the month is published."""
    start = month.replace(day=1)
    if published_only and not is_published(start):
        return None
    d = db.fetchrow(f"""SELECT d.*, p.name AS pool_name FROM {P}.cotr_driver d LEFT JOIN {P}.cotr_pool p ON p.code = d.pool_code
                         WHERE d.code = %s""", (driver_code,))
    if not d:
        return None
    rec = incentives(start, None, d["pool_code"])
    row = next((r for r in rec["rows"] if r["driver"] == driver_code), None)
    items = pay_adjustment_list(start, driver_code)
    salary = float(d.get("salary_monthly") or 0)
    allowance = float(d.get("allowance_monthly") or 0)
    incentive_pay = float(row["total"]) if row else 0.0
    bonus = sum(i["amount"] for i in items if i["kind"] == "bonus")
    deductions = [i for i in items if i["kind"] in PAY_DEDUCTIONS]
    cut = sum(i["amount"] for i in deductions)
    return {"month": start.isoformat()[:7], "employee": {"code": d["code"], "name": d["name"], "nickname": d.get("nickname"),
            "employeeId": d.get("employee_id"), "department": d.get("department"), "position": d.get("position"), "pool": d.get("pool_name"),
            "bank": d.get("bank_name"), "account": d.get("bank_account"), "holder": d.get("bank_holder")},
            "salary": salary, "allowance": allowance, "incentive": incentive_pay, "cups": row["cups"] if row else 0,
            "attendance": row["attendance"] if row else None, "bonus": bonus, "bonusItems": [i for i in items if i["kind"] == "bonus"],
            "deductions": deductions, "deductionTotal": cut,
            "takeHome": round(salary + allowance + incentive_pay + bonus - cut, 2), "published": is_published(start)}


def payslip_months(driver_code: str) -> list[str]:
    return [r["m"] for r in db.fetch(f"""SELECT to_char(month, 'YYYY-MM') AS m FROM {P}.cotr_payslip_month ORDER BY month DESC LIMIT 24""")]


# ---------------------------------------------------------------- the team of the sample "DATA COTR" (migration 060)

# Placement of the sheets -> pool
PLACEMENT = {"palmer": "POOL-PLM", "palmerah": "POOL-PLM", "tebet": "POOL-TBT", "kwitang": "POOL-KWI", "pasming": "POOL-PSM",
             "pasar minggu": "POOL-PSM", "cipete": "POOL-CPT", "ratna": "POOL-RTN"}


def _pool_of(placement: str) -> Optional[str]:
    return PLACEMENT.get(" ".join(str(placement or "").lower().split()))


def parse_team(data: bytes) -> list[dict]:
    """The employees of the workbook "DATA COTR": sheet DATA COTR (placement, department, employee id, names), the bike
    of DATA SEPEDA ARMADA, and the QRIS armada of the Report <pool> sheets matched by the nickname within the pool
    ("BNI 01 BIYAN" -> BIYAN). Empty when the workbook is not that format."""
    import openpyxl

    try:
        wb = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise CotrError("File bukan Excel (.xlsx) yang valid") from None

    def table(name: str) -> list[dict]:
        ws = next((w for w in wb.worksheets if w.title.strip().upper() == name), None)
        if ws is None:
            return []
        rows = list(ws.iter_rows(values_only=True))
        h = next((i for i, r in enumerate(rows[:10]) if any(str(v).strip().upper() == "ID EMPLOYEE" for v in r if v)), None)
        if h is None:
            return []
        head = [" ".join(str(v or "").upper().split()) for v in rows[h]]
        out = []
        for r in rows[h + 1:]:
            row = {head[i]: (" ".join(str(r[i]).split()) if r[i] is not None else "") for i in range(min(len(head), len(r))) if head[i]}
            if row.get("ID EMPLOYEE"):
                out.append(row)
        return out

    team = table("DATA COTR")
    if not team:
        return []
    bikes = {r["ID EMPLOYEE"].upper(): r.get("KODE SEPEDA", "") for r in table("DATA SEPEDA ARMADA")}
    armada: dict[tuple[str, str], str] = {}  # (pool, NICKNAME) -> "BNI 01"
    for ws in wb.worksheets:
        if not ws.title.upper().startswith("REPORT"):
            continue
        pool = _pool_of(ws.title[6:])
        for r in ws.iter_rows(values_only=True):
            for v in r[:3]:
                m = _ARMADA.match(" ".join(str(v).split())) if isinstance(v, str) else None
                nick = m.group(3).strip(" /").upper() if m else ""
                if m and nick and pool and not re.search(r"pesen|customer|total", nick, re.I):
                    armada.setdefault((pool, nick), armada_code(m.group(1), m.group(2)))
    out, seen = [], set()
    for r in team:
        emp = r["ID EMPLOYEE"].upper()
        if emp in seen or not re.match(r"^[A-Z]{2,}\d{3,}$", emp):
            continue
        seen.add(emp)
        pool = _pool_of(r.get("PLACEMENT", ""))
        nick = r.get("NAMA PANGGILAN", "")
        out.append({"employeeId": emp, "name": (r.get("NAMA LENGKAP") or nick).title(), "nickname": nick.title(),
                    "placement": r.get("PLACEMENT", ""), "pool": pool, "department": r.get("DEPARTMENT", ""),
                    "bike": bikes.get(emp, "") or "", "armada": armada.get((pool or "", nick.upper()))})
    return out


def import_team(user: dict, rows: list[dict], apply: bool) -> dict:
    """Preview (apply False) or save the team: one row per employee id (the driver code is the employee id), new ones
    created, the known ones (same employee id) updated; the QRIS armada found in the reports is linked (created when
    missing). A row whose placement is no pool is left out and listed."""
    pools = {r["code"] for r in db.fetch(f"SELECT code FROM {P}.cotr_pool")}
    fleet = {r["code"] for r in db.fetch(f"SELECT code FROM {P}.cotr_fleet")}
    known = {str(r["employee_id"]).upper(): r for r in db.fetch(
        f"SELECT code, name, nickname, pool_code, department, bike_code, fleet_code, employee_id FROM {P}.cotr_driver WHERE employee_id IS NOT NULL")}
    taken = {r["fleet_code"]: r["code"] for r in db.fetch(f"SELECT code, fleet_code FROM {P}.cotr_driver WHERE fleet_code IS NOT NULL")}
    plan, skipped = [], []
    for r in rows:
        if r["pool"] not in pools:
            skipped.append({**r, "reason": f"Placement {r['placement'] or '—'} tidak dikenal"})
            continue
        k = known.get(r["employeeId"])
        code = k["code"] if k else r["employeeId"]
        arm = r["armada"] if r["armada"] and taken.get(r["armada"], code) == code else None
        fields = {"name": r["name"], "nickname": r["nickname"], "pool_code": r["pool"], "department": r["department"],
                  "bike_code": r["bike"] or None, "fleet_code": arm or (k["fleet_code"] if k else None)}
        same = k and all((k.get(f) or None) == (v or None) for f, v in fields.items())
        plan.append({**r, "armada": arm, "code": code, "action": "new" if not k else ("same" if same else "update"),
                     "newArmada": bool(arm and arm not in fleet)})
    if apply:
        with db.transaction() as conn:
            for p in plan:
                if p["newArmada"]:
                    conn.execute(f"""INSERT INTO {P}.cotr_fleet (code, type, pool_code, updated_by) VALUES (%s, 'sepeda', %s, %s)
                                     ON CONFLICT (code) DO NOTHING""", (p["armada"], p["pool"], user["id"]))
                vals = (p["name"], p["nickname"], p["pool"], p["department"], p["bike"] or None, p["armada"])
                if p["action"] == "new":
                    conn.execute(f"""INSERT INTO {P}.cotr_driver (code, employee_id, name, nickname, pool_code, department, bike_code,
                                         fleet_code, position, updated_by)
                                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (code) DO NOTHING""",
                                 (p["code"], p["employeeId"], *vals, p["department"], user["id"]))
                elif p["action"] == "update":
                    conn.execute(f"""UPDATE {P}.cotr_driver SET name = %s, nickname = %s, pool_code = %s, department = %s,
                                         bike_code = %s, fleet_code = coalesce(%s, fleet_code), updated_at = now(), updated_by = %s
                                     WHERE code = %s""", (*vals, user["id"], p["code"]))
    by = lambda k: sum(p["action"] == k for p in plan)  # noqa: E731
    return {"rows": plan, "skipped": skipped, "new": by("new"), "update": by("update"), "same": by("same"),
            "newArmada": sum(p["newArmada"] for p in plan), "applied": apply, "format": "team",
            "departments": {d: sum(p["department"] == d for p in plan) for d in sorted({p["department"] for p in plan})}}


# ---------------------------------------------------------------- assignment advice (migration 061)

ADVICE_DAYS = 14
ADVICE_BUFFER = 0.10  # on top of the average sold …
HIGH_WASTE = 0.15  # … none when the driver wastes more than this share of what he carries


def advise(sold: dict[str, float], waste: dict[str, float], cupping: dict[str, float], days: int) -> dict:
    """What a driver should carry from his last days: per menu the average sold per day worked plus a buffer (none
    when his waste is high), rounded up; the cupping of a day on average. Pure: the numbers of the history."""
    if days <= 0:
        return {"days": 0, "sell": {}, "cupping": {}, "avgSold": {}, "wasteRate": None}
    menus = set(sold) | set(waste)
    carried = sum(sold.values()) + sum(waste.values())
    rate = round(sum(waste.values()) / carried, 4) if carried else None
    buffer = 0.0 if rate is not None and rate > HIGH_WASTE else ADVICE_BUFFER
    avg = {m: round(sold.get(m, 0) / days, 2) for m in menus}
    sell = {m: math.ceil(round(a * (1 + buffer), 6)) for m, a in avg.items() if a > 0}
    taste = {m: round(n / days) for m, n in cupping.items() if round(n / days) > 0}
    return {"days": days, "sell": sell, "cupping": taste, "avgSold": avg, "wasteRate": rate}


def suggestions(day: date, codes: list[str]) -> dict[str, dict]:
    """The advice of every driver from the shifts of the last ADVICE_DAYS days before `day`; a driver without any gets
    the average of the drivers of his pool (source "pool")."""
    if not codes:
        return {}
    since = day - timedelta(days=ADVICE_DAYS)
    shifts = db.fetch(f"""SELECT sh.id, sh.driver_code, d.pool_code, sh.end_waste, sh.end_cupping FROM {P}.cotr_shift sh
                            JOIN {P}.cotr_driver d ON d.code = sh.driver_code
                           WHERE sh.day >= %s AND sh.day < %s AND sh.status = 'closed'""", (since, day))
    sold: dict[str, dict[str, float]] = {}
    for r in db.fetch(f"""SELECT sh.driver_code, l->>'code' AS code, sum((l->>'qty')::int) AS qty FROM {P}.cotr_sale s
                            JOIN {P}.cotr_shift sh ON sh.id = s.shift_id, jsonb_array_elements(s.lines) l
                           WHERE s.voided_at IS NULL AND sh.day >= %s AND sh.day < %s AND sh.status = 'closed'
                           GROUP BY 1, 2""", (since, day)):
        sold.setdefault(r["driver_code"], {})[r["code"]] = float(r["qty"])
    days: dict[str, int] = {}
    waste: dict[str, dict[str, float]] = {}
    taste: dict[str, dict[str, float]] = {}
    pool_of: dict[str, str] = {}
    for sh in shifts:
        c = sh["driver_code"]
        days[c] = days.get(c, 0) + 1
        pool_of[c] = sh["pool_code"]
        for k, n in (sh["end_waste"] or {}).items():
            waste.setdefault(c, {})[k] = waste.get(c, {}).get(k, 0) + float(n)
        for k, n in (sh["end_cupping"] or {}).items():
            taste.setdefault(c, {})[k] = taste.get(c, {}).get(k, 0) + float(n)
    shift_days = {(r["driver_code"], r["day"]) for r in db.fetch(
        f"SELECT driver_code, day FROM {P}.cotr_shift WHERE day >= %s AND day < %s", (since, day))}
    for r in db.fetch(f"""SELECT r.driver_code, r.day, r.lines, d.pool_code FROM {P}.cotr_report_day r
                            JOIN {P}.cotr_driver d ON d.code = r.driver_code
                           WHERE r.day >= %s AND r.day < %s AND r.driver_code IS NOT NULL""", (since, day)):
        c = r["driver_code"]
        if (c, r["day"]) in shift_days or not r["lines"]:
            continue  # migration 089: the workbook only where the app has no shift that day
        days[c] = days.get(c, 0) + 1
        pool_of[c] = r["pool_code"]
        for k, x in r["lines"].items():
            sold.setdefault(c, {})[k] = sold.get(c, {}).get(k, 0) + float(x.get("t") or 0)
            if x.get("w"):
                waste.setdefault(c, {})[k] = waste.get(c, {}).get(k, 0) + float(x["w"])
            if x.get("c"):
                taste.setdefault(c, {})[k] = taste.get(c, {}).get(k, 0) + float(x["c"])
    out = {c: {**advise(sold.get(c, {}), waste.get(c, {}), taste.get(c, {}), days[c]), "source": "driver"} for c in days}
    # the pool average for the drivers without history
    pools: dict[str, list[dict]] = {}
    for c, a in out.items():
        pools.setdefault(pool_of[c], []).append(a)
    pool_avg: dict[str, dict] = {}
    for p, items in pools.items():
        keys = {k for a in items for k in a["avgSold"]}
        avg = {k: sum(a["avgSold"].get(k, 0) for a in items) / len(items) for k in keys}
        pool_avg[p] = {"days": 0, "sell": {k: math.ceil(round(v * (1 + ADVICE_BUFFER), 6)) for k, v in avg.items() if v > 0},
                       "cupping": {}, "avgSold": {k: round(v, 2) for k, v in avg.items()}, "wasteRate": None, "source": "pool"}
    for r in db.fetch(f"SELECT code, pool_code FROM {P}.cotr_driver WHERE code = ANY(%s)", (codes,)):
        if r["code"] not in out and r["pool_code"] in pool_avg:
            out[r["code"]] = pool_avg[r["pool_code"]]
    return out


# ---------------------------------------------------------------- production batches and distribution (migration 062)

def _batch_view(b: dict, sent: int, received: int) -> dict:
    waste = int(b.get("waste_qty") or 0)
    return {"id": str(b["id"]), "batchNo": b["batch_no"], "hub": b["hub_code"], "menu": b["menu_code"], "qty": b["qty"],
            "producedOn": _iso(b["produced_on"]), "expiresOn": _iso(b["expires_on"]), "note": b["note"],
            "sent": sent, "received": received, "atHub": b["qty"] - sent - waste, "createdAt": _iso(b["created_at"]),
            "waste": waste, "wasteReason": b.get("waste_reason")}


def batches(d_from: date, d_to: date, hub: Optional[str]) -> dict:
    """The batches produced in a period (a hub or every hub) with what was sent to the pools and is left at the hub."""
    rows = db.fetch(f"""SELECT * FROM {P}.cotr_batch WHERE produced_on BETWEEN %s AND %s AND (%s::text IS NULL OR hub_code = %s)
                         ORDER BY produced_on DESC, created_at DESC""", (d_from, d_to, hub, hub))
    ids = [r["id"] for r in rows]
    tr_rows = db.fetch(f"""SELECT t.*, p.name AS pool_name FROM {P}.cotr_transfer t JOIN {P}.cotr_pool p ON p.code = t.pool_code
                            WHERE t.batch_id = ANY(%s) ORDER BY t.sent_at""", (ids,)) if ids else []
    per: dict[str, list[dict]] = {}
    for t in tr_rows:
        per.setdefault(str(t["batch_id"]), []).append({"id": str(t["id"]), "pool": t["pool_code"], "poolName": t["pool_name"], "qty": t["qty"],
                                                      "status": t["status"], "note": t["note"], "sentAt": _iso(t["sent_at"]),
                                                      "receivedAt": _iso(t["received_at"])})
    out = []
    for b in rows:
        ts = per.get(str(b["id"]), [])
        out.append({**_batch_view(b, sum(t["qty"] for t in ts), sum(t["qty"] for t in ts if t["status"] == "received")), "transfers": ts})
    return {"from": d_from.isoformat(), "to": d_to.isoformat(), "data": out}


def add_batch(user: dict, data: dict) -> dict:
    hub = db.fetchrow(f"SELECT code FROM {P}.cotr_pool WHERE code = %s", (data.get("hub"),))
    menu = db.fetchrow(f"SELECT code, shelf_life_days FROM {P}.cotr_menu WHERE code = %s", (data.get("menu"),))
    if not hub or not menu:
        raise CotrError("Hub dan menu wajib dipilih")
    qty = int(data.get("qty") or 0)
    if qty <= 0:
        raise CotrError("Jumlah produksi harus lebih dari 0")
    made = data.get("producedOn") or today()
    expires = data.get("expiresOn") or (made + timedelta(days=menu["shelf_life_days"]) if menu["shelf_life_days"] else None)
    if expires is None:
        raise CotrError("Isi tanggal expired (atau umur simpan menu di Master Menu)")
    if expires < made:
        raise CotrError("Tanggal expired harus setelah tanggal produksi")
    with db.transaction() as conn:
        n = conn.execute(f"SELECT nextval('{P}.cotr_batch_no') AS n").fetchone()["n"]
        no = f"PRD-{made:%y%m%d}-{n:04d}"
        row = conn.execute(f"""INSERT INTO {P}.cotr_batch (batch_no, hub_code, menu_code, qty, produced_on, expires_on, note, created_by)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                           (no, hub["code"], menu["code"], qty, made, expires, (data.get("note") or "").strip() or None, user["id"])).fetchone()
    return _batch_view(row, 0, 0)


def delete_batch(batch_id: str) -> None:
    with db.transaction() as conn:
        if conn.execute(f"SELECT 1 FROM {P}.cotr_transfer WHERE batch_id::text = %s", (batch_id,)).fetchone():
            raise CotrError("Batch sudah dikirim ke pool dan tidak dapat dihapus")
        if conn.execute(f"DELETE FROM {P}.cotr_batch WHERE id::text = %s", (batch_id,)).rowcount == 0:
            raise CotrError("Batch tidak ditemukan")


def send_batch(user: dict, batch_id: str, pool: str, qty: int, note: Optional[str]) -> dict:
    """A part of a batch sent from the hub to a pool, at most what is left at the hub."""
    with db.transaction() as conn:
        b = conn.execute(f"SELECT * FROM {P}.cotr_batch WHERE id::text = %s FOR UPDATE", (batch_id,)).fetchone()
        if not b:
            raise CotrError("Batch tidak ditemukan")
        if not conn.execute(f"SELECT 1 FROM {P}.cotr_pool WHERE code = %s", (pool,)).fetchone():
            raise CotrError("Pool tidak ditemukan")
        sent = conn.execute(f"SELECT coalesce(sum(qty), 0) AS n FROM {P}.cotr_transfer WHERE batch_id = %s", (b["id"],)).fetchone()["n"]
        left = b["qty"] - sent - int(b.get("waste_qty") or 0)
        if qty <= 0 or qty > left:
            raise CotrError(f"Jumlah kirim 1 sampai {left}")
        row = conn.execute(f"""INSERT INTO {P}.cotr_transfer (batch_id, pool_code, qty, note, sent_by) VALUES (%s, %s, %s, %s, %s)
                               RETURNING id::text""", (b["id"], pool, qty, (note or "").strip() or None, user["id"])).fetchone()
    return {"id": row["id"]}


def receive_transfer(user: dict, transfer_id: str, pools: Optional[tuple] = None) -> None:
    row = db.fetchrow(f"""UPDATE {P}.cotr_transfer SET status = 'received', received_at = now(), received_by = %s
                           WHERE id::text = %s AND status = 'sent' RETURNING pool_code""", (user["id"], transfer_id))
    if not row:
        raise CotrError("Kiriman tidak ditemukan atau sudah diterima")


def pool_stock(pool: Optional[str], day: date) -> dict:
    """What a pool received from the hubs, per batch (first expired first) with the days left; and what is on the way."""
    rows = db.fetch(f"""SELECT t.id::text AS transfer, t.qty, t.status, t.sent_at, t.received_at, b.batch_no, b.menu_code, b.produced_on, b.expires_on,
                               m.name AS menu_name
                          FROM {P}.cotr_transfer t JOIN {P}.cotr_batch b ON b.id = t.batch_id JOIN {P}.cotr_menu m ON m.code = b.menu_code
                         WHERE (%s::text IS NULL OR t.pool_code = %s) AND b.expires_on >= %s - 3 ORDER BY b.expires_on, b.produced_on""",
                    (pool, pool, day))
    items = [{"transfer": r["transfer"], "batchNo": r["batch_no"], "menu": r["menu_code"], "menuName": r["menu_name"], "qty": r["qty"],
              "status": r["status"], "producedOn": _iso(r["produced_on"]), "expiresOn": _iso(r["expires_on"]),
              "daysLeft": (r["expires_on"] - day).days, "sentAt": _iso(r["sent_at"]), "receivedAt": _iso(r["received_at"])} for r in rows]
    return {"pool": pool, "day": day.isoformat(), "received": [i for i in items if i["status"] == "received"],
            "incoming": [i for i in items if i["status"] == "sent"]}


# ---------------------------------------------------------------- information label of an armada (migration 063)

# what a scan without sign-in shows of the armada: not the papers, the tracker, the QRIS nor the notes
FLEET_PUBLIC = ("code", "name", "type", "brand", "model", "color", "year_made", "plate", "capacity", "condition",
                "is_active", "last_service", "next_service", "acquired_on", "asset_no", "has_photo", "updated_at")
DRIVER_PUBLIC = ("code", "name", "nickname", "employee_id", "department", "position", "status", "joined_on", "has_photo",
                 "updated_at")


def fleet_by_token(token: str) -> Optional[dict]:
    return db.fetchrow(f"SELECT code FROM {P}.cotr_fleet WHERE label_token = %s", (token,))


def fleet_card(code: str, full: bool) -> Optional[dict]:
    """The armada as its label QR shows it: the vehicle, its pool, the driver(s) on it and today's shift. `full` (a
    signed-in COTR user) adds the papers, the tracker, the QRIS details, the notes and the phone numbers."""
    f = db.fetchrow(f"""SELECT f.*, t.name AS type_name FROM {P}.cotr_fleet f
                          LEFT JOIN {P}.cotr_fleet_type t ON t.code = f.type WHERE f.code = %s""", (code,))
    if not f:
        return None
    fleet = _plain(f)
    fleet.pop("label_token", None)
    type_name = fleet.pop("type_name", None)
    if not full:
        fleet = {k: fleet.get(k) for k in FLEET_PUBLIC}
    fleet["type_name"] = type_name
    pool = db.fetchrow(f"""SELECT code, name, kind, city, address, pic_name, phone, open_time, close_time FROM {P}.cotr_pool
                            WHERE code = %s""", (f["pool_code"],))
    drivers = [_plain(d) for d in db.fetch(f"""SELECT d.*, p.name AS pool_name FROM {P}.cotr_driver d
                                                LEFT JOIN {P}.cotr_pool p ON p.code = d.pool_code
                                               WHERE d.fleet_code = %s AND d.status <> 'inactive' ORDER BY d.status, d.code""", (code,))]
    if not full:
        drivers = [{k: d.get(k) for k in (*DRIVER_PUBLIC, "pool_name")} for d in drivers]
    else:
        drivers = [_hide("drivers", d, False) for d in drivers]
    shift = db.fetchrow(f"""SELECT s.driver_code, d.name, d.nickname, s.status, s.started_at, s.ended_at FROM {P}.cotr_shift s
                              JOIN {P}.cotr_driver d ON d.code = s.driver_code
                             WHERE s.fleet_code = %s AND s.day = %s ORDER BY s.started_at DESC LIMIT 1""", (code, today()))
    pool_out = _plain(pool) if pool else None
    if pool_out:
        for k in ("open_time", "close_time"):
            if pool_out.get(k):
                pool_out[k] = str(pool_out[k])[:5]
        if not full:
            pool_out.pop("phone", None)
    return {"full": full, "fleet": fleet, "pool": pool_out, "drivers": drivers,
            "today": ({"driver": shift["driver_code"], "name": shift["nickname"] or shift["name"], "status": shift["status"],
                       "startedAt": _iso(shift["started_at"]), "endedAt": _iso(shift["ended_at"])} if shift else None)}


def renew_label_token(code: str) -> str:
    row = db.fetchrow(f"""UPDATE {P}.cotr_fleet SET label_token = substr(replace(gen_random_uuid()::text, '-', ''), 1, 20),
                                  updated_at = now() WHERE code = %s RETURNING label_token""", (code,))
    if not row:
        raise CotrError("Armada tidak ditemukan")
    return row["label_token"]


# ---------------------------------------------------------------- fleet accessories (migration 080)

def fleet_accessories(fleet: Optional[str] = None) -> list[dict]:
    """The accessories on the fleet (and the ones taken off), with the shifts that carried them."""
    rows = db.fetch(f"""SELECT fa.*, a.name, a.category, a.unit, a.serial_no, a.condition
                          FROM {P}.cotr_fleet_accessory fa JOIN {P}.cotr_accessory a ON a.code = fa.accessory_code
                         WHERE (%(f)s::text IS NULL OR fa.fleet_code = %(f)s)
                         ORDER BY fa.removed_on IS NOT NULL, fa.fleet_code, a.category, a.name""", {"f": fleet})
    return [{"id": r["id"], "fleet": r["fleet_code"], "code": r["accessory_code"], "name": r["name"], "category": r["category"],
             "unit": r["unit"], "serialNo": r["serial_no"], "condition": r["condition"], "qty": r["qty"],
             "assignedOn": _iso(r["assigned_on"]), "removedOn": _iso(r["removed_on"]), "note": r["note"]} for r in rows]


def fleet_assign(user: dict, fleet: str, code: str, qty: int, note: Optional[str], assigned_on: Optional[date] = None) -> list[dict]:
    """Put an accessory on a fleet (one active row per fleet and accessory; the qty is updated when it is already on)."""
    if qty <= 0:
        raise CotrError("Jumlah minimal 1")
    on = db.fetchrow(f"SELECT fleet_code FROM {P}.cotr_fleet_accessory WHERE accessory_code = %s AND removed_on IS NULL AND fleet_code <> %s",
                     (code, fleet))
    a = db.fetchrow(f"SELECT serial_no FROM {P}.cotr_accessory WHERE code = %s", (code,))
    if not a:
        raise CotrError("Accessories tidak ditemukan")
    if on and a["serial_no"]:  # an item with a serial number is on one fleet at a time
        raise CotrError(f"{code} masih terpasang di fleet {on['fleet_code']}")
    with db.transaction() as conn:
        conn.execute(f"""INSERT INTO {P}.cotr_fleet_accessory (fleet_code, accessory_code, qty, note, updated_by, assigned_on)
                         VALUES (%s, %s, %s, %s, %s, %s)
                         ON CONFLICT (fleet_code, accessory_code) WHERE removed_on IS NULL
                         DO UPDATE SET qty = EXCLUDED.qty, note = EXCLUDED.note, assigned_on = EXCLUDED.assigned_on,
                                       updated_at = now(), updated_by = EXCLUDED.updated_by""",
                     (fleet, code, qty, (note or "").strip() or None, user["id"], assigned_on or today()))
    return fleet_accessories(fleet)


def fleet_unassign(user: dict, assignment_id: int) -> None:
    """Take an accessory off its fleet (kept with the day it was taken off)."""
    db.fetchrow(f"""UPDATE {P}.cotr_fleet_accessory SET removed_on = CURRENT_DATE, updated_at = now(), updated_by = %s
                     WHERE id = %s AND removed_on IS NULL RETURNING id""", (user["id"], assignment_id))


def accessory_usage(d_from: date, d_to: date) -> list[dict]:
    """Who carried which accessories and when: every shift of the period with the accessories of its fleet."""
    rows = db.fetch(f"""SELECT s.id::text AS shift, s.day, s.driver_code, d.name AS driver, sa.fleet_code, sa.accessory_code, a.name, sa.qty,
                               s.started_at, s.ended_at
                          FROM {P}.cotr_shift_accessory sa JOIN {P}.cotr_shift s ON s.id = sa.shift_id
                          JOIN {P}.cotr_accessory a ON a.code = sa.accessory_code
                          LEFT JOIN {P}.cotr_driver d ON d.code = s.driver_code
                         WHERE s.day BETWEEN %s AND %s ORDER BY s.day DESC, s.started_at DESC, a.name""", (d_from, d_to))
    return [{"shift": r["shift"], "day": _iso(r["day"]), "driver": r["driver_code"], "driverName": r["driver"], "fleet": r["fleet_code"],
             "code": r["accessory_code"], "name": r["name"], "qty": r["qty"], "startedAt": _iso(r["started_at"]),
             "endedAt": _iso(r["ended_at"])} for r in rows]


# ---------------------------------------------------------------- storage & production stock (migration 081)

def batch_waste(user: dict, batch_id: str, qty: int, reason: Optional[str]) -> dict:
    """The cups of a batch wasted while it was made (spilled, failed the taste): not sent, still used the materials."""
    with db.transaction() as conn:
        b = conn.execute(f"SELECT * FROM {P}.cotr_batch WHERE id::text = %s FOR UPDATE", (batch_id,)).fetchone()
        if not b:
            raise CotrError("Batch tidak ditemukan")
        sent = conn.execute(f"SELECT coalesce(sum(qty), 0) AS n FROM {P}.cotr_transfer WHERE batch_id = %s", (b["id"],)).fetchone()["n"]
        if qty < 0 or qty > b["qty"] - sent:
            raise CotrError(f"Waste produksi 0 sampai {b['qty'] - sent} cup")
        if qty and not (reason or "").strip():
            raise CotrError("Isi alasan waste produksi")
        row = conn.execute(f"UPDATE {P}.cotr_batch SET waste_qty = %s, waste_reason = %s WHERE id = %s RETURNING *",
                           (qty, (reason or "").strip() or None, b["id"])).fetchone()
    return _batch_view(row, sent, 0)


def _base_qty(item: dict, qty: float, unit: Optional[str]) -> float:
    """A quantity in the item's base unit: a pack (Pail, Dus …) times its content."""
    if unit and item.get("pack_unit") and unit.lower() == str(item["pack_unit"]).lower() and item.get("pack_qty"):
        return qty * float(item["pack_qty"])
    return qty


def receipts(pool: Optional[str], d_from: date, d_to: date) -> list[dict]:
    rows = db.fetch(f"""SELECT r.*, i.name, i.unit FROM {P}.cotr_receipt r JOIN {P}.cotr_item i ON i.code = r.item_code
                         WHERE r.day BETWEEN %s AND %s AND (%s::text IS NULL OR r.pool_code = %s) ORDER BY r.day DESC, r.created_at DESC""",
                     (d_from, d_to, pool, pool))
    return [{"id": str(r["id"]), "pool": r["pool_code"], "day": _iso(r["day"]), "item": r["item_code"], "name": r["name"], "unit": r["unit"],
             "qty": float(r["qty"]), "supplier": r["supplier"], "docNo": r["doc_no"], "note": r["note"]} for r in rows]


def receipt_add(user: dict, data: dict) -> dict:
    item = db.fetchrow(f"SELECT * FROM {P}.cotr_item WHERE code = %s", (data.get("item"),))
    if not item:
        raise CotrError("Item tidak ditemukan")
    qty = _base_qty(item, float(data.get("qty") or 0), data.get("unit"))
    if qty <= 0:
        raise CotrError("Jumlah harus lebih dari 0")
    try:
        row = db.fetchrow(f"""INSERT INTO {P}.cotr_receipt (pool_code, day, item_code, qty, supplier, doc_no, note, created_by)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text""",
                          (data.get("pool"), data.get("day") or today(), item["code"], qty, (data.get("supplier") or "").strip() or None,
                           (data.get("docNo") or "").strip() or None, (data.get("note") or "").strip() or None, user["id"]))
    except Exception as e:
        raise CotrError(_db_message(e)) from None
    return {"id": row["id"]}


def receipt_delete(receipt_id: str) -> None:
    db.fetchrow(f"DELETE FROM {P}.cotr_receipt WHERE id::text = %s RETURNING id", (receipt_id,))


def stock(pool: str, day: Optional[date] = None) -> dict:
    """The running stock of every stock item of a pool / hub: the latest approved count (its day) + received since −
    used by the batches made there since (cups made × recipe card) − waste since; valued at the item cost."""
    day = day or today()
    items = {i["code"]: i for i in db.fetch(f"SELECT * FROM {P}.cotr_item WHERE is_stock AND is_active ORDER BY category, name")}
    last = db.fetchrow(f"""SELECT id, day FROM {P}.cotr_stock_count WHERE pool_code = %s AND status = 'approved' AND day <= %s
                            ORDER BY day DESC, approved_at DESC LIMIT 1""", (pool, day))
    start = last["day"] if last else date(2000, 1, 1)
    so = {r["item_code"]: float(r["qty"]) for r in db.fetch(f"SELECT item_code, qty FROM {P}.cotr_stock_count_line WHERE count_id = %s",
                                                            (last["id"],))} if last else {}
    got: dict[str, float] = {}
    for r in db.fetch(f"""SELECT item_code, sum(qty) AS qty FROM {P}.cotr_receipt WHERE pool_code = %s AND day > %s AND day <= %s GROUP BY 1""",
                      (pool, start, day)):
        got[r["item_code"]] = float(r["qty"])
    boms = {b["code"]: b for b in db.fetch(f"SELECT * FROM {P}.cotr_bom")}
    menu = {m["code"]: m for m in db.fetch(f"SELECT code, bom_code FROM {P}.cotr_menu")}
    used: dict[str, dict] = {}
    made = db.fetch(f"""SELECT menu_code, sum(qty) AS qty FROM {P}.cotr_batch WHERE hub_code = %s AND produced_on > %s AND produced_on <= %s GROUP BY 1""",
                    (pool, start, day))
    for b in made:
        bom = (menu.get(b["menu_code"]) or {}).get("bom_code")
        if bom:
            explode(boms, bom, float(b["qty"]), used)
    waste: dict[str, float] = {}
    for r in db.fetch(f"""SELECT coalesce(item_code, item) AS code, qty, unit, kind FROM {P}.cotr_waste
                           WHERE pool_code = %s AND day > %s AND day <= %s""", (pool, start, day)):
        it = items.get(r["code"])
        if it:
            waste[r["code"]] = waste.get(r["code"], 0.0) + _base_qty(it, float(r["qty"]), r["unit"])
    rows = []
    for code, it in items.items():
        s0, g, u, w = so.get(code, 0.0), got.get(code, 0.0), float((used.get(code) or {}).get("qty", 0.0)), waste.get(code, 0.0)
        if not (s0 or g or u or w):
            continue
        est = s0 + g - u - w
        cost = float(it["cost"]) if it.get("cost") is not None else None
        rows.append({"code": code, "name": it["name"], "category": it["category"], "unit": it["unit"], "packUnit": it.get("pack_unit"),
                     "packQty": float(it["pack_qty"]) if it.get("pack_qty") else None, "so": s0, "received": g, "used": u, "waste": w,
                     "estimate": round(est, 3), "cost": cost, "value": round(est * cost, 2) if cost is not None else None})
    return {"pool": pool, "day": day.isoformat(), "soDay": _iso(last["day"]) if last else None, "rows": rows,
            "value": round(sum(r["value"] or 0 for r in rows), 2), "negative": sum(1 for r in rows if r["estimate"] < -1e-6)}


def stock_all(day: Optional[date] = None) -> dict:
    """The running stock of every pool / HUB with stock, added up per item."""
    pools = [r["code"] for r in db.fetch(f"""SELECT code FROM {P}.cotr_pool WHERE is_active AND code IN (
                SELECT pool_code FROM {P}.cotr_receipt UNION SELECT pool_code FROM {P}.cotr_stock_count UNION SELECT hub_code FROM {P}.cotr_batch
                UNION SELECT pool_code FROM {P}.cotr_waste WHERE item_code IS NOT NULL)""")]
    rows: dict[str, dict] = {}
    for p in pools:
        for r in stock(p, day)["rows"]:
            t = rows.setdefault(r["code"], {**r, "so": 0.0, "received": 0.0, "used": 0.0, "waste": 0.0, "estimate": 0.0, "value": None})
            for k in ("so", "received", "used", "waste", "estimate"):
                t[k] = round(t[k] + r[k], 3)
            if r["value"] is not None:
                t["value"] = round((t["value"] or 0) + r["value"], 2)
    out = sorted(rows.values(), key=lambda r: (r["category"] or "", r["name"]))
    return {"pool": None, "day": (day or today()).isoformat(), "soDay": None, "rows": out, "pools": pools,
            "value": round(sum(r["value"] or 0 for r in out), 2), "negative": sum(1 for r in out if r["estimate"] < -1e-6)}


def counts(pool: Optional[str]) -> list[dict]:
    rows = db.fetch(f"""SELECT c.*, p.name AS pool_name, (SELECT count(*) FROM {P}.cotr_stock_count_line l WHERE l.count_id = c.id)::int AS lines
                          FROM {P}.cotr_stock_count c JOIN {P}.cotr_pool p ON p.code = c.pool_code
                         WHERE (%s::text IS NULL OR c.pool_code = %s) ORDER BY c.day DESC, c.created_at DESC LIMIT 200""", (pool, pool))
    return [{"id": str(r["id"]), "pool": r["pool_code"], "poolName": r["pool_name"], "day": _iso(r["day"]), "status": r["status"],
             "note": r["note"], "lines": r["lines"], "submittedAt": _iso(r["submitted_at"]), "approvedAt": _iso(r["approved_at"])} for r in rows]


def count_detail(count_id: str) -> dict:
    c = db.fetchrow(f"SELECT * FROM {P}.cotr_stock_count WHERE id::text = %s", (count_id,))
    if not c:
        raise CotrError("Stock opname tidak ditemukan")
    lines = db.fetch(f"""SELECT l.*, i.name, i.unit, i.cost FROM {P}.cotr_stock_count_line l JOIN {P}.cotr_item i ON i.code = l.item_code
                          WHERE l.count_id = %s ORDER BY i.category, i.name""", (c["id"],))
    return {"id": str(c["id"]), "pool": c["pool_code"], "day": _iso(c["day"]), "status": c["status"], "note": c["note"],
            "lines": [{"item": r["item_code"], "name": r["name"], "unit": r["unit"], "qty": float(r["qty"]),
                       "expected": float(r["expected"]) if r["expected"] is not None else None,
                       "difference": round(float(r["qty"]) - float(r["expected"]), 3) if r["expected"] is not None else None,
                       "value": round((float(r["qty"]) - float(r["expected"] or 0)) * float(r["cost"]), 2) if r["cost"] is not None and r["expected"] is not None else None,
                       "note": r["note"]} for r in lines]}


def count_save(user: dict, data: dict) -> dict:
    """A stock opname of a pool / hub: the counted quantity of each item (base unit, or its pack), kept with the running
    stock of that moment; `submit` sends it to approval."""
    pool, day = data.get("pool"), data.get("day") or today()
    lines = data.get("lines") or {}
    if not lines:
        raise CotrError("Isi minimal satu item")
    items = {i["code"]: i for i in db.fetch(f"SELECT * FROM {P}.cotr_item WHERE code = ANY(%s)", (list(lines),))}
    expected = {r["code"]: r["estimate"] for r in stock(pool, day)["rows"]}
    with db.transaction() as conn:
        cid = data.get("id")
        if cid:
            c = conn.execute(f"SELECT * FROM {P}.cotr_stock_count WHERE id::text = %s FOR UPDATE", (cid,)).fetchone()
            if not c or c["status"] != "draft":
                raise CotrError("Stock opname sudah dikirim, tidak bisa diubah")
            conn.execute(f"DELETE FROM {P}.cotr_stock_count_line WHERE count_id = %s", (c["id"],))
        else:
            c = conn.execute(f"""INSERT INTO {P}.cotr_stock_count (pool_code, day, note, created_by) VALUES (%s, %s, %s, %s) RETURNING *""",
                             (pool, day, (data.get("note") or "").strip() or None, user["id"])).fetchone()
        for code, line in lines.items():
            it = items.get(code)
            if not it:
                raise CotrError(f"Item {code} tidak ditemukan")
            line = line if isinstance(line, dict) else {"qty": line}
            q = _base_qty(it, float(line.get("qty") or 0), line.get("unit"))
            if q < 0:
                raise CotrError("Jumlah tidak boleh minus")
            conn.execute(f"""INSERT INTO {P}.cotr_stock_count_line (count_id, item_code, qty, expected, note) VALUES (%s, %s, %s, %s, %s)""",
                         (c["id"], code, q, expected.get(code, 0.0), (line.get("note") or "").strip() or None))
        if data.get("submit"):
            conn.execute(f"UPDATE {P}.cotr_stock_count SET status = 'submitted', submitted_at = now() WHERE id = %s", (c["id"],))
    return count_detail(str(c["id"]))


def count_approve(user: dict, count_id: str) -> dict:
    row = db.fetchrow(f"""UPDATE {P}.cotr_stock_count SET status = 'approved', approved_by = %s, approved_at = now()
                           WHERE id::text = %s AND status = 'submitted' RETURNING id""", (user["id"], count_id))
    if not row:
        raise CotrError("Hanya stock opname yang sudah dikirim yang bisa di-approve")
    return count_detail(count_id)
