"""Stock of the finished products of a pool / HUB, cups per menu (migration 089).

Entered once as the opening stock of a pool; then every day runs by itself:
    start  = left of the day before (the opening on its day)
    + in   = cups received at the pool (transfers of the batches), each lot with the expiry of its batch
    − out  = cups given to the drivers (accepted assignments, reloads sent) − cups brought back at check out
    − the waste and the cupping of the pool (cotr_waste with a menu)
    − expired: lots (per production / batch code) past their expiry at the end of the day (first expired, first out) are not carried over.
The lots are used first expired first; more used than there is goes into a lot without expiry below zero (shown as a
shortage, so the team counts and opens the pool again).
"""
from datetime import date, timedelta
from typing import Any, Optional

from app import database as db
from app.cotr import P, CotrError, _iso, _menu_map, clean_stock, received, today

MAX_DAYS = 400  # an opening further back than this is too old to run from


def _add(lots: list[list], expires: Optional[date], qty: int, batch: Optional[str] = None) -> None:
    """A lot = [expires on, cups, batch no]; the same batch and expiry adds up."""
    if qty <= 0:
        return
    for lot in lots:
        if lot[0] == expires and lot[2] == batch:
            lot[1] += qty
            return
    lots.append([expires, qty, batch])
    lots.sort(key=lambda x: (x[0] is None, x[0] or date.max, x[2] or ""))


def _take(lots: list[list], qty: int) -> None:
    """Use `qty` cups, first expired first; more than there is goes below zero in the lot without expiry."""
    for lot in lots:
        if qty <= 0:
            break
        if lot[1] <= 0:
            continue
        n = min(lot[1], qty)
        lot[1] -= n
        qty -= n
    if qty > 0:
        for lot in lots:
            if lot[0] is None:
                lot[1] -= qty
                break
        else:
            lots.append([None, -qty, None])
    lots[:] = [x for x in lots if x[1] != 0]


def _total(lots: list[list]) -> int:
    return sum(x[1] for x in lots)


def opening_of(pool: str, day: date) -> Optional[dict]:
    """The opening the stock of the pool runs from on `day`: the latest one on or before it."""
    first = db.fetchrow(f"""SELECT day, max(created_at) AS at FROM {P}.cotr_pool_opening WHERE pool_code = %s AND day <= %s
                             GROUP BY day ORDER BY day DESC LIMIT 1""", (pool, day))
    if not first:
        return None
    rows = db.fetch(f"""SELECT o.*, u.full_name AS by_name FROM {P}.cotr_pool_opening o LEFT JOIN {P}.user_account u ON u.id = o.created_by
                         WHERE o.pool_code = %s AND o.day = %s ORDER BY o.menu_code""", (pool, first["day"]))
    return {"day": first["day"], "at": first["at"], "by": rows[0]["by_name"] if rows else None,
            "lines": [{"menu": r["menu_code"], "qty": r["qty"], "expiresOn": _iso(r["expires_on"]), "batchNo": r.get("batch_no"), "note": r["note"]}
                      for r in rows]}


