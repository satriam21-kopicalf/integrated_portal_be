"""COGS & Usage per outlet (page cc.cogs, 2026-11-07): one row per branch with SO awal and SO akhir in the period.

    sales         gross (ESB subtotal) and nett sales of the branch (agg_sales_daily, sales bills)
    system COGS   what the branch used by the recipe cards (app/running_stock.py system_usage: menus sold, choices,
                  bundles, menus made without a sale, additional items) × price
    actual COGS   SO awal + moves in − moves out − SO akhir (app/menu_costing.py actual_usage, app/stock_moves.py) × price,
                  over the materials counted at both ends
    usage ratio   actual ÷ system over the same materials (like for like): 100 % = used as the cards say
    waste         approved by Cost Control in the period (app/waste.py with_values): the part of actual − system it explains
    final         both counts approved by Cost Control (else the figures may still change)
Prices: the Pricelist valid at the end of the period, else the ESB HPP. The data checks of the branch go with it: SO
coverage of the materials, the SO anomalies (app/menu_costing.py count_anomalies), materials without a price.
"""
from datetime import date, timedelta
from typing import Optional

from app import database as db

P = "integration_portal"
FINAL = ("APPROVED", "EXPORTED")


def _f(v) -> float:
    return float(v) if v is not None else 0.0


def outlets(d_from: date, d_to: date, branches: Optional[list[str]]) -> dict:
    from app import menu_costing as mc
    from app import running_stock, waste
    from app.pic_checkin import _names
    start = mc.stock_at(d_from, d_from - timedelta(days=1), branches)
    end = mc.stock_at(d_to + timedelta(days=1), d_to, branches)
    covered = sorted(b for b in start if b in end)
    if not covered:
        return {"period": {"from": d_from.isoformat(), "to": d_to.isoformat()}, "data": [], "total": None,
                "counts": {"start": len(start), "end": len(end)}}
    used = running_stock.system_usage({b: d_from for b in covered}, d_to)
    units = {v["code"]: str(v.get("unit") or "").upper() for u in used.values() for v in u.values() if v.get("code")}
    actual = mc.actual_usage(d_from, d_to, covered, units)
    codes = sorted(units)
    price = mc.prices_on(d_to, codes or None)
    hpp = mc.esb_hpp(d_to) if any(c not in price for c in codes) else {}
    price.update({c: hpp[c][0] for c in codes if c not in price and c in hpp})
    # the counts approved or not (both ends)
    refs = {b: (start[b]["refs"], end[b]["refs"], start[b]["source"], end[b]["source"]) for b in covered}
    numbers = sorted({r for a, z, _, _ in refs.values() for r in a + z})
    status = {r["number"]: r["status"] for r in db.fetch("SELECT number, status FROM kc_inventory.stock_count WHERE number = ANY(%s)", (numbers,))}
    from app import test_branches
    src = {b: test_branches.sources().get(b, b) for b in covered}  # a test branch sells what its source sells
    by_src = {r["branch_code"]: r for r in db.fetch(
        f"""SELECT branch_code, sum(subtotal) AS gross, sum(nett_sales) AS nett, sum(bills)::int AS bills
              FROM {P}.agg_sales_daily WHERE tx_type = 'sales' AND sales_date BETWEEN %s AND %s AND branch_code = ANY(%s)
             GROUP BY 1""", (d_from, d_to, sorted(set(src.values()))))}
    sales = {b: by_src[s] for b, s in src.items() if s in by_src}
    wrows = db.fetch(f"""SELECT {waste.LIGHT} FROM {waste.T} WHERE status = 'approved' AND business_date BETWEEN %s AND %s AND branch_code = ANY(%s)""",
                     (d_from, d_to, covered))
    wval: dict[str, float] = {}
    for v in waste.with_values(wrows):
        wval[v["branchCode"]] = wval.get(v["branchCode"], 0.0) + (v.get("value") or 0.0)
    per: dict[str, list[dict]] = {}
    for r in actual.get("perBranch", []):
        per.setdefault(r["branch"], []).append(r)
    names = _names(covered)
    rows = []
    for b in covered:
        sys_items = used.get(b, {})
        system_value = sum(v["qty"] * price[v["code"]] for v in sys_items.values() if v.get("code") in price)
        unpriced = sorted({v["name"] for v in sys_items.values() if v.get("code") and v["code"] not in price})
        counted = {r["code"]: r for r in per.get(b, [])}
        like_sys = like_act = 0.0
        for code, r in counted.items():
            p = price.get(code)
            s = sys_items.get(code)
            if p is None or not s:
                continue
            like_sys += s["qty"] * p
            like_act += r["actual"] * p
        materials = [v for v in sys_items.values() if v.get("code")]
        cov = sum(1 for v in materials if v["code"] in counted)
        usage = {code: {"name": v["name"], "price": price.get(code)} for code, v in ((v["code"], v) for v in materials)}
        anomalies = mc.count_anomalies(per.get(b, []), usage, limit=1000)
        s = sales.get(b) or {}
        gross, nett = _f(s.get("gross")), _f(s.get("nett"))
        a, z, sa, sz = refs[b]
        final = sa == "portal" and sz == "portal" and all(status.get(n) in FINAL for n in a + z)
        rows.append({
            "branch": b, "branchName": names.get(b, b), "gross": round(gross, 2), "nett": round(nett, 2), "bills": int(s.get("bills") or 0),
            "systemCogs": round(system_value, 2), "actualCogs": round(like_act + (system_value - like_sys), 2),
            "systemRatio": round(system_value / nett, 6) if nett else None,
            "actualRatio": round((like_act + system_value - like_sys) / nett, 6) if nett else None,
            "usageRatio": round(like_act / like_sys, 6) if like_sys else None,
            "likeSystem": round(like_sys, 2), "likeActual": round(like_act, 2),
            "difference": round(like_act - like_sys, 2), "waste": round(wval.get(b, 0.0), 2),
            "unexplained": round(like_act - like_sys - wval.get(b, 0.0), 2),
            "coverage": {"materials": len(materials), "counted": cov}, "anomalies": len(anomalies),
            "anomalyValue": round(sum(x["impact"] for x in anomalies), 2), "unpriced": unpriced[:20],
            "final": final, "so": {"start": {"date": start[b]["date"], "refs": a, "source": sa}, "end": {"date": end[b]["date"], "refs": z, "source": sz}},
        })
    rows.sort(key=lambda r: -(r["usageRatio"] or 0))
    total = {
        "branches": len(rows), "final": sum(1 for r in rows if r["final"]),
        **{k: round(sum(r[k] for r in rows), 2) for k in ("gross", "nett", "systemCogs", "actualCogs", "likeSystem", "likeActual",
                                                         "difference", "waste", "unexplained", "anomalyValue")},
        "anomalies": sum(r["anomalies"] for r in rows),
    }
    nett = total["nett"]
    total["systemRatio"] = round(total["systemCogs"] / nett, 6) if nett else None
    total["actualRatio"] = round(total["actualCogs"] / nett, 6) if nett else None
    total["usageRatio"] = round(total["likeActual"] / total["likeSystem"], 6) if total["likeSystem"] else None
    return {"period": {"from": d_from.isoformat(), "to": d_to.isoformat()}, "data": rows, "total": total,
            "counts": {"start": len(start), "end": len(end)}}
