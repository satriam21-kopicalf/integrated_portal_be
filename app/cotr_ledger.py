"""Product stock of a pool / HUB per day (migration 089), as the sheet "Product" of the pool workbooks.

    opening    = the physical leftover (count) of the day before, less its lots past their expiry
                 (no count that day: the calculated leftover of the day before)
    + transfer = cups moved in from another pool or else (entered; minus = moved out)
    + arrived  = new cups from the storage: batches received at the pool + entered
    − sold     = the sales of the drivers of the pool (transactions, not voided)
    − waste    = of the drivers at check out + of the pool
    − cupping  = of the drivers at check out + of the pool
    = calculated; actual = the physical count (per production / batch code); balance = actual − calculated.
A day taken from the sheets (source 'sheet') keeps the figures of the sheet instead of the app's.
The first count of a pool is its opening stock: the stock runs from the day after it.
"""
import re
from datetime import date, timedelta
from typing import Any, Optional

from psycopg.types.json import Jsonb

from app import database as db
from app.cotr import P, CotrError, _iso, _menu_map, received, today

MAX_DAYS = 120
KINDS = ("transfer", "arrived", "sold", "waste", "cupping")


def batch_day(code: Optional[str], on: date) -> Optional[date]:
    """The day a production / batch code "SSDD" was made: the day of the month DD, on or before `on` (else last month)."""
    m = re.fullmatch(r"\D*(\d{2})(\d{2})\D*", str(code or "").strip())
    if not m:
        return None
    dd = int(m.group(2))
    y, mo = on.year, on.month
    for _ in range(2):
        try:
            d = date(y, mo, dd)
            if d <= on:
                return d
        except ValueError:
            pass
        y, mo = (y, mo - 1) if mo > 1 else (y - 1, 12)
    return None


def expiry(code: Optional[str], menu: dict, on: date, batches: dict[str, date]) -> Optional[date]:
    """The last day a lot can be sold: the batch of the app, else the code's day + the shelf life of the menu."""
    if code and code in batches:
        return batches[code]
    made = batch_day(code, on)
    days = menu.get("shelf_life_days")
    return made + timedelta(days=int(days)) if made and days else None


def _counts(pool: str, d0: date, d1: date) -> dict[date, dict[str, list[dict]]]:
    out: dict[date, dict[str, list[dict]]] = {}
    for r in db.fetch(f"""SELECT day, menu_code, batch_no, qty, expires_on FROM {P}.cotr_pool_count
                           WHERE pool_code = %s AND day BETWEEN %s AND %s ORDER BY day, menu_code, batch_no NULLS LAST""", (pool, d0, d1)):
        out.setdefault(r["day"], {}).setdefault(r["menu_code"], []).append(r)
    return out


def _system(pool: str, d0: date, d1: date) -> dict[date, dict[str, dict[str, int]]]:
    """The figures of the app per day and menu: arrived (batches received), sold, waste and cupping."""
    days: dict[date, dict[str, dict[str, int]]] = {}

    def put(d: date, kind: str, code: str, n: Any) -> None:
        if n:
            m = days.setdefault(d, {}).setdefault(code, {})
            m[kind] = m.get(kind, 0) + int(n)
    for r in db.fetch(f"""SELECT (t.received_at AT TIME ZONE 'Asia/Jakarta')::date AS day, b.menu_code, sum(t.qty) AS qty
                            FROM {P}.cotr_transfer t JOIN {P}.cotr_batch b ON b.id = t.batch_id
                           WHERE t.pool_code = %s AND t.status = 'received'
                             AND (t.received_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %s AND %s GROUP BY 1, 2""", (pool, d0, d1)):
        put(r["day"], "arrived", r["menu_code"], r["qty"])
    for r in db.fetch(f"""SELECT sh.day, l->>'code' AS code, sum((l->>'qty')::int) AS qty FROM {P}.cotr_sale s
                            JOIN {P}.cotr_shift sh ON sh.id = s.shift_id, jsonb_array_elements(s.lines) l
                           WHERE sh.pool_code = %s AND s.voided_at IS NULL AND sh.day BETWEEN %s AND %s GROUP BY 1, 2""", (pool, d0, d1)):
        put(r["day"], "sold", r["code"], r["qty"])
    for r in db.fetch(f"""SELECT day, end_waste, end_cupping FROM {P}.cotr_shift WHERE pool_code = %s AND status = 'closed'
                           AND day BETWEEN %s AND %s""", (pool, d0, d1)):
        for k, v in (r["end_waste"] or {}).items():
            put(r["day"], "waste", k, v)
        for k, v in (r["end_cupping"] or {}).items():
            put(r["day"], "cupping", k, v)
    for r in db.fetch(f"""SELECT day, menu_code, kind, sum(qty) AS qty FROM {P}.cotr_waste
                           WHERE pool_code = %s AND menu_code IS NOT NULL AND day BETWEEN %s AND %s GROUP BY 1, 2, 3""", (pool, d0, d1)):
        put(r["day"], "cupping" if r["kind"] == "cupping" else "waste", r["menu_code"], r["qty"])
    return days