def _flows(pool: str, d0: date, d1: date) -> dict[date, dict[str, Any]]:
    """Per day: lots in, cups given to the drivers, cups back, waste, cupping (each {menu: cups})."""
    days: dict[date, dict[str, Any]] = {}
    def at(d: date) -> dict:
        return days.setdefault(d, {"in": [], "out": {}, "back": {}, "waste": {}, "cupping": {}})
    def put(m: dict, code: str, n: Any) -> None:
        if n:
            m[code] = m.get(code, 0) + int(n)
    for r in db.fetch(f"""SELECT (t.received_at AT TIME ZONE 'Asia/Jakarta')::date AS day, b.menu_code, b.expires_on, t.qty, b.batch_no
                            FROM {P}.cotr_transfer t JOIN {P}.cotr_batch b ON b.id = t.batch_id
                           WHERE t.pool_code = %s AND t.status = 'received'
                             AND (t.received_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %s AND %s""", (pool, d0, d1)):
        at(r["day"])["in"].append((r["menu_code"], r["expires_on"], int(r["qty"]), r["batch_no"]))
    for r in db.fetch(f"""SELECT day, final_lines FROM {P}.cotr_dispatch WHERE pool_code = %s AND status = 'accepted'
                           AND day BETWEEN %s AND %s""", (pool, d0, d1)):
        for k, v in (r["final_lines"] or {}).items():
            put(at(r["day"])["out"], k, v)
    for r in db.fetch(f"""SELECT s.day, r.* FROM {P}.cotr_reload r JOIN {P}.cotr_shift s ON s.id = r.shift_id
                           WHERE s.pool_code = %s AND s.day BETWEEN %s AND %s AND r.status IN ('approved', 'disputed', 'received')""",
                      (pool, d0, d1)):
        sent = received(r) if r["status"] == "received" else (r["approved_lines"] or r["lines"])
        for k, v in (sent or {}).items():
            put(at(r["day"])["out"], k, v)
    for r in db.fetch(f"""SELECT day, end_stock FROM {P}.cotr_shift WHERE pool_code = %s AND status = 'closed'
                           AND day BETWEEN %s AND %s""", (pool, d0, d1)):
        for k, v in (r["end_stock"] or {}).items():
            put(at(r["day"])["back"], k, v)
    for r in db.fetch(f"""SELECT day, menu_code, kind, sum(qty) AS qty FROM {P}.cotr_waste
                           WHERE pool_code = %s AND menu_code IS NOT NULL AND day BETWEEN %s AND %s GROUP BY 1, 2, 3""", (pool, d0, d1)):
        put(at(r["day"])["cupping" if r["kind"] == "cupping" else "waste"], r["menu_code"], r["qty"])
    return days


def run(pool: str, day: date, history: int = 7) -> dict:
    """The stock of a pool on `day` per menu (start, in, out, back, waste, cupping, expired, left, the lots left with their
    expiry) and the last `history` days in total cups."""
    o = opening_of(pool, day)
    menus = _menu_map()
    if not o:
        return {"pool": pool, "day": day.isoformat(), "opening": None, "rows": [], "days": []}
    d0 = o["day"]
    if (day - d0).days > MAX_DAYS:
        raise CotrError("Stock awal pool terlalu lama: isi stock awal baru")
    lots: dict[str, list[list]] = {}
    for line in o["lines"]:
        _add(lots.setdefault(line["menu"], []), date.fromisoformat(line["expiresOn"]) if line["expiresOn"] else None, line["qty"], line.get("batchNo"))
    flows = _flows(pool, d0, day)
    hist, today_rows = [], {}
    d = d0
    while d <= day:
        f = flows.get(d, {"in": [], "out": {}, "back": {}, "waste": {}, "cupping": {}})
        codes = set(lots) | {x[0] for x in f["in"]} | set(f["out"]) | set(f["back"]) | set(f["waste"]) | set(f["cupping"])
        rows = {}
        for code in codes:
            ls = lots.setdefault(code, [])
            start = _total(ls)
            got = sum(x[2] for x in f["in"] if x[0] == code)
            for c, exp, n, batch in f["in"]:
                if c == code:
                    _add(ls, exp, n, batch)
            out, back = f["out"].get(code, 0), f["back"].get(code, 0)
            net = out - back
            if net > 0:
                _take(ls, net)
            elif net < 0:  # more back than given (a correction): back on the lot that expires first
                _add(ls, ls[0][0] if ls else None, -net, ls[0][2] if ls else None)
            w, c = f["waste"].get(code, 0), f["cupping"].get(code, 0)
            _take(ls, w + c)
            expired = sum(x[1] for x in ls if x[0] is not None and x[0] <= d and x[1] > 0)
            ls[:] = [x for x in ls if not (x[0] is not None and x[0] <= d and x[1] > 0)]
            rows[code] = {"start": start, "in": got, "out": out, "back": back, "waste": w, "cupping": c, "expired": expired, "left": _total(ls)}
        if (day - d).days < history:
            hist.append({"day": d.isoformat(), **{k: sum(r[k] for r in rows.values()) for k in
                                                 ("start", "in", "out", "back", "waste", "cupping", "expired", "left")}})
        if d == day:
            today_rows = rows
        d += timedelta(days=1)
    out_rows = []
    for code in sorted(set(today_rows) | set(lots), key=lambda c: ((menus.get(c) or {}).get("sort") or 0, (menus.get(c) or {}).get("name") or c)):
        r = today_rows.get(code) or {"start": _total(lots.get(code, [])), "in": 0, "out": 0, "back": 0, "waste": 0, "cupping": 0, "expired": 0,
                                     "left": _total(lots.get(code, []))}
        if not any(r.values()):
            continue
        out_rows.append({"menu": code, "name": (menus.get(code) or {}).get("name", code), **r,
                         "lots": [{"expiresOn": _iso(x[0]), "qty": x[1], "daysLeft": (x[0] - day).days if x[0] else None, "batchNo": x[2]}
                                  for x in lots.get(code, [])]})
    return {"pool": pool, "day": day.isoformat(), "opening": {"day": _iso(o["day"]), "by": o["by"], "at": _iso(o["at"]), "lines": o["lines"]},
            "rows": out_rows, "days": hist[::-1]}


