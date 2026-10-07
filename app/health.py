"""Company health: coffee-chain KPIs over the whole ESB history (GET /api/overview/health).

Network level, independent of the dashboard filters. The last 28 complete days (ending yesterday)
are compared with the same 28 weekdays a year earlier (364 days back) and with the 28 days before;
the monthly trend runs from the first month with complete data (Aug 2025).

Each indicator gets a status against a target. The targets are the rules of thumb coffee and quick-
service chains commonly use (same-store growth above inflation, discounts under ~8-12 % of sales,
voids under 1-2 % of bills, food attached to a fifth of beverage orders, few stores far below the
network median); they are guides for this business, not external certifications. Indicators without
a widely used target are shown for context ("info") and do not count in the score. Cost of goods is
left out until the Cost Control figures are final.
"""
from datetime import date, timedelta
from typing import Any, Optional

from app import database as db

PORTAL = "integration_portal"
DAILY = f"{PORTAL}.agg_sales_daily"
HOURLY = f"{PORTAL}.agg_sales_hourly"
MENU = f"{PORTAL}.agg_menu_daily"
ONLINE = ("GrabFood", "ShopeeFood", "GoFood", "Esb Order")
WINDOW = 28
YEAR = 364  # same weekdays a year earlier
FULL_DAYS = 25  # an outlet counts in same-store growth with sales on >= 25 of the 28 days in both windows
SCORE = {"good": 100, "watch": 50, "risk": 0}


def _f(v: Any) -> float:
    return float(v) if v is not None else 0.0


def _pct(a: float, b: float) -> Optional[float]:
    return round((a - b) / b * 100, 2) if b else None


def _share(a: float, b: float) -> Optional[float]:
    return round(a / b * 100, 2) if b else None


def _band_up(v: Optional[float], good: float, watch: float) -> Optional[str]:
    """Higher is better: >= good -> good, >= watch -> watch, else risk."""
    if v is None:
        return None
    return "good" if v >= good else "watch" if v >= watch else "risk"


def _band_down(v: Optional[float], good: float, watch: float) -> Optional[str]:
    """Lower is better: <= good -> good, <= watch -> watch, else risk."""
    if v is None:
        return None
    return "good" if v <= good else "watch" if v <= watch else "risk"


def window_totals(start: date, end: date) -> dict:
    r = db.fetchrow(f"""
        SELECT COALESCE(sum(bills) FILTER (WHERE tx_type = 'sales'), 0)::int AS bills,
               COALESCE(sum(subtotal) FILTER (WHERE tx_type = 'sales'), 0) AS subtotal,
               COALESCE(sum(nett_sales) FILTER (WHERE tx_type = 'sales'), 0) AS nett,
               COALESCE(sum(subtotal) FILTER (WHERE tx_type = 'sales' AND channel = ANY(%(online)s)), 0) AS online,
               COALESCE(sum(bills), 0)::int AS all_bills,
               COALESCE(sum(bills) FILTER (WHERE tx_type = 'void'), 0)::int AS void_bills,
               COALESCE(sum(bills_with_beverage) FILTER (WHERE tx_type = 'sales'), 0)::int AS bev,
               COALESCE(sum(bills_with_both) FILTER (WHERE tx_type = 'sales'), 0)::int AS both
        FROM {DAILY} WHERE sales_date BETWEEN %(s)s AND %(e)s""", {"s": start, "e": end, "online": list(ONLINE)}) or {}
    return {k: _f(v) for k, v in r.items()}


def outlet_rows(start: date, end: date) -> list[dict]:
    return db.fetch(f"""
        SELECT branch_code, count(DISTINCT sales_date)::int AS days, sum(subtotal) AS subtotal, sum(bills)::int AS bills
        FROM {DAILY} WHERE tx_type = 'sales' AND sales_date BETWEEN %s AND %s AND subtotal > 0
        GROUP BY 1""", (start, end))


def day_rows(start: date, end: date) -> list[dict]:
    return db.fetch(f"""SELECT sales_date, sum(subtotal) AS subtotal, sum(bills)::int AS bills FROM {DAILY}
                        WHERE tx_type = 'sales' AND sales_date BETWEEN %s AND %s GROUP BY 1""", (start, end))


def hour_rows(start: date, end: date) -> list[dict]:
    return db.fetch(f"SELECT hour, sum(bills)::int AS bills FROM {HOURLY} WHERE sales_date BETWEEN %s AND %s GROUP BY 1",
                    (start, end))


def menu_totals(start: date, end: date) -> list[dict]:
    return db.fetch(f"""SELECT menu_id, sum(subtotal) AS subtotal FROM {MENU}
                        WHERE kind = 'menu' AND sales_date BETWEEN %s AND %s GROUP BY 1""", (start, end))


