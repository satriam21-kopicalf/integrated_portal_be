"""GrabFood monitoring data (integration_esb.grab_*): outlet open/close status, official rating and customer reviews,
read from the Grab Merchant portal by scripts/sync_grab.js (integrated-esbapi) with a dashboard-refreshed session.

Every outlet of the merchant group is monitored (2026-10-27). An outlet is shown in the scope of its ESB branch; an
outlet not mapped to a branch yet is seen only by users with every branch (super admin). Reviews: every review the
sync has read, paged and filtered (outlet, rating, photo, reply, text, period); photos are Grab's signed CDN links.

The portal only reads these tables and lets a super admin refresh the session cookie; it never holds the password.
"""
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app import database as db

SCHEMA = "integration_esb"
MAX_PAGE = 100
WIB = timezone(timedelta(hours=7))


def _scope_sql(scope, alias="s") -> tuple[str, list]:
    if scope is None:
        return "", []
    if not scope:
        return f" AND {alias}.branch_code = '-'", []   # no branch: empty
    return f" AND {alias}.branch_code = ANY(%s)", [list(scope)]


def _iso(v):
    return v.isoformat() if v else None


def _like(q: str) -> str:
    """A search text as an ILIKE pattern that matches it literally (% and _ escaped)."""
    return "%" + re.sub(r"([%_\\])", r"\\\1", q.strip()) + "%"


def session_info() -> dict:
    """State of the saved merchant session for the dashboard (valid, when refreshed, when last used)."""
    row = db.fetchrow(f"SELECT is_valid, updated_at, updated_by, last_checked_at, note FROM {SCHEMA}.grab_session WHERE id = 1")
    if not row:
        return {"present": False, "valid": False}
    return {"present": True, "valid": row["is_valid"], "updatedAt": _iso(row["updated_at"]),
            "updatedBy": row["updated_by"], "lastCheckedAt": _iso(row["last_checked_at"]), "note": row["note"]}


