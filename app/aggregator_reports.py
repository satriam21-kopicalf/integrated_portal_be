"""Aggregator Insight stage 2 (migration 140, 2026-11-24): Opening & Closing and Out of Stock, from the change events
BotFood keeps of the day (me/histories), pulled every 15 minutes into agg_status_event / agg_stock_event.

Opening & Closing, per outlet, platform and day (times in WIB, "according to BotFood": the time is when BotFood saw the
change, so a grace period applies, agg_state grace_minutes):
    status   on_time / late / not_opened / waiting (opening hour not reached yet) / no_hours (BotFood has no opening
             hour for it) / no_data (before the first event kept)
    late     minutes after the opening hour, beyond the grace
    early    closed for good before the closing hour (beyond the grace): minutes early
    pause    the minutes closed between the opening and the closing hour, apart from that early close
Every row carries the member of the Aggregator team handling the outlet that day (handle schedule).

Out of Stock: what is out now (agg_item) per outlet, platform and category, and in a period how often each item went
out and for how long (agg_stock_event). Packaging categories are left out unless asked.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Optional

from app import aggregator as agg
from app import database as db

P = agg.P
WIB = agg.WIB


def _time(v) -> Optional[time]:
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


def _at(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=WIB)


def day_row(events_today: list[dict], was_open: Optional[bool], d: date, ot: Optional[time], ct: Optional[time],
            g: int, now: datetime, have_data: bool) -> dict:
    """One outlet, platform and day: status, opened at, late / early / pause minutes. Pure (tested)."""
    row = {"status": None, "openedAt": None, "lateMin": None, "earlyMin": None, "pauseMin": 0}
    day0 = _at(d, time(0))
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


def opening(d_from: date, d_to: date, channel: Optional[str] = None) -> dict:
    g = grace()
    now = datetime.now(WIB)
    chans = [c for c in _channels() if not channel or c["channel"] == channel]
    start, end = _at(d_from, time(0)), _at(d_to, time(0)) + timedelta(days=1)
    ev: dict[int, list[dict]] = defaultdict(list)
    for e in db.fetch(f"""SELECT channel_id, at, is_open, open_time, close_time FROM {P}.agg_status_event
                          WHERE at >= %s - interval '3 days' AND at < %s ORDER BY at""", (start, end)):
        ev[e["channel_id"]].append(e)
    handlers = _handlers(d_from, d_to)
    first_event = db.fetchrow(f"SELECT min(at) AS a FROM {P}.agg_status_event")["a"]
    rows = []
    for c in chans:
        es = ev.get(c["id"], [])
        d = d_from
        while d <= d_to:
            day0 = _at(d, time(0))
            day1 = day0 + timedelta(days=1)
            if day0 > now:
                break
            todays = [e for e in es if day0 <= e["at"].astimezone(WIB) < day1]
            before = [e for e in es if e["at"] < day0]
            ot = next((e["open_time"] for e in reversed(todays) if e["open_time"]), None) or c["open_time"]
            ct = next((e["close_time"] for e in reversed(todays) if e["close_time"]), None) or c["close_time"]
            have = first_event is not None and day1 > first_event.astimezone(WIB)
            r = day_row(todays, before[-1]["is_open"] if before else None, d, ot, ct, g, now, have)
            rows.append({"day": d.isoformat(), "channelId": c["id"], "outlet": c["branch_name"] or c["name"], "branchCode": c["branch_code"],
                         "platform": c["channel"], "openTime": ot.strftime("%H:%M") if ot else None, "closeTime": ct.strftime("%H:%M") if ct else None,
                         "events": len(todays), "member": handlers.get((d, c["branch_code"])) if c["branch_code"] else None, **r})
            d += timedelta(days=1)
    counted = [r for r in rows if r["status"] in ("on_time", "late", "not_opened")]
    summary = {
        "grace": g, "rows": len(rows), "counted": len(counted),
        "onTime": sum(r["status"] == "on_time" for r in rows), "late": sum(r["status"] == "late" for r in rows),
        "notOpened": sum(r["status"] == "not_opened" for r in rows), "earlyClose": sum(1 for r in rows if r["earlyMin"]),
        "pauseMin": sum(r["pauseMin"] for r in rows), "noHours": sum(r["status"] == "no_hours" for r in rows),
        "noData": sum(r["status"] == "no_data" for r in rows),
        "dataFrom": first_event.isoformat() if first_event else None,
    }
    return {"summary": summary, "rows": rows}


def _cat(c: Optional[str]) -> str:
    c = (c or "Tanpa kategori").strip().upper()
    for p in ("BEVERAGE - ", "FOOD - ", "OTHER - "):
        if c.startswith(p):
            c = c[len(p):]
    return c


def oos(d_from: date, d_to: date, channel: Optional[str] = None, packaging: bool = False) -> dict:
    """Out now per outlet / platform / category, and per item in the period: how often it went out, for how long."""
    chans = {c["id"]: c for c in _channels() if not channel or c["channel"] == channel}
    now = datetime.now(WIB)
    start = _at(d_from, time(0))
    end = min(_at(d_to, time(0)) + timedelta(days=1), now)

    def keep(cat: str) -> bool:
        return packaging or "PACKAGING" not in cat

    current = []
    cats: dict[tuple[int, str], str] = {}
    for i in db.fetch(f"SELECT channel_id, name, category, in_stock, last_not_active_at FROM {P}.agg_item"):
        cat = _cat(i["category"])
        cats[(i["channel_id"], i["name"].lower())] = cat
        c = chans.get(i["channel_id"])
        if c and i["in_stock"] is False and keep(cat):
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
        "summary": {"outNow": len(current), "outletsWithOos": len(by_outlet), "itemsInPeriod": len(items),
                    "timesInPeriod": sum(r["times"] for r in items), "minutesInPeriod": sum(r["minutes"] for r in items),
                    "dataFrom": first.isoformat() if first else None},
        "byCategory": sorted(({"category": k, "n": v} for k, v in by_cat.items()), key=lambda r: -r["n"]),
        "byOutlet": sorted(({"outlet": k, "n": v} for k, v in by_outlet.items()), key=lambda r: -r["n"]),
        "current": sorted(current, key=lambda r: (r["outlet"], r["platform"], r["category"], r["item"])),
        "items": items,
    }
