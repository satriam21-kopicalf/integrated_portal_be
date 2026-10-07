"""Company health: the current condition of the business from all ESB data (GET /api/overview/health).

Every figure covers the whole ESB history, from the first complete day (Aug 2025) to yesterday,
network level and independent of the dashboard filters; nothing is compared with an earlier
period. Indicators with a common coffee / quick-service chain rule of thumb get a status
(discount rate, voids, online share, food attach, weak outlets); the others describe the business.
Cost of goods is left out until the Cost Control figures are final.
"""
from datetime import date, timedelta
from typing import Any, Optional

from app import database as db

PORTAL = "integration_portal"
DAILY = f"{PORTAL}.agg_sales_daily"
MENU_MONTHLY = f"{PORTAL}.agg_menu_monthly"
HOURLY_MONTHLY = f"{PORTAL}.agg_hourly_monthly"
ONLINE = ("GrabFood", "ShopeeFood", "GoFood", "Esb Order")
ACTIVE_DAYS = 7  # an outlet is active when it sold in the last 7 days
SCORE = {"good": 100, "watch": 50, "risk": 0}


def _f(v: Any) -> float:
    return float(v) if v is not None else 0.0


def _share(a: float, b: float) -> Optional[float]:
    return round(a / b * 100, 2) if b else None


def _band_up(v: Optional[float], good: float, watch: float) -> Optional[str]:
    if v is None:
        return None
    return "good" if v >= good else "watch" if v >= watch else "risk"


def _band_down(v: Optional[float], good: float, watch: float) -> Optional[str]:
    if v is None:
        return None
    return "good" if v <= good else "watch" if v <= watch else "risk"


def totals(start: date, end: date) -> dict:
    r = db.fetchrow(f"""
        SELECT COALESCE(sum(bills) FILTER (WHERE tx_type = 'sales'), 0)::bigint AS bills,
               COALESCE(sum(subtotal) FILTER (WHERE tx_type = 'sales'), 0) AS subtotal,
               COALESCE(sum(nett_sales) FILTER (WHERE tx_type = 'sales'), 0) AS nett,
               COALESCE(sum(subtotal) FILTER (WHERE tx_type = 'sales' AND channel = ANY(%(online)s)), 0) AS online,
               COALESCE(sum(bills), 0)::bigint AS all_bills,
               COALESCE(sum(bills) FILTER (WHERE tx_type = 'void'), 0)::bigint AS void_bills,
               COALESCE(sum(bills_with_beverage) FILTER (WHERE tx_type = 'sales'), 0)::bigint AS bev,
               COALESCE(sum(bills_with_both) FILTER (WHERE tx_type = 'sales'), 0)::bigint AS both
        FROM {DAILY} WHERE sales_date BETWEEN %(s)s AND %(e)s""", {"s": start, "e": end, "online": list(ONLINE)}) or {}
    return {k: _f(v) for k, v in r.items()}


def outlet_rows(start: date, end: date) -> list[dict]:
    return db.fetch(f"""
        SELECT branch_code, count(DISTINCT sales_date)::int AS days, sum(subtotal) AS subtotal, sum(bills)::int AS bills,
               max(sales_date) AS last_day
        FROM {DAILY} WHERE tx_type = 'sales' AND sales_date BETWEEN %s AND %s AND subtotal > 0
        GROUP BY 1""", (start, end))


def day_rows(start: date, end: date) -> list[dict]:
    return db.fetch(f"""SELECT sales_date, sum(subtotal) AS subtotal FROM {DAILY}
                        WHERE tx_type = 'sales' AND sales_date BETWEEN %s AND %s GROUP BY 1""", (start, end))


def hour_rows(start: date, end: date) -> list[dict]:
    return db.fetch(f"SELECT hour, sum(bills)::bigint AS bills FROM {HOURLY_MONTHLY} WHERE month BETWEEN %s AND %s GROUP BY 1",
                    (start.replace(day=1), end))


def menu_totals(start: date, end: date) -> list[dict]:
    return db.fetch(f"""SELECT menu_id, sum(subtotal) AS subtotal FROM {MENU_MONTHLY}
                        WHERE kind = 'menu' AND month BETWEEN %s AND %s GROUP BY 1""", (start.replace(day=1), end))


