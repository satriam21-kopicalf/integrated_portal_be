"""Cost Control: COGS ratio, usage ratio, waste, stock variance and purchase forecast per outlet.

Read from integration_portal.agg_cost_period / agg_cost_item_period (app/cost_control.py).
Periods follow the outlet stock-opname rhythm (days 1-7, 8-14, 15-21, 22-end of month);
a date range selects every period that starts inside it.

Ratios are given on two bases: net sales (after discounts, default) and subtotal.
  theoretical COGS  POS sales x BOM at ESB HPP
  actual COGS       theoretical + other usage (waste, R&D, ...) + manufacturing - stock variance
  variance          stock opname: physical - system (negative = loss); posted in ESB or pending (Draft/New)
  usage ratio       actual / theoretical usage (100% = used exactly what the recipes say)
Statuses (good / warning / serious / critical) use the thresholds in cost_settings.
Network totals, medians and status counts only include locations with POS sales in the period
(bulk-order and other stock locations without sales are listed apart in `withoutSales`).
Implausible lines of unposted opnames are left out of actual COGS (app/cost_control.py) and listed by
  GET /api/cost-control/issues    dateFrom, dateTo, branch

  GET /api/cost-control/meta
  GET /api/cost-control/summary   dateFrom, dateTo, branch
  GET /api/cost-control/trend     dateFrom, dateTo, branch, grain=period|month
  GET /api/cost-control/items     dateFrom, dateTo, branch, limit
  GET /api/cost-control/forecast  branch (items when exactly one)
`branch` is a branch_code or several separated by commas.
  PUT /api/cost-control/settings  (superadmin) {key: {good, warning, serious}}
"""
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from app import database as db
from app.cost_control import IGNORED_STATUSES, POSTED_STATUSES, SUSPECT_FLOOR, SUSPECT_SHARE, period_bounds
from app.database import SCHEMA
from app.routes.auth import require_superadmin
from app.utils import TTLCache, data_version, jsonable, normalize_branch, parse_branches, today

router = APIRouter(prefix="/api/cost-control", tags=["cost-control"])

PORTAL = "integration_portal"
PERIOD = f"{PORTAL}.agg_cost_period"
ITEM = f"{PORTAL}.agg_cost_item_period"
SETTINGS = f"{PORTAL}.cost_settings"
CACHE_TTL = 300
BAND_KEYS = ("cogs_bands", "usage_bands", "variance_bands", "waste_bands")
_cache = TTLCache()

DEFAULT_SETTINGS = {
    "cogs_bands": {"good": 35, "warning": 40, "serious": 45},
    "usage_bands": {"good": 2, "warning": 5, "serious": 10},
    "variance_bands": {"good": 1, "warning": 2, "serious": 3},
    "waste_bands": {"good": 1, "warning": 2, "serious": 3},
    "forecast": {"lookback_days": 28, "safety_days": 2, "trend_cap_pct": 20},
}


class BadRequest(ValueError):
    pass


def _f(v: Any) -> float:
    return float(v) if isinstance(v, Decimal) else float(v or 0)


def pct(a: float, b: float) -> Optional[float]:
    return round(a / b * 100, 2) if b else None


def settings() -> dict:
    out = {k: dict(v) for k, v in DEFAULT_SETTINGS.items()}
    for r in db.fetch(f"SELECT key, value FROM {SETTINGS}"):
        out[r["key"]] = {**out.get(r["key"], {}), **(r["value"] or {})}
    return out


def band(value: Optional[float], bands: dict) -> Optional[str]:
    """good / warning / serious / critical for a value where higher is worse."""
    if value is None:
        return None
    if value <= bands["good"]:
        return "good"
    if value <= bands["warning"]:
        return "warning"
    if value <= bands["serious"]:
        return "serious"
    return "critical"