def _moves(pool: str, d0: date, d1: date) -> tuple[dict, set]:
    """Entered / sheet figures per day and menu, and the days taken from the sheets."""
    days: dict[date, dict[str, dict[str, int]]] = {}
    sheet: set[date] = set()
    for r in db.fetch(f"""SELECT day, menu_code, kind, source, sum(qty) AS qty FROM {P}.cotr_pool_move
                           WHERE pool_code = %s AND day BETWEEN %s AND %s GROUP BY 1, 2, 3, 4""", (pool, d0, d1)):
        m = days.setdefault(r["day"], {}).setdefault(r["menu_code"], {})
        m[r["kind"]] = m.get(r["kind"], 0) + int(r["qty"])
        if r["source"] == "sheet":
            sheet.add(r["day"])
    return days, sheet


def first_count(pool: str) -> Optional[date]:
    r = db.fetchrow(f"SELECT min(day) AS day FROM {P}.cotr_pool_count WHERE pool_code = %s", (pool,))
    return r["day"] if r else None


def run(pool: str, d_from: date, d_to: date) -> dict:
    """Every day of the period per menu: opening … balance, and the lots of the count with their expiry."""
    menus = _menu_map()
    start = first_count(pool)
    listed = sorted((m for m in menus.values() if m.get("is_active", True)), key=lambda m: (m.get("sort") or 0, m["name"]))
    base = {"pool": pool, "from": d_from.isoformat(), "to": d_to.isoformat(), "start": _iso(start),
            "menus": [{"code": m["code"], "name": m["name"]} for m in listed]}
    if not start or start > d_to:
        return base | {"days": []}
    start = max(start, d_from - timedelta(days=MAX_DAYS)) if (d_from - start).days > MAX_DAYS else start
    counts = _counts(pool, start, d_to)
    system = _system(pool, start + timedelta(days=1), d_to)
    moves, sheet = _moves(pool, start + timedelta(days=1), d_to)
    batches = {r["batch_no"]: r["expires_on"] for r in db.fetch(
        f"SELECT batch_no, expires_on FROM {P}.cotr_batch WHERE produced_on >= %s", (start - timedelta(days=30),))}
    codes = [m["code"] for m in listed]

    def lots_of(d: date, code: str) -> list[dict]:
        return [{"batchNo": x["batch_no"], "qty": x["qty"],
                 "expiresOn": _iso(x["expires_on"] or expiry(x["batch_no"], menus.get(code, {}), d, batches))}
                for x in counts.get(d, {}).get(code, [])]

    def expired_on(lots: list[dict], d: date) -> int:
        return sum(l["qty"] for l in lots if l["expiresOn"] and l["expiresOn"] <= d.isoformat())

    left = {c: sum(x["qty"] for x in counts.get(start, {}).get(c, [])) for c in codes}
    gone = {c: expired_on(lots_of(start, c), start) for c in codes}
    out_days = []
    if d_from <= start:
        out_days.append({"day": start.isoformat(), "source": "opening",
                         "rows": {c: {"actual": left[c], "expired": gone[c], "lots": lots_of(start, c)} for c in codes}})
    d = start + timedelta(days=1)
    while d <= d_to:
        rows = {}
        for c in codes:
            f = {k: 0 for k in KINDS}
            srcs = [moves.get(d) or {}] if d in sheet else [system.get(d) or {}, moves.get(d) or {}]
            for src in srcs:
                for k, v in src.get(c, {}).items():
                    f[k] += v
            opening = left[c] - gone[c]
            calc = opening + f["transfer"] + f["arrived"] - f["sold"] - f["waste"] - f["cupping"]
            counted = counts.get(d, {}).get(c)
            actual = sum(x["qty"] for x in counted) if counted is not None else None
            lots = lots_of(d, c)
            exp = expired_on(lots, d)
            rows[c] = {"opening": opening, **f, "calculated": calc, "actual": actual,
                       "balance": actual - calc if actual is not None else None, "expired": exp, "lots": lots}
            left[c], gone[c] = (actual if actual is not None else calc), exp
        if d >= d_from:
            out_days.append({"day": d.isoformat(), "source": "sheet" if d in sheet else "app", "rows": rows})
        d += timedelta(days=1)
    return base | {"days": out_days}


