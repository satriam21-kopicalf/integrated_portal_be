"""Live sales: today's running totals and the latest sales that reached the database.

    GET /api/live?limit=30&branch=CCI01&channel=GoFood,GrabFood

Read straight from integration_esb.transactions_pos_sales (today only, indexed
by sales_date), so it is as fresh as the last ESB sync (hourly at :05).
Only ESB "Sales" (Finished + bill number) are counted, like the rest of the portal.

Times: salesDateIn is the outlet's local wall-clock time (WITA branches in Bali
are one hour ahead of WIB) and carries no timezone. The feed is therefore
ordered by arrival (synced_at, i.e. the sync batch) and then by order time.
"""
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app import database as db
from app.config import get_settings
from app.database import SCHEMA, TABLE_TRANSACTIONS
from app.esb_report import TYPE_CONDITIONS, active_line_sql
from app.utils import TTLCache, data_version

router = APIRouter(prefix="/api/live", tags=["live"])

CACHE_TTL = 20
MAX_ITEMS = 4
SALES = TYPE_CONDITIONS["sales"]

_cache = TTLCache()


def now_local() -> datetime:
    return datetime.now(ZoneInfo(get_settings().timezone)).replace(tzinfo=None)


def filters(branch: Optional[str], channels: list[str]) -> tuple[str, dict]:
    parts: list[str] = []
    params: dict[str, Any] = {}
    if branch:
        parts.append("h.branch_code = %(branch)s")
        params["branch"] = branch
    if channels:
        parts.append("h.visit_purpose = ANY(%(channels)s)")
        params["channels"] = channels
    return "".join(f" AND {p}" for p in parts), params


def day_rows(day, cond: str, params: dict) -> list[dict]:
    """Sales of one day per hour of the order time (outlet clock) and channel."""
    return db.fetch(
        f"""SELECT extract(hour FROM h.sales_date_in AT TIME ZONE 'UTC')::int AS hour, h.visit_purpose AS channel,
                   count(*)::int AS bills, COALESCE(sum(h.subtotal), 0) AS subtotal,
                   COALESCE(sum(h.nett_sales), 0) AS nett,
                   max(h.synced_at) AS synced
            FROM {TABLE_TRANSACTIONS} h
            WHERE h.sales_date >= %(day)s::date AND h.sales_date < %(day)s::date + 1 AND {SALES}{cond}
            GROUP BY 1, 2""",
        {**params, "day": day},
    )


def summarise_day(rows: list[dict]) -> dict:
    """Totals, per hour and per channel from day_rows()."""
    hours: dict[int, dict] = {}
    channels: dict[str, dict] = {}
    for r in rows:
        for bucket in (hours.setdefault(r["hour"], {"hour": r["hour"], "bills": 0, "subtotal": 0.0}),
                       channels.setdefault(r["channel"] or "Unknown", {"channel": r["channel"] or "Unknown", "bills": 0, "subtotal": 0.0})):
            bucket["bills"] += r["bills"]
            bucket["subtotal"] += float(r["subtotal"])
    bills = sum(r["bills"] for r in rows)
    subtotal = sum(float(r["subtotal"]) for r in rows)
    return {
        "bills": bills,
        "subtotal": subtotal,
        "nettSales": sum(float(r["nett"]) for r in rows),
        "avgTicket": round(subtotal / bills, 2) if bills else 0,
        "hours": sorted(hours.values(), key=lambda h: h["hour"]),
        "channels": sorted(channels.values(), key=lambda c: -c["subtotal"]),
    }


def last_batch(day, cond: str, params: dict) -> dict:
    """Sales that arrived with the most recent sync run (synced_at within the same hour)."""
    row = db.fetchrow(
        f"""WITH recent AS (
                SELECT h.subtotal, date_trunc('hour', h.synced_at) AS batch FROM {TABLE_TRANSACTIONS} h
                WHERE h.sales_date >= %(day)s::date - 1 AND h.sales_date < %(day)s::date + 1 AND {SALES}{cond})
            SELECT max(batch) AS batch, count(*) FILTER (WHERE batch = (SELECT max(batch) FROM recent))::int AS bills,
                   COALESCE(sum(subtotal) FILTER (WHERE batch = (SELECT max(batch) FROM recent)), 0) AS subtotal
            FROM recent""",
        {**params, "day": day},
    ) or {}
    return {"bills": int(row.get("bills") or 0), "subtotal": float(row.get("subtotal") or 0),
            "syncedHour": row["batch"].isoformat() if row.get("batch") else None}


def until_now(day, clock: str, cond: str, params: dict) -> dict:
    """Bills and sales of `day` with an order time up to `clock` (HH:MM:SS)."""
    row = db.fetchrow(
        f"""SELECT count(*)::int AS bills, COALESCE(sum(h.subtotal), 0) AS subtotal,
                   COALESCE(sum(h.nett_sales), 0) AS nett
            FROM {TABLE_TRANSACTIONS} h
            WHERE h.sales_date >= %(day)s::date AND h.sales_date < %(day)s::date + 1 AND {SALES}{cond}
              AND (h.sales_date_in AT TIME ZONE 'UTC')::time <= %(clock)s::time""",
        {**params, "day": day, "clock": clock},
    ) or {}
    return {"bills": int(row.get("bills") or 0), "subtotal": float(row.get("subtotal") or 0),
            "nettSales": float(row.get("nett") or 0)}


