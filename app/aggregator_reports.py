"""Aggregator Insight stage 2 (migration 140, 2026-11-24): Opening & Closing and Out of Stock, from the change events
BotFood keeps of the day (me/histories), pulled every 15 minutes into agg_status_event / agg_stock_event.

Opening & Closing, per outlet, platform and day (times in WIB, "according to BotFood": the time is when BotFood saw the
change, so a grace period applies, agg_state grace_minutes):
    status   on_time / late / not_opened / waiting (opening hour not reached yet) / no_hours (BotFood has no opening
             hour for it) / no_data (before the first event kept)
    late     minutes after the opening hour, beyond the grace
    early    closed for good before the closing hour (beyond the grace): minutes early
    pause    the minutes closed between the opening and the closing hour, apart from that early close
BotFood sees an opening late (often an hour or more), so its "late" is an upper bound. The first POS order of the platform
at the outlet proves it was open by then (2026-11-24): the earlier of the two counts (evidence "order"); without such an
order the late row stays as BotFood says, not proven.
Every row carries the member of the Aggregator team handling the outlet that day (handle schedule).

Out of Stock: what is out now (agg_item) per outlet, platform and category, and in a period how often each item went
out and for how long (agg_stock_event). Packaging categories are left out unless asked.
"""
import time
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Optional

from app import aggregator as agg
from app import database as db

P = agg.P
WIB = agg.WIB


def _time(v) -> Optional[dtime]:
    try:
        return datetime.strptime(str(v)[:8], "%H:%M:%S").time() if v else None
    except ValueError:
        return None


def pull_histories() -> int:
    """Today's change events of BotFood (outlets and items), kept by their id."""
    d = agg._get("me/histories", timeout=180)
    n = 0
    with db.transaction(timeout_ms=300_000) as conn:
        for e in d.get("branch_channels") or []:
            at, cid, eid = agg._ts(e.get("created_at")), agg._int(e.get("branch_channel_id")), agg._int(e.get("id"))
            if not at or cid is None or eid is None:
                continue
            ot, ct = _time(e.get("branch_open_time")), _time(e.get("branch_close_time"))
            n += conn.execute(f"""INSERT INTO {P}.agg_status_event (id, channel_id, at, is_open, open_time, close_time, is_warning)
                                  VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                              (eid, cid, at, bool(agg._bool(e.get("is_open"))), ot, ct, agg._bool(e.get("is_warning")))).rowcount
            if ot or ct:
                conn.execute(f"UPDATE {P}.agg_channel SET open_time = %s, close_time = %s WHERE id = %s", (ot, ct, cid))
        for e in d.get("items") or []:
            at, cid, eid = agg._ts(e.get("created_at")), agg._int(e.get("branch_channel_id")), agg._int(e.get("id"))
            if not at or cid is None or eid is None:
                continue
            n += conn.execute(f"""INSERT INTO {P}.agg_stock_event (id, item_id, channel_id, item_name, in_stock, remarks, at)
                                  VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                              (eid, agg._int(e.get("item_id")), cid, e.get("item_name") or "", bool(agg._bool(e.get("in_stock"))),
                               e.get("remarks"), at)).rowcount
    return n


def _hm(s: str) -> Optional[dtime]:
    try:
        return datetime.strptime(s.strip()[:5], "%H:%M").time()
    except ValueError:
        return None


def stretches(text: Optional[str]) -> list[tuple[dtime, dtime]]:
    """'07:00 - 08:11; 12:00 - 12:30;' to [(07:00, 08:11), (12:00, 12:30)]."""
    out = []
    for part in (text or "").split(";"):
        if "-" in part:
            a, b = part.split("-", 1)
            ta, tb = _hm(a), _hm(b)
            if ta and tb:
                out.append((ta, tb))
    return out


