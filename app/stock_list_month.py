"""Monitoring Stock → Stock List per month (Cost Control, 2026-11-19): one row per branch × item.

  Previous Balance  the stock opname at the end of the month before (29 / 30 / 31; portal count, else the ESB document)
  Ending Stock      the latest checkpoint count of the month (7 / 14 / 21 / 28 / month end), up to today
  window            the day after Previous Balance .. the Ending Stock day (no checkpoint yet: up to today / month end)
  Items In          Goods Receipt, Transfer In, Transit In (entered by Cost Control, app/stock_transit.py)
  Items Out         POS Sales, POS Non Sales (recipe cards), Transfer Out, Transit Out, Item Journal (approved waste),
                    Product Sales (waste of Premix Tumpah / Human Error)
  other             the other ESB moves (outlet purchase, production, delivery, supplier return): no column, inside the
                    Estimate Stock so the sum still adds up
  Estimate Stock    Previous + In + other − Out
  System Usage      POS Sales + POS Non Sales
  POS Sales         usage = "esb" (2026-11-24): the material usage ESB itself books per outlet and day (pos_material_usage,
                    the "POS Sales" lines of the ESB Stock Card), so Previous (portal SO) + receipts (portal) − this usage
                    follows the ESB stock; the days ESB has not made yet (today) are taken from the recipe cards.
                    usage = "recipe": the recipe cards alone. POS Non Sales (ESB bills CUPPING, WASTE …: ESB takes no
                    stock for them) and the waste of the portal stay apart in both.
  Actual Usage      Previous + In + other − Transfer Out − Transit Out − Item Journal − Product Sales − Ending
  Variance          Actual − System (= Estimate − Ending), % of System"""
from datetime import date, timedelta
from typing import Optional

from app.utils import TTLCache

CACHE_S = 300
_cache = TTLCache()
IN_KINDS = ("received", "transferIn")


def month_bounds(month: str) -> tuple[date, date]:
    """'2026-11' → (1 Nov, 30 Nov)."""
    first = date.fromisoformat(month + "-01")
    nxt = first.replace(day=28) + timedelta(days=4)
    return first, nxt - timedelta(days=nxt.day)


def figures(prev: float, moves: dict[str, float], transit_in: float, transit_out: float, sales: float, non_sales: float,
            journal: float, product: float, ending: Optional[float]) -> dict:
    """The columns of one row (pure). `moves` are signed (received / transferIn > 0, transferOut < 0, other ±);
    `journal` is the whole approved waste with `product` inside it."""
    received = moves.get("received", 0.0)
    t_in = moves.get("transferIn", 0.0)
    t_out = -moves.get("transferOut", 0.0)
    other = sum(q for k, q in moves.items() if k not in ("received", "transferIn", "transferOut"))
    item_journal = journal - product
    system = sales + non_sales
    before_use = prev + received + t_in + transit_in + other - t_out - transit_out - item_journal - product
    estimate = before_use - system
    actual = before_use - ending if ending is not None else None
    variance = actual - system if actual is not None else None
    r = lambda v: round(v, 3) if v is not None else None  # noqa: E731
    return {"prev": r(prev), "received": r(received), "transferIn": r(t_in), "transitIn": r(transit_in), "other": r(other),
            "sales": r(sales), "nonSales": r(non_sales), "transferOut": r(t_out), "transitOut": r(transit_out),
            "journal": r(item_journal), "productSales": r(product), "estimate": r(estimate), "ending": r(ending),
            "system": r(system), "actual": r(actual), "variance": r(variance),
            "variancePct": round(variance / system, 6) if variance is not None and system else None}


def stock_list(month: str, branches: Optional[list[str]], usage: str = "recipe") -> dict:
    key = f"{usage}:{month}:{','.join(sorted(branches)) if branches is not None else '*'}"
    hit = _cache.get(key)
    if hit is None:
        hit = _build(month, branches, usage)
        _cache.set(key, hit, CACHE_S)
    return hit


def clear() -> None:
    _cache.clear()


class _Follower:
    """Dropped with the running stock (a transfer, waste, goods receipt or transit changed something)."""
    def stale(self) -> None:
        clear()

    def reset(self) -> None:
        clear()


def _follow() -> None:
    from app import running_stock
    running_stock.follow(_Follower())


_follow()


