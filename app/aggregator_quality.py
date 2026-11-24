"""Aggregator Insight stage 3 (migration 151, 2026-11-25): Reviews, Complaints, Void, Price & Menu and Reports.

Reviews    what BotFood keeps of the customers' reviews; the day is the order's, else the day BotFood got it. BotFood gives no
           merchant reply, so nothing is said about replied or not. The topic of a low review comes from its words (TOPICS).
Complaints tickets of the team: from a low review (one ticket per review) or entered by hand, with the first response, the
           resolution and the member handling the outlet that day (handle schedule).
Void       the void / cancelled bills of the delivery apps in ESB, with the reason the team gives each one, and the voids the
           team enters by hand (not in ESB).
Price&Menu per menu name and platform: the prices that differ between outlets, the menus most outlets have and one misses;
           price changes since 25 Nov 2026 (trigger); what sells better on a platform than in the others.
"""
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from statistics import mode
from typing import Any, Optional

from app import aggregator as agg
from app import database as db

P = agg.P
WIB = agg.WIB
LOW = 3  # a review of 3 stars or less
PLATFORMS = ("GoFood", "GrabFood", "ShopeeFood")
TOPICS: list[tuple[str, str]] = [
    ("Pesanan kurang / salah", r"kurang|salah|ga ?ada|gak ada|tidak ada|ketinggalan|beda|tertukar|ga ?dapet|ga ?dikasih|gadikasi|tidak di ?kasih|lupa"),
    ("Kemasan / tumpah", r"tumpah|bocor|kemasan|tutup|seal|rusak|penyok|plastik|sedotan"),
    ("Rasa / kualitas", r"rasa|hambar|manis|pahit|asam|basi|enak|encer|aneh|dingin|panas|gosong|keras|bau"),
    ("Es / suhu", r"\bes\b|mencair|cair|suhu|es batu"),
    ("Lama / pengantaran", r"lama|telat|lambat|nunggu|menunggu|driver|antar"),
    ("Pelayanan", r"kasir|pelayan|ramah|jutek|barista|judes|cancel|batal|respon"),
    ("Harga / porsi", r"mahal|harga|porsi|sedikit|dikit"),
]
COMPLAINT_STATUS = ("open", "progress", "done")
COMPLAINT_SOURCE = ("review", "chat", "app", "other")
VOID_REASONS = ("Stok habis", "Outlet tutup / belum buka", "Salah input", "Driver tidak datang", "Customer batal", "Sistem / aplikasi", "Lainnya")


class QualityError(ValueError):
    pass


def topic(comment: Optional[str]) -> str:
    """The topic of a review from its words (the first that matches). Pure (tested)."""
    c = (comment or "").lower()
    if not c.strip():
        return "Tanpa komentar"
    return next((name for name, rx in TOPICS if re.search(rx, c)), "Lainnya")


def _span(a: date, z: date) -> tuple[datetime, datetime]:
    return agg_at(a), agg_at(z + timedelta(days=1))


def agg_at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=WIB)


def _names() -> dict[int, tuple[Optional[str], str]]:
    """{BotFood outlet id: (our branch code, name)}."""
    return {r["bf_branch_id"]: (r["branch_code"], r["name"]) for r in db.fetch(
        f"""SELECT m.bf_branch_id, m.branch_code, coalesce(b.branch_name, m.bf_name) AS name FROM {P}.agg_branch_map m
             LEFT JOIN integration_esb.master_branches b ON b.branch_code = m.branch_code""")}


def _handlers(a: date, z: date) -> dict[tuple[date, str], str]:
    from app import aggregator_reports
    return aggregator_reports._handlers(a, z) if (z - a).days <= 92 else {}


# ---------------------------------------------------------------- reviews