def _date(value: Optional[str], name: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BadRequest(f"{name} must be YYYY-MM-DD") from exc


def date_range(date_from: Optional[str], date_to: Optional[str]) -> tuple[date, date]:
    """Default: the current month so far (or last month during its first week)."""
    end = _date(date_to, "dateTo") or today() - timedelta(days=1)
    start = _date(date_from, "dateFrom")
    if start is None:
        start = end.replace(day=1)
        if end.day < 8:
            start = (start - timedelta(days=1)).replace(day=1)
    if start > end:
        raise BadRequest("dateFrom must not be after dateTo")
    return period_bounds(start)[0], end


def freshness() -> dict:
    row = db.fetchrow(
        f"SELECT min(period_start) AS first, max(period_end) AS last, max(refreshed_at) AS refreshed FROM {PERIOD}") or {}
    sync = db.fetchrow(f"SELECT last_sync_at FROM {SCHEMA}.sync_metadata WHERE entity_type = 'inventory_valuation'") or {}
    return jsonable({"dataFrom": row.get("first"), "dataTo": row.get("last"), "refreshedAt": row.get("refreshed"),
                     "valuationSyncedAt": sync.get("last_sync_at")})


def cached(name: str, key: str, build) -> JSONResponse:
    fresh = freshness().get("refreshedAt")
    full = f"{name}:{key}:{fresh}:{data_version.get()}"
    body = _cache.get(full)
    if body is None:
        body = build()
        _cache.set(full, body, CACHE_TTL)
    return JSONResponse(body)


def branch_names() -> dict[str, str]:
    rows = db.fetch(
        f"SELECT DISTINCT ON (branch_code) branch_code, branch_name FROM {SCHEMA}.master_branches "
        "WHERE COALESCE(branch_code, '') <> '' ORDER BY branch_code, COALESCE(is_deleted, false), branch_name")
    return {r["branch_code"]: r["branch_name"] for r in rows}


SUM_COLS = ("bills", "subtotal", "net_sales", "other_cost_subtotal", "purchase_value", "transfer_in_value",
            "transfer_out_value", "theoretical_cogs", "other_usage", "manufacturing_net", "posted_variance",
            "pending_variance", "actual_cogs", "excluded_pending_variance", "excluded_pending_lines")


def metrics(r: dict, s: dict) -> dict:
    """Figures + ratios + statuses of one aggregated row (outlet, period or total)."""
    net, sub = _f(r["net_sales"]), _f(r["subtotal"])
    theo, act, other = _f(r["theoretical_cogs"]), _f(r["actual_cogs"]), _f(r["other_usage"])
    variance = _f(r["posted_variance"]) + _f(r["pending_variance"])
    has_opname = int(r.get("opname_count") or 0) > 0 or abs(_f(r["posted_variance"])) > 0.5
    usage = pct(act, theo)
    out = {
        "bills": int(r.get("bills") or 0),
        "netSales": net, "subtotal": sub, "otherCostSubtotal": _f(r.get("other_cost_subtotal")),
        "beginValue": _f(r.get("begin_value")), "endValue": _f(r.get("end_value")),
        "purchases": _f(r["purchase_value"]),
        "transfersIn": _f(r["transfer_in_value"]), "transfersOut": _f(r["transfer_out_value"]),
        "theoreticalCogs": theo, "actualCogs": act, "otherUsage": other,
        "manufacturingNet": _f(r["manufacturing_net"]),
        "postedVariance": _f(r["posted_variance"]), "pendingVariance": _f(r["pending_variance"]), "variance": variance,
        "opnameCount": int(r.get("opname_count") or 0), "pendingOpnameCount": int(r.get("pending_opname_count") or 0),
        "lastOpnameDate": r["last_opname_date"].isoformat() if r.get("last_opname_date") else None,
        "hasOpname": has_opname,
        # implausible lines of unposted opnames, left out of actual COGS (see /issues)
        "excludedPendingVariance": _f(r.get("excluded_pending_variance")),
        "excludedPendingLines": int(_f(r.get("excluded_pending_lines"))),
        "theoreticalPctNet": pct(theo, net), "actualPctNet": pct(act, net),
        "theoreticalPctSubtotal": pct(theo, sub), "actualPctSubtotal": pct(act, sub),
        "wastePctNet": pct(other, net), "wastePctSubtotal": pct(other, sub),
        "variancePctNet": pct(variance, net),
        "usageRatio": usage,
    }
    out["gapPpNet"] = round(out["actualPctNet"] - out["theoreticalPctNet"], 2) if net else None
    out["gapPpSubtotal"] = round(out["actualPctSubtotal"] - out["theoreticalPctSubtotal"], 2) if sub else None
    out["status"] = {
        "cogsNet": band(out["actualPctNet"], s["cogs_bands"]),
        "cogsSubtotal": band(out["actualPctSubtotal"], s["cogs_bands"]),
        "theoreticalNet": band(out["theoreticalPctNet"], s["cogs_bands"]),
        "theoreticalSubtotal": band(out["theoreticalPctSubtotal"], s["cogs_bands"]),
        "usage": band(abs(usage - 100), s["usage_bands"]) if usage is not None and has_opname else None,
        "gapNet": band(abs(out["gapPpNet"]), s["variance_bands"]) if out["gapPpNet"] is not None and has_opname else None,
        "waste": band(out["wastePctNet"], s["waste_bands"]),
    }
    return out


def _sum_rows(rows: list[dict]) -> dict:
    out = {c: sum(_f(r.get(c)) for r in rows) for c in SUM_COLS}
    out["opname_count"] = sum(int(r.get("opname_count") or 0) for r in rows)
    out["pending_opname_count"] = sum(int(r.get("pending_opname_count") or 0) for r in rows)
    dates = [r["last_opname_date"] for r in rows if r.get("last_opname_date")]
    out["last_opname_date"] = max(dates) if dates else None
    return out


def period_rows(start: date, end: date, branch: Optional[str] = None) -> list[dict]:
    where = "period_start BETWEEN %(start)s AND %(end)s"
    params: dict[str, Any] = {"start": start, "end": end}
    if branch:
        where += " AND branch_code = ANY(%(branches)s)"
        params["branches"] = parse_branches(branch)
    return db.fetch(f"SELECT * FROM {PERIOD} WHERE {where} ORDER BY period_start, branch_code", params)


def _first_last(rows: list[dict], branch_rows: dict[str, list[dict]]) -> dict[str, tuple[float, float]]:
    """Begin value of the first and end value of the last period, per outlet."""
    out = {}
    for code, rs in branch_rows.items():
        rs = sorted(rs, key=lambda r: r["period_start"])
        out[code] = (_f(rs[0]["begin_value"]), _f(rs[-1]["end_value"]))
    return out


# ------------------------------------------------------------------ endpoints

@router.get("/meta")
def get_meta():
    s = settings()
    periods = db.fetch(f"SELECT period_start, max(period_end) AS period_end FROM {PERIOD} GROUP BY 1 ORDER BY 1 DESC LIMIT 60")
    return JSONResponse({"settings": s, "freshness": freshness(), "periods": [jsonable(p) for p in periods]})


@router.get("/summary")
def get_summary(dateFrom: Optional[str] = Query(None), dateTo: Optional[str] = Query(None),
                branch: Optional[str] = Query(None)):
    try:
        start, end = date_range(dateFrom, dateTo)
    except BadRequest as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    branch = normalize_branch(branch)

    def build() -> dict:
        s = settings()
        rows = period_rows(start, end, branch)
        names = branch_names()
        by_branch: dict[str, list[dict]] = {}
        for r in rows:
            by_branch.setdefault(r["branch_code"], []).append(r)
        edges = _first_last(rows, by_branch)
        outlets = []
        for code, rs in by_branch.items():
            agg = _sum_rows(rs)
            agg["begin_value"], agg["end_value"] = edges[code]
            m = metrics(agg, s)
            outlets.append({"branchCode": code, "branchName": names.get(code, code), "periods": len(rs), **m})
        outlets.sort(key=lambda o: -o["netSales"])
        # network figures only over locations that sold through the POS (no bulk-order stock without sales)
        selling = {o["branchCode"] for o in outlets if o["netSales"] > 0}
        total = _sum_rows([r for r in rows if r["branch_code"] in selling])
        total["begin_value"] = sum(b for c, (b, _) in edges.items() if c in selling)
        total["end_value"] = sum(e for c, (_, e) in edges.items() if c in selling)
        without_sales = [{"branchCode": o["branchCode"], "branchName": o["branchName"], "actualCogs": o["actualCogs"],
                          "theoreticalCogs": o["theoreticalCogs"]} for o in outlets if o["branchCode"] not in selling]
        active = [o for o in outlets if o["netSales"] > 0]

        def median(key: str) -> Optional[float]:
            vals = sorted(o[key] for o in active if o.get(key) is not None)
            if not vals:
                return None
            n = len(vals)
            return round(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2, 2)

        status_counts: dict[str, int] = {}
        for o in active:
            st = o["status"]["cogsNet"] or "none"
            status_counts[st] = status_counts.get(st, 0) + 1
        periods = sorted({(r["period_start"], r["period_end"]) for r in rows})
        return {
            "filters": {"dateFrom": start.isoformat(), "dateTo": end.isoformat(), "branch": branch},
            "periods": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in periods],
            "settings": s,
            "total": metrics(total, s),
            "medians": {k: median(k) for k in ("actualPctNet", "actualPctSubtotal", "theoreticalPctNet",
                                                "theoreticalPctSubtotal", "usageRatio", "wastePctNet")},
            "statusCounts": status_counts,
            "outlets": outlets,
            "withoutSales": without_sales,
            "freshness": freshness(),
        }

    return cached("summary", f"{start}:{end}:{branch}", build)