def run_all(d_from: date, d_to: date) -> dict:
    """Every pool / HUB together (the HUBs with a count): each figure and lot added up per day and menu."""
    pools = [r["code"] for r in db.fetch(f"SELECT DISTINCT pool_code AS code FROM {P}.cotr_pool_count ORDER BY 1")]
    runs = [run(p, d_from, d_to) for p in pools]
    menus = runs[0]["menus"] if runs else run("", d_from, d_to)["menus"]
    days: dict[str, dict] = {}
    for r in runs:
        for d in r["days"]:
            if d["source"] == "opening":
                continue
            day = days.setdefault(d["day"], {"day": d["day"], "source": d["source"], "rows": {}, "pools": 0})
            day["pools"] += 1
            if d["source"] != day["source"]:
                day["source"] = "mixed"
            for code, c in d["rows"].items():
                t = day["rows"].setdefault(code, {"actual": 0, "balance": 0, "expired": 0, "lots": [], "uncounted": 0})
                for k in ("opening", *KINDS, "calculated", "expired"):
                    t[k] = t.get(k, 0) + (c.get(k) or 0)
                if c.get("actual") is None:
                    t["uncounted"] += 1
                else:
                    t["actual"] += c["actual"]
                    t["balance"] += c["balance"] or 0
                t["lots"] += [l | {"pool": r["pool"]} for l in c.get("lots", [])]
    for day in days.values():
        for t in day["rows"].values():
            if t.pop("uncounted") == day["pools"]:
                t["actual"] = t["balance"] = None  # no pool counted it yet
    starts = [r["start"] for r in runs if r["start"]]
    return {"pool": None, "from": d_from.isoformat(), "to": d_to.isoformat(), "start": min(starts) if starts else None, "pools": pools,
            "menus": menus, "days": sorted(days.values(), key=lambda x: x["day"])}