def reviews(a: date, z: date, platform: Optional[str] = None, limit: int = 800) -> dict:
    t0, t1 = _span(a, z)
    names = _names()
    rows = db.fetch(f"""SELECT id, bf_branch_id, outlet_name, channel, rating, comment, item_name, order_id, tags, customer_name,
                               coalesce(ordered_at, created_at) AS at FROM {P}.agg_review
                         WHERE coalesce(ordered_at, created_at) >= %s AND coalesce(ordered_at, created_at) < %s
                           AND (%s::text IS NULL OR channel = %s)""", (t0, t1, platform, platform))
    rated = [r for r in rows if r["rating"] is not None]
    low = [r for r in rated if r["rating"] <= LOW]
    by: dict[str, list] = defaultdict(list)
    day: dict[str, list] = defaultdict(list)
    for r in rated:
        by[names.get(r["bf_branch_id"], (None, r["outlet_name"]))[1] or "—"].append(r["rating"])
        day[r["at"].astimezone(WIB).date().isoformat()].append(r["rating"])
    who = _handlers(a, z)
    tickets = {str(r["review_id"]): r["id"] for r in db.fetch(f"SELECT id, review_id FROM {P}.agg_complaint WHERE review_id IS NOT NULL")}
    avg = lambda v: round(sum(v) / len(v), 2) if v else None  # noqa: E731
    shown = sorted(low + [r for r in rated if r["rating"] > LOW and (r["comment"] or "").strip()], key=lambda r: r["at"], reverse=True)[:limit]
    out = []
    for r in shown:
        code, name = names.get(r["bf_branch_id"], (None, r["outlet_name"] or "—"))
        d = r["at"].astimezone(WIB)
        out.append({"id": str(r["id"]), "at": d.isoformat(), "outlet": name, "branchCode": code, "platform": r["channel"], "rating": r["rating"],
                    "comment": r["comment"], "item": r["item_name"], "orderId": r["order_id"], "tags": r["tags"], "topic": topic(r["comment"]) if r["rating"] <= LOW else None,
                    "member": who.get((d.date(), code)) if code else None, "complaintId": tickets.get(str(r["id"]))})
    return {"summary": {"n": len(rated), "avg": avg([float(r["rating"]) for r in rated]), "low": len(low), "withComment": sum(bool((r["comment"] or "").strip()) for r in rated),
                        "noTicket": sum(str(r["id"]) not in tickets for r in low), "limit": limit},
            "byRating": [{"rating": k, "n": v} for k, v in sorted(Counter(int(r["rating"]) for r in rated).items(), reverse=True)],
            "byPlatform": [{"platform": p, "n": len(v), "avg": avg(v), "low": sum(x <= LOW for x in v)} for p in PLATFORMS
                           if (v := [float(r["rating"]) for r in rated if r["channel"] == p])],
            "byOutlet": sorted(({"outlet": k, "n": len(v), "avg": avg([float(x) for x in v]), "low": sum(x <= LOW for x in v)} for k, v in by.items()),
                               key=lambda x: (-x["low"], x["avg"] or 5)),
            "topics": [{"topic": k, "n": v} for k, v in Counter(topic(r["comment"]) for r in low).most_common()],
            "days": [{"day": k, "n": len(v), "avg": avg([float(x) for x in v])} for k, v in sorted(day.items())], "rows": out}


# ---------------------------------------------------------------- complaints