def month_rows() -> list[dict]:
    return db.fetch(f"""
        SELECT date_trunc('month', sales_date)::date AS month, sum(subtotal) AS subtotal, sum(bills)::int AS bills,
               count(DISTINCT branch_code)::int AS outlets, count(DISTINCT (branch_code, sales_date))::int AS outlet_days,
               count(DISTINCT sales_date)::int AS days
        FROM {DAILY} WHERE tx_type = 'sales' AND subtotal > 0 GROUP BY 1 ORDER BY 1""")


def _median(values: list[float]) -> Optional[float]:
    s = sorted(values)
    if not s:
        return None
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def build_health(today: date, data_from: date) -> dict:
    end = today - timedelta(days=1)
    start = end - timedelta(days=WINDOW - 1)
    ly_start, ly_end = start - timedelta(days=YEAR), end - timedelta(days=YEAR)
    pv_start, pv_end = start - timedelta(days=WINDOW), start - timedelta(days=1)
    has_ly = ly_start >= data_from

    cur, prev = window_totals(start, end), window_totals(pv_start, pv_end)
    ly = window_totals(ly_start, ly_end) if has_ly else None
    out_cur = {r["branch_code"]: r for r in outlet_rows(start, end)}
    out_ly = {r["branch_code"]: r for r in outlet_rows(ly_start, ly_end)} if has_ly else {}

    indicators: list[dict] = []

    def add(key: str, group: str, label: str, value: Optional[float], unit: str, status: Optional[str], target: str,
            detail: str, **extra: Any) -> None:
        indicators.append({"key": key, "group": group, "label": label, "value": value, "unit": unit,
                           "status": status or "info", "target": target, "detail": detail, **extra})

    # --- growth
    ticket = lambda t: t["subtotal"] / t["bills"] if t and t["bills"] else None  # noqa: E731
    if has_ly:
        g = _pct(cur["subtotal"], ly["subtotal"])
        add("salesYoY", "Growth", "Sales growth vs last year", g, "%", _band_up(g, 5, 0), "≥ +5% (≥ 0% watch)",
            f"Last 28 days Rp {cur['subtotal']:,.0f} vs same 28 weekdays last year Rp {ly['subtotal']:,.0f}, all outlets (new stores included).",
            current=cur["subtotal"], previous=ly["subtotal"])
        same = [c for c, r in out_cur.items() if r["days"] >= FULL_DAYS and c in out_ly and out_ly[c]["days"] >= FULL_DAYS]
        s_cur = sum(_f(out_cur[c]["subtotal"]) for c in same)
        s_ly = sum(_f(out_ly[c]["subtotal"]) for c in same)
        b_cur = sum(int(out_cur[c]["bills"]) for c in same)
        b_ly = sum(int(out_ly[c]["bills"]) for c in same)
        sssg = _pct(s_cur, s_ly)
        add("sssg", "Growth", "Same-store sales growth (SSSG)", sssg, "%", _band_up(sssg, 3, 0), "≥ +3% (≥ 0% watch)",
            f"{len(same)} outlets open the full 28 days in both years: Rp {s_cur:,.0f} vs Rp {s_ly:,.0f}. The core health measure of a chain: growth without new stores.",
            outlets=len(same))
        traffic = _pct(b_cur, b_ly)
        add("sssTraffic", "Growth", "Same-store transactions (traffic)", traffic, "%", _band_up(traffic, 0, -3), "≥ 0% (≥ −3% watch)",
            f"Bills of the same {len(same)} outlets: {b_cur:,} vs {b_ly:,}. Traffic shows whether customers come back.")
        tk_c, tk_l = (s_cur / b_cur if b_cur else None), (s_ly / b_ly if b_ly else None)
        tk = _pct(tk_c or 0, tk_l or 0) if tk_c and tk_l else None
        add("ticketYoY", "Growth", "Average ticket vs last year", tk, "%", _band_up(tk, 2, 0), "≥ +2% (keeps pace with inflation)",
            f"Same-store average ticket Rp {tk_c or 0:,.0f} vs Rp {tk_l or 0:,.0f}.")
    mom = _pct(cur["subtotal"], prev["subtotal"])
    add("sales28", "Growth", "Sales vs previous 28 days", mom, "%", None, "context",
        f"Rp {cur['subtotal']:,.0f} vs Rp {prev['subtotal']:,.0f} ({pv_start:%d %b} – {pv_end:%d %b}).")

    # --- outlet productivity
    per_day = {c: _f(r["subtotal"]) / r["days"] for c, r in out_cur.items() if r["days"]}
    med = _median(list(per_day.values()))
    weak = [c for c, v in per_day.items() if med and v < 0.5 * med]
    weak_pct = _share(len(weak), len(per_day))
    add("outletDaily", "Outlets", "Average daily sales per outlet", round(med, 0) if med else None, "Rp", None, "context",
        f"Median of {len(per_day)} active outlets, last 28 days; average bills per outlet per day "
        f"{_median([r['bills'] / r['days'] for r in out_cur.values() if r['days']]) or 0:,.0f}.")
    add("weakOutlets", "Outlets", "Outlets below half the network median", weak_pct, "%", _band_down(weak_pct, 10, 20), "≤ 10% of outlets (≤ 20% watch)",
        f"{len(weak)} of {len(per_day)} outlets sell less than half the median per day — candidates for a turnaround plan.",
        outlets=sorted(weak))
    ranked = sorted(per_day.values(), reverse=True)
    top_n = max(1, round(len(ranked) * 0.2))
    top_share = _share(sum(ranked[:top_n]), sum(ranked))
    add("topOutlets", "Outlets", "Sales share of the top 20% outlets", top_share, "%", None, "context",
        f"The best {top_n} outlets make {top_share or 0:.1f}% of sales; a share far above ~40% means the network leans on a few stores.")

    # --- sales quality
    disc = _share(cur["subtotal"] - cur["nett"], cur["subtotal"])
    add("discountRate", "Sales quality", "Discount rate", disc, "%", _band_down(disc, 8, 12), "≤ 8% of sales (≤ 12% watch)",
        "Bill, menu, promotion and voucher discounts as a share of subtotal. Online platform commissions are not in ESB and come on top.")
    void = _share(cur["void_bills"], cur["all_bills"])
    add("voidRate", "Sales quality", "Void & cancel rate", void, "%", _band_down(void, 1, 2), "≤ 1% of bills (≤ 2% watch)",
        f"{int(cur['void_bills']):,} of {int(cur['all_bills']):,} bills voided or cancelled — high values point to errors or misuse at the till.")
    online = _share(cur["online"], cur["subtotal"])
    add("onlineShare", "Sales quality", "Online / delivery share", online, "%", _band_down(online, 40, 55), "≤ 40% (≤ 55% watch)",
        "GoFood, GrabFood, ShopeeFood and ESB online orders. Platform commissions (~20–30%) make a high share costly.")

    # --- menu & basket
    attach = _share(cur["both"], cur["bev"])
    add("foodAttach", "Menu & basket", "Food attach rate", attach, "%", _band_up(attach, 20, 10), "≥ 20% of beverage bills (≥ 10% watch)",
        "Beverage bills that also carry a food item — the main lever for a higher ticket in coffee shops.")
    menus = sorted((_f(r["subtotal"]) for r in menu_totals(start, end)), reverse=True)
    top10 = _share(sum(menus[:10]), sum(menus))
    add("menuTop10", "Menu & basket", "Top 10 menus share of menu sales", top10, "%", None, "context",
        f"{len(menus)} menus sold; a share above ~70% means a narrow menu, below ~40% a long tail worth pruning.")

    # --- demand pattern
    hours = sorted((int(r["bills"]) for r in hour_rows(start, end)), reverse=True)
    peak3 = _share(sum(hours[:3]), sum(hours))
    add("peakHours", "Demand", "Bills in the 3 busiest hours", peak3, "%", None, "context",
        "How much of the day hangs on the peak hours: staffing and stock must be ready then.")
    days = day_rows(start, end)
    wk = [_f(r["subtotal"]) for r in days if r["sales_date"].isoweekday() >= 6]
    wd = [_f(r["subtotal"]) for r in days if r["sales_date"].isoweekday() < 6]
    uplift = _pct(sum(wk) / len(wk), sum(wd) / len(wd)) if wk and wd else None
    add("weekendUplift", "Demand", "Weekend vs weekday sales per day", uplift, "%", None, "context",
        "Average Saturday/Sunday against Monday–Friday.")

    # --- history since the ESB roll-out
    months = []
    for r in month_rows():
        if r["month"] < data_from.replace(day=1):
            continue
        months.append({"month": r["month"].isoformat(), "subtotal": _f(r["subtotal"]), "bills": int(r["bills"]),
                       "outlets": int(r["outlets"]), "days": int(r["days"]),
                       "perOutletDay": round(_f(r["subtotal"]) / r["outlet_days"], 0) if r["outlet_days"] else None,
                       "avgTicket": round(_f(r["subtotal"]) / r["bills"], 0) if r["bills"] else None})

    scored = [i for i in indicators if i["status"] in SCORE]
    score = round(sum(SCORE[i["status"]] for i in scored) / len(scored)) if scored else None
    grade = None if score is None else "healthy" if score >= 75 else "watch" if score >= 50 else "risk"
    return {
        "asOf": end.isoformat(),
        "window": {"from": start.isoformat(), "to": end.isoformat(), "days": WINDOW,
                   "lastYear": {"from": ly_start.isoformat(), "to": ly_end.isoformat()} if has_ly else None,
                   "previous": {"from": pv_start.isoformat(), "to": pv_end.isoformat()}},
        "dataFrom": data_from.isoformat(),
        "score": score, "grade": grade,
        "counts": {s: sum(1 for i in indicators if i["status"] == s) for s in ("good", "watch", "risk", "info")},
        "strengths": [i["label"] for i in indicators if i["status"] == "good"],
        "concerns": [i["label"] for i in indicators if i["status"] in ("watch", "risk")],
        "indicators": indicators,
        "months": months,
        "excluded": "Cost of goods (COGS, usage) is left out until the Cost Control figures are final.",
    }
