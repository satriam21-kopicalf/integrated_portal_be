"""Aggregator Insight, stage 1 (migration 139, 2026-11-24): the BotFood data of the brand Calf in our database and the
handle schedule of the Aggregator team.

BotFood (api.botfood.id, token BOTFOOD_TOKEN, read only): every hour the global report (every outlet on GrabFood /
GoFood / ShopeeFood with rating, open state and items with stock) and the new reviews (10 per page, newest first, until a
page is already known); between the hours the reviews are filled back day by day to REVIEW_BACKFILL_TO. BotFood is a
signal with its own delay (synced_at): the official status is the merchant account. Only the fields of the reports are
kept — its answer also carries proxy and GoFood credential fields, never stored.

Handle schedule: a weekly template (agg_handle: member, outlet, slot, weekdays, valid from / to) with exceptions per date
(agg_handle_override); who_handles(day) answers for every outlet who works on it that day, the history included.
"""
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from app import database as db

logger = logging.getLogger(__name__)
P = "integration_portal"
BASE = "https://api.botfood.id/api/v1/"
WIB = ZoneInfo("Asia/Jakarta")
PAUSE = 1.5                       # between two BotFood requests
EVERY_S = 3600                    # the global report and the new reviews
REVIEW_BACKFILL_TO = date(2026, 11, 1)
LOCK_DIR = os.getenv("AGG_LOCK_DIR", "/data/media")


def token() -> Optional[str]:
    return os.getenv("BOTFOOD_TOKEN") or None


def _get(path: str, params: Optional[dict] = None, timeout: int = 120) -> dict:
    r = requests.get(BASE + path, params=params or {}, timeout=timeout,
                     headers={"Authorization": f"Bearer {token()}", "Accept": "application/json"})
    r.raise_for_status()
    return r.json().get("data") or {}


def _ts(v) -> Optional[datetime]:
    """BotFood writes local time (Asia/Jakarta) without a zone."""
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=WIB)
    except ValueError:
        return None