def save_count(user: dict, pool: str, day: date, lines: list[dict]) -> dict:
    """The physical leftover of a pool at the end of a day (Sisa Aktual Fisik) per menu and batch code; replaces the
    count of that day. The first count of a pool is its opening stock."""
    if day > today():
        raise CotrError("Tanggal tidak boleh di masa depan")
    if not db.fetchrow(f"SELECT 1 FROM {P}.cotr_pool WHERE code = %s", (pool,)):
        raise CotrError("Pool tidak ditemukan")
    menus = _menu_map()
    rows = []
    for x in lines:
        code, qty = x.get("menu"), int(x.get("qty") or 0)
        if code not in menus:
            raise CotrError(f"Menu {code} tidak ditemukan")
        if qty < 0:
            raise CotrError("Jumlah tidak boleh minus")
        rows.append((code, (x.get("batchNo") or "").strip().upper() or None, qty,
                     date.fromisoformat(x["expiresOn"]) if x.get("expiresOn") else None))
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {P}.cotr_pool_count WHERE pool_code = %s AND day = %s", (pool, day))
        for code, batch, qty, exp in rows:
            conn.execute(f"""INSERT INTO {P}.cotr_pool_count (pool_code, day, menu_code, batch_no, qty, expires_on, created_by)
                             VALUES (%s, %s, %s, %s, %s, %s, %s)""", (pool, day, code, batch, qty, exp, user["id"]))
        if not rows:  # nothing left still counts
            for code in menus:
                conn.execute(f"INSERT INTO {P}.cotr_pool_count (pool_code, day, menu_code, qty, created_by) VALUES (%s, %s, %s, 0, %s)",
                             (pool, day, code, user["id"]))
    return run(pool, day, day)


def save_moves(user: dict, pool: str, day: date, kind: str, lines: dict[str, int], note: Optional[str]) -> dict:
    """The cups moved in / out (transfer) or arrived at a pool on a day, entered by hand; replaces the entered ones."""
    if kind not in ("transfer", "arrived"):
        raise CotrError("Jenis tidak valid")
    menus = _menu_map()
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {P}.cotr_pool_move WHERE pool_code = %s AND day = %s AND kind = %s AND source = 'manual'", (pool, day, kind))
        for code, n in lines.items():
            if code not in menus:
                raise CotrError(f"Menu {code} tidak ditemukan")
            if int(n) and (kind == "transfer" or int(n) > 0):
                conn.execute(f"""INSERT INTO {P}.cotr_pool_move (pool_code, day, menu_code, kind, qty, note, created_by)
                                 VALUES (%s, %s, %s, %s, %s, %s, %s)""", (pool, day, code, kind, int(n), (note or "").strip() or None, user["id"]))
    return run(pool, day, day)


def available(pool: str, day: date, except_driver: Optional[str] = None) -> Optional[dict[str, int]]:
    """Cups a pool can still give on `day`: opening + transfer + arrived − given to the drivers (assignments, reloads)
    + brought back at check out − waste and cupping of the pool; None when the pool has no count before that day."""
    start = first_count(pool)
    if not start or start >= day:
        return None
    rows = run(pool, day, day)["days"][-1]["rows"]
    left = {c: x["opening"] + x["transfer"] + x["arrived"] for c, x in rows.items()}

    def take(lines: Optional[dict], sign: int = -1) -> None:
        for k, v in (lines or {}).items():
            left[k] = left.get(k, 0) + sign * int(v)
    for x in db.fetch(f"SELECT driver_code, status, lines, final_lines FROM {P}.cotr_dispatch WHERE pool_code = %s AND day = %s", (pool, day)):
        if x["driver_code"] != except_driver:
            take(x["final_lines"] if x["status"] == "accepted" else x["lines"])
    for x in db.fetch(f"""SELECT r.* FROM {P}.cotr_reload r JOIN {P}.cotr_shift s ON s.id = r.shift_id
                           WHERE s.pool_code = %s AND s.day = %s AND r.status IN ('approved', 'disputed', 'received')""", (pool, day)):
        take(received(x) if x["status"] == "received" else (x["approved_lines"] or x["lines"]))
    for x in db.fetch(f"SELECT end_stock FROM {P}.cotr_shift WHERE pool_code = %s AND day = %s AND status = 'closed'", (pool, day)):
        take(x["end_stock"], +1)
    for x in db.fetch(f"""SELECT menu_code, sum(qty) AS qty FROM {P}.cotr_waste WHERE pool_code = %s AND day = %s
                           AND menu_code IS NOT NULL GROUP BY 1""", (pool, day)):
        take({x["menu_code"]: x["qty"]})
    return left


