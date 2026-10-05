"""Overview analytics, read from the integration_portal aggregates (app/aggregates.py).

Common query parameters:
  dateFrom, dateTo  YYYY-MM-DD; default: the last 30 complete days (ending yesterday, WIB)
  branch            branch_code, or several separated by commas (CCI01,CCI04)
  channel           comma separated visitPurposeName values, e.g. "Dine In,GoFood"

The comparison period has the same length and ends the day before dateFrom.
Comparisons that reach before OVERVIEW_DATA_FROM (ESB roll-out finished at the
end of July 2025) are left empty, so growth figures are never computed against
a partially onboarded history.
Figures are ESB "Sales" (Finished + bill number) unless stated otherwise, so
totals equal /api/summary and the ESB Sales Recapitulation report.
Only fields that are 100% filled are used (integrated_portal/docs/overview-analytics.md).

/growth: sales growth on subtotal against the previous period, the same days a year earlier or
bucket over bucket (per day), with the branches and channels behind it.

Drill-down endpoints (the dashboard's detail drawers): /hourly-compare (busy hours of two periods
or of several branches), /breakdown (sales by branch / channel / payment / date / type, optionally
for one payment method) and /menu-detail (one menu by day, branch and channel).
"""
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app import database as db
from app.config import get_settings
from app.database import SCHEMA
from app.scope import allowed_branches, scoped_branch
from app.utils import TTLCache, data_version, parse_branches, today

router = APIRouter(prefix="/api/overview", tags=["overview"])

PORTAL = "integration_portal"
DAILY = f"{PORTAL}.agg_sales_daily"
HOURLY = f"{PORTAL}.agg_sales_hourly"
MENU = f"{PORTAL}.agg_menu_daily"
MENU_MONTHLY = f"{PORTAL}.agg_menu_monthly"
HOURLY_MONTHLY = f"{PORTAL}.agg_hourly_monthly"
REFRESH_LOG = f"{PORTAL}.agg_refresh_log"
DEFAULT_DAYS = 30
MAX_DAYS = 3 * 366
CACHE_TTL = 300
GRANULARITIES = ("day", "week", "month")

_cache = TTLCache()


class BadRequest(ValueError):
    pass


# ---------------------------------------------------------------- filters

@dataclass(frozen=True)
class Filters:
    start: date
    end: date
    branch: Optional[str] = None
    channels: tuple[str, ...] = ()
    data_from: date = date.min

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def prev_start(self) -> date:
        return self.start - timedelta(days=self.days)

    @property
    def prev_end(self) -> date:
        return self.start - timedelta(days=1)

    @property
    def prev_complete(self) -> bool:
        return self.prev_start >= self.data_from

    def dims(self) -> tuple[list[str], dict]:
        parts: list[str] = []
        params: dict[str, Any] = {}
        if self.branch:
            parts.append("branch_code = ANY(%(branches)s)")
            params["branches"] = parse_branches(self.branch)
        if self.channels:
            parts.append("channel = ANY(%(channels)s)")
            params["channels"] = list(self.channels)
        return parts, params

    def where(self, start: date, end: date) -> tuple[str, dict]:
        parts, params = self.dims()
        return " AND ".join(["sales_date BETWEEN %(start)s AND %(end)s", *parts]), {**params, "start": start, "end": end}

    def describe(self) -> dict:
        return {
            "from": self.start.isoformat(), "to": self.end.isoformat(), "days": self.days,
            "previous": {"from": self.prev_start.isoformat(), "to": self.prev_end.isoformat(),
                         "complete": self.prev_complete},
            "branch": self.branch, "branches": parse_branches(self.branch), "channels": list(self.channels),
        }

    def key(self) -> str:
        return f"{self.start}:{self.end}:{self.branch}:{','.join(self.channels)}"


