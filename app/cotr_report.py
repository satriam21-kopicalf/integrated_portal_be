"""Calf On The Road → Report Armada entered in the portal (migration 134, 2026-11-23), as the sheet of the pools.

Per pool and day, one block per armada (a driver of the pool) with every menu: B brought, S left, C cupping, W waste and
T sold = B − S − C − W, the total at the menu price; then TOTAL PENDAPATAN, PENDAPATAN QRIS, PENDAPATAN CASH (total −
QRIS), ACTUAL CASH, OPERASIONAL (the requests of the driver the office approved that day) and BALANCE = actual cash −
(cash revenue − operational): minus = short, plus = over.

Who enters what: the storage (page cotr.storage) the products; the office (page cotr.finance) the QRIS and the actual cash.
The row entered here (source 'portal') is the official day of that armada — the driver app is shown beside it to match.
"""
from datetime import date
from typing import Optional

from app import database as db
from app.cotr import CotrError, P, _menu_map

PARTS = ("b", "s", "c", "w")


def _who(user: dict) -> str:
    return user.get("full_name") or user.get("username") or str(user.get("id"))


def _ops(pool: str, d: date) -> dict[str, dict]:
    """{driver: {approved, waiting}} of the requests of the day (WIB)."""
    out: dict[str, dict] = {}
    for r in db.fetch(f"""SELECT q.driver_code, q.status, sum(q.amount) AS amount FROM {P}.cotr_request q
                            JOIN {P}.cotr_driver d ON d.code = q.driver_code
                           WHERE d.pool_code = %s AND (q.at AT TIME ZONE 'Asia/Jakarta')::date = %s AND q.status IN ('approved', 'sent')
                           GROUP BY 1, 2""", (pool, d)):
        x = out.setdefault(r["driver_code"], {"approved": 0.0, "waiting": 0.0})
        x["approved" if r["status"] == "approved" else "waiting"] += float(r["amount"])
    return out


def _app(pool: str, d: date) -> dict[str, dict]:
    """The driver-app figures of the day per driver (to match): lines b/s/c/w/t, QRIS, cash, cash counted."""
    from app.cotr_ledger import report_armada as report
    out = {}
    for drv in report(pool, d, d)["drivers"]:
        for x in drv["days"]:
            if x.get("status") in ("open", "closed") and drv.get("code"):
                out[drv["code"]] = {"lines": {k: {p: v.get(p) for p in (*PARTS, "t")} for k, v in x["lines"].items()},
                                    "qris": x["qris"], "cash": x["cash"], "cashActual": x["cashActual"], "total": x["total"]}
    return out


def sheet(pool: str, d: date) -> dict:
    menus = sorted((m for m in _menu_map().values() if m.get("is_active", True)), key=lambda m: (m.get("sort") or 0, m["name"]))
    price = {m["code"]: float(m.get("price") or 0) for m in menus}
    drivers = db.fetch(f"""SELECT d.code, d.name, d.nickname, d.fleet_code, f.name AS fleet_name FROM {P}.cotr_driver d
                             LEFT JOIN {P}.cotr_fleet f ON f.code = d.fleet_code
                            WHERE d.pool_code = %s AND coalesce(d.status, 'active') = 'active'
                            ORDER BY d.fleet_code NULLS LAST, d.name""", (pool,))
    rows: dict[str, dict] = {}
    for r in db.fetch(f"SELECT * FROM {P}.cotr_report_day WHERE pool_code = %s AND day = %s AND driver_code IS NOT NULL ORDER BY id", (pool, d)):
        rows.setdefault(r["driver_code"], r)
    ops, app = _ops(pool, d), _app(pool, d)
    armadas = []
    for drv in drivers:
        r = rows.get(drv["code"])
        lines = {k: {p: int((v or {}).get(p) or 0) for p in (*PARTS, "t")} for k, v in ((r or {}).get("lines") or {}).items()}
        total = sum(lines.get(k, {}).get("t", 0) * price.get(k, 0) for k in price)
        qris = float(r["qris"]) if r and r["qris"] is not None else None
        actual = float(r["cash_actual"]) if r and r["cash_actual"] is not None else None
        op = ops.get(drv["code"], {"approved": 0.0, "waiting": 0.0})
        cash = total - (qris or 0)
        armadas.append({
            "driver": drv["code"], "name": drv["name"], "nickname": drv["nickname"], "fleet": drv["fleet_code"], "fleetName": drv["fleet_name"],
            "source": r["source"] if r else None, "lines": lines, "total": total, "qris": qris, "cash": cash, "cashActual": actual,
            "operational": op["approved"], "operationalWaiting": op["waiting"],
            "balance": (actual - (cash - op["approved"])) if actual is not None else None,
            "productsBy": r.get("products_by") if r else None, "productsAt": r["products_at"].isoformat() if r and r.get("products_at") else None,
            "moneyBy": r.get("money_by") if r else None, "moneyAt": r["money_at"].isoformat() if r and r.get("money_at") else None,
            "app": app.get(drv["code"]),
        })
    return {"pool": pool, "day": d.isoformat(), "menus": [{"code": m["code"], "name": m["name"], "price": price[m["code"]]} for m in menus],
            "armadas": armadas}