def check_assignment(pool: Optional[str], day: date, driver: str, lines: dict) -> None:
    """An assignment never gives more than the pool has (once the pool has its opening count)."""
    if not pool:
        return
    left = available(pool, day, except_driver=driver)
    if left is None:
        return
    menus = _menu_map()
    short = [f"{menus[k]['name']} (sisa {max(left.get(k, 0), 0)})" for k, n in lines.items() if k in menus and n > left.get(k, 0)]
    if short:
        raise CotrError("Stok pool tidak cukup: " + ", ".join(short))


# ---------------------------------------------------------------- Report Armada (the sheet of the pool workbooks)

def report_armada(pool: str, d_from: date, d_to: date) -> dict:
    """Per driver of the pool, per day and menu: B (brought: start + reloads received), S (left at check out), C, W,
    T (sold) and its total at the menu price; the cash and QRIS, and the operational costs asked (requests)."""
    menus = _menu_map()
    shifts = db.fetch(f"""SELECT s.*, d.name AS driver_name, d.nickname FROM {P}.cotr_shift s JOIN {P}.cotr_driver d ON d.code = s.driver_code
                           WHERE (%s::text IS NULL OR s.pool_code = %s) AND s.day BETWEEN %s AND %s ORDER BY d.name, s.day""",
                      (pool, pool, d_from, d_to))
    ids = [s["id"] for s in shifts]
    reloads = db.fetch(f"SELECT * FROM {P}.cotr_reload WHERE shift_id = ANY(%s) AND status = 'received'", (ids,)) if ids else []
    sales = db.fetch(f"SELECT shift_id, lines, payment, total FROM {P}.cotr_sale WHERE shift_id = ANY(%s) AND voided_at IS NULL", (ids,)) if ids else []
    reqs = db.fetch(f"""SELECT shift_id, sum(amount) AS amount FROM {P}.cotr_request WHERE shift_id = ANY(%s) AND status <> 'rejected'
                         GROUP BY 1""", (ids,)) if ids else []
    drivers: dict[str, dict] = {}
    for s in shifts:
        brought = {k: int(v) for k, v in (s["start_stock"] or {}).items()}
        for r in reloads:
            if r["shift_id"] == s["id"]:
                for k, v in received(r).items():
                    brought[k] = brought.get(k, 0) + int(v)
        sold: dict[str, int] = {}
        cash = qris = 0.0
        for x in sales:
            if x["shift_id"] != s["id"]:
                continue
            for line in x["lines"]:
                sold[line["code"]] = sold.get(line["code"], 0) + int(line["qty"])
            if x["payment"] == "qris":
                qris += float(x["total"])
            else:
                cash += float(x["total"])
        end, waste, tasted = s["end_stock"] or {}, s.get("end_waste") or {}, s.get("end_cupping") or {}
        lines = {}
        for code in set(brought) | set(sold) | set(end) | set(waste) | set(tasted):
            t = sold.get(code, 0)
            lines[code] = {"b": brought.get(code, 0), "s": end.get(code) if s["status"] == "closed" else None, "c": tasted.get(code, 0),
                           "w": waste.get(code, 0), "t": t, "total": t * float((menus.get(code) or {}).get("price") or 0)}
        d = drivers.setdefault(s["driver_code"], {"code": s["driver_code"], "name": s["driver_name"], "nickname": s["nickname"], "pool": s["pool_code"], "days": []})
        d["days"].append({"day": _iso(s["day"]), "status": s["status"], "lines": lines, "total": sum(x["total"] for x in lines.values()),
                          "cash": cash, "qris": qris, "operational": sum(float(r["amount"]) for r in reqs if r["shift_id"] == s["id"]),
                          "cashActual": float(s["cash_actual"]) if s.get("cash_actual") is not None else None})
    # the days of the sheets (before the app) where the driver has no shift
    have = {(d["code"], x["day"]) for d in drivers.values() for x in d["days"]}
    for r in db.fetch(f"""SELECT r.*, d.name AS driver_name, d.nickname FROM {P}.cotr_report_day r
                           LEFT JOIN {P}.cotr_driver d ON d.code = r.driver_code
                          WHERE (%s::text IS NULL OR r.pool_code = %s) AND r.day BETWEEN %s AND %s ORDER BY r.label, r.day""",
                      (pool, pool, d_from, d_to)):
        key = r["driver_code"] or r["label"]
        if (key, _iso(r["day"])) in have:
            continue
        lines = {k: v | {"total": int(v.get("t") or 0) * float((menus.get(k) or {}).get("price") or 0)} for k, v in (r["lines"] or {}).items()}
        total = sum(x["total"] for x in lines.values())
        qris = float(r["qris"]) if r["qris"] is not None else 0.0
        d = drivers.setdefault(key, {"code": r["driver_code"], "name": r["driver_name"] or r["label"], "nickname": r["nickname"], "pool": r["pool_code"], "days": []})
        d.setdefault("label", r["label"])
        d["days"].append({"day": _iso(r["day"]), "status": "sheet", "lines": lines, "total": total, "qris": qris, "cash": total - qris,
                          "operational": float(r["operational"]) if r["operational"] is not None else 0.0, "operationalNote": r["operational_note"],
                          "cashActual": float(r["cash_actual"]) if r["cash_actual"] is not None else None})
    for d in drivers.values():
        d["days"].sort(key=lambda x: x["day"])
    listed = sorted(menus.values(), key=lambda m: (m.get("sort") or 0, m["name"]))
    return {"pool": pool, "from": d_from.isoformat(), "to": d_to.isoformat(),
            "menus": [{"code": m["code"], "name": m["name"], "price": float(m["price"])} for m in listed], "drivers": list(drivers.values())}