def _int(v) -> Optional[int]:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _num(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _bool(v) -> Optional[bool]:
    return None if v is None else str(v) in ("1", "true", "True")


# ---------------------------------------------------------------- pulls

def _log(kind: str):
    return db.fetchrow(f"INSERT INTO {P}.agg_pull (kind) VALUES (%s) RETURNING id", (kind,))["id"]


def _done(rid: int, rows: Optional[int], err: Optional[str]) -> None:
    with db.transaction() as conn:
        conn.execute(f"UPDATE {P}.agg_pull SET finished_at = now(), rows = %s, error = %s WHERE id = %s", (rows, err, rid))


def pull_global() -> int:
    """Every channel and item, as BotFood last read them; a changed stock is kept as an event."""
    d = _get("me/report/global", timeout=240)
    chans, items = d.get("branch_channels") or [], d.get("items") or []
    with db.transaction(timeout_ms=600_000) as conn:
        for c in chans:
            row = (_int(c.get("id")), _int(c.get("branch_id")), c.get("name") or "", c.get("channel") or "", _num(c.get("rating")),
                   _bool(c.get("is_open")), _int(c.get("items_total")), _int(c.get("items_active")), _ts(c.get("last_syncronized_at")))
            if row[0] is None or row[1] is None:
                continue
            conn.execute(f"""INSERT INTO {P}.agg_channel (id, bf_branch_id, name, channel, rating, is_open, items_total, items_active, synced_at)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                             ON CONFLICT (id) DO UPDATE SET bf_branch_id = EXCLUDED.bf_branch_id, name = EXCLUDED.name,
                               channel = EXCLUDED.channel, rating = EXCLUDED.rating, is_open = EXCLUDED.is_open,
                               items_total = EXCLUDED.items_total, items_active = EXCLUDED.items_active,
                               synced_at = EXCLUDED.synced_at, fetched_at = now()""", row)
            if row[8]:
                conn.execute(f"""INSERT INTO {P}.agg_channel_log (channel_id, synced_at, is_open, rating, items_total, items_active)
                                 VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                             (row[0], row[8], row[5], row[4], row[6], row[7]))
            conn.execute(f"""INSERT INTO {P}.agg_branch_map (bf_branch_id, bf_name) VALUES (%s, %s)
                             ON CONFLICT (bf_branch_id) DO NOTHING""", (row[1], row[2]))
        for i in items:
            iid, stock = _int(i.get("id")), _bool(i.get("in_stock"))
            if iid is None:
                continue
            before = conn.execute(f"SELECT in_stock FROM {P}.agg_item WHERE id = %s", (iid,)).fetchone()
            conn.execute(f"""INSERT INTO {P}.agg_item (id, channel_id, name, category, in_stock, price, last_not_active_at)
                             VALUES (%s, %s, %s, %s, %s, %s, %s)
                             ON CONFLICT (id) DO UPDATE SET channel_id = EXCLUDED.channel_id, name = EXCLUDED.name,
                               category = EXCLUDED.category, in_stock = EXCLUDED.in_stock, price = EXCLUDED.price,
                               last_not_active_at = EXCLUDED.last_not_active_at, fetched_at = now()""",
                         (iid, _int(i.get("branch_channel_id")), i.get("name") or "", i.get("category_title"), stock,
                          _int(i.get("price")), _ts(i.get("last_not_active_at"))))
            if stock is not None and (before is None or before["in_stock"] != stock):
                conn.execute(f"INSERT INTO {P}.agg_item_event (item_id, in_stock) VALUES (%s, %s) ON CONFLICT DO NOTHING", (iid, stock))
    automatch()
    return len(chans) + len(items)


def _save_reviews(rows: list[dict]) -> int:
    """Upsert; the number of reviews not known before."""
    new = 0
    with db.transaction() as conn:
        for r in rows:
            rid = _int(r.get("id"))
            if rid is None:
                continue
            got = conn.execute(
                f"""INSERT INTO {P}.agg_review (id, channel_id, bf_branch_id, outlet_name, channel, rating, comment, item_name, order_id,
                                               tags, merchant_reply, merchant_reply_at, is_reviewed, customer_name, ordered_at, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET merchant_reply = EXCLUDED.merchant_reply, merchant_reply_at = EXCLUDED.merchant_reply_at,
                      is_reviewed = EXCLUDED.is_reviewed, tags = EXCLUDED.tags, fetched_at = now()
                    RETURNING (xmax = 0) AS inserted""",
                (rid, _int(r.get("branch_channel_id")), _int(r.get("branch_id")), r.get("branch_channel_name"), r.get("branch_channel_channel"),
                 _int(r.get("rating")), r.get("comment"), r.get("item_name"), r.get("order_id") and str(r.get("order_id")),
                 json.dumps(r.get("tags"), ensure_ascii=False) if r.get("tags") is not None else None, r.get("merchant_reply"),
                 _ts(r.get("merchant_reply_at")), _bool(r.get("is_reviewed")), r.get("customer_name"), _ts(r.get("ordered_at")),
                 _ts(r.get("created_at")))).fetchone()
            new += bool(got and got["inserted"])
    return new


def _reviews_page(page: int, d_from: Optional[date] = None, d_to: Optional[date] = None) -> tuple[list[dict], int]:
    p = {"page": page, "sort_key": "reviews.created_at", "sort_value": "desc"}
    if d_from:
        p["from_created_at"] = d_from.isoformat()
    if d_to:
        p["until_created_at"] = d_to.isoformat()
    d = _get("me/reviews", p)
    return d.get("reviews") or [], int(d.get("total_page") or 0)


def pull_new_reviews(max_pages: int = 60) -> int:
    """The newest reviews, page by page, until a page brings nothing new."""
    total = 0
    for page in range(1, max_pages + 1):
        rows, pages = _reviews_page(page)
        n = _save_reviews(rows)
        total += n
        if not rows or n == 0 or page >= pages:
            break
        time.sleep(PAUSE)
    return total


def backfill_one_day() -> Optional[int]:
    """One more day of reviews back in time (agg_state review_backfill_day), until REVIEW_BACKFILL_TO; None when done."""
    st = db.fetchrow(f"SELECT value FROM {P}.agg_state WHERE key = 'review_backfill_day'")
    day = date.fromisoformat(st["value"]) if st and st["value"] else None
    if not day or day < REVIEW_BACKFILL_TO:
        return None
    total, page = 0, 1
    while True:
        rows, pages = _reviews_page(page, day, day)
        total += _save_reviews(rows)
        if page >= pages or not rows:
            break
        page += 1
        time.sleep(PAUSE)
    with db.transaction() as conn:
        conn.execute(f"UPDATE {P}.agg_state SET value = %s WHERE key = 'review_backfill_day'", ((day - timedelta(days=1)).isoformat(),))
    return total


def run(kind: str) -> dict:
    """One pull, logged in agg_pull; never raises."""
    rid, rows, err = _log(kind), None, None
    try:
        rows = {"global": pull_global, "reviews": pull_new_reviews, "backfill": backfill_one_day}[kind]()
    except Exception as e:  # noqa: BLE001
        err = str(e)[:500]
        logger.info("aggregator %s: %s", kind, err)
    _done(rid, rows, err)
    return {"kind": kind, "rows": rows, "error": err}


def _loop() -> None:
    time.sleep(150)
    last = 0.0
    while True:
        try:
            if token() and time.time() - last >= EVERY_S:
                last = time.time()
                run("global")
                run("reviews")
            elif token():
                if run("backfill")["rows"] is None and db.fetchrow(
                        f"SELECT 1 FROM {P}.agg_state WHERE key = 'review_backfill_day' AND value < %s", (REVIEW_BACKFILL_TO.isoformat(),)):
                    time.sleep(600)  # the backfill is complete
        except Exception as e:  # noqa: BLE001 - the loop never dies
            logger.warning("aggregator loop: %s", e)
        time.sleep(30)


_lock = None


def start() -> None:
    """One puller for the four uvicorn workers (lock file), only when a token is set."""
    global _lock
    if not token():
        return
    import fcntl
    try:
        _lock = open(os.path.join(LOCK_DIR, ".aggregator.lock"), "w")
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    threading.Thread(target=_loop, name="aggregator-botfood", daemon=True).start()


# ---------------------------------------------------------------- outlets

def _key(name: Optional[str]) -> str:
    """'Kopi Calf, Koja' and 'Kopi Calf To Go Koja' → 'koja'; 'Calf To Go, BTR' → 'btr'."""
    t = " " + "".join(ch if ch.isalnum() else " " for ch in (name or "").lower()) + " "
    for w in (" kopi ", " calf ", " to go ", " ctg ", " tgp "):
        while w in t:
            t = t.replace(w, " ")
    return " ".join(t.split())


def outlets() -> list[dict]:
    return db.fetch("""SELECT branch_code, branch_name FROM integration_esb.master_branches
                       WHERE coalesce(is_active, true) AND NOT coalesce(is_deleted, false)
                         AND branch_name ILIKE '%%calf%%'                      -- the Kopi Calf outlets: not Wasgee Tea
                         AND branch_name !~* '^(bulk order|head office)'       -- nor the bulk-order and office branches
                       ORDER BY branch_name""")


def automatch() -> int:
    """The BotFood outlets not mapped yet get our branch whose name has the same key, when exactly one has it."""
    idx: dict[str, list[str]] = {}
    for o in outlets():
        idx.setdefault(_key(o["branch_name"]), []).append(o["branch_code"])
    n = 0
    with db.transaction() as conn:
        taken = {r["branch_code"] for r in conn.execute(f"SELECT branch_code FROM {P}.agg_branch_map WHERE branch_code IS NOT NULL").fetchall()}
        for m in conn.execute(f"SELECT bf_branch_id, bf_name FROM {P}.agg_branch_map WHERE branch_code IS NULL AND matched = 'auto'").fetchall():
            hit = idx.get(_key(m["bf_name"])) or []
            if not hit:  # 'Kopi Calf, Kopo' ↔ 'Kopi Calf To Go Kopo Bandung', 'Mangun Jaya' ↔ 'Mangunjaya': one outlet only
                k = _key(m["bf_name"])
                ws = set(k.split())
                hit = [c for kk, cs in idx.items() if kk and (kk.replace(" ", "") == k.replace(" ", "") or set(kk.split()) <= ws or ws <= set(kk.split()))
                       for c in cs if c not in taken]
            if len(hit) == 1:
                taken.add(hit[0])
                conn.execute(f"UPDATE {P}.agg_branch_map SET branch_code = %s, updated_at = now() WHERE bf_branch_id = %s", (hit[0], m["bf_branch_id"]))
                n += 1
    return n


def branch_map() -> list[dict]:
    return db.fetch(f"""SELECT m.bf_branch_id AS "bfBranchId", m.bf_name AS "bfName", m.branch_code AS "branchCode", m.matched,
                               b.branch_name AS "branchName"
                        FROM {P}.agg_branch_map m LEFT JOIN integration_esb.master_branches b ON b.branch_code = m.branch_code
                        ORDER BY m.branch_code IS NOT NULL, m.bf_name""")


def set_branch(user: dict, bf_branch_id: int, branch_code: Optional[str]) -> None:
    with db.transaction() as conn:
        conn.execute(f"""UPDATE {P}.agg_branch_map SET branch_code = %s, matched = 'manual', updated_at = now(), updated_by = %s
                         WHERE bf_branch_id = %s""", (branch_code, user["id"], bf_branch_id))


def status() -> dict:
    """How fresh the data is: the last pull of each kind, BotFood's own delay, the counts, the backfill position."""
    last = {r["kind"]: r for r in db.fetch(f"""SELECT DISTINCT ON (kind) kind, started_at, finished_at, rows, error FROM {P}.agg_pull
                                              ORDER BY kind, started_at DESC""")}
    ch = db.fetchrow(f"""SELECT count(*) AS n, min(synced_at) AS oldest, max(synced_at) AS newest,
                                percentile_disc(0.5) WITHIN GROUP (ORDER BY synced_at) AS median,
                                count(*) FILTER (WHERE is_open) AS open FROM {P}.agg_channel""")
    iso = lambda v: v.isoformat() if v else None  # noqa: E731
    return {
        "configured": bool(token()),
        "pulls": {k: {"startedAt": iso(v["started_at"]), "finishedAt": iso(v["finished_at"]), "rows": v["rows"], "error": v["error"]}
                  for k, v in last.items()},
        "channels": ch["n"], "open": ch["open"], "syncedOldest": iso(ch["oldest"]), "syncedMedian": iso(ch["median"]), "syncedNewest": iso(ch["newest"]),
        "items": db.fetchrow(f"SELECT count(*) AS n FROM {P}.agg_item")["n"],
        "outOfStock": db.fetchrow(f"SELECT count(*) AS n FROM {P}.agg_item WHERE in_stock = false")["n"],
        "reviews": db.fetchrow(f"SELECT count(*) AS n, min(created_at) AS a FROM {P}.agg_review")["n"],
        "reviewsFrom": iso(db.fetchrow(f"SELECT min(created_at) AS a FROM {P}.agg_review")["a"]),
        "unmapped": db.fetchrow(f"SELECT count(*) AS n FROM {P}.agg_branch_map WHERE branch_code IS NULL")["n"],
        "backfillDay": (db.fetchrow(f"SELECT value FROM {P}.agg_state WHERE key = 'review_backfill_day'") or {}).get("value"),
    }


# ---------------------------------------------------------------- handle schedule

def slots() -> list[dict]:
    return db.fetch(f"SELECT code, label, sort, active FROM {P}.agg_slot ORDER BY sort, code")


def members() -> list[dict]:
    """The accounts that open Aggregator Insight (superadmins aside), with their HRMS link."""
    from app import access
    rows = db.fetch(f"""SELECT id, full_name, username, role, business_line, job_title, hrms_employee_id, is_active FROM {P}.user_account
                        WHERE is_active ORDER BY full_name""")
    out = []
    for u in rows:
        if u["role"] == "superadmin" or not any(access.has_page(u, p) for p in ("agg.schedule", "agg.overview")):
            continue
        out.append({"id": str(u["id"]), "name": u["full_name"] or u["username"], "jobTitle": u["job_title"],
                    "hrmsLinked": bool(u["hrms_employee_id"])})
    return out


def who_handles(day: date) -> list[dict]:
    """Every outlet with the member(s) handling it on `day`, per slot: the template valid that day on that weekday, an
    exception of that date replacing it."""
    wd = day.isoweekday()
    tpl = db.fetch(f"""SELECT h.branch_code, h.slot, h.user_id, h.weekdays, h.id FROM {P}.agg_handle h
                       WHERE h.valid_from <= %s AND (h.valid_to IS NULL OR h.valid_to >= %s)
                         AND (h.weekdays IS NULL OR %s = ANY (h.weekdays))""", (day, day, wd))
    ovr = {(o["branch_code"], o["slot"]): o for o in db.fetch(
        f"SELECT branch_code, slot, user_id, note FROM {P}.agg_handle_override WHERE day = %s", (day,))}
    names = {str(u["id"]): u["full_name"] or u["username"] for u in db.fetch(f"SELECT id, full_name, username FROM {P}.user_account")}
    by: dict[tuple, dict] = {}
    for t in tpl:
        by[(t["branch_code"], t["slot"])] = {"userId": str(t["user_id"]), "source": "template", "handleId": t["id"], "weekdays": t["weekdays"]}
    for k, o in ovr.items():
        by[k] = {"userId": str(o["user_id"]) if o["user_id"] else None, "source": "exception", "note": o["note"]}
    out = []
    for o in outlets():
        row = {"branchCode": o["branch_code"], "branchName": o["branch_name"], "slots": {}}
        for s in slots():
            a = by.get((o["branch_code"], s["code"]))
            if a:
                row["slots"][s["code"]] = {**a, "name": names.get(a["userId"]) if a.get("userId") else None}
        out.append(row)
    return out


def assign(user: dict, member_id: str, branch_codes: list[str], slot: str, weekdays: Optional[list[int]], valid_from: date) -> int:
    """The member handles these outlets in `slot` from `valid_from` (on `weekdays`, all when empty): the rows they replace
    end the day before, so the past keeps its history."""
    wds = sorted({int(w) for w in weekdays if 1 <= int(w) <= 7}) if weekdays else None
    with db.transaction() as conn:
        for b in branch_codes:
            conn.execute(f"""UPDATE {P}.agg_handle SET valid_to = %s - 1 WHERE branch_code = %s AND slot = %s
                             AND (valid_to IS NULL OR valid_to >= %s) AND valid_from < %s
                             AND (weekdays IS NULL OR %s::smallint[] IS NULL OR weekdays && %s::smallint[])""",
                         (valid_from, b, slot, valid_from, valid_from, wds, wds))
            conn.execute(f"""DELETE FROM {P}.agg_handle WHERE branch_code = %s AND slot = %s AND valid_from >= %s
                             AND (weekdays IS NULL OR %s::smallint[] IS NULL OR weekdays && %s::smallint[])""", (b, slot, valid_from, wds, wds))
            conn.execute(f"""INSERT INTO {P}.agg_handle (branch_code, slot, weekdays, user_id, valid_from, created_by)
                             VALUES (%s, %s, %s, %s, %s, %s)""", (b, slot, wds, member_id, valid_from, user["id"]))
    return len(branch_codes)


def unassign(user: dict, branch_codes: list[str], slot: str, valid_from: date) -> int:
    with db.transaction() as conn:
        for b in branch_codes:
            conn.execute(f"UPDATE {P}.agg_handle SET valid_to = %s - 1 WHERE branch_code = %s AND slot = %s AND (valid_to IS NULL OR valid_to >= %s) AND valid_from < %s",
                         (valid_from, b, slot, valid_from, valid_from))
            conn.execute(f"DELETE FROM {P}.agg_handle WHERE branch_code = %s AND slot = %s AND valid_from >= %s", (b, slot, valid_from))
    return len(branch_codes)


def set_exception(user: dict, day: date, branch_code: str, slot: str, member_id: Optional[str], note: Optional[str], clear: bool = False) -> None:
    with db.transaction() as conn:
        if clear:
            conn.execute(f"DELETE FROM {P}.agg_handle_override WHERE day = %s AND branch_code = %s AND slot = %s", (day, branch_code, slot))
            return
        conn.execute(f"""INSERT INTO {P}.agg_handle_override (day, branch_code, slot, user_id, note, created_by) VALUES (%s, %s, %s, %s, %s, %s)
                         ON CONFLICT (day, branch_code, slot) DO UPDATE SET user_id = EXCLUDED.user_id, note = EXCLUDED.note,
                           created_at = now(), created_by = EXCLUDED.created_by""", (day, branch_code, slot, member_id, note, user["id"]))


def add_slot(code: str, label: str, sort: int) -> None:
    with db.transaction() as conn:
        conn.execute(f"""INSERT INTO {P}.agg_slot (code, label, sort) VALUES (%s, %s, %s)
                         ON CONFLICT (code) DO UPDATE SET label = EXCLUDED.label, sort = EXCLUDED.sort, active = true""", (code, label, sort))