def available(pool: str, day: date, except_driver: Optional[str] = None) -> Optional[dict[str, int]]:
    """Cups a pool can still give on `day` ({menu: cups}), or None when the pool has no opening (not checked). The
    assignment of `except_driver` that is being changed is given back first."""
    if not opening_of(pool, day):
        return None
    left = {r["menu"]: r["left"] + r["expired"] for r in run(pool, day, history=1)["rows"]}  # what expires tonight can still go out today
    if except_driver:
        x = db.fetchrow(f"SELECT final_lines FROM {P}.cotr_dispatch WHERE day = %s AND driver_code = %s AND status = 'accepted'", (day, except_driver))
        for k, v in ((x or {}).get("final_lines") or {}).items():
            left[k] = left.get(k, 0) + int(v)
    # assignments still waiting for the driver's check are already promised
    for r in db.fetch(f"""SELECT driver_code, lines FROM {P}.cotr_dispatch WHERE pool_code = %s AND day = %s AND status IN ('assigned', 'disputed')""",
                      (pool, day)):
        if r["driver_code"] == except_driver:
            continue
        for k, v in (r["lines"] or {}).items():
            left[k] = left.get(k, 0) - int(v)
    return left


def save_opening(user: dict, pool: str, day: date, lines: list[dict], note: Optional[str]) -> dict:
    """The opening stock of a pool on a day (all menus at once): the stock runs from it; saving it again replaces it."""
    if day > today():
        raise CotrError("Tanggal stock awal tidak boleh di masa depan")
    if not db.fetchrow(f"SELECT 1 FROM {P}.cotr_pool WHERE code = %s", (pool,)):
        raise CotrError("Pool tidak ditemukan")
    menus = _menu_map()
    clean = []
    for x in lines:
        code, qty = x.get("menu"), int(x.get("qty") or 0)
        if code not in menus:
            raise CotrError(f"Menu {code} tidak ditemukan")
        if qty < 0:
            raise CotrError("Jumlah tidak boleh minus")
        exp = date.fromisoformat(x["expiresOn"]) if x.get("expiresOn") else None
        if qty and exp and exp < day:
            raise CotrError(f"{menus[code]['name']}: expired sebelum tanggal stock awal")
        if qty:
            clean.append((code, qty, exp, (x.get("batchNo") or "").strip().upper() or None))
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {P}.cotr_pool_opening WHERE pool_code = %s AND day = %s", (pool, day))
        for code, qty, exp, batch in clean:
            conn.execute(f"""INSERT INTO {P}.cotr_pool_opening (pool_code, day, menu_code, qty, expires_on, batch_no, note, created_by)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""", (pool, day, code, qty, exp, batch, (note or "").strip() or None, user["id"]))
        if not clean:  # an opening of nothing still starts the stock on that day
            conn.execute(f"""INSERT INTO {P}.cotr_pool_opening (pool_code, day, menu_code, qty, note, created_by)
                             SELECT %s, %s, code, 0, %s, %s FROM {P}.cotr_menu WHERE is_active ORDER BY sort LIMIT 1""",
                         (pool, day, (note or "").strip() or None, user["id"]))
    return run(pool, day)


def check_assignment(pool: Optional[str], day: date, driver: str, lines: dict) -> None:
    """An assignment never gives more than the pool has (only once the pool has an opening stock)."""
    if not pool:
        return
    left = available(pool, day, except_driver=driver)
    if left is None:
        return
    given = clean_stock(lines, _menu_map())
    short = [f"{_menu_map()[k]['name']} (sisa {max(left.get(k, 0), 0)})" for k, n in given.items() if n > left.get(k, 0)]
    if short:
        raise CotrError("Stok pool tidak cukup: " + ", ".join(short))