@router.get("/trend")
def get_trend(dateFrom: Optional[str] = Query(None), dateTo: Optional[str] = Query(None),
              branch: Optional[str] = Query(None), grain: str = Query("period")):
    try:
        start, end = date_range(dateFrom, dateTo)
    except BadRequest as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    grain = grain if grain in ("period", "month") else "period"
    branch = normalize_branch(branch)

    def build() -> dict:
        s = settings()
        rows = period_rows(start, end, branch)
        buckets: dict[date, list[dict]] = {}
        for r in rows:
            key = r["period_start"] if grain == "period" else r["period_start"].replace(day=1)
            buckets.setdefault(key, []).append(r)
        series = []
        for key in sorted(buckets):
            rs = [r for r in buckets[key] if _f(r["net_sales"]) > 0]  # locations with POS sales only
            agg = _sum_rows(rs)
            agg["begin_value"] = agg["end_value"] = 0
            m = metrics(agg, s)
            series.append({"start": key.isoformat(), "end": max(r["period_end"] for r in rs).isoformat(), **m})
        return {"filters": {"dateFrom": start.isoformat(), "dateTo": end.isoformat(), "branch": branch, "grain": grain},
                "series": series}

    return cached("trend", f"{start}:{end}:{branch}:{grain}", build)


@router.get("/items")
def get_items(dateFrom: Optional[str] = Query(None), dateTo: Optional[str] = Query(None),
              branch: Optional[str] = Query(None), limit: int = Query(300, ge=1, le=2000)):
    try:
        start, end = date_range(dateFrom, dateTo)
    except BadRequest as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    branch = normalize_branch(branch)

    def build() -> dict:
        s = settings()
        where = "period_start BETWEEN %(start)s AND %(end)s"
        params: dict[str, Any] = {"start": start, "end": end, "limit": limit}
        if branch:
            where += " AND branch_code = ANY(%(branches)s)"
            params["branches"] = parse_branches(branch)
        rows = db.fetch(
            f"""SELECT product_id, max(product_code) AS product_code, max(product_name) AS product_name,
                       max(category) AS category, max(base_unit) AS base_unit,
                       sum(purchase_qty) AS purchase_qty, sum(purchase_value) AS purchase_value,
                       sum(theoretical_qty) AS theoretical_qty, sum(theoretical_value) AS theoretical_value,
                       sum(other_qty) AS other_qty, sum(other_value) AS other_value,
                       sum(variance_qty) AS variance_qty, sum(variance_value) AS variance_value,
                       sum(actual_qty) AS actual_qty, sum(actual_value) AS actual_value
                FROM {ITEM} WHERE {where}
                GROUP BY product_id
                HAVING sum(abs(theoretical_value)) + sum(abs(variance_value)) + sum(abs(other_value)) + sum(abs(purchase_value)) > 0
                ORDER BY sum(variance_value) ASC, sum(actual_value) DESC
                LIMIT %(limit)s""", params)
        items = []
        for r in rows:
            theo_q, act_q = _f(r["theoretical_qty"]), _f(r["actual_qty"])
            usage = pct(act_q, theo_q)
            items.append({
                "productId": r["product_id"], "productCode": r["product_code"], "productName": r["product_name"],
                "category": r["category"], "unit": r["base_unit"],
                "purchaseQty": _f(r["purchase_qty"]), "purchaseValue": _f(r["purchase_value"]),
                "theoreticalQty": theo_q, "theoreticalValue": _f(r["theoretical_value"]),
                "otherQty": _f(r["other_qty"]), "otherValue": _f(r["other_value"]),
                "varianceQty": _f(r["variance_qty"]), "varianceValue": _f(r["variance_value"]),
                "actualQty": act_q, "actualValue": _f(r["actual_value"]),
                "usageRatio": usage,
                "status": band(abs(usage - 100), s["usage_bands"]) if usage is not None and abs(_f(r["variance_qty"])) > 0 else None,
            })
        return {"filters": {"dateFrom": start.isoformat(), "dateTo": end.isoformat(), "branch": branch}, "items": items}

    return cached("items", f"{start}:{end}:{branch}:{limit}", build)