def _complaint(r: dict, names: dict[str, str], who: dict) -> dict:
    iso = lambda v: v.astimezone(WIB).isoformat() if v else None  # noqa: E731
    mins = lambda x: int((x - r["created_at"]).total_seconds() // 60) if x else None  # noqa: E731
    return {"id": r["id"], "reviewId": str(r["review_id"]) if r["review_id"] else None, "branchCode": r["branch_code"], "outlet": names.get(r["branch_code"], r["branch_code"] or "—"),
            "platform": r["channel"], "orderNo": r["order_no"], "day": r["happened_on"].isoformat(), "source": r["source"], "category": r["category"],
            "detail": r["detail"], "status": r["status"], "resolution": r["resolution"], "compensation": float(r["compensation"]) if r["compensation"] is not None else None,
            "createdAt": iso(r["created_at"]), "createdBy": r["created_by_name"], "firstResponseAt": iso(r["first_response_at"]), "resolvedAt": iso(r["resolved_at"]),
            "responseMin": mins(r["first_response_at"]), "resolveMin": mins(r["resolved_at"]), "handledBy": r["handled_by_name"],
            "member": who.get((r["happened_on"], r["branch_code"])) if r["branch_code"] else None}


def _branch_names() -> dict[str, str]:
    return {o["branch_code"]: o["branch_name"] for o in agg.outlets()}


def complaints(a: date, z: date, platform: Optional[str] = None) -> dict:
    rows = db.fetch(f"""SELECT * FROM {P}.agg_complaint WHERE (happened_on BETWEEN %s AND %s OR status <> 'done') AND (%s::text IS NULL OR channel = %s)
                        ORDER BY (status = 'done'), happened_on DESC, id DESC LIMIT 2000""", (a, z, platform, platform))
    names, who = _branch_names(), _handlers(a, z)
    data = [_complaint(r, names, who) for r in rows]
    ok = [x for x in data if x["responseMin"] is not None]
    done = [x for x in data if x["resolveMin"] is not None]
    med = lambda v: sorted(v)[len(v) // 2] if v else None  # noqa: E731
    return {"summary": {"n": len(data), **{s: sum(x["status"] == s for x in data) for s in COMPLAINT_STATUS},
                        "responseMedian": med([x["responseMin"] for x in ok]), "resolveMedian": med([x["resolveMin"] for x in done]),
                        "compensation": sum(x["compensation"] or 0 for x in data)},
            "categories": [{"category": k, "n": v} for k, v in Counter(x["category"] for x in data).most_common()],
            "topics": [t for t, _ in TOPICS] + ["Lainnya"], "outlets": [{"code": k, "name": v} for k, v in names.items()], "data": data}


def _who(user: dict) -> str:
    return user.get("full_name") or user["username"]


def create_complaint(user: dict, body: dict) -> dict:
    rid = body.get("reviewId")
    if rid:
        r = db.fetchrow(f"SELECT *, coalesce(ordered_at, created_at) AS at FROM {P}.agg_review WHERE id::text = %s", (str(rid),))
        if r is None:
            raise QualityError("Review tidak ditemukan")
        have = db.fetchrow(f"SELECT id FROM {P}.agg_complaint WHERE review_id = %s", (r["id"],))
        if have:
            return {"id": have["id"], "existing": True}
        code, _ = _names().get(r["bf_branch_id"], (None, None))
        vals = (r["id"], code, r["channel"], r["order_id"] or None, r["at"].astimezone(WIB).date(), "review", topic(r["comment"]),
                (r["comment"] or f"Rating {r['rating']} tanpa komentar")[:2000])
    else:
        if body.get("source") not in COMPLAINT_SOURCE or body.get("platform") not in PLATFORMS:
            raise QualityError("Pilih sumber dan platform")
        if body.get("branchCode") not in _branch_names():
            raise QualityError("Pilih outlet")
        if not (body.get("detail") or "").strip():
            raise QualityError("Tulis isi komplain")
        day = date.fromisoformat(body["day"]) if body.get("day") else datetime.now(WIB).date()
        if day > datetime.now(WIB).date():
            raise QualityError("Tanggal tidak boleh di masa depan")
        vals = (None, body["branchCode"], body["platform"], (body.get("orderNo") or "").strip()[:60] or None, day, body["source"],
                (body.get("category") or topic(body["detail"]))[:60], body["detail"].strip()[:2000])
    r = db.fetchrow(f"""INSERT INTO {P}.agg_complaint (review_id, branch_code, channel, order_no, happened_on, source, category, detail, created_by, created_by_name)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", (*vals, user["id"], _who(user)))
    return {"id": r["id"], "existing": False}


def update_complaint(user: dict, complaint_id: int, body: dict) -> dict:
    cur = db.fetchrow(f"SELECT * FROM {P}.agg_complaint WHERE id = %s", (complaint_id,))
    if cur is None:
        raise LookupError("Komplain tidak ditemukan")
    status = body.get("status", cur["status"])
    if status not in COMPLAINT_STATUS:
        raise QualityError("Status tidak dikenal")
    resolution = (body.get("resolution", cur["resolution"]) or "").strip()[:2000] or None
    if status == "done" and not resolution:
        raise QualityError("Tulis penyelesaiannya sebelum ditandai selesai")
    comp = body.get("compensation", cur["compensation"])
    if comp is not None and float(comp) < 0:
        raise QualityError("Kompensasi tidak boleh minus")
    db.fetchrow(f"""UPDATE {P}.agg_complaint SET status = %s, category = %s, resolution = %s, compensation = %s, handled_by_name = %s, updated_at = now(),
                        first_response_at = CASE WHEN %s <> 'open' THEN coalesce(first_response_at, now()) ELSE first_response_at END,
                        resolved_at = CASE WHEN %s = 'done' THEN coalesce(resolved_at, now()) ELSE NULL END WHERE id = %s RETURNING id""",
                (status, (body.get("category") or cur["category"])[:60], resolution, comp, _who(user), status, status, complaint_id))
    r = db.fetchrow(f"SELECT * FROM {P}.agg_complaint WHERE id = %s", (complaint_id,))
    return _complaint(r, _branch_names(), {})


# ---------------------------------------------------------------- void

CHANNEL = "CASE lower(h.visit_purpose) WHEN 'gofood' THEN 'GoFood' WHEN 'grabfood' THEN 'GrabFood' ELSE 'ShopeeFood' END"


def voids(a: date, z: date, platform: Optional[str] = None) -> dict:
    from app.database import TABLE_TRANSACTIONS
    names, who = _branch_names(), _handlers(a, z)
    rows = db.fetch(
        f"""SELECT h.sales_num, h.bill_num, h.branch_code, {CHANNEL} AS channel, h.total_amount, h.sales_date::date AS day, h.sales_date_in, h.status,
                   h.raw_data->>'editedBy' AS edited, h.raw_data->>'additionalInfo' AS info, n.reason, n.note, n.by_name
              FROM {TABLE_TRANSACTIONS} h LEFT JOIN {P}.agg_void n ON n.sales_num = h.sales_num
             WHERE h.sales_date >= %s AND h.sales_date < %s::date + 1 AND h.status IN ('Void', 'Cancelled')
               AND lower(h.visit_purpose) IN ('gofood', 'grabfood', 'shopeefood') ORDER BY h.sales_date_in DESC""", (a, z))
    data = [{"key": r["sales_num"], "manual": False, "orderNo": r["info"] or r["bill_num"] or r["sales_num"], "bill": r["bill_num"], "branchCode": r["branch_code"],
             "outlet": names.get(r["branch_code"], r["branch_code"]), "platform": r["channel"], "day": r["day"].isoformat(),
             "time": r["sales_date_in"].strftime("%H:%M") if r["sales_date_in"] else None, "amount": float(r["total_amount"] or 0), "editedBy": r["edited"],
             "reason": r["reason"], "note": r["note"], "by": r["by_name"], "member": who.get((r["day"], r["branch_code"]))} for r in rows]
    for r in db.fetch(f"SELECT * FROM {P}.agg_void WHERE sales_num IS NULL AND happened_on BETWEEN %s AND %s ORDER BY happened_on DESC, id DESC", (a, z)):
        data.append({"key": f"m{r['id']}", "manual": True, "orderNo": r["order_no"], "bill": None, "branchCode": r["branch_code"], "outlet": names.get(r["branch_code"], r["branch_code"]),
                     "platform": r["channel"], "day": r["happened_on"].isoformat(), "time": None, "amount": float(r["amount"] or 0), "editedBy": None,
                     "reason": r["reason"], "note": r["note"], "by": r["by_name"], "member": who.get((r["happened_on"], r["branch_code"]))})
    if platform:
        data = [x for x in data if x["platform"] == platform]
    sales = db.fetchrow(
        f"""SELECT count(*) AS n FROM {TABLE_TRANSACTIONS} h WHERE h.sales_date >= %s AND h.sales_date < %s::date + 1 AND h.status = 'Finished'
             AND coalesce(h.bill_num, '') <> '' AND lower(h.visit_purpose) IN ('gofood', 'grabfood', 'shopeefood')
             AND (%s::text IS NULL OR lower(h.visit_purpose) = lower(%s))""", (a, z, platform, platform))["n"]
    by: dict[str, dict] = {}
    for x in data:
        o = by.setdefault(x["outlet"], {"outlet": x["outlet"], "n": 0, "amount": 0.0})
        o["n"] += 1
        o["amount"] += x["amount"]
    n = len(data)
    return {"summary": {"n": n, "amount": sum(x["amount"] for x in data), "noReason": sum(not x["reason"] for x in data), "manual": sum(x["manual"] for x in data),
                        "bills": int(sales), "pct": round(n / (n + sales) * 100, 2) if n + sales else None},
            "reasons": [{"reason": k, "n": v} for k, v in Counter(x["reason"] or "Belum diisi" for x in data).most_common()],
            "byOutlet": sorted(by.values(), key=lambda o: -o["n"]), "choices": list(VOID_REASONS), "outlets": [{"code": k, "name": v} for k, v in names.items()],
            "data": data[:3000]}


def set_void_reason(user: dict, keys: list[str], reason: str, note: Optional[str]) -> int:
    if reason not in VOID_REASONS:
        raise QualityError("Alasan tidak dikenal")
    note = (note or "").strip()[:500] or None
    if reason == "Lainnya" and not note:
        raise QualityError("Tulis keterangan untuk alasan Lainnya")
    with db.transaction() as conn:
        for k in dict.fromkeys(keys):
            if re.fullmatch(r"m\d+", k):
                conn.execute(f"UPDATE {P}.agg_void SET reason = %s, note = %s, by_name = %s, updated_at = now() WHERE id = %s AND sales_num IS NULL", (reason, note, _who(user), int(k[1:])))
            else:
                conn.execute(f"""INSERT INTO {P}.agg_void (sales_num, reason, note, by_name, created_by) VALUES (%s, %s, %s, %s, %s)
                                 ON CONFLICT (sales_num) DO UPDATE SET reason = EXCLUDED.reason, note = EXCLUDED.note, by_name = EXCLUDED.by_name, updated_at = now()""",
                             (k, reason, note, _who(user), user["id"]))
    return len(keys)


def add_void(user: dict, body: dict) -> int:
    if body.get("platform") not in PLATFORMS or body.get("branchCode") not in _branch_names():
        raise QualityError("Pilih outlet dan platform")
    if body.get("reason") not in VOID_REASONS:
        raise QualityError("Pilih alasan")
    order = (body.get("orderNo") or "").strip()[:60]
    if not order:
        raise QualityError("Isi nomor order")
    day = date.fromisoformat(body["day"])
    if day > datetime.now(WIB).date():
        raise QualityError("Tanggal tidak boleh di masa depan")
    if float(body.get("amount") or 0) < 0:
        raise QualityError("Nominal tidak boleh minus")
    if db.fetchrow(f"SELECT 1 AS x FROM {P}.agg_void WHERE order_no = %s AND channel = %s AND sales_num IS NULL", (order, body["platform"])):
        raise QualityError("Order ini sudah dicatat")
    return db.fetchrow(f"""INSERT INTO {P}.agg_void (branch_code, channel, order_no, happened_on, amount, reason, note, by_name, created_by)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                       (body["branchCode"], body["platform"], order, day, body.get("amount") or 0, body["reason"], (body.get("note") or "").strip()[:500] or None,
                        _who(user), user["id"]))["id"]


# ---------------------------------------------------------------- price & menu

def menu_key(name: str) -> str:
    """The same menu written differently ("Es Kopi Calf  (Reguler)" = "es kopi calf reguler"). Pure (tested)."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split())


def price_spread(items: list[dict]) -> list[dict]:
    """The menus of one platform whose price differs between outlets: the usual price (most outlets) and who differs.
    items: {name, price, outlet, platform}. Pure (tested)."""
    by: dict[tuple, list[dict]] = defaultdict(list)
    for i in items:
        if i["price"]:
            by[(i["platform"], menu_key(i["name"]))].append(i)
    out = []
    for (p, _), v in by.items():
        prices = [float(x["price"]) for x in v]
        if len(v) < 3 or len(set(prices)) == 1:
            continue
        usual = mode(prices)
        diff = [{"outlet": x["outlet"], "price": float(x["price"])} for x in v if float(x["price"]) != usual]
        out.append({"platform": p, "menu": v[0]["name"], "outlets": len(v), "usual": usual, "min": min(prices), "max": max(prices),
                    "different": len(diff), "who": sorted(diff, key=lambda x: x["price"])})
    return sorted(out, key=lambda x: (-x["different"], x["menu"]))


def missing_menus(items: list[dict], share: float = 0.8) -> list[dict]:
    """The menus at least `share` of the outlets of a platform have and an outlet does not. Pure (tested)."""
    outlets: dict[str, set] = defaultdict(set)
    has: dict[tuple, set] = defaultdict(set)
    label: dict[tuple, str] = {}
    for i in items:
        outlets[i["platform"]].add(i["outlet"])
        k = (i["platform"], menu_key(i["name"]))
        has[k].add(i["outlet"])
        label.setdefault(k, i["name"])
    out = []
    for (p, k), have in has.items():
        if len(outlets[p]) >= 5 and len(have) >= share * len(outlets[p]):
            for o in sorted(outlets[p] - have):
                out.append({"platform": p, "outlet": o, "menu": label[(p, k)], "have": len(have), "of": len(outlets[p])})
    return sorted(out, key=lambda x: (x["outlet"], x["platform"], x["menu"]))


def menu(platform: Optional[str] = None) -> dict:
    names = _names()
    rows = db.fetch(f"""SELECT i.name, i.category, i.price, i.in_stock, c.channel, c.bf_branch_id, c.name AS cname FROM {P}.agg_item i
                          JOIN {P}.agg_channel c ON c.id = i.channel_id WHERE (%s::text IS NULL OR c.channel = %s)""", (platform, platform))
    packaging = lambda r: any(w in (r["category"] or "").upper() for w in ("PACKAG", "KEMASAN"))  # noqa: E731
    items = [{"name": r["name"], "price": float(r["price"]) if r["price"] is not None else None, "platform": r["channel"],
              "outlet": names.get(r["bf_branch_id"], (None, r["cname"]))[1]} for r in rows if not packaging(r)]
    spread, missing = price_spread(items), missing_menus(items)
    changes = [{"at": r["at"].astimezone(WIB).isoformat(), "menu": r["name"], "platform": r["channel"], "outlet": names.get(r["bf_branch_id"], (None, "—"))[1],
                "old": float(r["old_price"]), "new": float(r["new_price"])} for r in db.fetch(
        f"""SELECT l.at, i.name, c.channel, c.bf_branch_id, l.old_price, l.new_price FROM {P}.agg_price_log l JOIN {P}.agg_item i ON i.id = l.item_id
             JOIN {P}.agg_channel c ON c.id = i.channel_id WHERE (%s::text IS NULL OR c.channel = %s) ORDER BY l.at DESC LIMIT 500""", (platform, platform))]
    return {"summary": {"items": len(items), "spread": len(spread), "differ": sum(x["different"] for x in spread), "missing": len(missing), "changes": len(changes)},
            "spread": spread[:800], "missing": missing[:3000], "changes": changes}


def fit(channel: Optional[str], branch: Optional[str], days: int = 30) -> dict:
    """What sells on a delivery app: the share of every menu on the platform against its share on the other channels
    (1.5 = half again as strong there). Menus of 20 portions or more."""
    z = datetime.now(WIB).date() - timedelta(days=1)
    a = z - timedelta(days=days - 1)
    rows = db.fetch(f"""SELECT menu_name, lower(channel) AS ch, sum(qty) AS qty, sum(subtotal) AS amount FROM {P}.agg_menu_daily
                         WHERE sales_date BETWEEN %s AND %s AND kind = 'menu' AND (%s::text IS NULL OR branch_code = %s) GROUP BY 1, 2""", (a, z, branch, branch))
    if not rows:
        rows = db.fetch(f"""SELECT menu_name, lower(channel) AS ch, sum(qty) AS qty, sum(subtotal) AS amount FROM {P}.agg_menu_daily
                             WHERE sales_date BETWEEN %s AND %s AND (%s::text IS NULL OR branch_code = %s) GROUP BY 1, 2""", (a, z, branch, branch))
    on = {channel.lower()} if channel else {p.lower() for p in PLATFORMS}
    here: Counter = Counter()
    rest: Counter = Counter()
    money: Counter = Counter()
    for r in rows:
        if r["ch"] in on:
            here[r["menu_name"]] += float(r["qty"] or 0)
            money[r["menu_name"]] += float(r["amount"] or 0)
        else:
            rest[r["menu_name"]] += float(r["qty"] or 0)
    th, tr = sum(here.values()) or 1, sum(rest.values()) or 1
    out = []
    for m in set(here) | set(rest):
        if here[m] + rest[m] < 20:
            continue
        sh, sr = here[m] / th, rest[m] / tr
        out.append({"menu": m, "qty": here[m], "amount": money[m], "share": round(sh * 100, 2), "shareElsewhere": round(sr * 100, 2),
                    "lift": round(sh / sr, 2) if sr else None})
    out.sort(key=lambda x: -x["qty"])
    return {"from": a.isoformat(), "to": z.isoformat(), "data": out[:300]}


# ---------------------------------------------------------------- hours per outlet (those BotFood has none for)

def hours() -> dict[str, dict]:
    return {r["branch_code"]: r for r in db.fetch(f"SELECT branch_code, open_time, close_time, by_name FROM {P}.agg_hours")}


def hours_list() -> list[dict]:
    have = hours()
    bf = {r["branch_code"]: r for r in db.fetch(
        f"""SELECT m.branch_code, min(c.open_time) AS o, max(c.close_time) AS z FROM {P}.agg_channel c JOIN {P}.agg_branch_map m ON m.bf_branch_id = c.bf_branch_id
             WHERE m.branch_code IS NOT NULL AND c.open_time IS NOT NULL GROUP BY 1""")}
    f = lambda t: t.strftime("%H:%M") if t else None  # noqa: E731
    return [{"code": k, "name": n, "botfood": f"{f(bf[k]['o'])}–{f(bf[k]['z']) or ''}" if k in bf else None, "open": f(have[k]["open_time"]) if k in have else None,
             "close": f(have[k]["close_time"]) if k in have else None, "by": have[k]["by_name"] if k in have else None} for k, n in _branch_names().items()]


def set_hours(user: dict, rows: list[dict]) -> None:
    known = _branch_names()
    with db.transaction() as conn:
        for r in rows:
            if r["code"] not in known:
                raise QualityError("Outlet tidak dikenal")
            if not r.get("open"):
                conn.execute(f"DELETE FROM {P}.agg_hours WHERE branch_code = %s", (r["code"],))
                continue
            conn.execute(f"""INSERT INTO {P}.agg_hours (branch_code, open_time, close_time, by_name) VALUES (%s, %s, %s, %s)
                             ON CONFLICT (branch_code) DO UPDATE SET open_time = EXCLUDED.open_time, close_time = EXCLUDED.close_time, by_name = EXCLUDED.by_name, updated_at = now()""",
                         (r["code"], r["open"], r.get("close") or None, _who(user)))


# ---------------------------------------------------------------- overview & reports

def brief(a: date, z: date) -> dict:
    """The figures of the Overview page."""
    rv, cp, vd = reviews(a, z, limit=0), complaints(a, z), voids(a, z)
    m = menu()["summary"]
    return {"from": a.isoformat(), "to": z.isoformat(), "reviews": rv["summary"], "topics": rv["topics"][:5], "complaints": cp["summary"], "voids": vd["summary"],
            "worst": rv["byOutlet"][:5], "menu": m}


def report(a: date, z: date, platform: Optional[str] = None) -> dict[str, Any]:
    """Everything of a period per outlet: the sheets of the Reports page (Summary first)."""
    from app import aggregator_reports
    op = aggregator_reports.opening(a, z, platform)
    oos = aggregator_reports.oos(a, z, platform, False, 14)
    rv, cp, vd = reviews(a, z, platform), complaints(a, z, platform), voids(a, z, platform)
    per: dict[str, dict] = defaultdict(lambda: {"days": 0, "on": 0, "late": 0, "closed": 0, "oos": 0, "reviews": 0, "avg": None, "low": 0, "complaints": 0, "voids": 0, "void": 0.0, "members": set()})
    for r in op["rows"]:
        if r["status"] in ("on_time", "late", "not_opened"):
            p = per[r["outlet"]]
            p["days"] += 1
            p["on"] += r["status"] == "on_time"
            p["late"] += r["status"] == "late"
            p["closed"] += r["status"] == "not_opened"
            if r["member"]:
                p["members"].add(r["member"])
    for x in oos["items"]:
        per[x["outlet"]]["oos"] += x["times"]
    for x in rv["byOutlet"]:
        per[x["outlet"]].update(reviews=x["n"], avg=x["avg"], low=x["low"])
    for x in cp["data"]:
        per[x["outlet"]]["complaints"] += 1
    for x in vd["byOutlet"]:
        per[x["outlet"]].update(voids=x["n"], void=x["amount"])
    s = op["summary"]
    summary = [["Hari-outlet dihitung", s["counted"]], ["Tepat waktu (minimal)", s["onTime"]],
               ["Tepat waktu % (minimal)", round(s["onTime"] / s["counted"] * 100, 1) if s["counted"] else None], ["Terlambat (maksimal)", s["late"]],
               ["Tidak buka", s["notOpened"]], ["Item kosong sekarang", oos["summary"]["outNow"]], ["Kali kosong dalam periode", oos["summary"]["timesInPeriod"]],
               ["Review", rv["summary"]["n"]], ["Rating rata-rata", rv["summary"]["avg"]], ["Review rendah (≤ 3)", rv["summary"]["low"]],
               ["Komplain", cp["summary"]["n"]], ["Komplain selesai", cp["summary"]["done"]], ["Void", vd["summary"]["n"]], ["Nilai void (Rp)", round(vd["summary"]["amount"])],
               ["Void % dari order", vd["summary"]["pct"]], ["Void tanpa alasan", vd["summary"]["noReason"]]]
    return {"from": a.isoformat(), "to": z.isoformat(), "summary": summary, "sheets": [
        {"sheet": "Summary", "title": f"Aggregator {a:%d/%m/%Y} – {z:%d/%m/%Y}" + (f" · {platform}" if platform else ""), "header": ["Ukuran", "Nilai"], "rows": summary},
        {"sheet": "Per outlet", "header": ["Outlet", "Hari-outlet", "Tepat waktu (min)", "Terlambat (maks)", "Tidak buka", "Kali item kosong", "Review", "Rating", "Review rendah",
                                           "Komplain", "Void", "Nilai void", "Ditangani"],
         "rows": [[k, p["days"], p["on"], p["late"], p["closed"], p["oos"], p["reviews"], p["avg"], p["low"], p["complaints"], p["voids"], round(p["void"]), ", ".join(sorted(p["members"]))]
                  for k, p in sorted(per.items())]},
        {"sheet": "Topik review rendah", "header": ["Topik", "Jumlah"], "rows": [[x["topic"], x["n"]] for x in rv["topics"]]},
        {"sheet": "Review rendah", "header": ["Waktu", "Outlet", "Platform", "Rating", "Topik", "Komentar", "Item", "Order", "Ditangani"],
         "rows": [[x["at"][:16].replace("T", " "), x["outlet"], x["platform"], x["rating"], x["topic"], x["comment"], x["item"], x["orderId"], x["member"]] for x in rv["rows"] if x["rating"] <= LOW]},
        {"sheet": "Komplain", "header": ["Tanggal", "Outlet", "Platform", "Order", "Sumber", "Kategori", "Isi", "Status", "Penyelesaian", "Kompensasi", "Respons (menit)", "Selesai (menit)", "Ditangani"],
         "rows": [[x["day"], x["outlet"], x["platform"], x["orderNo"], x["source"], x["category"], x["detail"], x["status"], x["resolution"], x["compensation"], x["responseMin"], x["resolveMin"], x["handledBy"]] for x in cp["data"]]},
        {"sheet": "Void", "header": ["Tanggal", "Jam", "Outlet", "Platform", "Order / bill", "Nominal", "Alasan", "Keterangan", "Sumber", "Diisi oleh"],
         "rows": [[x["day"], x["time"], x["outlet"], x["platform"], x["orderNo"], x["amount"], x["reason"], x["note"], "Manual" if x["manual"] else "ESB", x["by"]] for x in vd["data"]]},
        {"sheet": "Alasan void", "header": ["Alasan", "Jumlah"], "rows": [[x["reason"], x["n"]] for x in vd["reasons"]]},
    ]}