def _minutes(hhmm: str) -> Optional[int]:
    m = re.match(r"^(\d{1,2}):(\d{2})", hhmm or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def day_ranges(opening_hours, day: date) -> list[tuple[int, int]]:
    """Opening ranges of `day` in minutes since 00:00 WIB from Grab's weekly hours (7 entries, Sunday first). A range
    ending at or before its start runs past midnight and is cut at 24:00."""
    if not isinstance(opening_hours, list) or len(opening_hours) != 7:
        return []
    out = []
    for r in (opening_hours[(day.weekday() + 1) % 7] or {}).get("ranges") or []:
        a, b = _minutes(r.get("start")), _minutes(r.get("end"))
        if a is None or b is None:
            continue
        out.append((a, b if b > a else 24 * 60))
    return out


def closed_minutes(ranges: list[tuple[int, int]], events: list[tuple[datetime, Optional[bool]]], day_start: datetime,
                   now: datetime) -> int:
    """Minutes of `ranges` (minutes since day_start) up to `now` during which the outlet was not open. `events` are the
    status changes (time, is_open) in time order, the first one possibly before day_start; before the first known
    status the outlet counts as open (no data, no blame)."""
    if not ranges:
        return 0
    state, closed = True, 0.0
    cuts = [(day_start, None)] + [(t, o) for t, o in events if t > day_start] + [(now, None)]
    for t, o in events:
        if t <= day_start:
            state = o is not False
    for (t0, o0), (t1, _) in zip(cuts, cuts[1:]):
        if o0 is not None:
            state = o0 is not False
        if state or t1 <= t0:
            continue
        a = (t0 - day_start).total_seconds() / 60
        b = (t1 - day_start).total_seconds() / 60
        for ra, rb in ranges:
            closed += max(0.0, min(b, rb) - max(a, ra))
    return int(round(closed))


def _hours_view(opening_hours, events, is_open, now: datetime) -> dict:
    """Opening hours of the outlet: the week, today, whether it should be open now, minutes closed in hours today."""
    local = now.astimezone(WIB)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    today = day_ranges(opening_hours, local.date())
    mins = local.hour * 60 + local.minute
    should = any(a <= mins < b for a, b in today) if opening_hours else None
    fmt = lambda m: f"{m // 60:02d}:{m % 60:02d}"  # noqa: E731
    week = [[{"start": fmt(a), "end": fmt(b)} for a, b in day_ranges(opening_hours, (day_start + timedelta(days=i)).date())]
            for i in range(-((local.weekday() + 1) % 7), 7 - ((local.weekday() + 1) % 7))] if opening_hours else []
    return {"week": week, "today": [{"start": fmt(a), "end": fmt(b)} for a, b in today], "shouldBeOpen": should,
            "closedInHours": bool(should and is_open is False),
            "closedMinutesToday": closed_minutes(today, events, day_start, local) if opening_hours else None}


# CASE, not AND: Postgres may evaluate jsonb_array_length first, which fails on a review without photos (JSON null)
HAS_PHOTO = "(CASE WHEN jsonb_typeof(gr.image_urls) = 'array' THEN jsonb_array_length(gr.image_urls) ELSE 0 END) > 0"


def overview(scope) -> list[dict]:
    """One row per Grab outlet in scope: ESB branch, latest status, official rating, reviews (30 days and all)."""
    cond, params = _scope_sql(scope)
    rows = db.fetch(
        f"""
        SELECT s.merchant_id, s.merchant_name, s.branch_code, s.city_name, s.address, s.is_active, s.photo_url, s.menu_synced_at,
               s.opening_hours, s.close_period_start, s.close_period_end,
               COALESCE(b.branch_name, s.merchant_name) AS branch_name,
               st.is_open, st.status_label, st.is_busy, st.close_reason, st.pause_cause, st.open_until, st.pause_until,
               st.checked_at AS status_at,
               r.score, r.rating_count, r.distribution, r.captured_on AS rating_on,
               rv.review_count, rv.avg_recent, rv.low_count, rv.unreplied, rv.last_review_at,
               ra.total_reviews, ra.photo_reviews, mn.out_of_stock, mn.switched_off
        FROM {SCHEMA}.grab_store s
        LEFT JOIN {SCHEMA}.master_branches b ON b.branch_code = s.branch_code
        LEFT JOIN {SCHEMA}.grab_store_status_now st ON st.merchant_id = s.merchant_id
        LEFT JOIN LATERAL (
            SELECT score, rating_count, distribution, captured_on FROM {SCHEMA}.grab_rating g
            WHERE g.merchant_id = s.merchant_id ORDER BY captured_on DESC LIMIT 1
        ) r ON true
        LEFT JOIN LATERAL (
            SELECT count(*)::int AS review_count, round(avg(rating), 2) AS avg_recent,
                   count(*) FILTER (WHERE rating <= 2)::int AS low_count,
                   count(*) FILTER (WHERE NOT has_reply)::int AS unreplied,
                   max(created_at) AS last_review_at
            FROM {SCHEMA}.grab_review gr WHERE gr.merchant_id = s.merchant_id AND gr.created_at >= NOW() - INTERVAL '30 days'
        ) rv ON true
        LEFT JOIN LATERAL (
            SELECT count(*)::int AS total_reviews,
                   count(*) FILTER (WHERE {HAS_PHOTO})::int AS photo_reviews
            FROM {SCHEMA}.grab_review gr WHERE gr.merchant_id = s.merchant_id
        ) ra ON true
        LEFT JOIN LATERAL (
            SELECT count(*) FILTER (WHERE m.status = 'OUT_OF_STOCK')::int AS out_of_stock,
                   count(*) FILTER (WHERE m.status = 'OFF')::int AS switched_off
            FROM {SCHEMA}.grab_menu_item m WHERE m.merchant_id = s.merchant_id AND m.status <> 'AVAILABLE'
        ) mn ON true
        WHERE s.is_active {cond}
        ORDER BY branch_name
        """, params or None)
    # status changes since yesterday (the one before today's 00:00 WIB gives the state at the start of the day)
    now = datetime.now(WIB)
    since = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
    events: dict[str, list] = {}
    if rows:
        for e in db.fetch(f"""SELECT merchant_id, checked_at, is_open FROM {SCHEMA}.grab_store_status
                              WHERE checked_at >= %s AND merchant_id = ANY(%s) ORDER BY merchant_id, checked_at""",
                          [since, [r["merchant_id"] for r in rows]]):
            events.setdefault(e["merchant_id"], []).append((e["checked_at"], e["is_open"]))
    return [{
        "merchantId": r["merchant_id"], "merchantName": r["merchant_name"], "branchCode": r["branch_code"],
        "branchName": r["branch_name"], "cityName": r["city_name"], "address": r["address"], "photoUrl": r["photo_url"],
        "status": {"isOpen": r["is_open"], "label": r["status_label"], "isBusy": r["is_busy"],
                   "closeReason": r["close_reason"], "pauseCause": r["pause_cause"],
                   "openUntil": _iso(r["open_until"]), "pauseUntil": _iso(r["pause_until"]), "checkedAt": _iso(r["status_at"])},
        "rating": {"score": float(r["score"]) if r["score"] is not None and (r["rating_count"] or 0) > 0 else None,  # 0 = no rating yet
                   "count": r["rating_count"],
                   "distribution": r["distribution"], "capturedOn": _iso(r["rating_on"])},
        "reviews30d": {"count": r["review_count"] or 0, "avg": float(r["avg_recent"]) if r["avg_recent"] is not None else None,
                       "low": r["low_count"] or 0, "unreplied": r["unreplied"] or 0, "lastAt": _iso(r["last_review_at"])},
        "reviewsAll": {"count": r["total_reviews"] or 0, "withPhoto": r["photo_reviews"] or 0},
        "menu": {"outOfStock": r["out_of_stock"] or 0, "off": r["switched_off"] or 0, "syncedAt": _iso(r["menu_synced_at"])},
        "hours": {**_hours_view(r["opening_hours"], events.get(r["merchant_id"], []), r["is_open"], now),
                  "closePeriodStart": _iso(r["close_period_start"]), "closePeriodEnd": _iso(r["close_period_end"])},
    } for r in rows]


def _review(r) -> dict:
    images = r["image_urls"] if isinstance(r["image_urls"], list) else []
    return {
        "reviewId": r["review_id"], "merchantId": r["merchant_id"], "branchCode": r["branch_code"],
        "branchName": r["branch_name"] or r["merchant_name"], "merchantName": r["merchant_name"],
        "createdAt": _iso(r["created_at"]), "rating": r["rating"], "description": r["description"],
        "orderedItems": r["ordered_items"] if isinstance(r["ordered_items"], list) else [],
        "aspects": r["aspects"] if isinstance(r["aspects"], list) else [], "hasReply": bool(r["has_reply"]),
        "serviceType": r["service_type"], "imageUrls": [u for u in images if isinstance(u, str) and u.startswith("https://")],
        "eaterName": r["eater_name"], "orderId": r["order_id"], "bookingCode": r["booking_code"], "status": r["status"],
    }


REVIEW_COLS = f"""gr.review_id, gr.merchant_id, gr.created_at, gr.rating, gr.description, gr.ordered_items, gr.aspects,
    gr.has_reply, gr.service_type, gr.image_urls, gr.eater_name, gr.order_id, gr.booking_code, gr.status,
    s.branch_code, s.merchant_name, b.branch_name
    FROM {SCHEMA}.grab_review gr
    JOIN {SCHEMA}.grab_store s ON s.merchant_id = gr.merchant_id
    LEFT JOIN {SCHEMA}.master_branches b ON b.branch_code = s.branch_code"""


def reviews(scope, *, merchant: Optional[str] = None, branch: Optional[str] = None, ratings: Optional[list[int]] = None,
            photo: Optional[bool] = None, replied: Optional[bool] = None, q: Optional[str] = None,
            date_from: Optional[date] = None, date_to: Optional[date] = None, page: int = 1, size: int = 20) -> dict:
    """Every review in scope, newest first, filtered and paged: {data, total, page, size, counts}."""
    cond, params = _scope_sql(scope)
    where, args = [f"TRUE {cond}"], list(params)
    if merchant:
        where.append("gr.merchant_id = %s"); args.append(merchant)
    if branch:
        where.append("s.branch_code = %s"); args.append(branch)
    if ratings:
        where.append("gr.rating = ANY(%s)"); args.append([int(x) for x in ratings])
    if photo is not None:
        where.append(HAS_PHOTO if photo else f"NOT ({HAS_PHOTO})")
    if replied is not None:
        where.append("gr.has_reply" if replied else "NOT gr.has_reply")
    if q and q.strip():
        like = _like(q)
        where.append("(gr.description ILIKE %s OR gr.ordered_items::text ILIKE %s OR gr.eater_name ILIKE %s OR gr.order_id ILIKE %s)")
        args += [like, like, like, like]
    if date_from:
        where.append("gr.created_at >= %s"); args.append(date_from)
    if date_to:
        where.append("gr.created_at < %s"); args.append(date_to + timedelta(days=1))
    sql_where = " AND ".join(where)
    size = max(1, min(int(size), MAX_PAGE))
    page = max(1, int(page))
    head = db.fetchrow(
        f"""SELECT count(*)::int AS total, count(*) FILTER (WHERE {HAS_PHOTO})::int AS with_photo,
                   count(*) FILTER (WHERE NOT gr.has_reply)::int AS unreplied, count(*) FILTER (WHERE gr.rating <= 2)::int AS low,
                   round(avg(gr.rating), 2) AS avg
            FROM {SCHEMA}.grab_review gr JOIN {SCHEMA}.grab_store s ON s.merchant_id = gr.merchant_id WHERE {sql_where}""",
        args) or {}
    rows = db.fetch(f"SELECT {REVIEW_COLS} WHERE {sql_where} ORDER BY gr.created_at DESC, gr.review_id LIMIT %s OFFSET %s",
                    [*args, size, (page - 1) * size])
    return {"data": [_review(r) for r in rows], "total": head.get("total") or 0, "page": page, "size": size,
            "counts": {"withPhoto": head.get("with_photo") or 0, "unreplied": head.get("unreplied") or 0,
                       "low": head.get("low") or 0, "avg": float(head["avg"]) if head.get("avg") is not None else None}}


def review(review_id: str, scope) -> Optional[dict]:
    """One review (the preview), or None when it is not in the user's scope."""
    cond, params = _scope_sql(scope)
    r = db.fetchrow(f"SELECT {REVIEW_COLS} WHERE gr.review_id = %s {cond}", [review_id, *params])
    return _review(r) if r else None


def branch_options() -> list[dict]:
    """ESB branches (company CALF) for mapping a Grab store, with the store already using each one (if any)."""
    rows = db.fetch(
        f"""SELECT b.branch_code, b.branch_name, s.merchant_id AS used_by
            FROM {SCHEMA}.master_branches b
            LEFT JOIN {SCHEMA}.grab_store s ON s.branch_code = b.branch_code
            WHERE COALESCE(b.company_code, 'CALF') = 'CALF' AND b.branch_name NOT ILIKE 'BULK%%'
            ORDER BY b.branch_name""")
    return [{"code": r["branch_code"], "name": r["branch_name"], "usedBy": r["used_by"]} for r in rows]


def set_branch(merchant_id: str, branch_code: Optional[str]) -> dict:
    """Map a Grab store to its ESB branch by hand (super admin); None unmaps it. One store per branch."""
    store = db.fetchrow(f"SELECT merchant_id FROM {SCHEMA}.grab_store WHERE merchant_id = %s", [merchant_id])
    if not store:
        raise LookupError("Outlet Grab tidak ditemukan")
    if branch_code:
        if not db.fetchrow(f"SELECT 1 AS ok FROM {SCHEMA}.master_branches WHERE branch_code = %s", [branch_code]):
            raise ValueError(f"Cabang {branch_code} tidak ada di ESB")
        other = db.fetchrow(f"SELECT merchant_name FROM {SCHEMA}.grab_store WHERE branch_code = %s AND merchant_id <> %s",
                            [branch_code, merchant_id])
        if other:
            raise ValueError(f"Cabang {branch_code} sudah dipakai {other['merchant_name']}")
    with db.transaction() as c:
        # 'manual' also when unmapped: the engine does not map it again by name
        c.execute(f"UPDATE {SCHEMA}.grab_store SET branch_code = %s, branch_match = 'manual' WHERE merchant_id = %s",
                  (branch_code or None, merchant_id))
    return {"merchantId": merchant_id, "branchCode": branch_code or None}


MENU_STATUSES = ("AVAILABLE", "OUT_OF_STOCK", "OFF")


def menu(scope, *, merchant: Optional[str] = None, status: Optional[str] = "unavailable", kind: Optional[str] = None,
         q: Optional[str] = None, limit: int = 2000) -> dict:
    """Menu items / add-ons of the outlets in scope with their GrabFood availability.
    status: 'unavailable' (out of stock or switched off, the default), one of MENU_STATUSES, 'all', or 'price': the
    menu items whose price differs from the price most outlets of the same city use (refPrice; at least 3 outlets)."""
    cond, params = _scope_sql(scope)
    where, args = [f"s.is_active {cond}"], list(params)
    if merchant:
        where.append("m.merchant_id = %s"); args.append(merchant)
    if status == "unavailable":
        where.append("m.status <> 'AVAILABLE'")
    elif status in MENU_STATUSES:
        where.append("m.status = %s"); args.append(status)
    elif status == "price":
        where.append("m.kind = 'item' AND ref.price IS NOT NULL AND m.price IS DISTINCT FROM ref.price")
    if kind in ("item", "modifier"):
        where.append("m.kind = %s"); args.append(kind)
    if q and q.strip():
        like = _like(q)
        where.append("(m.name ILIKE %s OR m.category ILIKE %s OR m.item_code ILIKE %s)"); args += [like, like, like]
    rows = db.fetch(
        f"""WITH ref AS (
                -- the price most outlets of a city ask for a menu item (every outlet, not only the scope), once
                SELECT lower(x.name) AS n, xs.city_name AS city, mode() WITHIN GROUP (ORDER BY x.price) AS price,
                       count(DISTINCT x.merchant_id)::int AS outlets
                FROM {SCHEMA}.grab_menu_item x JOIN {SCHEMA}.grab_store xs ON xs.merchant_id = x.merchant_id AND xs.is_active
                WHERE x.kind = 'item' AND x.price IS NOT NULL
                GROUP BY 1, 2 HAVING count(DISTINCT x.merchant_id) >= 3)
            SELECT m.merchant_id, m.item_id, m.kind, m.name, m.category, m.item_code, m.price, m.image_url, m.status,
                   m.available_at, m.status_since, m.seen_at, s.branch_code, COALESCE(b.branch_name, s.merchant_name) AS branch_name,
                   ref.price AS ref_price, ref.outlets AS ref_outlets
            FROM {SCHEMA}.grab_menu_item m
            JOIN {SCHEMA}.grab_store s ON s.merchant_id = m.merchant_id
            LEFT JOIN {SCHEMA}.master_branches b ON b.branch_code = s.branch_code
            LEFT JOIN ref ON m.kind = 'item' AND ref.n = lower(m.name) AND ref.city IS NOT DISTINCT FROM s.city_name
            WHERE {" AND ".join(where)}
            ORDER BY branch_name, m.kind, m.category NULLS LAST, m.name LIMIT %s""",
        [*args, max(1, min(int(limit), 5000))])
    data = [{
        "merchantId": r["merchant_id"], "branchCode": r["branch_code"], "branchName": r["branch_name"], "itemId": r["item_id"],
        "kind": r["kind"], "name": r["name"], "category": r["category"], "itemCode": r["item_code"],
        "price": float(r["price"]) if r["price"] is not None else None, "imageUrl": r["image_url"], "status": r["status"],
        "availableAt": _iso(r["available_at"]), "since": _iso(r["status_since"]), "seenAt": _iso(r["seen_at"]),
        "refPrice": float(r["ref_price"]) if r["ref_price"] is not None else None, "refOutlets": r["ref_outlets"],
    } for r in rows]
    return {"data": data, "counts": {k: sum(1 for x in data if x["status"] == k) for k in MENU_STATUSES}}


def set_session(cookie: str, csrf: Optional[str], user: dict) -> dict:
    """Save a fresh merchant session (super admin pasted the cookie). csrf is derived from the cookie when absent."""
    cookie = (cookie or "").strip()
    if "mexusers_authn_token=" not in cookie:
        raise ValueError("Cookie Grab tidak valid: tidak ada mexusers_authn_token")
    if not csrf:
        m = re.search(r"mexusers_authn_token_jti=([^;]+)", cookie)
        csrf = m.group(1) if m else None
    with db.transaction() as c:
        c.execute(
            f"""INSERT INTO {SCHEMA}.grab_session (id, cookie, csrf, is_valid, updated_at, updated_by, note)
                VALUES (1, %s, %s, true, NOW(), %s, NULL)
                ON CONFLICT (id) DO UPDATE SET cookie = EXCLUDED.cookie, csrf = EXCLUDED.csrf, is_valid = true,
                  updated_at = NOW(), updated_by = EXCLUDED.updated_by, note = NULL""",
            (cookie, csrf, user.get("username")))
    return {"valid": True}