# ------------------------------------------------------------------ forecast

HORIZONS = (7, 14, 30)


def _trend_factor(branch: Optional[str], cap_pct: float) -> dict[str, float]:
    """Net sales of the last 14 days vs the 14 before, per outlet (capped)."""
    end = today() - timedelta(days=1)
    rows = db.fetch(
        f"""SELECT branch_code,
                   sum(nett_sales) FILTER (WHERE sales_date > %(mid)s) AS recent,
                   sum(nett_sales) FILTER (WHERE sales_date <= %(mid)s) AS before
            FROM {PORTAL}.agg_sales_daily
            WHERE tx_type = 'sales' AND sales_date BETWEEN %(start)s AND %(end)s
                  {"AND branch_code = ANY(%(branches)s)" if branch else ""}
            GROUP BY 1""",
        {"start": end - timedelta(days=27), "mid": end - timedelta(days=14), "end": end, "branches": parse_branches(branch)})
    cap = cap_pct / 100
    out = {}
    for r in rows:
        recent, before = _f(r["recent"]), _f(r["before"])
        out[r["branch_code"]] = 1 + max(-cap, min(cap, (recent - before) / before)) if before else 1.0
    return out


@router.get("/forecast")
def get_forecast(branch: Optional[str] = Query(None)):
    branch = normalize_branch(branch)
    single = branch is not None and "," not in branch
    """Purchase need and spend for the next 7 / 14 / 30 days, per outlet (and per item for one outlet).

    daily usage  = actual usage of the periods in the look-back window / days (falls back to
                   theoretical + other usage when no opname was taken)
    need (h)     = daily usage x h x sales trend + safety stock (safety_days) - book stock now
    spend (h)    = need x unit cost (book value / qty, else last purchase price, else usage cost)
    """
    def build() -> dict:
        s = settings()
        fc = s["forecast"]
        lookback = int(fc.get("lookback_days", 28))
        safety = float(fc.get("safety_days", 2))
        end = today() - timedelta(days=1)
        first = period_bounds(end - timedelta(days=lookback - 1))[0]
        params: dict[str, Any] = {"first": first, "end": end, "branches": parse_branches(branch)}
        branch_sql = "AND i.branch_code = ANY(%(branches)s)" if branch else ""
        rows = db.fetch(
            f"""WITH p AS (
                    SELECT branch_code, period_start, period_end, (period_end - period_start + 1) AS days, opname_count,
                           posted_variance
                    FROM {PERIOD} WHERE period_start BETWEEN %(first)s AND %(end)s
                ),
                days AS (SELECT branch_code, sum(days)::numeric AS days,
                                bool_or(opname_count > 0 OR abs(posted_variance) > 0.5) AS has_opname,
                                max(period_start) AS last_period
                         FROM p GROUP BY 1)
                SELECT i.branch_code, i.product_id, max(i.product_code) AS product_code, max(i.product_name) AS product_name,
                       max(i.category) AS category, max(i.base_unit) AS base_unit, d.days, d.has_opname,
                       sum(i.actual_qty) AS actual_qty, sum(i.actual_value) AS actual_value,
                       sum(i.theoretical_qty + i.other_qty) AS plan_qty,
                       sum(i.purchase_qty) AS purchase_qty, sum(i.purchase_value) AS purchase_value,
                       max(i.end_qty) FILTER (WHERE i.period_start = d.last_period) AS end_qty,
                       max(i.end_value) FILTER (WHERE i.period_start = d.last_period) AS end_value
                FROM {ITEM} i
                JOIN p ON p.branch_code = i.branch_code AND p.period_start = i.period_start
                JOIN days d ON d.branch_code = i.branch_code
                WHERE 1 = 1 {branch_sql}
                GROUP BY i.branch_code, i.product_id, d.days, d.has_opname""", params)
        trend = _trend_factor(branch, float(fc.get("trend_cap_pct", 20)))
        names = branch_names()
        outlets: dict[str, dict] = {}
        items = []
        for r in rows:
            days = _f(r["days"]) or 1
            actual_q, plan_q = _f(r["actual_qty"]), _f(r["plan_qty"])
            usage_q = actual_q if r["has_opname"] and actual_q > 0 else plan_q
            if usage_q <= 0:
                continue
            daily = usage_q / days
            stock = max(0.0, _f(r["end_qty"]))
            end_qty, end_val = _f(r["end_qty"]), _f(r["end_value"])
            pq, pv = _f(r["purchase_qty"]), _f(r["purchase_value"])
            if end_qty > 0 and end_val > 0:
                unit_cost = end_val / end_qty
            elif pq > 0 and pv > 0:
                unit_cost = pv / pq
            else:
                unit_cost = _f(r["actual_value"]) / actual_q if actual_q > 0 else 0.0
            factor = trend.get(r["branch_code"], 1.0)
            need = {h: max(0.0, daily * h * factor + daily * safety - stock) for h in HORIZONS}
            spend = {h: need[h] * unit_cost for h in HORIZONS}
            o = outlets.setdefault(r["branch_code"], {
                "branchCode": r["branch_code"], "branchName": names.get(r["branch_code"], r["branch_code"]),
                "trendFactor": round(factor, 3), "lookbackDays": int(days),
                "avgWeeklyPurchases": 0.0, **{f"spend{h}": 0.0 for h in HORIZONS}, "items": 0})
            o["avgWeeklyPurchases"] += pv / days * 7
            for h in HORIZONS:
                o[f"spend{h}"] += spend[h]
            o["items"] += 1
            if single:
                items.append({
                    "productId": r["product_id"], "productCode": r["product_code"], "productName": r["product_name"],
                    "category": r["category"], "unit": r["base_unit"], "dailyUsage": round(daily, 4),
                    "stock": round(stock, 4), "unitCost": round(unit_cost, 4), "basedOn": "actual" if usage_q == actual_q else "plan",
                    **{f"need{h}": round(need[h], 4) for h in HORIZONS}, **{f"spend{h}": round(spend[h], 2) for h in HORIZONS},
                })
        out_list = sorted(outlets.values(), key=lambda o: -o["spend30"])
        for o in out_list:
            for k in ("avgWeeklyPurchases", *(f"spend{h}" for h in HORIZONS)):
                o[k] = round(o[k], 2)
        totals = {k: round(sum(o[k] for o in out_list), 2) for k in ("avgWeeklyPurchases", *(f"spend{h}" for h in HORIZONS))}
        items.sort(key=lambda i: -i["spend30"])
        return {"branch": branch, "asOf": end.isoformat(), "horizons": list(HORIZONS), "settings": fc,
                "totals": totals, "outlets": out_list, "items": items}

    return cached("forecast", f"{branch}", build)