def pull_daily(day: date) -> int:
    """BotFood's daily availability report of one day (every outlet and platform), 100 per page."""
    n, page = 0, 1
    while True:
        d = agg._get("me/report/atp", {"date": day.isoformat(), "data": 100, "page": page}, timeout=120)
        rows = d.get("branch_channel_availability_reports") or []
        with db.transaction() as conn:
            for r in rows:
                cid = agg._int(r.get("branch_channel_id"))
                if cid is None:
                    continue
                conn.execute(f"""INSERT INTO {P}.agg_daily (channel_id, day, bf_branch_id, name, channel, open_time, close_time, open_state,
                                   close_state, has_break, active_s, inactive_s, hours_s, timeline, inactive_timeline, items_pct)
                                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                 ON CONFLICT (channel_id, day) DO UPDATE SET open_state = EXCLUDED.open_state, close_state = EXCLUDED.close_state,
                                   has_break = EXCLUDED.has_break, active_s = EXCLUDED.active_s, inactive_s = EXCLUDED.inactive_s,
                                   hours_s = EXCLUDED.hours_s, timeline = EXCLUDED.timeline, inactive_timeline = EXCLUDED.inactive_timeline,
                                   items_pct = EXCLUDED.items_pct, open_time = EXCLUDED.open_time, close_time = EXCLUDED.close_time, fetched_at = now()""",
                             (cid, day, agg._int(r.get("branch_id")), r.get("branch_channel_name"), r.get("branch_channel_channel"),
                              _time(r.get("open_time")), _time(r.get("close_time")), r.get("open_state"), r.get("close_state"),
                              agg._bool(r.get("has_break")), agg._int(r.get("active_time")), agg._int(r.get("inactive_time")),
                              agg._int(r.get("operational_hours_duration")), r.get("timeline"), r.get("inactive_timeline"),
                              agg._num(r.get("items_availability_percentage"))))
                n += 1
        if page >= int(d.get("total_page") or 0) or not rows:
            break
        page += 1
        time.sleep(agg.PAUSE)
    return n


def daily_due() -> bool:
    """Yesterday's report: once BotFood has made it (after 05:00 WIB) and while it is not kept yet."""
    now = datetime.now(WIB)
    return now.hour >= 5 and not db.fetchrow(f"SELECT 1 FROM {P}.agg_daily WHERE day = %s LIMIT 1", ((now - timedelta(days=1)).date(),))


def pull_daily_due() -> Optional[int]:
    return pull_daily((datetime.now(WIB) - timedelta(days=1)).date()) if daily_due() else None


DAILY_FROM = date(2026, 1, 1)


def backfill_daily() -> Optional[int]:
    """One more day of the daily report back in time (agg_state daily_backfill_day), to DAILY_FROM; None when done."""
    st = db.fetchrow(f"SELECT value FROM {P}.agg_state WHERE key = 'daily_backfill_day'")
    day = date.fromisoformat(st["value"]) if st and st["value"] else None
    if not day or day < DAILY_FROM:
        return None
    n = pull_daily(day)
    with db.transaction() as conn:
        conn.execute(f"UPDATE {P}.agg_state SET value = %s WHERE key = 'daily_backfill_day'", ((day - timedelta(days=1)).isoformat(),))
    return n


def from_daily(r: dict, g: int) -> dict:
    """A day of BotFood's report as a row of Opening & Closing (pure, tested): its open / close state, the minutes late
    (the closed stretch from the opening hour), early (closed until the closing hour) and the pauses in between."""
    out = {"status": None, "openedAt": None, "lateMin": None, "earlyMin": None, "pauseMin": 0}
    ot, ct = r.get("open_time"), r.get("close_time")
    if not ot:
        out["status"] = "no_hours"
        return out
    st = (r.get("open_state") or "").lower()
    if st == "not_open":
        out["status"] = "not_opened"
        return out
    mins = lambda a, b: (b.hour * 60 + b.minute) - (a.hour * 60 + a.minute)  # noqa: E731
    on = stretches(r.get("timeline"))
    off = stretches(r.get("inactive_timeline"))
    if on:
        out["openedAt"] = on[0][0].strftime("%H:%M")
    late = 0
    for a, b in off:
        if a <= ot < b or (a == ot):
            late = mins(ot, b)
    out["lateMin"] = late if late > g else 0
    out["status"] = "late" if st == "late" or late > g else "on_time"
    early = 0
    if ct:
        for a, b in off:
            if b >= ct and a > ot:
                early = mins(a, ct)
    out["earlyMin"] = early if early > g else None
    for a, b in off:
        if a > ot and (not ct or b < ct):
            out["pauseMin"] += max(0, mins(a, b))
    return out