def esb_usage(windows: dict[str, tuple[date, date]]) -> tuple[dict[str, dict[str, tuple[float, str]]], dict[str, date]]:
    """({branch: {item code: (qty, unit)}}, {branch: the last day ESB has made}): the material usage ESB books from the
    sales of every branch inside its window. A test branch reads the usage of its source branch."""
    from app import database as db
    from app import test_branches
    from app.database import SCHEMA
    if not windows:
        return {}, {}
    source = {b: test_branches.sources().get(b, b) for b in windows}
    a, z = min(w[0] for w in windows.values()), max(w[1] for w in windows.values())
    takers: dict[str, list[str]] = {}
    for b, src in source.items():
        takers.setdefault(src, []).append(b)
    last = {r["company_code"]: r["d"] for r in db.fetch(
        f"SELECT company_code, max(sales_date)::date AS d FROM {SCHEMA}.pos_material_usage GROUP BY 1")}
    company = {r["branch_code"]: r["company_code"] for r in db.fetch(
        f"SELECT DISTINCT branch_code, company_code FROM {SCHEMA}.pos_material_usage WHERE sales_date >= %s AND branch_code = ANY(%s)",
        (a - timedelta(days=31), sorted(takers)))}
    out: dict[str, dict[str, tuple[float, str]]] = {}
    until = {b: last[company[src]] for b, src in source.items() if src in company}
    rows = db.fetch(f"""SELECT branch_code, sales_date::date AS d, product_code, upper(coalesce(unit, '')) AS unit, sum(qty) AS qty
                          FROM {SCHEMA}.pos_material_usage
                         WHERE sales_date BETWEEN %s AND %s AND branch_code = ANY(%s) AND coalesce(product_code, '') <> ''
                         GROUP BY 1, 2, 3, 4""", (a, z, sorted(takers)))
    for r in rows:
        for b in takers.get(r["branch_code"], []):
            if windows[b][0] <= r["d"] <= windows[b][1]:
                mine = out.setdefault(b, {})
                q, u = mine.get(r["product_code"], (0.0, r["unit"]))
                mine[r["product_code"]] = (q + float(r["qty"] or 0), u)
    return out, until


def _to(mc, packs: dict, code: str, qty: float, unit: str, target: str) -> float:
    """A quantity of the ESB usage in the unit of the row (its base unit almost always)."""
    if unit == target:
        return qty
    q = mc.to_unit(qty, unit, target)
    return q if q is not None else 0.0