def _median(values: list[float]) -> Optional[float]:
    s = sorted(values)
    if not s:
        return None
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def build_health(today: date, data_from: date) -> dict:
    end = today - timedelta(days=1)
    start = data_from
    t = totals(start, end)
    outlets = outlet_rows(start, end)
    indicators: list[dict] = []

    def add(key: str, group: str, label: str, value: Optional[float], unit: str, status: Optional[str] = None,
            target: Optional[str] = None, **extra: Any) -> None:
        indicators.append({"key": key, "group": group, "label": label, "value": value, "unit": unit,
                           "status": status or "info", "target": target, **extra})

    # --- the business as a whole
    active = [o for o in outlets if o["last_day"] and o["last_day"] >= end - timedelta(days=ACTIVE_DAYS - 1)]
    outlet_days = sum(o["days"] for o in outlets)
    add("sales", "Business", "Total sales", t["subtotal"], "Rp")
    add("bills", "Business", "Total transactions", t["bills"], "count")
    add("avgTicket", "Business", "Average ticket", round(t["subtotal"] / t["bills"]) if t["bills"] else None, "Rp")
    add("activeOutlets", "Business", "Active outlets", len(active), "count", total=len(outlets))
    add("outletDaily", "Business", "Sales per outlet per day", round(t["subtotal"] / outlet_days) if outlet_days else None, "Rp")
    add("billsPerOutletDay", "Business", "Transactions per outlet per day", round(t["bills"] / outlet_days) if outlet_days else None, "count")

    # --- quality of sales (scored)
    disc = _share(t["subtotal"] - t["nett"], t["subtotal"])
    add("discountRate", "Sales quality", "Discount rate", disc, "%", _band_down(disc, 8, 12), "≤ 8%")
    void = _share(t["void_bills"], t["all_bills"])
    add("voidRate", "Sales quality", "Void & cancel rate", void, "%", _band_down(void, 1, 2), "≤ 1%")
    online = _share(t["online"], t["subtotal"])
    add("onlineShare", "Sales quality", "Online / delivery share", online, "%", _band_down(online, 40, 55), "≤ 40%")
    attach = _share(t["both"], t["bev"])
    add("foodAttach", "Sales quality", "Food attach rate", attach, "%", _band_up(attach, 20, 10), "≥ 20%")

    # --- outlets
    per_day = {o["branch_code"]: _f(o["subtotal"]) / o["days"] for o in active if o["days"]}
    med = _median(list(per_day.values()))
    weak = sorted(c for c, v in per_day.items() if med and v < 0.5 * med)
    weak_pct = _share(len(weak), len(per_day))
    add("weakOutlets", "Outlets", "Outlets below half the median", weak_pct, "%", _band_down(weak_pct, 10, 20), "≤ 10%",
        outlets=weak)
    ranked = sorted(per_day.values(), reverse=True)
    top_n = max(1, round(len(ranked) * 0.2)) if ranked else 0
    add("topOutlets", "Outlets", "Sales share of the top 20% outlets", _share(sum(ranked[:top_n]), sum(ranked)), "%")

    # --- menu & demand
    menus = sorted((_f(r["subtotal"]) for r in menu_totals(start, end)), reverse=True)
    add("menuTop10", "Menu & demand", "Top 10 menus share of menu sales", _share(sum(menus[:10]), sum(menus)), "%")
    hours = sorted((int(r["bills"]) for r in hour_rows(start, end)), reverse=True)
    add("peakHours", "Menu & demand", "Transactions in the 3 busiest hours", _share(sum(hours[:3]), sum(hours)), "%")
    days = day_rows(start, end)
    wk = [_f(r["subtotal"]) for r in days if r["sales_date"].isoweekday() >= 6]
    wd = [_f(r["subtotal"]) for r in days if r["sales_date"].isoweekday() < 6]
    add("weekendShare", "Menu & demand", "Weekend share of sales", _share(sum(wk), sum(wk) + sum(wd)), "%")

    scored = [i for i in indicators if i["status"] in SCORE]
    score = round(sum(SCORE[i["status"]] for i in scored) / len(scored)) if scored else None
    grade = None if score is None else "healthy" if score >= 75 else "watch" if score >= 50 else "risk"
    return {
        "period": {"from": start.isoformat(), "to": end.isoformat(), "days": (end - start).days + 1},
        "score": score, "grade": grade,
        "counts": {s: sum(1 for i in indicators if i["status"] == s) for s in ("good", "watch", "risk", "info")},
        "indicators": indicators,
    }