@router.put("/settings")
def put_settings(body: dict = Body(...), user: dict = Depends(require_superadmin)):
    updates = {}
    for key, value in body.items():
        if key in BAND_KEYS:
            try:
                good, warning, serious = (float(value[k]) for k in ("good", "warning", "serious"))
            except (KeyError, TypeError, ValueError):
                return JSONResponse({"error": f"{key} perlu good, warning, serious (angka)", "field": key}, status_code=422)
            if not 0 <= good <= warning <= serious <= 100:
                return JSONResponse({"error": f"{key}: harus 0 <= good <= warning <= serious <= 100", "field": key}, status_code=422)
            updates[key] = {"good": good, "warning": warning, "serious": serious}
        elif key == "forecast":
            try:
                updates[key] = {"lookback_days": int(value["lookback_days"]), "safety_days": float(value["safety_days"]),
                                "trend_cap_pct": float(value["trend_cap_pct"])}
            except (KeyError, TypeError, ValueError):
                return JSONResponse({"error": "forecast perlu lookback_days, safety_days, trend_cap_pct", "field": key}, status_code=422)
            if not (7 <= updates[key]["lookback_days"] <= 120 and 0 <= updates[key]["safety_days"] <= 14):
                return JSONResponse({"error": "forecast di luar batas (lookback 7-120 hari, safety 0-14 hari)", "field": key}, status_code=422)
        else:
            return JSONResponse({"error": f"Pengaturan tidak dikenal: {key}", "field": key}, status_code=422)
    with db.transaction() as conn:
        for key, value in updates.items():
            conn.execute(
                f"""INSERT INTO {SETTINGS} (key, value, updated_at, updated_by) VALUES (%s, %s::jsonb, now(), %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
                (key, json.dumps(value), user["username"]))
    _cache.clear()
    return JSONResponse({"settings": settings()})


@router.get("/issues")
def get_issues(dateFrom: Optional[str] = Query(None), dateTo: Optional[str] = Query(None),
               branch: Optional[str] = Query(None)):
    """Data to fix in ESB: implausible lines of unposted opnames (left out of actual COGS), unposted
    opname documents per outlet, and locations with stock usage but no POS sales."""
    try:
        start, end = date_range(dateFrom, dateTo)
    except BadRequest as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    branch = normalize_branch(branch)

    def build() -> dict:
        params: dict[str, Any] = {"start": start, "end": end, "floor": SUSPECT_FLOOR, "share": SUSPECT_SHARE}
        cond = ""
        if branch:
            cond = " AND c.branch_code = ANY(%(branches)s)"
            params["branches"] = parse_branches(branch)
        lines = db.fetch(f"""
            SELECT c.branch_code, d.doc_num, d.doc_date, d.status_name, l.product_id,
                   COALESCE(l.raw_data->>'productName', l.product_id) AS product_name, l.qty AS physical_qty,
                   COALESCE(NULLIF(l.raw_data->>'stockQty', '')::numeric, 0) AS system_qty, COALESCE(l.price, 0) AS hpp,
                   (l.qty - COALESCE(NULLIF(l.raw_data->>'stockQty', '')::numeric, 0)) * COALESCE(l.price, 0) AS variance,
                   c.theoretical_cogs
            FROM {SCHEMA}.erp_documents d
            JOIN {SCHEMA}.erp_document_lines l ON l.module = d.module AND l.doc_num = d.doc_num
            JOIN {PERIOD} c ON c.location_id = d.location_id AND d.doc_date BETWEEN c.period_start AND c.period_end
            WHERE d.module = 'stock_opname' AND d.deleted_at IS NULL
              AND d.status_name NOT IN {POSTED_STATUSES!r} AND d.status_name NOT IN {IGNORED_STATUSES!r}
              AND c.period_start BETWEEN %(start)s AND %(end)s{cond}
              AND abs((l.qty - COALESCE(NULLIF(l.raw_data->>'stockQty', '')::numeric, 0)) * COALESCE(l.price, 0))
                  > GREATEST(%(floor)s, %(share)s * COALESCE(c.theoretical_cogs, 0))
            ORDER BY abs((l.qty - COALESCE(NULLIF(l.raw_data->>'stockQty', '')::numeric, 0)) * COALESCE(l.price, 0)) DESC""", params)
        pending = db.fetch(f"""
            SELECT c.branch_code, d.doc_num, d.doc_date, d.status_name, d.line_count
            FROM {SCHEMA}.erp_documents d
            JOIN {PERIOD} c ON c.location_id = d.location_id AND d.doc_date BETWEEN c.period_start AND c.period_end
            WHERE d.module = 'stock_opname' AND d.deleted_at IS NULL
              AND d.status_name NOT IN {POSTED_STATUSES!r} AND d.status_name NOT IN {IGNORED_STATUSES!r}
              AND c.period_start BETWEEN %(start)s AND %(end)s{cond}
            ORDER BY d.doc_date, c.branch_code""", params)
        names = branch_names()
        rows = period_rows(start, end, branch)
        sales: dict[str, float] = {}
        costs: dict[str, float] = {}
        for r in rows:
            sales[r["branch_code"]] = sales.get(r["branch_code"], 0.0) + _f(r["net_sales"])
            costs[r["branch_code"]] = costs.get(r["branch_code"], 0.0) + _f(r["actual_cogs"])
        return {
            "filters": {"dateFrom": start.isoformat(), "dateTo": end.isoformat(), "branch": branch},
            "rule": {"floor": SUSPECT_FLOOR, "share": SUSPECT_SHARE},
            "suspectLines": [{
                "branchCode": r["branch_code"], "branchName": names.get(r["branch_code"], r["branch_code"]),
                "docNum": r["doc_num"], "docDate": r["doc_date"].isoformat(), "status": r["status_name"],
                "productId": r["product_id"], "productName": r["product_name"], "physicalQty": _f(r["physical_qty"]),
                "systemQty": _f(r["system_qty"]), "hpp": _f(r["hpp"]), "variance": _f(r["variance"]),
                "periodTheoreticalCogs": _f(r["theoretical_cogs"]),
            } for r in lines],
            "pendingOpnames": [{
                "branchCode": r["branch_code"], "branchName": names.get(r["branch_code"], r["branch_code"]),
                "docNum": r["doc_num"], "docDate": r["doc_date"].isoformat(), "status": r["status_name"],
                "lines": int(r["line_count"] or 0),
            } for r in pending],
            "withoutSales": sorted(({"branchCode": c, "branchName": names.get(c, c), "actualCogs": v}
                                    for c, v in costs.items() if sales.get(c, 0) <= 0 and v), key=lambda x: -x["actualCogs"]),
        }

    return cached("issues", f"{start}:{end}:{branch}", build)