def _build(month: str, branches: Optional[list[str]], usage: str = "recipe") -> dict:
    from app import menu_costing as mc
    from app import running_stock, stock_moves, stock_transit
    from app import stock_transfer as st
    first, last = month_bounds(month)
    today = date.today()
    upto = min(today, last)
    # Previous Balance: the count closest to the last day of the month before (back to the 29th, never after the 1st)
    prev = mc.stock_at(first - timedelta(days=1), first - timedelta(days=1), branches, back=3)
    prev = {b: v for b, v in prev.items() if date.fromisoformat(v["date"][:10]) <= first}
    # Ending Stock: the latest checkpoint count of the month up to today
    end = mc.portal_counts(upto, branches, back=(upto - first).days, ahead=0) if upto >= first else {}
    end = {b: v for b, v in end.items() if first <= date.fromisoformat(v["date"]) <= upto}
    names = st._names(sorted(set(prev) | set(end)))
    windows: dict[str, tuple[date, date]] = {}
    heads = []
    for b in sorted(set(prev) | set(end), key=lambda x: names.get(x, x)):
        start = date.fromisoformat(prev[b]["date"][:10]) + timedelta(days=1) if b in prev else first
        stop = date.fromisoformat(end[b]["date"]) if b in end else upto
        if stop >= start:
            windows[b] = (start, stop)
        heads.append({"branch": b, "branchName": names.get(b, b),
                      "prevDate": prev[b]["date"][:10] if b in prev else None, "prevRefs": prev[b]["refs"] if b in prev else [],
                      "prevSource": prev[b]["source"] if b in prev else None,
                      "endDate": end[b]["date"] if b in end else None, "endRefs": end[b]["refs"] if b in end else [],
                      "from": start.isoformat(), "to": stop.isoformat()})
    by_window: dict[tuple[date, date], list[str]] = {}
    for b, w in windows.items():
        by_window.setdefault(w, []).append(b)
    moved: dict[str, dict] = {}
    waste: dict[str, dict[str, float]] = {}
    product: dict[str, dict[str, float]] = {}
    used: dict[str, dict[str, dict]] = {}
    given: dict[str, dict[str, dict]] = {}
    for (a, z), bs in by_window.items():
        moved.update(stock_moves.moves(a, z, bs))
        waste.update(running_stock._waste({b: a for b in bs}, z, None, product))
        used.update(running_stock.system_usage({b: a for b in bs}, z, None, given))
    esb_used: dict[str, dict[str, tuple[float, str]]] = {}
    tail: dict[str, dict[str, dict]] = {}
    tail_given: dict[str, dict[str, dict]] = {}
    until: dict[str, date] = {}
    if usage == "esb":
        esb_used, until = esb_usage(windows)
        late: dict[tuple[date, date], list[str]] = {}
        for b, (a, z) in windows.items():
            frm = max(a, until[b] + timedelta(days=1)) if b in until else a
            if frm <= z:
                late.setdefault((frm, z), []).append(b)
        for (a, z), bs in late.items():  # the days ESB has not made yet
            tail.update(running_stock.system_usage({b: a for b in bs}, z, None, tail_given))
    transit = stock_transit.totals(windows)
    codes = sorted({c for v in (*prev.values(), *end.values()) for c in v["items"]}
                   | {c for v in used.values() for c in v if not c.startswith(("wip:", "n:", "o:"))} | {c for v in transit.values() for c in v}
                   | {c for v in esb_used.values() for c in v})
    packs = mc.item_units(codes)
    info, price = st.items_info(codes), st.prices(codes, upto)
    rows = []
    for h in heads:
        b = h["branch"]
        p, e = prev.get(b), end.get(b)
        units: dict[str, str] = {}
        for src in (p, e):
            for code, (_, u) in (src or {}).get("items", {}).items():
                units.setdefault(code, u)
        for code, v in used.get(b, {}).items():
            if not code.startswith(("wip:", "n:", "o:")) and v.get("code"):
                units.setdefault(v["code"], str(v.get("unit") or "").upper())
        for code in transit.get(b, {}):
            units.setdefault(code, (packs.get(code) or ("",))[0])
        for code, (_, u) in esb_used.get(b, {}).items():
            units.setdefault(code, u)
        esb = {**((p or {}).get("esb") or {}), **((e or {}).get("esb") or {})}
        for code, unit in sorted(units.items()):
            qa = (mc.to_unit(p["items"][code][0], p["items"][code][1], unit) or 0.0) if p and code in p["items"] else 0.0
            qz = mc.to_unit(e["items"][code][0], e["items"][code][1], unit) if e and code in e["items"] else (0.0 if e else None)
            mv = {}
            for kind, lines in moved.get(b, {}).items():
                q = mc._bought(lines, code, unit, esb.get(code), packs.get(code))
                if q:
                    mv[kind] = q
            u = used.get(b, {}).get(code)
            use = (mc.to_unit(u["qty"], u.get("unit"), unit) or 0.0) if u else 0.0
            g = given.get(b, {}).get(code)
            use_ns = (mc.to_unit(g["qty"], g.get("unit"), unit) or 0.0) if g else 0.0
            tr = transit.get(b, {}).get(code, {"in": [], "out": []})
            t_in = sum(mc.to_unit(q, uu, unit) or 0.0 for q, uu in tr["in"])
            t_out = sum(mc.to_unit(q, uu, unit) or 0.0 for q, uu in tr["out"])
            w, wp = waste.get(b, {}).get(code, 0.0), product.get(b, {}).get(code, 0.0)
            if not (qa or qz or mv or use or w or t_in or t_out or code in esb_used.get(b, {})):
                continue
            sales = use - use_ns
            if usage == "esb":
                eq = esb_used.get(b, {}).get(code)
                sales = (_to(mc, packs, code, eq[0], eq[1], unit)) if eq else 0.0
                t, tg = tail.get(b, {}).get(code), tail_given.get(b, {}).get(code)
                if t:
                    sales += (mc.to_unit(t["qty"], t.get("unit"), unit) or 0.0) - ((mc.to_unit(tg["qty"], tg.get("unit"), unit) or 0.0) if tg else 0.0)
            f = figures(qa or 0.0, mv, t_in, t_out, sales, use_ns, w, wp, qz)
            pr = price.get(code)
            rows.append({"branch": b, "branchName": h["branchName"], "code": code, "name": (info.get(code) or {}).get("name") or code,
                         "category": (info.get(code) or {}).get("category") or "", "unit": unit, **f, "price": pr,
                         "estimateValue": round(f["estimate"] * pr, 2) if pr is not None else None})
    for h in heads:
        mine = [r for r in rows if r["branch"] == h["branch"]]
        h["items"] = len(mine)
        h["estimateValue"] = round(sum(r["estimateValue"] or 0.0 for r in mine), 2)
    for h in heads:
        h["usageUntil"] = until[h["branch"]].isoformat() if h["branch"] in until else None
    return {"usage": usage, "month": month, "from": first.isoformat(), "to": last.isoformat(), "upto": upto.isoformat(),
            "branches": heads, "rows": rows, "estimatedAt": today.isoformat()}