# ---------------------------------------------------------------- the pool workbooks (sheets Product and Report Armada)

def _key(text: str) -> str:
    return re.sub(r"[^a-z]", "", (text or "").lower())


def _driver_by_label(pool: str) -> list[tuple[str, str, str]]:
    """The drivers of a pool as (nickname, full name, code), letters only, to find the names the sheets use."""
    return [(_key(d["nickname"] or ""), _key(d["name"] or ""), d["code"])
            for d in db.fetch(f"SELECT code, name, nickname FROM {P}.cotr_driver WHERE pool_code = %s", (pool,))]


def _match(label: str, drivers: list[tuple[str, str, str]]) -> Optional[str]:
    """"BNI 10 DAHLAN" → the driver nicknamed Dahlan; "BAHANUALI" → Bahanu Ali; "M.APRIZAL" → Muhammad Aprizal."""
    words = label.split()
    name = " ".join(w for w in words if not re.fullmatch(r"(BNI|BRI|BCA|MANDIRI|\d+)", w.upper()))
    k = _key(name)
    if len(k) < 3:
        return None
    first = _key(name.replace(".", " ").split()[-1] if "." in name else name.split()[0])
    for test in (lambda n, f: k == n or k == f,                      # the nickname or the full name
                 lambda n, f: bool(n) and (k.startswith(n) or n.startswith(k)),
                 lambda n, f: k in f or (len(first) >= 4 and first in f)):
        hit = [c for n, f, c in drivers if test(n, f)]
        if len(hit) == 1:
            return hit[0]
    return None