def _parse_date(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BadRequest(f"{name} must be YYYY-MM-DD") from exc


def parse_filters(date_from: Optional[str], date_to: Optional[str], branch: Optional[str],
                  channel: Optional[str]) -> Filters:
    end = _parse_date(date_to, "dateTo") if date_to else today() - timedelta(days=1)
    start = _parse_date(date_from, "dateFrom") if date_from else end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        raise BadRequest("dateFrom must not be after dateTo")
    if (end - start).days + 1 > MAX_DAYS:
        raise BadRequest(f"the period is limited to {MAX_DAYS} days")
    channels = tuple(sorted({c.strip() for c in (channel or "").split(",") if c.strip()}))
    return Filters(start, end, scoped_branch(branch), channels, data_start())


def data_start() -> date:
    """First date the Overview treats as complete history."""
    return date.fromisoformat(get_settings().overview_data_from)


# ---------------------------------------------------------------- helpers

def _f(value: Any) -> float:
    return float(value) if isinstance(value, Decimal) else float(value or 0)


def ratio(a: float, b: float) -> Optional[float]:
    return a / b if b else None


def delta_pct(current: float, previous: float) -> Optional[float]:
    return round((current - previous) / previous * 100, 2) if previous else None


def r2(value: Optional[float]) -> Optional[float]:
    return round(value, 2) if value is not None else None


def auto_granularity(days: int) -> str:
    return "day" if days <= 62 else "week" if days <= 210 else "month"


def resolve_granularity(value: Optional[str], days: int) -> str:
    return value if value in GRANULARITIES else auto_granularity(days)


def bucket_of(day: date, granularity: str) -> date:
    if granularity == "week":
        return day - timedelta(days=day.weekday())
    if granularity == "month":
        return day.replace(day=1)
    return day


def bucket_sql(granularity: str) -> str:
    """SQL equivalent of bucket_of (ISO weeks start on Monday)."""
    return "sales_date" if granularity == "day" else f"date_trunc('{granularity}', sales_date)::date"


def buckets(f: Filters, granularity: str) -> list[dict]:
    """Bucket start dates covering the period, with the number of period days in each."""
    out: dict[date, int] = {}
    day = f.start
    while day <= f.end:
        b = bucket_of(day, granularity)
        out[b] = out.get(b, 0) + 1
        day += timedelta(days=1)
    return [{"date": b, "days": n} for b, n in out.items()]


def sales_rows(f: Filters, start: date, end: date, group: tuple[str, ...] = (), select: str = "",
               table: str = DAILY, tx_type: Optional[str] = "sales", payment_method: Optional[str] = None) -> list[dict]:
    """sum(bills), sum(subtotal) (+ sum(nett_sales)) of `table` grouped by the `group` expressions."""
    where, params = f.where(start, end)
    if tx_type:
        where += " AND tx_type = %(tx_type)s"
        params["tx_type"] = tx_type
    if payment_method:
        where += " AND payment_method = %(payment_method)s"
        params["payment_method"] = payment_method
    measures = "sum(bills)::int AS bills, sum(subtotal) AS subtotal"
    if table == DAILY:
        measures += ", sum(nett_sales) AS nett"
    cols = "".join(f"{g}, " for g in group)
    group_by = f" GROUP BY {', '.join(str(i) for i in range(1, len(group) + 1))}" if group else ""
    return db.fetch(
        f"SELECT {cols}{measures}{', ' + select if select else ''} FROM {table} WHERE {where}{group_by}", params)


def day_rows(f: Filters, start: date, end: date, group: tuple[str, ...], tx_type: Optional[str] = None) -> list[dict]:
    """Like sales_rows, plus `days` = number of sales dates with transactions in each group.

    Pre-aggregates to branch x date x type first, so the day count is a plain
    count(*) instead of a (much slower) count(DISTINCT sales_date).
    """
    where, params = f.where(start, end)
    if tx_type:
        where += " AND tx_type = %(tx_type)s"
        params["tx_type"] = tx_type
    group_by = ", ".join(str(i) for i in range(1, len(group) + 1))
    return db.fetch(
        f"""SELECT {', '.join(group)}, sum(bills)::int AS bills, sum(subtotal) AS subtotal, sum(nett) AS nett,
                   count(*)::int AS days
            FROM (SELECT branch_code, sales_date, tx_type, sum(bills) AS bills, sum(subtotal) AS subtotal,
                         sum(nett_sales) AS nett
                  FROM {DAILY} WHERE {where} GROUP BY 1, 2, 3) d
            GROUP BY {group_by}""",
        params,
    )


def prev_rows(f: Filters, group: tuple[str, ...] = (), select: str = "") -> list[dict]:
    """sales_rows of the comparison period, empty when it reaches before the complete history."""
    return sales_rows(f, f.prev_start, f.prev_end, group, select) if f.prev_complete else []


def totals(rows: list[dict]) -> dict:
    bills = sum(int(r["bills"] or 0) for r in rows)
    subtotal = sum(_f(r["subtotal"]) for r in rows)
    nett = sum(_f(r.get("nett")) for r in rows)
    return {"bills": bills, "subtotal": subtotal, "nettSales": nett, "avgTicket": ratio(subtotal, bills) or 0.0}


def branch_names() -> dict[str, str]:
    rows = db.fetch(
        f"SELECT DISTINCT ON (branch_code) branch_code, branch_name FROM {SCHEMA}.master_branches "
        "WHERE COALESCE(branch_code, '') <> '' ORDER BY branch_code, COALESCE(is_deleted, false), branch_name")
    return {r["branch_code"]: r["branch_name"] for r in rows}


def freshness() -> dict:
    row = db.fetchrow(
        f"SELECT min(sales_date) AS data_from, max(sales_date) AS data_to, max(source_synced_at) AS synced, "
        f"max(refreshed_at) AS refreshed FROM {REFRESH_LOG}") or {}

    def iso(v):
        return v.isoformat() if v is not None else None

    first = row.get("data_from")
    return {"dataFrom": iso(max(first, data_start()) if first else None), "dataTo": iso(row.get("data_to")),
            "lastSyncedAt": iso(row.get("synced")), "refreshedAt": iso(row.get("refreshed"))}


def respond(name: str, f: Optional[Filters], extra: str, build: Callable[[], dict]) -> JSONResponse:
    key = f"{name}:{f.key() if f else ''}:{extra}:{data_version.get()}"
    body = _cache.get(key)
    if body is None:
        body = build()
        _cache.set(key, body, CACHE_TTL)
    return JSONResponse(body)


def endpoint(name: str, extra: str, build: Callable[[Filters], dict], dateFrom, dateTo, branch, channel):
    try:
        f = parse_filters(dateFrom, dateTo, branch, channel)
    except BadRequest as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return respond(name, f, extra, lambda: {"filters": f.describe(), **build(f)})


# ---------------------------------------------------------------- builders

def build_kpis(f: Filters) -> dict:
    cur = sales_rows(f, f.start, f.end, ("sales_date",))
    c, p = totals(cur), totals(prev_rows(f))
    by_day = {r["sales_date"]: r for r in cur}
    daily = []
    day = f.start
    while day <= f.end:
        t = totals([by_day[day]] if day in by_day else [])
        daily.append({"date": day.isoformat(), "subtotal": t["subtotal"], "nettSales": t["nettSales"],
                      "bills": t["bills"], "avgTicket": r2(t["avgTicket"])})
        day += timedelta(days=1)
    kpis = {}
    for key, field in (("sales", "subtotal"), ("nettSales", "nettSales"), ("bills", "bills"), ("avgTicket", "avgTicket")):
        kpis[key] = {"value": r2(c[field]), "previous": r2(p[field]), "deltaPct": delta_pct(c[field], p[field])}
    return {"kpis": kpis, "daily": daily, "freshness": freshness()}


def build_trend(f: Filters, granularity: str) -> dict:
    cur = sales_rows(f, f.start, f.end, ("sales_date",))
    prev = prev_rows(f, ("sales_date",))
    by_channel = sales_rows(f, f.start, f.end, ("sales_date", "channel"))
    series = {b["date"]: {"date": b["date"].isoformat(), "days": b["days"], "current": [], "previous": [], "channels": {}}
              for b in buckets(f, granularity)}
    for r in cur:
        series[bucket_of(r["sales_date"], granularity)]["current"].append(r)
    for r in by_channel:  # per-channel stacks of the "By channel" chart
        v = series[bucket_of(r["sales_date"], granularity)]["channels"].setdefault(r["channel"], {"subtotal": 0.0, "bills": 0})
        v["subtotal"] += _f(r["subtotal"])
        v["bills"] += int(r["bills"])
    for r in prev:  # shift the previous period onto the current timeline
        series[bucket_of(r["sales_date"] + timedelta(days=f.days), granularity)]["previous"].append(r)
    out = []
    for s in series.values():
        c, p = totals(s.pop("current")), totals(s.pop("previous"))
        out.append({**s, "subtotal": c["subtotal"], "nettSales": c["nettSales"], "bills": c["bills"],
                    "discountPct": r2(ratio((c["subtotal"] - c["nettSales"]) * 100, c["subtotal"])),
                    "previous": {"subtotal": p["subtotal"], "nettSales": p["nettSales"], "bills": p["bills"]}})
    return {"granularity": granularity, "series": out}


def build_channels(f: Filters, granularity: str) -> dict:
    cur = sales_rows(f, f.start, f.end, ("sales_date", "channel"))
    prev = {r["channel"]: _f(r["subtotal"]) for r in prev_rows(f, ("channel",))}
    by_channel: dict[str, list] = {}
    for r in cur:
        by_channel.setdefault(r["channel"], []).append(r)
    grand = sum(_f(r["subtotal"]) for r in cur)
    channels = []
    for name, rows in by_channel.items():
        t = totals(rows)
        channels.append({
            "channel": name, "bills": t["bills"], "subtotal": t["subtotal"], "nettSales": t["nettSales"],
            "share": r2(ratio(t["subtotal"] * 100, grand)), "avgTicket": r2(t["avgTicket"]),
            "discountPct": r2(ratio((t["subtotal"] - t["nettSales"]) * 100, t["subtotal"])),
            "previousSubtotal": prev.get(name, 0.0), "deltaPct": delta_pct(t["subtotal"], prev.get(name, 0.0)),
        })
    channels.sort(key=lambda c: -c["subtotal"])
    series = {b["date"]: {"date": b["date"].isoformat(), "days": b["days"], "values": {}} for b in buckets(f, granularity)}
    for r in cur:
        v = series[bucket_of(r["sales_date"], granularity)]["values"].setdefault(r["channel"], {"subtotal": 0.0, "bills": 0})
        v["subtotal"] += _f(r["subtotal"])
        v["bills"] += int(r["bills"])
    return {"granularity": granularity, "channels": channels, "series": list(series.values())}


def build_branches(f: Filters, granularity: str) -> dict:
    by_type = day_rows(f, f.start, f.end, ("branch_code", "tx_type"))
    daily = sales_rows(f, f.start, f.end, ("branch_code", f"{bucket_sql(granularity)} AS sales_date"))
    prev = {r["branch_code"]: _f(r["subtotal"]) for r in prev_rows(f, ("branch_code",))}
    names = branch_names()
    marks = [b["date"] for b in buckets(f, granularity)]
    index = {d: i for i, d in enumerate(marks)}
    rows: dict[str, dict] = {}

    def row(code: str) -> dict:
        return rows.setdefault(code, {
            "branchCode": code, "branchName": names.get(code, code), "subtotal": 0.0, "nettSales": 0.0, "bills": 0,
            "activeDays": 0, "allBills": 0, "voidBills": 0, "spark": [0.0] * len(marks)})

    for r in by_type:
        b = row(r["branch_code"])
        b["allBills"] += int(r["bills"])
        if r["tx_type"] == "sales":
            b.update(subtotal=_f(r["subtotal"]), nettSales=_f(r["nett"]), bills=int(r["bills"]), activeDays=r["days"])
        elif r["tx_type"] == "void":
            b["voidBills"] = int(r["bills"])
    for r in daily:
        row(r["branch_code"])["spark"][index[bucket_of(r["sales_date"], granularity)]] += _f(r["subtotal"])
    for code, subtotal in prev.items():
        if subtotal:
            row(code)
    out = []
    for b in rows.values():
        if not b["subtotal"] and not prev.get(b["branchCode"]):
            continue
        p = prev.get(b["branchCode"], 0.0)
        out.append({
            **{k: v for k, v in b.items() if k != "allBills"},
            "avgTicket": r2(ratio(b["subtotal"], b["bills"]) or 0.0),
            "subtotalPerDay": r2(ratio(b["subtotal"], b["activeDays"]) or 0.0),
            "previousSubtotal": p, "deltaPct": delta_pct(b["subtotal"], p),
            "isNew": bool(f.prev_complete and b["subtotal"] and not p),
            "voidRate": r2(ratio(b["voidBills"] * 100, b["allBills"])),
        })
    out.sort(key=lambda b: -b["subtotal"])
    return {"granularity": granularity, "buckets": [d.isoformat() for d in marks], "branches": out}


def _period_days_per_dow(f: Filters, data_from: Optional[date], data_to: Optional[date]) -> dict[int, int]:
    start = max(f.start, data_from) if data_from else f.start
    end = min(f.end, data_to) if data_to else f.end
    counts = {d: 0 for d in range(1, 8)}
    day = start
    while day <= end:
        counts[day.isoweekday()] += 1
        day += timedelta(days=1)
    return counts


def hourly_rows(f: Filters) -> list[dict]:
    """Bills and sales per ISO weekday x hour over the period."""
    src, params = rollup_source(f, HOURLY_MONTHLY, HOURLY, "dow, hour, bills, subtotal",
                                "extract(isodow FROM sales_date)::smallint, hour, bills, subtotal")
    return db.fetch(f"SELECT dow::int AS dow, hour, sum(bills)::int AS bills, sum(subtotal) AS subtotal "
                    f"FROM ({src}) s GROUP BY 1, 2", params)


def build_hourly(f: Filters) -> dict:
    rows = hourly_rows(f)
    fresh = freshness()
    data_from = date.fromisoformat(fresh["dataFrom"]) if fresh["dataFrom"] else None
    data_to = date.fromisoformat(fresh["dataTo"]) if fresh["dataTo"] else None
    days = _period_days_per_dow(f, data_from, data_to)
    cells = []
    for r in rows:
        dow = r["dow"]
        n = days.get(dow) or 0
        cells.append({"dow": dow, "hour": r["hour"], "bills": int(r["bills"]), "subtotal": _f(r["subtotal"]),
                      "avgBills": r2(ratio(int(r["bills"]), n) or 0.0), "avgSubtotal": r2(ratio(_f(r["subtotal"]), n) or 0.0)})
    cells.sort(key=lambda c: (c["dow"], c["hour"]))
    peak = max(cells, key=lambda c: c["avgBills"], default=None)
    return {"daysPerDow": days, "cells": cells, "peak": peak}


def _add_months(d: date, n: int) -> date:
    m = d.year * 12 + d.month - 1 + n
    return date(m // 12, m % 12 + 1, 1)


def full_months(start: date, end: date) -> tuple[date, date]:
    """[first, stop) of the calendar months lying completely inside [start, end] (may be empty)."""
    first = start if start.day == 1 else _add_months(start, 1)
    stop = (end + timedelta(days=1)).replace(day=1)
    return (first, stop) if first < stop else (date.max, date.max)


def rollup_source(f: Filters, monthly: str, daily: str, monthly_cols: str, daily_cols: str) -> tuple[str, dict]:
    """SQL union over the period: full months from the monthly rollup, edge days from the daily table."""
    dims, params = f.dims()
    first, stop = full_months(f.start, f.end)
    params.update(start=f.start, end=f.end, first=first, stop=stop)
    cond = "".join(f" AND {d}" for d in dims)
    return (
        f"""SELECT {monthly_cols} FROM {monthly} WHERE month >= %(first)s AND month < %(stop)s{cond}
            UNION ALL
            SELECT {daily_cols} FROM {daily} WHERE sales_date BETWEEN %(start)s AND %(end)s
                AND NOT (sales_date >= %(first)s AND sales_date < %(stop)s){cond}""",
        params,
    )


def menu_rows(f: Filters) -> list[dict]:
    """Per menu and kind over the period."""
    cols = "menu_id, kind, menu_name, category, category_detail, bills, qty, subtotal, discount"
    src, params = rollup_source(f, MENU_MONTHLY, MENU, cols, cols)
    return db.fetch(
        f"""WITH src AS ({src}), t AS (
                SELECT menu_id, kind, max(menu_name) AS name, max(category) AS category,
                       max(category_detail) AS category_detail, sum(bills)::int AS bills,
                       sum(qty) AS qty, sum(subtotal) AS subtotal, sum(discount) AS discount
                FROM src GROUP BY menu_id, kind
            )
            SELECT t.menu_id, t.kind, COALESCE(m.menu_name, t.name) AS menu_name, t.category, t.category_detail,
                   t.bills, t.qty, t.subtotal, t.discount
            FROM t LEFT JOIN {SCHEMA}.master_pos_menu m ON m.menu_id = t.menu_id""",
        params,
    )


def build_menus(f: Filters, limit: int, sort: str) -> dict:
    rows = menu_rows(f)
    menus = [r for r in rows if r["kind"] == "menu"]
    order = "qty" if sort == "qty" else "subtotal"
    top = sorted(menus, key=lambda m: (-_f(m[order]), m["menu_id"]))[:limit]
    cats: dict[tuple, dict] = {}
    for m in menus:
        c = cats.setdefault((m["category"], m["category_detail"]), {
            "category": m["category"], "category_detail": m["category_detail"], "qty": 0.0, "subtotal": 0.0})
        c["qty"] += _f(m["qty"])
        c["subtotal"] += _f(m["subtotal"])
    cats = list(cats.values())
    merged: dict[tuple, dict] = {}  # an add-on can be both a package and an extra
    for a in rows:
        if a["kind"] == "menu" or a["category"] != "EXTRA":
            continue
        g = merged.setdefault((a["category_detail"], a["menu_id"]), {**a, "qty": 0.0, "subtotal": 0.0})
        g["qty"] += _f(a["qty"])
        g["subtotal"] += _f(a["subtotal"])
    addons = list(merged.values())
    menu_total = sum(_f(c["subtotal"]) for c in cats)
    qty_total = sum(_f(c["qty"]) for c in cats)
    categories: dict[str, dict] = {}
    for c in cats:
        g = categories.setdefault(c["category"], {"category": c["category"], "qty": 0.0, "subtotal": 0.0, "details": []})
        g["qty"] += _f(c["qty"])
        g["subtotal"] += _f(c["subtotal"])
        g["details"].append({"name": c["category_detail"], "qty": _f(c["qty"]), "subtotal": _f(c["subtotal"])})
    for g in categories.values():
        g["share"] = r2(ratio(g["subtotal"] * 100, menu_total))
        g["details"].sort(key=lambda d: -d["subtotal"])
    groups: dict[str, dict] = {}
    for a in addons:
        g = groups.setdefault(a["category_detail"], {"group": a["category_detail"], "qty": 0.0, "subtotal": 0.0, "options": []})
        g["qty"] += _f(a["qty"])
        g["subtotal"] += _f(a["subtotal"])
        g["options"].append({"menuId": a["menu_id"], "name": a["menu_name"], "qty": _f(a["qty"]), "subtotal": _f(a["subtotal"])})
    for g in groups.values():
        g["options"].sort(key=lambda o: -o["qty"])
        for o in g["options"]:
            o["share"] = r2(ratio(o["qty"] * 100, g["qty"]))
        g["options"] = g["options"][:10]
    return {
        "totals": {"subtotal": menu_total, "qty": qty_total},
        "top": [{"menuId": m["menu_id"], "name": m["menu_name"], "category": m["category"],
                 "categoryDetail": m["category_detail"], "bills": m["bills"], "qty": _f(m["qty"]),
                 "subtotal": _f(m["subtotal"]), "discount": _f(m["discount"]),
                 "share": r2(ratio(_f(m["subtotal"]) * 100, menu_total))} for m in top],
        "categories": sorted(categories.values(), key=lambda g: -g["subtotal"]),
        "addons": sorted(groups.values(), key=lambda g: -g["qty"]),
    }


def _p90(values: list[float]) -> Optional[float]:
    if len(values) < 5:
        return None
    s = sorted(values)
    k = (len(s) - 1) * 0.9
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def build_deductions(f: Filters) -> dict:
    per_day = sales_rows(f, f.start, f.end, ("sales_date", "tx_type"), tx_type=None)
    per_branch = sales_rows(f, f.start, f.end, ("branch_code", "tx_type"), tx_type=None)
    methods = sales_rows(f, f.start, f.end, ("payment_method",), tx_type="other_cost")
    names = branch_names()

    kinds = ("sales", "void", "other_cost", "open")
    tot = {k: {"bills": 0, "subtotal": 0.0} for k in (*kinds, "gross")}
    days: dict[date, dict] = {}
    for r in per_day:
        d = days.setdefault(r["sales_date"], {k: {"bills": 0, "subtotal": 0.0} for k in (*kinds, "gross")})
        for target in (tot, d):
            for k in (r["tx_type"], "gross"):
                target[k]["bills"] += int(r["bills"])
                target[k]["subtotal"] += _f(r["subtotal"])
    branches: dict[str, dict] = {}
    for r in per_branch:
        b = branches.setdefault(r["branch_code"], {"branchCode": r["branch_code"],
                                                   "branchName": names.get(r["branch_code"], r["branch_code"]),
                                                   "bills": 0, "voidBills": 0, "voidSubtotal": 0.0,
                                                   "otherCostBills": 0, "otherCostSubtotal": 0.0})
        b["bills"] += int(r["bills"])
        if r["tx_type"] == "void":
            b["voidBills"] += int(r["bills"])
            b["voidSubtotal"] += _f(r["subtotal"])
        elif r["tx_type"] == "other_cost":
            b["otherCostBills"] += int(r["bills"])
            b["otherCostSubtotal"] += _f(r["subtotal"])
    for b in branches.values():
        b["voidRate"] = r2(ratio(b["voidBills"] * 100, b["bills"]) or 0.0)
    threshold = r2(_p90([b["voidRate"] for b in branches.values() if b["bills"] >= 100]))
    for b in branches.values():
        b["status"] = "review" if threshold is not None and b["voidRate"] > threshold and b["voidBills"] >= 3 else "normal"

    def out(t: dict) -> dict:
        return {k: {"bills": v["bills"], "subtotal": v["subtotal"]} for k, v in t.items()}

    return {
        "totals": {**out(tot), "otherCost": tot["other_cost"]},
        "voidRate": r2(ratio(tot["void"]["bills"] * 100, tot["gross"]["bills"]) or 0.0),
        "threshold": threshold,
        "otherCostByMethod": sorted(({"method": r["payment_method"], "bills": int(r["bills"]),
                                      "subtotal": _f(r["subtotal"])} for r in methods), key=lambda m: -m["subtotal"]),
        "branches": sorted((b for b in branches.values() if b["voidBills"] or b["otherCostBills"]),
                           key=lambda b: (-b["voidRate"], -b["voidBills"])),
        "daily": [{"date": d.isoformat(), "bills": v["gross"]["bills"], "voidBills": v["void"]["bills"],
                   "voidSubtotal": v["void"]["subtotal"], "otherCostSubtotal": v["other_cost"]["subtotal"],
                   "voidRate": r2(ratio(v["void"]["bills"] * 100, v["gross"]["bills"]) or 0.0)}
                  for d, v in sorted(days.items())],
    }


def build_monthly(f: Filters) -> dict:
    """Calendar months of the selected period (only its days count in each month).

    Growth compares average sales per calendar day, so partial and 30/31-day
    months compare fairly; the previous month and the same month a year ago
    are taken whole. Same-store = branches with sales on >= 90% of the days of
    both the selected part of the month and the whole previous month.
    """
    fresh = freshness()
    data_from = date.fromisoformat(fresh["dataFrom"]) if fresh["dataFrom"] else f.data_from
    end = min(f.end, date.fromisoformat(fresh["dataTo"])) if fresh["dataTo"] else f.end
    start = max(f.start, data_from)
    if start > end:
        return {"months": []}
    first, last = start.replace(day=1), end.replace(day=1)
    month = "date_trunc('month', sales_date)::date AS month"

    def index(rows: list[dict]) -> dict[date, dict[str, dict]]:
        out: dict[date, dict[str, dict]] = {}
        for r in rows:
            out.setdefault(r["month"], {})[r["branch_code"]] = r
        return out

    selected = index(day_rows(f, start, end, (month, "branch_code"), "sales"))
    # whole comparison months: previous month and the same month last year
    scan_from = max(_add_months(first, -12), data_from.replace(day=1))
    compare = index(day_rows(f, scan_from, end, (month, "branch_code"), "sales"))

    def span(m: date, lo: date, hi: date) -> int:
        a, b = max(m, lo, data_from), min(_add_months(m, 1) - timedelta(days=1), hi)
        return max((b - a).days + 1, 0)

    def avg(rows: dict[str, dict], days: int, codes: Optional[set] = None) -> Optional[float]:
        if not days or not rows:
            return None
        return sum(_f(r["subtotal"]) for c, r in rows.items() if codes is None or c in codes) / days

    out = []
    m = first
    while m <= last:
        n = span(m, start, end)
        if n:
            branches = selected.get(m, {})
            prev_m, yoy_m = _add_months(m, -1), _add_months(m, -12)
            prev_rows, prev_days = compare.get(prev_m, {}), span(prev_m, prev_m, end)
            same = {c for c, r in branches.items() if r["days"] >= 0.9 * n
                    and c in prev_rows and prev_rows[c]["days"] >= 0.9 * prev_days}
            cur_avg = avg(branches, n)
            prev_avg = avg(prev_rows, prev_days)
            yoy_avg = avg(compare.get(yoy_m, {}), span(yoy_m, yoy_m, end))
            ss_cur, ss_prev = avg(branches, n, same), avg(prev_rows, prev_days, same)
            out.append({
                "month": m.isoformat(), "days": n, "partial": n < (_add_months(m, 1) - m).days,
                "subtotal": sum(_f(r["subtotal"]) for r in branches.values()),
                "nettSales": sum(_f(r["nett"]) for r in branches.values()),
                "bills": sum(int(r["bills"]) for r in branches.values()),
                "branches": sum(1 for r in branches.values() if _f(r["subtotal"])),
                "avgDaily": r2(cur_avg),
                "momPct": delta_pct(cur_avg or 0, prev_avg or 0) if cur_avg is not None else None,
                "yoyPct": delta_pct(cur_avg or 0, yoy_avg or 0) if cur_avg is not None else None,
                "sameStore": {"branches": len(same),
                              "growthPct": delta_pct(ss_cur or 0, ss_prev or 0) if same else None},
            })
        m = _add_months(m, 1)
    return {"months": out}


def build_payments(f: Filters) -> dict:
    rows = sales_rows(f, f.start, f.end, ("payment_type", "payment_method"))
    t = totals(rows)
    types: dict[str, dict] = {}
    methods = []
    for r in rows:
        bills, subtotal = int(r["bills"]), _f(r["subtotal"])
        methods.append({"type": r["payment_type"], "method": r["payment_method"], "bills": bills, "subtotal": subtotal,
                        "share": r2(ratio(subtotal * 100, t["subtotal"])), "billShare": r2(ratio(bills * 100, t["bills"]))})
        g = types.setdefault(r["payment_type"], {"type": r["payment_type"], "bills": 0, "subtotal": 0.0})
        g["bills"] += bills
        g["subtotal"] += subtotal
    for g in types.values():
        g["share"] = r2(ratio(g["subtotal"] * 100, t["subtotal"]))
    return {"methods": sorted(methods, key=lambda m: -m["subtotal"]),
            "types": sorted(types.values(), key=lambda g: -g["subtotal"])}


BASKET_COLS = ("sum(menu_lines)::int AS lines, sum(item_qty) AS qty, sum(bills_with_beverage)::int AS bev, "
               "sum(bills_with_food)::int AS food, sum(bills_with_both)::int AS both")


def _basket(rows: list[dict]) -> dict:
    bills = sum(int(r["bills"]) for r in rows)
    bev = sum(int(r["bev"]) for r in rows)
    food = sum(int(r["food"]) for r in rows)
    both = sum(int(r["both"]) for r in rows)
    return {
        "bills": bills,
        "linesPerBill": r2(ratio(sum(int(r["lines"]) for r in rows), bills)),
        "qtyPerBill": r2(ratio(sum(_f(r["qty"]) for r in rows), bills)),
        "beverageBills": bev, "foodBills": food, "bothBills": both,
        "foodSharePct": r2(ratio(food * 100, bills)),
        "foodAttachPct": r2(ratio(both * 100, bev)),  # beverage bills that also have food
    }


def build_basket(f: Filters, granularity: str) -> dict:
    daily = sales_rows(f, f.start, f.end, ("sales_date",), BASKET_COLS)
    by_channel = sales_rows(f, f.start, f.end, ("channel",), BASKET_COLS)
    prev = prev_rows(f, (), BASKET_COLS)
    grouped: dict[date, list] = {b["date"]: [] for b in buckets(f, granularity)}
    for r in daily:
        grouped[bucket_of(r["sales_date"], granularity)].append(r)
    return {
        "granularity": granularity,
        "totals": _basket(daily),
        "previous": _basket([r for r in prev if r["bills"]]),
        "channels": sorted(({"channel": r["channel"], **_basket([r])} for r in by_channel), key=lambda c: -c["bills"]),
        "series": [{"date": d.isoformat(), **_basket(rows)} for d, rows in grouped.items()],
    }


# ---------------------------------------------------------------- drill-down builders

MAX_COMPARE_BRANCHES = 8


def hours_profile(f: Filters) -> dict:
    """Busy hours of one period: per hour (all weekdays) and per weekday x hour, averaged per day."""
    h = build_hourly(f)
    days = sum(h["daysPerDow"].values())
    by_hour: dict[int, dict] = {}
    for c in h["cells"]:
        x = by_hour.setdefault(c["hour"], {"bills": 0, "subtotal": 0.0})
        x["bills"] += c["bills"]
        x["subtotal"] += c["subtotal"]
    bills = sum(x["bills"] for x in by_hour.values())
    subtotal = sum(x["subtotal"] for x in by_hour.values())
    hours = [{"hour": hr, "bills": x["bills"], "subtotal": x["subtotal"],
              "avgBills": r2(ratio(x["bills"], days) or 0.0), "avgSubtotal": r2(ratio(x["subtotal"], days) or 0.0),
              "share": r2(ratio(x["bills"] * 100, bills) or 0.0)} for hr, x in sorted(by_hour.items())]
    peak = max(hours, key=lambda x: x["bills"], default=None)
    return {"from": f.start.isoformat(), "to": f.end.isoformat(), "days": days, "complete": f.start >= f.data_from,
            "bills": bills, "subtotal": subtotal, "avgBillsPerDay": r2(ratio(bills, days) or 0.0),
            "hours": hours, "cells": h["cells"], "daysPerDow": h["daysPerDow"], "peakHour": peak["hour"] if peak else None}


def hourly_branch_rows(f: Filters) -> list[dict]:
    """Bills and sales per branch x hour over the period."""
    src, params = rollup_source(f, HOURLY_MONTHLY, HOURLY, "branch_code, hour, bills, subtotal",
                                "branch_code, hour, bills, subtotal")
    return db.fetch(f"SELECT branch_code, hour, sum(bills)::int AS bills, sum(subtotal) AS subtotal "
                    f"FROM ({src}) s GROUP BY 1, 2", params)


def build_hourly_compare(f: Filters, mode: str, compare_from: Optional[str], compare_to: Optional[str],
                         compare_branches: Optional[str]) -> dict:
    if mode == "branches":
        codes = parse_branches(scoped_branch(compare_branches)) if compare_branches else []
        codes = [c for c in codes if c != "-"]
        if not codes:
            # the selected branches, else the busiest branches of the period
            selected = parse_branches(f.branch)
            codes = selected if len(selected) >= 2 else [
                b["branchCode"] for b in sorted(build_branches(f, "day")["branches"], key=lambda b: -b["bills"]) if b["bills"]][:5]
        codes = codes[:MAX_COMPARE_BRANCHES]
        fb = replace(f, branch=",".join(sorted(codes)) or "-")
        rows = hourly_branch_rows(fb) if codes else []
        active = {r["branch_code"]: r for r in day_rows(fb, f.start, f.end, ("branch_code",), "sales")} if codes else {}
        names = branch_names()
        out = []
        for code in codes:
            mine = [r for r in rows if r["branch_code"] == code]
            days = int((active.get(code) or {}).get("days") or 0)
            bills = sum(int(r["bills"]) for r in mine)
            hours = [{"hour": r["hour"], "bills": int(r["bills"]), "subtotal": _f(r["subtotal"]),
                      "avgBills": r2(ratio(int(r["bills"]), days) or 0.0), "avgSubtotal": r2(ratio(_f(r["subtotal"]), days) or 0.0),
                      "share": r2(ratio(int(r["bills"]) * 100, bills) or 0.0)} for r in sorted(mine, key=lambda r: r["hour"])]
            peak = max(hours, key=lambda x: x["bills"], default=None)
            out.append({"branchCode": code, "branchName": names.get(code, code), "activeDays": days, "bills": bills,
                        "subtotal": sum(x["subtotal"] for x in hours), "avgBillsPerDay": r2(ratio(bills, days) or 0.0),
                        "hours": hours, "peakHour": peak["hour"] if peak else None})
        return {"mode": "branches", "branches": out}

    # period: the selected period against another one (default: the previous period of the same length)
    try:
        cs = date.fromisoformat(compare_from) if compare_from else f.prev_start
        ce = date.fromisoformat(compare_to) if compare_to else (cs + timedelta(days=f.days - 1) if compare_from else f.prev_end)
    except ValueError as exc:
        raise BadRequest("compareFrom/compareTo must be YYYY-MM-DD") from exc
    if cs > ce or (ce - cs).days + 1 > MAX_DAYS:
        raise BadRequest("invalid comparison period")
    return {"mode": "period", "current": hours_profile(f), "compare": hours_profile(replace(f, start=cs, end=ce))}


GROWTH_BASES = ("previous", "lastYear", "sequential")
YEAR_SHIFT = 364  # 52 weeks: the same weekdays a year earlier


def _bucket_before(b: date, granularity: str) -> date:
    if granularity == "week":
        return b - timedelta(days=7)
    if granularity == "month":
        return _add_months(b, -1)
    return b - timedelta(days=1)


def _growth(cur: float, cmp: float) -> Optional[float]:
    return delta_pct(cur, cmp) if cmp else None


def build_growth(f: Filters, granularity: str, basis: str) -> dict:
    """Sales growth on subtotal (gross sales) of the period, per bucket, branch and channel.

    basis: previous = the previous period of the same length, aligned day by day;
           lastYear = the same days 52 weeks earlier (same weekdays);
           sequential = every bucket against the bucket before it, per calendar day, so partial
           first/last weeks or months compare fairly (totals: against the previous period).
    """
    shift = YEAR_SHIFT if basis == "lastYear" else f.days
    cs, ce = f.start - timedelta(days=shift), f.end - timedelta(days=shift)
    complete = cs >= f.data_from
    cur_rows = sales_rows(f, f.start, f.end, ("sales_date",))
    cmp_rows = sales_rows(f, cs, ce, ("sales_date",)) if complete else []
    marks = buckets(f, granularity)
    series = []

    if basis == "sequential":
        # every bucket of the period (only its days inside the period) against the bucket before it,
        # per calendar day; the first one against the whole bucket just before the period
        cur_b = {m["date"]: {"subtotal": 0.0, "bills": 0, "days": m["days"], "from": max(m["date"], f.start)} for m in marks}
        for r in cur_rows:
            b = cur_b[bucket_of(r["sales_date"], granularity)]
            b["subtotal"] += _f(r["subtotal"])
            b["bills"] += int(r["bills"])
        prev = None
        if marks:
            b0_start = max(_bucket_before(marks[0]["date"], granularity), f.data_from)
            b0_end = marks[0]["date"] - timedelta(days=1)
            if b0_start <= b0_end:
                t0 = totals(sales_rows(f, b0_start, b0_end, ("sales_date",)))
                prev = {"subtotal": t0["subtotal"], "bills": t0["bills"], "days": (b0_end - b0_start).days + 1, "from": b0_start}
        for m in marks:
            c = cur_b[m["date"]]
            cur_avg = ratio(c["subtotal"], c["days"]) or 0.0
            p_avg = ratio(prev["subtotal"], prev["days"]) if prev else None
            series.append({
                "date": m["date"].isoformat(), "days": c["days"], "subtotal": c["subtotal"], "bills": c["bills"],
                "compareSubtotal": prev["subtotal"] if prev else None, "compareBills": prev["bills"] if prev else None,
                "compareDays": prev["days"] if prev else None, "compareFrom": prev["from"].isoformat() if prev else None,
                "avgPerDay": r2(cur_avg), "compareAvgPerDay": r2(p_avg),
                "growthPct": _growth(cur_avg, p_avg) if p_avg else None,
                "growthAbs": r2(cur_avg - p_avg) if p_avg is not None else None,  # per day
            })
            prev = c
    else:
        by_bucket: dict[date, dict] = {m["date"]: {"subtotal": 0.0, "bills": 0, "cs": 0.0, "cb": 0} for m in marks}
        for r in cur_rows:
            b = by_bucket[bucket_of(r["sales_date"], granularity)]
            b["subtotal"] += _f(r["subtotal"])
            b["bills"] += int(r["bills"])
        for r in cmp_rows:  # compare days moved onto the current timeline
            b = by_bucket.get(bucket_of(r["sales_date"] + timedelta(days=shift), granularity))
            if b is not None:
                b["cs"] += _f(r["subtotal"])
                b["cb"] += int(r["bills"])
        for m in marks:
            b = by_bucket[m["date"]]
            series.append({
                "date": m["date"].isoformat(), "days": m["days"], "subtotal": b["subtotal"], "bills": b["bills"],
                "compareSubtotal": b["cs"] if complete else None, "compareBills": b["cb"] if complete else None,
                "compareDays": m["days"] if complete else None,
                "compareFrom": (max(m["date"], f.start) - timedelta(days=shift)).isoformat() if complete else None,
                "avgPerDay": r2(ratio(b["subtotal"], m["days"]) or 0.0),
                "compareAvgPerDay": r2(ratio(b["cs"], m["days"])) if complete else None,
                "growthPct": _growth(b["subtotal"], b["cs"]) if complete else None,
                "growthAbs": r2(b["subtotal"] - b["cs"]) if complete else None,
            })

    def split(col: str, names: Optional[dict] = None) -> list[dict]:
        cur = {r[col]: r for r in sales_rows(f, f.start, f.end, (col,))}
        cmp = {r[col]: r for r in sales_rows(f, cs, ce, (col,))} if complete else {}
        cmp_total = sum(_f(r["subtotal"]) for r in cmp.values())
        out = []
        for key in set(cur) | set(cmp):
            c, p = _f((cur.get(key) or {}).get("subtotal")), _f((cmp.get(key) or {}).get("subtotal"))
            out.append({"key": key, "label": (names or {}).get(key, key), "subtotal": c, "compareSubtotal": p if complete else None,
                        "bills": int((cur.get(key) or {}).get("bills") or 0), "compareBills": int((cmp.get(key) or {}).get("bills") or 0) if complete else None,
                        "growthAbs": c - p if complete else None, "growthPct": _growth(c, p) if complete else None,
                        # percentage points of the total growth this row explains (they add up to the total growth %)
                        "contributionPp": r2(ratio((c - p) * 100, cmp_total)) if complete and cmp_total else None,
                        "status": None if not complete else "new" if c and not p else "lost" if p and not c else
                        "growing" if c > p else "declining" if c < p else "flat"})
        return sorted(out, key=lambda x: -(x["growthAbs"] if x["growthAbs"] is not None else x["subtotal"]))

    cur_t, cmp_t = totals(cur_rows), totals(cmp_rows)
    return {
        "granularity": granularity,
        "compare": {"basis": basis, "from": cs.isoformat(), "to": ce.isoformat(), "complete": complete},
        "totals": {
            "subtotal": cur_t["subtotal"], "bills": cur_t["bills"], "avgTicket": r2(cur_t["avgTicket"]),
            "compareSubtotal": cmp_t["subtotal"] if complete else None, "compareBills": cmp_t["bills"] if complete else None,
            "compareAvgTicket": r2(cmp_t["avgTicket"]) if complete else None,
            "growthAbs": cur_t["subtotal"] - cmp_t["subtotal"] if complete else None,
            "growthPct": _growth(cur_t["subtotal"], cmp_t["subtotal"]) if complete else None,
            "billsGrowthPct": _growth(cur_t["bills"], cmp_t["bills"]) if complete else None,
            "avgTicketGrowthPct": _growth(cur_t["avgTicket"], cmp_t["avgTicket"]) if complete else None,
            "bucketsUp": sum(1 for s in series if (s["growthPct"] or 0) > 0),
            "bucketsDown": sum(1 for s in series if (s["growthPct"] or 0) < 0),
        },
        "series": series,
        "branches": split("branch_code", branch_names()),
        "channels": split("channel"),
    }


BREAKDOWN = {"branch": "branch_code", "channel": "channel", "payment": "payment_method",
             "paymentType": "payment_type", "date": "sales_date", "type": "tx_type"}


def build_breakdown(f: Filters, by: str, payment_method: Optional[str], tx_type: Optional[str]) -> dict:
    """Sales of the period split by one dimension (optionally for one payment method / transaction type)."""
    col = BREAKDOWN[by]
    tx = None if tx_type == "all" or by == "type" else (tx_type or "sales")
    cur = sales_rows(f, f.start, f.end, (col,), tx_type=tx, payment_method=payment_method)
    prev = {} if by == "date" else {r[col]: _f(r["subtotal"]) for r in (
        sales_rows(f, f.prev_start, f.prev_end, (col,), tx_type=tx, payment_method=payment_method) if f.prev_complete else [])}
    total = sum(_f(r["subtotal"]) for r in cur)
    bills_total = sum(int(r["bills"] or 0) for r in cur)
    names = branch_names() if by == "branch" else {}
    rows = []
    for r in cur:
        key = r[col]
        subtotal, bills, nett = _f(r["subtotal"]), int(r["bills"] or 0), _f(r.get("nett"))
        rows.append({"key": key.isoformat() if isinstance(key, date) else key, "label": names.get(key, key) if by == "branch" else (
                        key.isoformat() if isinstance(key, date) else key),
                     "bills": bills, "subtotal": subtotal, "nettSales": nett, "avgTicket": r2(ratio(subtotal, bills)),
                     "share": r2(ratio(subtotal * 100, total)), "billShare": r2(ratio(bills * 100, bills_total)),
                     "previousSubtotal": prev.get(key), "deltaPct": delta_pct(subtotal, prev.get(key) or 0) if prev else None})
    if by == "date":  # every day of the period, also the ones without sales
        have = {r["key"] for r in rows}
        day = f.start
        while day <= f.end:
            if day.isoformat() not in have:
                rows.append({"key": day.isoformat(), "label": day.isoformat(), "bills": 0, "subtotal": 0.0, "nettSales": 0.0,
                             "avgTicket": None, "share": 0.0, "billShare": 0.0, "previousSubtotal": None, "deltaPct": None})
            day += timedelta(days=1)
        rows.sort(key=lambda r: r["key"])
    else:
        rows.sort(key=lambda r: -r["subtotal"])
    return {"by": by, "paymentMethod": payment_method, "txType": tx or "all",
            "totals": {"bills": bills_total, "subtotal": total, "nettSales": sum(r["nettSales"] for r in rows)}, "rows": rows}


def menu_detail_rows(f: Filters, menu_id: str, kind: str, group: str) -> list[dict]:
    """One menu over the period grouped by `group` (sales_date | month | branch_code | channel)."""
    dims, params = f.dims()
    params.update(start=f.start, end=f.end, menu_id=menu_id, kind=kind)
    cond = "".join(f" AND {d}" for d in dims) + " AND menu_id = %(menu_id)s AND kind = %(kind)s"
    if group in ("sales_date", "month"):
        if group == "sales_date":
            src = f"SELECT sales_date AS g, bills, qty, subtotal, discount FROM {MENU} WHERE sales_date BETWEEN %(start)s AND %(end)s{cond}"
        else:  # long periods: full months from the monthly rollup, edge days from the daily table
            first, stop = full_months(f.start, f.end)
            params.update(first=first, stop=stop)
            src = (f"SELECT month AS g, bills, qty, subtotal, discount FROM {MENU_MONTHLY} WHERE month >= %(first)s AND month < %(stop)s{cond} "
                   f"UNION ALL SELECT date_trunc('month', sales_date)::date, bills, qty, subtotal, discount FROM {MENU} "
                   f"WHERE sales_date BETWEEN %(start)s AND %(end)s AND NOT (sales_date >= %(first)s AND sales_date < %(stop)s){cond}")
        return db.fetch(f"SELECT g, sum(bills)::int AS bills, sum(qty) AS qty, sum(subtotal) AS subtotal, sum(discount) AS discount "
                        f"FROM ({src}) s GROUP BY 1 ORDER BY 1", params)
    src, p2 = rollup_source(f, MENU_MONTHLY, MENU, f"{group}, menu_id, kind, bills, qty, subtotal, discount",
                            f"{group}, menu_id, kind, bills, qty, subtotal, discount")
    p2.update(menu_id=menu_id, kind=kind)
    return db.fetch(f"SELECT {group} AS g, sum(bills)::int AS bills, sum(qty) AS qty, sum(subtotal) AS subtotal, "
                    f"sum(discount) AS discount FROM ({src}) s WHERE menu_id = %(menu_id)s AND kind = %(kind)s GROUP BY 1", p2)


def build_menu_detail(f: Filters, menu_id: str, kind: str) -> dict:
    group = "sales_date" if f.days <= 92 else "month"
    series = menu_detail_rows(f, menu_id, kind, group)
    branches = menu_detail_rows(f, menu_id, kind, "branch_code")
    channels = menu_detail_rows(f, menu_id, kind, "channel")
    menus = menu_rows(f)
    info = next((m for m in menus if m["menu_id"] == menu_id and m["kind"] == kind), None)
    all_menus = sum(_f(m["subtotal"]) for m in menus if m["kind"] == "menu")
    prev = menu_detail_rows(replace(f, start=f.prev_start, end=f.prev_end), menu_id, kind, "channel") if f.prev_complete else []
    names = branch_names()

    def tot(rows):
        return {"bills": sum(int(r["bills"] or 0) for r in rows), "qty": sum(_f(r["qty"]) for r in rows),
                "subtotal": sum(_f(r["subtotal"]) for r in rows), "discount": sum(_f(r["discount"]) for r in rows)}

    t, p = tot(branches), tot(prev)

    def rows(items, label=lambda k: k):
        return sorted(({"key": str(r["g"]), "label": label(r["g"]), "bills": int(r["bills"] or 0), "qty": _f(r["qty"]),
                        "subtotal": _f(r["subtotal"]), "share": r2(ratio(_f(r["subtotal"]) * 100, t["subtotal"]))} for r in items),
                      key=lambda x: -x["subtotal"])

    return {
        "menu": {"menuId": menu_id, "kind": kind, "name": (info or {}).get("menu_name") or menu_id,
                 "category": (info or {}).get("category"), "categoryDetail": (info or {}).get("category_detail")},
        "totals": {**t, "avgPrice": r2(ratio(t["subtotal"], t["qty"])), "shareOfMenus": r2(ratio(t["subtotal"] * 100, all_menus)),
                   "previousQty": p["qty"] if f.prev_complete else None, "previousSubtotal": p["subtotal"] if f.prev_complete else None,
                   "qtyDeltaPct": delta_pct(t["qty"], p["qty"]) if f.prev_complete else None},
        "granularity": "day" if group == "sales_date" else "month",
        "series": [{"date": r["g"].isoformat(), "bills": int(r["bills"] or 0), "qty": _f(r["qty"]), "subtotal": _f(r["subtotal"])} for r in series],
        "branches": rows(branches, lambda k: names.get(k, k)),
        "channels": rows(channels),
    }


# ---------------------------------------------------------------- routes

@router.get("/meta")
def get_meta():
    """Filter options (channels by volume) and data coverage / freshness."""
    def build() -> dict:
        rows = db.fetch(f"SELECT channel, sum(bills)::int AS bills FROM {DAILY} WHERE tx_type = 'sales' "
                        "GROUP BY channel ORDER BY 2 DESC")
        end = today() - timedelta(days=1)
        return {"channels": [{"channel": r["channel"], "bills": r["bills"]} for r in rows],
                "defaultPeriod": {"from": (end - timedelta(days=DEFAULT_DAYS - 1)).isoformat(), "to": end.isoformat()},
                "freshness": freshness()}
    if allowed_branches() is not None:
        # role "user": channel names only, not the network-wide bill counts
        body = _cache.get("meta-scoped:" + data_version.get())
        if body is None:
            meta = build()
            body = {**meta, "channels": [{"channel": c["channel"], "bills": None} for c in meta["channels"]]}
            _cache.set("meta-scoped:" + data_version.get(), body, CACHE_TTL)
        return JSONResponse(body)
    return respond("meta", None, "", build)


@router.get("/kpis")
def get_kpis(dateFrom: Optional[str] = None, dateTo: Optional[str] = None,
             branch: Optional[str] = None, channel: Optional[str] = None):
    """Sales, Nett Sales, Bills, Avg Ticket vs the previous period + daily values (sparklines)."""
    return endpoint("kpis", "", build_kpis, dateFrom, dateTo, branch, channel)


@router.get("/trend")
def get_trend(dateFrom: Optional[str] = None, dateTo: Optional[str] = None, branch: Optional[str] = None,
              channel: Optional[str] = None, granularity: Optional[str] = None):
    """Sales per day/week/month with the previous period shifted onto the same timeline."""
    def build(f):
        return build_trend(f, resolve_granularity(granularity, f.days))
    return endpoint("trend", granularity or "", build, dateFrom, dateTo, branch, channel)


@router.get("/channels")
def get_channels(dateFrom: Optional[str] = None, dateTo: Optional[str] = None, branch: Optional[str] = None,
                 channel: Optional[str] = None, granularity: Optional[str] = None):
    """Per channel: bills, sales, share, avg ticket, discount %, growth; plus a per-bucket mix."""
    def build(f):
        return build_channels(f, resolve_granularity(granularity, f.days))
    return endpoint("channels", granularity or "", build, dateFrom, dateTo, branch, channel)


@router.get("/branches")
def get_branches(dateFrom: Optional[str] = None, dateTo: Optional[str] = None, branch: Optional[str] = None,
                 channel: Optional[str] = None, granularity: Optional[str] = None):
    """Branch leaderboard: sales, bills, avg ticket, growth, void rate and a sparkline."""
    def build(f):
        return build_branches(f, resolve_granularity(granularity, f.days))
    return endpoint("branches", granularity or "", build, dateFrom, dateTo, branch, channel)


@router.get("/hourly")
def get_hourly(dateFrom: Optional[str] = None, dateTo: Optional[str] = None,
               branch: Optional[str] = None, channel: Optional[str] = None):
    """Day of week (1 = Monday) x hour of salesDateIn: average bills and sales per day."""
    return endpoint("hourly", "", build_hourly, dateFrom, dateTo, branch, channel)


@router.get("/menus")
def get_menus(dateFrom: Optional[str] = None, dateTo: Optional[str] = None, branch: Optional[str] = None,
              channel: Optional[str] = None, limit: int = Query(10, ge=1, le=2000), sort: str = "subtotal"):
    """Top menus (by subtotal or qty), category mix and add-on preferences."""
    def build(f):
        return build_menus(f, limit, sort)
    return endpoint("menus", f"{limit}:{sort}", build, dateFrom, dateTo, branch, channel)


@router.get("/deductions")
def get_deductions(dateFrom: Optional[str] = None, dateTo: Optional[str] = None,
                   branch: Optional[str] = None, channel: Optional[str] = None):
    """Void/Cancelled, Other Cost (CUPPING, WASTE, ...) and open bills per day, branch and method."""
    return endpoint("deductions", "", build_deductions, dateFrom, dateTo, branch, channel)


@router.get("/monthly")
def get_monthly(dateFrom: Optional[str] = None, dateTo: Optional[str] = None,
                branch: Optional[str] = None, channel: Optional[str] = None):
    """Months of the selected period with MoM, YoY and same-store growth (per calendar-day averages)."""
    return endpoint("monthly", "", build_monthly, dateFrom, dateTo, branch, channel)


@router.get("/payments")
def get_payments(dateFrom: Optional[str] = None, dateTo: Optional[str] = None,
                 branch: Optional[str] = None, channel: Optional[str] = None):
    """Payment method mix (first payment of each bill)."""
    return endpoint("payments", "", build_payments, dateFrom, dateTo, branch, channel)


@router.get("/basket")
def get_basket(dateFrom: Optional[str] = None, dateTo: Optional[str] = None, branch: Optional[str] = None,
               channel: Optional[str] = None, granularity: Optional[str] = None):
    """Menu lines and qty per bill, food share and food attach rate on beverage bills."""
    def build(f):
        return build_basket(f, resolve_granularity(granularity, f.days))
    return endpoint("basket", granularity or "", build, dateFrom, dateTo, branch, channel)


@router.get("/growth")
def get_growth(dateFrom: Optional[str] = None, dateTo: Optional[str] = None, branch: Optional[str] = None,
               channel: Optional[str] = None, granularity: Optional[str] = None, basis: str = "previous"):
    """Sales growth on subtotal (gross sales): totals, per day/week/month, per branch and channel.
    basis = previous (default) | lastYear (52 weeks back) | sequential (each bucket vs the one before, per day)."""
    if basis not in GROWTH_BASES:
        return JSONResponse({"error": f"basis must be one of {', '.join(GROWTH_BASES)}"}, status_code=400)

    def build(f):
        return build_growth(f, resolve_granularity(granularity, f.days), basis)
    return endpoint("growth", f"{granularity or ''}:{basis}", build, dateFrom, dateTo, branch, channel)


def drill(name: str, extra: str, build: Callable[[Filters], dict], dateFrom, dateTo, branch, channel):
    """endpoint() that also turns BadRequest raised while building into a 400."""
    try:
        f = parse_filters(dateFrom, dateTo, branch, channel)
        return respond(name, f, extra, lambda: {"filters": f.describe(), **build(f)})
    except BadRequest as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@router.get("/hourly-compare")
def get_hourly_compare(dateFrom: Optional[str] = None, dateTo: Optional[str] = None, branch: Optional[str] = None,
                       channel: Optional[str] = None, mode: str = "period", compareFrom: Optional[str] = None,
                       compareTo: Optional[str] = None, compareBranches: Optional[str] = None):
    """Busy hours compared: mode=period (vs compareFrom..compareTo, default the previous period) or
    mode=branches (compareBranches, default the selected branches or the 5 busiest; max 8)."""
    mode = "branches" if mode == "branches" else "period"
    return drill("hourly-compare", f"{mode}:{compareFrom}:{compareTo}:{scoped_branch(compareBranches) if compareBranches else ''}",
                 lambda f: build_hourly_compare(f, mode, compareFrom, compareTo, compareBranches), dateFrom, dateTo, branch, channel)


@router.get("/breakdown")
def get_breakdown(by: str = "branch", dateFrom: Optional[str] = None, dateTo: Optional[str] = None,
                  branch: Optional[str] = None, channel: Optional[str] = None, paymentMethod: Optional[str] = None,
                  txType: Optional[str] = None):
    """Sales split by branch | channel | payment | paymentType | date | type (txType: sales, void, other_cost, open, all)."""
    if by not in BREAKDOWN:
        return JSONResponse({"error": f"by must be one of {', '.join(BREAKDOWN)}"}, status_code=400)
    if txType not in (None, "sales", "void", "other_cost", "open", "all"):
        return JSONResponse({"error": "invalid txType"}, status_code=400)
    return drill("breakdown", f"{by}:{paymentMethod}:{txType}",
                 lambda f: build_breakdown(f, by, paymentMethod, txType), dateFrom, dateTo, branch, channel)


@router.get("/menu-detail")
def get_menu_detail(menuId: str, kind: str = "menu", dateFrom: Optional[str] = None, dateTo: Optional[str] = None,
                    branch: Optional[str] = None, channel: Optional[str] = None):
    """One menu (or add-on): qty and sales per day (per month above 92 days), branch and channel, vs the previous period."""
    if kind not in ("menu", "package", "extra"):
        return JSONResponse({"error": "invalid kind"}, status_code=400)
    return drill("menu-detail", f"{menuId}:{kind}", lambda f: build_menu_detail(f, menuId, kind), dateFrom, dateTo, branch, channel)
