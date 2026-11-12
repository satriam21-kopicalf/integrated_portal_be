"""Calf On The Road in the Dashboard (2026-11-13): the sales of a period against a comparison period, as the Dashboard
of Kopi Calf / Wasgee shows theirs — for the COTR tab of the Dashboard (any account opening the Dashboard) and the
COTR dashboard.

    cups / value   the cups sold per pool, day and menu from the Product stock of the pools (cotr_ledger.run: the
                   workbook, else ESB COTR, else the driver app — one source a day), valued at the menu price
    payments       cash and QRIS of the drivers' days (Report Armada: the app shifts, else the workbooks)
    drivers        cups and value per driver (Report Armada)
    waste, cupping of the pools and the drivers
The comparison defaults to the same weekdays whole weeks before. Kept warm per period (app/utils.Fresh, CACHE_S).
"""
from datetime import date, timedelta
from typing import Optional

from app import database as db
from app.cotr import P
from app.utils import Fresh

CACHE_S = 120
MAX_DAYS = 92
_views: dict[tuple, Fresh] = {}


def default_compare(d_from: date, d_to: date) -> tuple[date, date]:
    """The same weekdays, whole weeks before (a day against the same weekday last week)."""
    days = (d_to - d_from).days + 1
    back = timedelta(days=-(-days // 7) * 7)
    return d_from - back, d_to - back


def _figures(d_from: date, d_to: date) -> dict:
    from app import cotr_ledger
    menus = {m["code"]: m for m in db.fetch(f"SELECT code, name, price, category FROM {P}.cotr_menu")}
    names = {r["code"]: r["name"] for r in db.fetch(f"SELECT code, name FROM {P}.cotr_pool")}
    price = lambda c: float((menus.get(c) or {}).get("price") or 0)  # noqa: E731
    by_day: dict[str, dict] = {}
    by_pool: dict[str, dict] = {}
    by_menu: dict[str, dict] = {}
    waste = cupping = 0
    pools = [r["code"] for r in db.fetch(f"SELECT DISTINCT pool_code AS code FROM {P}.cotr_pool_count ORDER BY 1")]
    for pool in pools:
        for d in cotr_ledger.run(pool, d_from, d_to)["days"]:
            if d["source"] == "opening" or d["day"] < d_from.isoformat():
                continue
            day = by_day.setdefault(d["day"], {"day": d["day"], "cups": 0, "value": 0.0})
            p = by_pool.setdefault(pool, {"pool": pool, "name": names.get(pool, pool), "cups": 0, "value": 0.0, "days": 0})
            p["days"] += 1
            for code, r in d["rows"].items():
                n = int(r.get("sold") or 0)
                waste += int(r.get("waste") or 0)
                cupping += int(r.get("cupping") or 0)
                if not n:
                    continue
                v = n * price(code)
                day["cups"] += n
                day["value"] += v
                p["cups"] += n
                p["value"] += v
                m = by_menu.setdefault(code, {"code": code, "name": (menus.get(code) or {}).get("name", code),
                                              "category": (menus.get(code) or {}).get("category"), "cups": 0, "value": 0.0})
                m["cups"] += n
                m["value"] += v
    armada = cotr_ledger.report_armada(None, d_from, d_to)
    cash = qris = 0.0
    drivers = []
    for dr in armada["drivers"]:
        cups = value = 0
        for x in dr["days"]:
            cash += float(x.get("cash") or 0)
            qris += float(x.get("qris") or 0)
            cups += sum(int(v.get("t") or 0) for v in x["lines"].values())
            value += float(x.get("total") or 0)
        if cups or value:
            drivers.append({"code": dr["code"], "name": dr["name"], "pool": dr["pool"], "poolName": names.get(dr["pool"], dr["pool"]),
                            "cups": cups, "value": round(value, 2), "days": len(dr["days"])})
    drivers.sort(key=lambda x: (-x["cups"], x["name"] or ""))
    cups = sum(x["cups"] for x in by_day.values())
    value = sum(x["value"] for x in by_day.values())
    days_with = sum(1 for x in by_day.values() if x["cups"])
    return {"cups": cups, "value": round(value, 2), "days": days_with, "waste": waste, "cupping": cupping,
            "cash": round(cash, 2), "qris": round(qris, 2), "drivers": drivers,
            "perDay": sorted(by_day.values(), key=lambda x: x["day"]), "perPool": by_pool, "perMenu": by_menu}


def _overview(d_from: date, d_to: date, c_from: date, c_to: date) -> dict:
    now, before = _figures(d_from, d_to), _figures(c_from, c_to)

    def change(a: float, b: float) -> Optional[float]:
        return round((a - b) / b, 4) if b else None
    pools = []
    for code, p in now["perPool"].items():
        prev = before["perPool"].get(code, {})
        pools.append({**p, "value": round(p["value"], 2), "share": round(p["value"] / now["value"], 4) if now["value"] else None,
                      "change": change(p["cups"], prev.get("cups", 0)), "previousCups": prev.get("cups", 0)})
    pools.sort(key=lambda x: -x["cups"])
    menus = sorted(({**m, "value": round(m["value"], 2), "share": round(m["value"] / now["value"], 4) if now["value"] else None,
                     "change": change(m["cups"], (before["perMenu"].get(code) or {}).get("cups", 0))}
                    for code, m in now["perMenu"].items()), key=lambda x: -x["cups"])
    prev_days = before["perDay"]
    trend = [{"day": x["day"], "cups": x["cups"], "value": round(x["value"], 2),
              "previousCups": prev_days[i]["cups"] if i < len(prev_days) else None,
              "previousValue": round(prev_days[i]["value"], 2) if i < len(prev_days) else None}
             for i, x in enumerate(now["perDay"])]
    s = {k: now[k] for k in ("cups", "value", "days", "waste", "cupping", "cash", "qris")}
    s["perDay"] = round(now["cups"] / now["days"], 1) if now["days"] else None
    s["drivers"] = len(now["drivers"])
    s["avgPrice"] = round(now["value"] / now["cups"], 0) if now["cups"] else None
    p = {k: before[k] for k in ("cups", "value", "days", "waste", "cupping", "cash", "qris")}
    p["drivers"] = len(before["drivers"])
    return {"period": {"from": d_from.isoformat(), "to": d_to.isoformat()}, "compare": {"from": c_from.isoformat(), "to": c_to.isoformat()},
            "summary": s, "previous": p,
            "changes": {k: change(s[k] or 0, p[k] or 0) for k in ("cups", "value", "drivers", "waste", "cupping")},
            "trend": trend, "pools": pools, "menus": menus, "drivers": now["drivers"][:15]}


def overview(d_from: date, d_to: date, c_from: Optional[date] = None, c_to: Optional[date] = None) -> dict:
    if c_from is None or c_to is None:
        c_from, c_to = default_compare(d_from, d_to)
    key = (d_from, d_to, c_from, c_to)
    f = _views.get(key)
    if f is None:
        if len(_views) > 60:
            _views.clear()
        f = _views[key] = Fresh(lambda: _overview(d_from, d_to, c_from, c_to), CACHE_S)
    return f.get()
