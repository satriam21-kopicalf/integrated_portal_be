"""The daily dashboard of Calf On The Road (recommendation #7, 2026-11-12): one day of every pool / HUB at a glance.

    pools     per pool: cups sold, arrived, waste, cupping, the leftover counted and its balance, the source of the day
              (workbook, ESB or the app, as the Product stock of the pool: cotr_ledger.run) and 14 days of cups sold
    drivers   per driver: cups and sales of the day (the app shifts, else the Report Armada of the workbooks)
    todo      what waits: reloads and requests to decide, area requests, open fleet issues, workbook names without a
              driver, pool-days where the workbook and ESB differ (last 7 days)
The day defaults to the latest day with figures (the workbooks arrive a day or two late).
"""
from datetime import date, timedelta
from typing import Optional

from app import database as db
from app.cotr import P, today

TREND_DAYS = 14


def latest_day() -> date:
    r = db.fetchrow(f"""SELECT greatest((SELECT max(day) FROM {P}.cotr_pool_move WHERE qty <> 0), (SELECT max(day) FROM {P}.cotr_shift),
                                        (SELECT max(day) FROM {P}.cotr_report_day)) AS day""")
    d = (r or {}).get("day")
    return min(d, today()) if d else today()


def dashboard(day: Optional[date]) -> dict:
    from app import cotr_ledger
    day = day or latest_day()
    start = day - timedelta(days=TREND_DAYS - 1)
    names = {r["code"]: r["name"] for r in db.fetch(f"SELECT code, name FROM {P}.cotr_pool")}
    menus = {m["code"]: m for m in db.fetch(f"SELECT code, name, price FROM {P}.cotr_menu")}
    pools, trend = [], {}
    for code in [r["code"] for r in db.fetch(f"SELECT DISTINCT pool_code AS code FROM {P}.cotr_pool_count ORDER BY 1")]:
        run = cotr_ledger.run(code, start, day)
        series = []
        today_row = None
        for d in run["days"]:
            if d["source"] == "opening":
                continue
            sold = sum(r.get("sold") or 0 for r in d["rows"].values())
            series.append({"day": d["day"], "sold": sold})
            trend[d["day"]] = trend.get(d["day"], 0) + sold
            if d["day"] == day.isoformat():
                today_row = d
        if not today_row:
            pools.append({"pool": code, "name": names.get(code, code), "source": None, "series": series})
            continue
        rows = today_row["rows"].values()
        total = lambda k: sum(r.get(k) or 0 for r in rows)  # noqa: E731
        counted = [r for r in rows if r.get("actual") is not None]
        pools.append({"pool": code, "name": names.get(code, code), "source": today_row["source"], "series": series,
                      "sold": total("sold"), "arrived": total("arrived"), "transfer": total("transfer"), "waste": total("waste"),
                      "cupping": total("cupping"), "revenue": sum((r.get("sold") or 0) * float((menus.get(c) or {}).get("price") or 0) for c, r in today_row["rows"].items()), "actual": sum(r["actual"] for r in counted) if counted else None,
                      "balance": sum(r["balance"] or 0 for r in counted) if counted else None, "expired": total("expired"),
                      "menus": [{"code": c, "name": (menus.get(c) or {}).get("name", c), "sold": r.get("sold") or 0, "actual": r.get("actual")}
                                for c, r in today_row["rows"].items() if (r.get("sold") or r.get("actual"))]})
    # per driver: the app shifts, else the workbook (cotr_ledger.report_armada does both)
    armada = cotr_ledger.report_armada(None, day, day)
    drivers = []
    for d in armada["drivers"]:
        for x in d["days"]:
            cups = sum(int(v.get("t") or 0) for v in x["lines"].values())
            if cups or x["total"]:
                drivers.append({"code": d["code"], "name": d["name"], "pool": d["pool"], "poolName": names.get(d["pool"], d["pool"]),
                                "cups": cups, "total": x["total"], "source": "sheet" if x["status"] == "sheet" else "app", "tied": bool(d["code"])})
    drivers.sort(key=lambda x: (-x["cups"], x["name"] or ""))
    week = cotr_ledger.reconcile(None, day - timedelta(days=6), day)["summary"]
    one = lambda q, *a: (db.fetchrow(q, a or None) or {}).get("n") or 0  # noqa: E731
    todo = {
        "reloads": one(f"SELECT count(*)::int AS n FROM {P}.cotr_reload WHERE status = 'requested'"),
        "requests": one(f"SELECT count(*)::int AS n FROM {P}.cotr_request WHERE status = 'sent'"),
        "areaRequests": one(f"SELECT count(*)::int AS n FROM {P}.cotr_area_request WHERE status = 'sent'"),
        "issues": one(f"SELECT count(*)::int AS n FROM {P}.cotr_fleet_issue WHERE status IN ('open', 'in_progress')"),
        "unmatchedNames": len(cotr_ledger.unmatched_labels(None)["unmatched"]),
        "reconcileDiff": week["diff"],
    }
    sold = sum(p.get("sold") or 0 for p in pools)
    revenue = sum(p.get("revenue") or 0 for p in pools)  # cups sold × the menu price
    prev = trend.get((day - timedelta(days=7)).isoformat())
    return {"day": day.isoformat(), "latest": latest_day().isoformat(), "today": today().isoformat(),
            "summary": {"sold": sold, "revenue": revenue, "drivers": len(drivers), "pools": sum(1 for p in pools if p["source"]),
                        "waste": sum(p.get("waste") or 0 for p in pools), "cupping": sum(p.get("cupping") or 0 for p in pools),
                        "balance": sum(p.get("balance") or 0 for p in pools if p.get("balance") is not None),
                        "sameDayLastWeek": prev},
            "trend": [{"day": (start + timedelta(days=i)).isoformat(), "sold": trend.get((start + timedelta(days=i)).isoformat())}
                      for i in range(TREND_DAYS)],
            "pools": pools, "drivers": drivers, "todo": todo}