def latest(day, limit: int, cond: str, params: dict) -> list[dict]:
    """Newest sales by arrival (hourly sync batch), then order time; the JSON payload is only read for these rows."""
    heads = db.fetch(
        f"""SELECT h.sales_num, h.bill_num, h.branch_code, h.visit_purpose, h.payment_method, h.subtotal,
                   h.total_amount, h.sales_date_in, h.synced_at
            FROM {TABLE_TRANSACTIONS} h
            WHERE h.sales_date >= %(day)s::date - 1 AND h.sales_date < %(day)s::date + 1 AND {SALES}{cond}
            ORDER BY date_trunc('hour', h.synced_at) DESC, h.sales_date_in DESC, h.sales_num DESC
            LIMIT %(limit)s""",
        {**params, "day": day, "limit": limit},
    )
    if not heads:
        return []
    menus = db.fetch(
        f"""SELECT h.sales_num,
                   (SELECT COALESCE(jsonb_agg(jsonb_build_object('name', m->>'menuName', 'qty', m->>'qty')), '[]'::jsonb)
                    FROM jsonb_array_elements(COALESCE(h.raw_data->'salesMenus', '[]'::jsonb)) m
                    WHERE {active_line_sql('m')}) AS items
            FROM {TABLE_TRANSACTIONS} h WHERE h.sales_num = ANY(%(nums)s)""",
        {"nums": [h["sales_num"] for h in heads]},
    )
    items = {m["sales_num"]: m["items"] or [] for m in menus}
    names = {r["branch_code"]: r["branch_name"] for r in db.fetch(
        f"SELECT DISTINCT ON (branch_code) branch_code, branch_name FROM {SCHEMA}.master_branches "
        "WHERE branch_code = ANY(%(codes)s) ORDER BY branch_code, COALESCE(is_deleted, false)",
        {"codes": list({h["branch_code"] for h in heads})},
    )}
    out = []
    for h in heads:
        lines = items.get(h["sales_num"], [])
        qty = sum(float(i.get("qty") or 0) for i in lines)
        out.append({
            "salesNum": h["sales_num"],
            "billNum": h["bill_num"],
            "branchCode": h["branch_code"],
            "branchName": names.get(h["branch_code"], h["branch_code"]),
            "channel": h["visit_purpose"],
            "paymentMethod": h["payment_method"],
            "subtotal": float(h["subtotal"] or 0),
            "total": float(h["total_amount"] or 0),
            # outlet wall clock, stored as if it were UTC
            "orderTime": h["sales_date_in"].strftime("%Y-%m-%dT%H:%M:%S") if h["sales_date_in"] else None,
            "syncedAt": h["synced_at"].isoformat() if h["synced_at"] else None,
            "itemQty": qty,
            "items": [{"name": i.get("name"), "qty": float(i.get("qty") or 0)} for i in lines[:MAX_ITEMS]],
            "moreItems": max(0, len(lines) - MAX_ITEMS),
        })
    return out


def pct(cur: float, prev: float):
    return round((cur - prev) / prev * 100, 2) if prev else None


def build(limit: int, branch: Optional[str], channels: list[str]) -> dict:
    now = now_local()
    today = now.date()
    yesterday = today - timedelta(days=1)
    cond, params = filters(branch, channels)
    rows = day_rows(today.isoformat(), cond, params)
    t = summarise_day(rows)
    y = summarise_day(day_rows(yesterday.isoformat(), cond, params))
    synced = max((r["synced"] for r in rows if r["synced"]), default=None)
    before = until_now(yesterday.isoformat(), now.strftime("%H:%M:%S"), cond, params)
    return {
        "serverTime": now.isoformat(timespec="seconds"),
        "lastSyncedAt": synced.isoformat() if synced else None,
        "filters": {"branch": branch, "channels": channels},
        "today": {
            "date": today.isoformat(),
            **t,
            "yesterdaySameTime": before,
            "deltaPct": pct(t["subtotal"], before["subtotal"]),
            "billsDeltaPct": pct(t["bills"], before["bills"]),
            "nettDeltaPct": pct(t["nettSales"], before["nettSales"]),
        },
        # full previous day, for the hourly comparison and "% of yesterday"
        "yesterday": {"date": yesterday.isoformat(), "bills": y["bills"], "subtotal": y["subtotal"], "hours": y["hours"]},
        "lastBatch": last_batch(today.isoformat(), cond, params),
        "transactions": latest(today.isoformat(), limit, cond, params),
    }


@router.get("")
def get_live(limit: int = Query(30, ge=1, le=100), branch: Optional[str] = None, channel: Optional[str] = None):
    """Today so far (vs yesterday at the same time) and the latest sales."""
    channels = sorted({c.strip() for c in (channel or "").split(",") if c.strip()})
    key = f"live:{limit}:{branch}:{','.join(channels)}:{data_version.get()}"
    body = _cache.get(key)
    if body is None:
        body = build(limit, branch or None, channels)
        _cache.set(key, body, CACHE_TTL)
    return JSONResponse(body)