def import_workbook(user: dict, pool: str, data: bytes, d_from: date, d_to: Optional[date], apply: bool) -> dict:
    """The sheets Product (the figures, the leftover per batch code) and Report Armada (B / S / C / W / T per armada) of a
    pool from `d_from`; the leftover of the day before is the opening. Saved as source 'sheet' (again: replaced)."""
    from app.cotr_sheet import read_workbook
    menus = _menu_map()
    wb = read_workbook(data, menus)
    d_to = d_to or today()
    days = {d: x for d, x in wb["product"].items() if d_from - timedelta(days=1) <= d <= d_to
            and any(k in KINDS or k == "actual" for f in x["figures"].values() for k in f)}
    counts, moves = [], []
    for d, x in sorted(days.items()):
        for code, f in x["figures"].items():
            if "actual" in f:
                lots = x["lots"].get(code) or []
                if lots and sum(q for _, q in lots) == f["actual"]:
                    counts += [(d, code, b, q) for b, q in lots]
                else:  # no block, or one that does not add up: the total without a batch code
                    counts.append((d, code, None, f["actual"]))
            if d >= d_from:
                for k in KINDS:
                    if f.get(k):
                        moves.append((d, code, k, f[k]))
        if d >= d_from and not any(m[0] == d for m in moves):
            moves.append((d, next(iter(menus)), "sold", 0))  # the day is the sheet's even with nothing moved
    first = d_from - timedelta(days=1)
    if not any(c[0] == first for c in counts) and d_from in days:  # no count the day before: the sheet's opening of the first day
        counts = [(first, code, None, f["opening"]) for code, f in days[d_from]["figures"].items() if "opening" in f] + counts
    drivers = _driver_by_label(pool)
    month = wb["month"] or d_from.replace(day=1)
    reports = []
    for r in wb["report"]:
        try:
            d = month.replace(day=r["day"])
        except ValueError:
            continue
        if not d_from <= d <= d_to:
            continue
        code = _match(r["label"], drivers)
        reports.append((d, r, code))
    summary = {"days": [d.isoformat() for d in sorted(days) if d >= d_from], "counts": len(counts), "figures": len(moves),
               "reports": len(reports), "unmatched": sorted({r["label"] for _, r, c in reports if not c})}
    if not apply:
        return summary
    with db.transaction() as conn:
        lo = d_from - timedelta(days=1)
        conn.execute(f"DELETE FROM {P}.cotr_pool_move WHERE pool_code = %s AND source = 'sheet' AND day BETWEEN %s AND %s", (pool, d_from, d_to))
        conn.execute(f"DELETE FROM {P}.cotr_report_day WHERE pool_code = %s AND source = 'sheet' AND day BETWEEN %s AND %s", (pool, d_from, d_to))
        conn.execute(f"DELETE FROM {P}.cotr_pool_count WHERE pool_code = %s AND source = 'sheet' AND day BETWEEN %s AND %s", (pool, lo, d_to))
        for d in {c[0] for c in counts}:  # the sheet's count of a day replaces the one entered
            conn.execute(f"DELETE FROM {P}.cotr_pool_count WHERE pool_code = %s AND day = %s", (pool, d))
        for d, code, batch, qty in counts:
            conn.execute(f"""INSERT INTO {P}.cotr_pool_count (pool_code, day, menu_code, batch_no, qty, source, created_by)
                             VALUES (%s, %s, %s, %s, %s, 'sheet', %s)""", (pool, d, code, batch, qty, user["id"]))
        for d, code, kind, qty in moves:
            conn.execute(f"""INSERT INTO {P}.cotr_pool_move (pool_code, day, menu_code, kind, qty, source, created_by)
                             VALUES (%s, %s, %s, %s, %s, 'sheet', %s)""", (pool, d, code, kind, qty, user["id"]))
        for d, r, code in reports:
            conn.execute(f"""INSERT INTO {P}.cotr_report_day (pool_code, day, label, driver_code, lines, qris, cash_actual, operational,
                                 operational_note)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                             ON CONFLICT (pool_code, day, label) DO UPDATE SET lines = EXCLUDED.lines, driver_code = EXCLUDED.driver_code,
                                 qris = EXCLUDED.qris, cash_actual = EXCLUDED.cash_actual, operational = EXCLUDED.operational,
                                 operational_note = EXCLUDED.operational_note""",
                         (pool, d, r["label"], code, Jsonb(r["lines"]), r["qris"], r["cashActual"], r["operational"], r["operationalNote"]))
    return summary | {"saved": True}