def grace() -> int:
    r = db.fetchrow(f"SELECT value FROM {P}.agg_state WHERE key = 'grace_minutes'")
    try:
        return int(r["value"]) if r else 15
    except (TypeError, ValueError):
        return 15


def set_grace(minutes: int) -> None:
    with db.transaction() as conn:
        conn.execute(f"""INSERT INTO {P}.agg_state (key, value) VALUES ('grace_minutes', %s)
                         ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (str(int(minutes)),))


def _channels() -> list[dict]:
    return db.fetch(f"""SELECT c.id, c.name, c.channel, c.open_time, c.close_time, c.bf_branch_id, m.branch_code, b.branch_name
                        FROM {P}.agg_channel c LEFT JOIN {P}.agg_branch_map m ON m.bf_branch_id = c.bf_branch_id
                        LEFT JOIN integration_esb.master_branches b ON b.branch_code = m.branch_code""")


def _handlers(d_from: date, d_to: date) -> dict[tuple[date, str], str]:
    """(day, branch code) to the member(s) handling it, from the handle schedule."""
    out: dict[tuple[date, str], str] = {}
    d = d_from
    while d <= d_to:
        for o in agg.who_handles(d):
            names = sorted({a["name"] for a in o["slots"].values() if a.get("name")})
            if names:
                out[(d, o["branchCode"])] = ", ".join(names)
        d += timedelta(days=1)
    return out


def _at(day: date, t: dtime) -> datetime:
    return datetime.combine(day, t, tzinfo=WIB)


def day_row(events_today: list[dict], was_open: Optional[bool], d: date, ot: Optional[dtime], ct: Optional[dtime],
            g: int, now: datetime, have_data: bool) -> dict:
    """One outlet, platform and day: status, opened at, late / early / pause minutes. Pure (tested)."""
    row = {"status": None, "openedAt": None, "lateMin": None, "earlyMin": None, "pauseMin": 0}
    day0 = _at(d, dtime(0))
    day1 = day0 + timedelta(days=1)
    if not have_data:
        row["status"] = "no_data"
        return row
    if not ot:
        row["status"] = "no_hours"
        return row
    open_at = _at(d, ot)
    close_at = _at(d, ct) if ct and ct > ot else day1
    if was_open is None and events_today:
        was_open = events_today[0]["is_open"] is False  # the first change of the day is a closing: it was open
    opens = [e for e in events_today if e["is_open"]]
    first_open = day0 if was_open else (opens[0]["at"].astimezone(WIB) if opens else None)
    if first_open is None:
        row["status"] = "not_opened" if now >= open_at + timedelta(minutes=g) else "waiting"
        return row
    row["openedAt"] = first_open.strftime("%H:%M") if first_open > day0 else None
    late = int((first_open - open_at).total_seconds() // 60)
    row["lateMin"] = late if late > g else 0
    row["status"] = "late" if late > g else "on_time"
    state, since, stretches = True, first_open, []
    for e in events_today:
        t = e["at"].astimezone(WIB)
        if t <= first_open:
            continue
        if state and not e["is_open"]:
            state, since = False, t
        elif not state and e["is_open"]:
            stretches.append((since, t))
            state = True
    if not state:
        stretches.append((since, None))
    upto = min(close_at, now)
    for a, b in stretches:
        if a >= upto:
            continue
        if b is None and now >= close_at:
            if close_at - a > timedelta(minutes=g):
                row["earlyMin"] = int((close_at - a).total_seconds() // 60)
            continue
        stop = min(b or upto, upto)
        row["pauseMin"] += int((stop - a).total_seconds() // 60)
    return row


_orders: dict[date, dict[tuple[str, str], int]] = {}  # past days, kept in memory
ORDER_FROM = 4  # before 04:00 an order belongs to the night before


def first_orders(days: set[date]) -> dict[tuple[date, str, str], int]:
    """(day, branch, platform) to the minute of the day of its first POS order (sales_date_in is WIB wall time)."""
    today_ = datetime.now(WIB).date()
    need = sorted(d for d in days if d not in _orders)
    if need:
        got: dict[date, dict[tuple[str, str], int]] = {d: {} for d in need}
        for r in db.fetch("""SELECT branch_code AS b, sales_date::date AS d, visit_purpose AS v, min(sales_date_in) AS f
                             FROM integration_esb.transactions_pos_sales
                             WHERE sales_date >= %s AND sales_date < %s::date + 1
                               AND visit_purpose IN ('GrabFood', 'GoFood', 'ShopeeFood')
                               AND extract(hour FROM sales_date_in AT TIME ZONE 'UTC') >= %s
                             GROUP BY 1, 2, 3""", (need[0], need[-1], ORDER_FROM)):
            if r["d"] in got and r["f"]:
                f = r["f"].astimezone(timezone.utc) if r["f"].tzinfo else r["f"]
                got[r["d"]][(r["b"], r["v"])] = f.hour * 60 + f.minute
        out = {**_orders, **got}
        for d in need:
            if d < today_ - timedelta(days=1):  # the last days still get late bills
                _orders[d] = got[d]
    else:
        out = _orders
    return {(d, b, v): m for d in days for (b, v), m in out.get(d, {}).items()}


def proven(row: dict, ot: Optional[dtime], order: Optional[int], g: int) -> dict:
    """A late / not opened row with the first order of the platform: the earlier one counts. Pure (tested)."""
    if ot is None or order is None or row["status"] not in ("late", "not_opened"):
        return row
    at = int(row["openedAt"][:2]) * 60 + int(row["openedAt"][3:]) if row.get("openedAt") else None
    if at is not None and order >= at:
        return row
    late = order - (ot.hour * 60 + ot.minute)
    if row["lateMin"] and late >= row["lateMin"]:
        return row
    return {**row, "openedAt": f"{order // 60:02d}:{order % 60:02d}", "evidence": "order",
            "lateMin": late if late > g else 0, "status": "late" if late > g else "on_time"}


def opening(d_from: date, d_to: date, channel: Optional[str] = None) -> dict:
    g = grace()
    now = datetime.now(WIB)
    chans = [c for c in _channels() if not channel or c["channel"] == channel]
    start, end = _at(d_from, dtime(0)), _at(d_to, dtime(0)) + timedelta(days=1)
    ev: dict[int, list[dict]] = defaultdict(list)
    for e in db.fetch(f"""SELECT channel_id, at, is_open, open_time, close_time FROM {P}.agg_status_event
                          WHERE at >= %s - interval '3 days' AND at < %s ORDER BY at""", (start, end)):
        ev[e["channel_id"]].append(e)
    handlers = _handlers(d_from, d_to)
    from app import aggregator_quality
    try:
        own = aggregator_quality.hours()  # the hours the team entered, for the outlets BotFood has none for
    except Exception:  # noqa: BLE001 - before migration 151
        own = {}
    first_event = db.fetchrow(f"SELECT min(at) AS a FROM {P}.agg_status_event")["a"]
    daily = {(r["channel_id"], r["day"]): r for r in db.fetch(
        f"SELECT * FROM {P}.agg_daily WHERE day BETWEEN %s AND %s", (d_from, d_to))}
    rows = []
    for c in chans:
        es = ev.get(c["id"], [])
        d = d_from
        while d <= d_to:
            day0 = _at(d, dtime(0))
            day1 = day0 + timedelta(days=1)
            if day0 > now:
                break
            todays = [e for e in es if day0 <= e["at"].astimezone(WIB) < day1]
            before = [e for e in es if e["at"] < day0]
            ot = next((e["open_time"] for e in reversed(todays) if e["open_time"]), None) or c["open_time"]
            ct = next((e["close_time"] for e in reversed(todays) if e["close_time"]), None) or c["close_time"]
            rep = daily.get((c["id"], d))
            mine = own.get(c["branch_code"]) if c["branch_code"] else None
            if mine:
                ot, ct = ot or mine["open_time"], ct or mine["close_time"]
            if rep:  # a past day: BotFood's own report
                ot, ct = rep["open_time"] or ot, rep["close_time"] or ct
                if mine and not rep["open_time"]:  # its stretches against the hours of the team
                    rep = {**rep, "open_time": ot, "close_time": ct, "open_state": None if rep["open_state"] != "not_open" or stretches(rep.get("timeline")) else "not_open"}
                r = {**from_daily(rep, g), "source": "report", "itemsPct": float(rep["items_pct"]) if rep["items_pct"] is not None else None}
            else:
                have = first_event is not None and day1 > first_event.astimezone(WIB)
                r = {**day_row(todays, before[-1]["is_open"] if before else None, d, ot, ct, g, now, have), "source": "live", "itemsPct": None}
            rows.append({"day": d.isoformat(), "channelId": c["id"], "outlet": c["branch_name"] or c["name"], "branchCode": c["branch_code"],
                         "platform": c["channel"], "openTime": ot.strftime("%H:%M") if ot else None, "closeTime": ct.strftime("%H:%M") if ct else None,
                         "events": len(todays), "member": handlers.get((d, c["branch_code"])) if c["branch_code"] else None, **r})
            d += timedelta(days=1)
    late_days = {date.fromisoformat(r["day"]) for r in rows if r["status"] in ("late", "not_opened") and r["branchCode"]}
    orders = first_orders(late_days) if late_days else {}
    rows = [{"evidence": "botfood", **proven(r, _time(r["openTime"] + ":00") if r["openTime"] else None,
                                             orders.get((date.fromisoformat(r["day"]), r["branchCode"], r["platform"])), g)} for r in rows]
    counted = [r for r in rows if r["status"] in ("on_time", "late", "not_opened")]
    summary = {
        "byOrder": sum(r["evidence"] == "order" and r["status"] == "on_time" for r in rows),
        "grace": g, "rows": len(rows), "counted": len(counted),
        "onTime": sum(r["status"] == "on_time" for r in rows), "late": sum(r["status"] == "late" for r in rows),
        "notOpened": sum(r["status"] == "not_opened" for r in rows), "earlyClose": sum(1 for r in rows if r["earlyMin"]),
        "pauseMin": sum(r["pauseMin"] for r in rows), "noHours": sum(r["status"] == "no_hours" for r in rows),
        "noData": sum(r["status"] == "no_data" for r in rows),
        "dataFrom": first_event.isoformat() if first_event else None,
        "reportFrom": (db.fetchrow(f"SELECT min(day) AS d FROM {P}.agg_daily")["d"] or now.date()).isoformat(),
    }
    return {"summary": summary, "rows": rows}


def _cat(c: Optional[str]) -> str:
    c = (c or "Tanpa kategori").strip().upper()
    for p in ("BEVERAGE - ", "FOOD - ", "OTHER - "):
        if c.startswith(p):
            c = c[len(p):]
    return c


def oos(d_from: date, d_to: date, channel: Optional[str] = None, packaging: bool = False, stale_days: int = 14) -> dict:
    """Out now per outlet / platform / category, and per item in the period: how often it went out, for how long."""
    chans = {c["id"]: c for c in _channels() if not channel or c["channel"] == channel}
    now = datetime.now(WIB)
    start = _at(d_from, dtime(0))
    end = min(_at(d_to, dtime(0)) + timedelta(days=1), now)

    def keep(cat: str) -> bool:
        return packaging or "PACKAGING" not in cat

    current = []
    stale = 0  # inactive for longer than stale_days: a menu switched off, not an item out of stock
    cats: dict[tuple[int, str], str] = {}
    for i in db.fetch(f"SELECT channel_id, name, category, in_stock, last_not_active_at FROM {P}.agg_item"):
        cat = _cat(i["category"])
        cats[(i["channel_id"], i["name"].lower())] = cat
        c = chans.get(i["channel_id"])
        if c and i["in_stock"] is False and keep(cat):
            if i["last_not_active_at"] and (now - i["last_not_active_at"].astimezone(WIB)).days > stale_days:
                stale += 1
                continue
            current.append({"outlet": c["branch_name"] or c["name"], "branchCode": c["branch_code"], "platform": c["channel"], "category": cat,
                            "item": i["name"], "since": i["last_not_active_at"].isoformat() if i["last_not_active_at"] else None})
    ev = db.fetch(f"""SELECT channel_id, item_name, in_stock, remarks, at FROM {P}.agg_stock_event
                      WHERE at < %s AND at >= %s - interval '7 days' ORDER BY channel_id, item_name, at""", (end, start))
    per: dict[tuple, dict] = {}
    out_since: dict[tuple, Optional[datetime]] = {}

    def entry(k: tuple, c: dict, cat: str, name: str) -> dict:
        return per.setdefault(k, {"outlet": c["branch_name"] or c["name"], "branchCode": c["branch_code"], "platform": c["channel"],
                                  "category": cat, "item": name, "times": 0, "minutes": 0, "remarks": set()})

    for e in ev:
        c = chans.get(e["channel_id"])
        if not c:
            continue
        k = (e["channel_id"], e["item_name"])
        cat = cats.get((e["channel_id"], e["item_name"].lower()), "Tanpa kategori")
        if not keep(cat):
            continue
        t = e["at"].astimezone(WIB)
        if not e["in_stock"]:
            if out_since.get(k) is None:
                out_since[k] = t
            if t >= start:
                p = entry(k, c, cat, e["item_name"])
                p["times"] += 1
                if e["remarks"]:
                    p["remarks"].add(e["remarks"])
        elif out_since.get(k):
            a = max(out_since[k], start)
            if t > start:
                entry(k, c, cat, e["item_name"])["minutes"] += int((t - a).total_seconds() // 60)
            out_since[k] = None
    for k, a in out_since.items():  # still out at the end of the period
        if a and k in per:
            per[k]["minutes"] += int((end - max(a, start)).total_seconds() // 60)
    items = sorted(({**v, "remarks": ", ".join(sorted(v["remarks"]))} for v in per.values()), key=lambda r: (-r["minutes"], r["outlet"]))
    by_outlet: dict[str, int] = defaultdict(int)
    by_cat: dict[str, int] = defaultdict(int)
    for r in current:
        by_outlet[r["outlet"]] += 1
        by_cat[r["category"]] += 1
    first = db.fetchrow(f"SELECT min(at) AS a FROM {P}.agg_stock_event")["a"]
    return {
        "summary": {"outNow": len(current), "inactiveLong": stale, "staleDays": stale_days, "outletsWithOos": len(by_outlet), "itemsInPeriod": len(items),
                    "timesInPeriod": sum(r["times"] for r in items), "minutesInPeriod": sum(r["minutes"] for r in items),
                    "dataFrom": first.isoformat() if first else None},
        "byCategory": sorted(({"category": k, "n": v} for k, v in by_cat.items()), key=lambda r: -r["n"]),
        "byOutlet": sorted(({"outlet": k, "n": v} for k, v in by_outlet.items()), key=lambda r: -r["n"]),
        "current": sorted(current, key=lambda r: (r["outlet"], r["platform"], r["category"], r["item"])),
        "items": items,
    }