def sheet_all(d: date) -> dict:
    """The armadas of every active HUB, each with its pool (the pool is the one it is saved to)."""
    pools = db.fetch(f"SELECT code, name FROM {P}.cotr_pool WHERE kind = 'parent' AND is_active ORDER BY name")
    sheets = [(p, sheet(p["code"], d)) for p in pools]
    return {"pool": None, "day": d.isoformat(), "menus": sheets[0][1]["menus"] if sheets else [],
            "armadas": [{**a, "pool": p["code"], "poolName": p["name"]} for p, s in sheets for a in s["armadas"]]}


def _row(conn, pool: str, d: date, driver: str) -> dict:
    drv = db.fetchrow(f"""SELECT d.code, d.name, d.pool_code, d.fleet_code FROM {P}.cotr_driver d WHERE d.code = %s""", (driver,))
    if not drv or drv["pool_code"] != pool:
        raise CotrError("Armada tidak ada di pool ini")
    label = " ".join(x for x in (drv["fleet_code"], drv["name"]) if x)
    # one row per armada and day: a workbook row of the same armada and day (another label) becomes the portal row
    have = db.fetchrow(f"SELECT id FROM {P}.cotr_report_day WHERE pool_code = %s AND day = %s AND driver_code = %s ORDER BY id LIMIT 1",
                       (pool, d, driver))
    if have:
        conn.execute(f"UPDATE {P}.cotr_report_day SET source = 'portal' WHERE id = %s", (have["id"],))
    else:
        conn.execute(f"""INSERT INTO {P}.cotr_report_day (pool_code, day, label, driver_code, source) VALUES (%s, %s, %s, %s, 'portal')
                         ON CONFLICT (pool_code, day, label) DO UPDATE SET driver_code = EXCLUDED.driver_code, source = 'portal'""",
                     (pool, d, label, driver))
    return {"label": label}


def save_products(user: dict, pool: str, d: date, driver: str, lines: dict[str, dict]) -> dict:
    """The storage: B / S / C / W per menu; T = B − S − C − W (never below zero)."""
    menus = _menu_map()
    clean: dict[str, dict] = {}
    for code, v in (lines or {}).items():
        if code not in menus:
            raise CotrError(f"Menu {code} tidak ditemukan")
        x = {p: int((v or {}).get(p) or 0) for p in PARTS}
        if any(n < 0 for n in x.values()):
            raise CotrError("Jumlah tidak boleh minus")
        t = x["b"] - x["s"] - x["c"] - x["w"]
        if t < 0:
            raise CotrError(f"{menus[code]['name']}: sisa + cupping + waste lebih dari bawaan")
        if any(x.values()):
            clean[code] = x | {"t": t}
    import json
    with db.transaction() as conn:
        _row(conn, pool, d, driver)
        conn.execute(f"""UPDATE {P}.cotr_report_day SET lines = %s::jsonb, products_by = %s, products_at = now()
                         WHERE id = (SELECT id FROM {P}.cotr_report_day WHERE pool_code = %s AND day = %s AND driver_code = %s ORDER BY id LIMIT 1)""",
                     (json.dumps(clean), _who(user), pool, d, driver))
    return sheet(pool, d)


def save_money(user: dict, pool: str, d: date, driver: str, qris: Optional[float], cash_actual: Optional[float]) -> dict:
    """The office: QRIS and the actual cash handed in; the approved requests of the day are kept as the operational."""
    for v in (qris, cash_actual):
        if v is not None and v < 0:
            raise CotrError("Nominal tidak boleh minus")
    op = _ops(pool, d).get(driver, {}).get("approved", 0.0)
    with db.transaction() as conn:
        _row(conn, pool, d, driver)
        conn.execute(f"""UPDATE {P}.cotr_report_day SET qris = %s, cash_actual = %s, operational = %s, money_by = %s, money_at = now()
                         WHERE id = (SELECT id FROM {P}.cotr_report_day WHERE pool_code = %s AND day = %s AND driver_code = %s ORDER BY id LIMIT 1)""",
                     (qris, cash_actual, op, _who(user), pool, d, driver))
    return sheet(pool, d)
