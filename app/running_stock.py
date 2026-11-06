"""Running stock of the outlets (2026-11-07): the stock of each branch now, from its latest stock opname and everything
that moved since — the base of the Stock List (estimate), the Estimasi Belanja (MRP) and the Transfer Stock.

    estimate = SO (latest portal count of the branch, business day D = the stock at the start of D)
             + moves since D (app/stock_moves.py: goods receipts with the Cost Control corrections of GRs marked
               Finished, outlet purchases, production in / out, deliveries, returns, Transfer Stock received)
             − waste approved since D (raw materials in their base unit, premix through its ingredients)
             − the system usage since D (menus sold × recipe cards, the choices of the menus, bundles, the menus made
               without a sale — ESB other-cost bills —, the additional items per bill / portion / outlet-day)

The estimate is as good as the recipe cards and the Pricelist; the SO stays the official figure, and the next count
starts again from its own quantities. `final` tells whether the SO it starts from is approved by Cost Control.
"""
from datetime import date
from typing import Optional

from app import database as db
from app.scope import MAIN_COMPANY
from app.utils import TTLCache

P = "integration_portal"
CACHE_S = 300  # the running stock of every branch takes a few seconds: kept 5 minutes (cleared by a transfer received)
LATEST_SO_DAYS = 60
FINAL = ("APPROVED", "EXPORTED")


def _f(v) -> float:
    return float(v) if v is not None else 0.0


def latest_counts(branches: Optional[list[str]]) -> dict[str, dict]:
    """{branch: {date, refs, items: {code: (qty, unit)}, esb, final}}: the latest portal stock opname of every branch."""
    from app import menu_costing as mc
    counts = mc.portal_counts(date.today(), branches, back=LATEST_SO_DAYS, ahead=0)
    if not counts:
        return counts
    refs = sorted({r for c in counts.values() for r in c["refs"]})
    status = {r["number"]: r["status"] for r in db.fetch("SELECT number, status FROM kc_inventory.stock_count WHERE number = ANY(%s)", (refs,))}
    for c in counts.values():
        c["final"] = all(status.get(r) in FINAL for r in c["refs"])
        c["statuses"] = sorted({status.get(r) or "?" for r in c["refs"]})
    return counts


def _waste(starts: dict[str, date], d_to: date) -> dict[str, dict[str, float]]:
    """{branch: {item code: qty in the base unit}} of the waste approved by Cost Control since the count of the branch."""
    from app import menu_costing as mc
    from app import waste
    if not starts:
        return {}
    rows = db.fetch(f"""SELECT branch_code, business_date, scope, item_code, qty, unit, exploded FROM {waste.T}
                        WHERE status = 'approved' AND business_date BETWEEN %s AND %s AND branch_code = ANY(%s)""",
                    (min(starts.values()), d_to, list(starts)))
    codes = sorted({c for r in rows for c in ([x.get("code") for x in (r["exploded"] or [])] if r["scope"] == "premix" else [r["item_code"]]) if c})
    units = mc.item_units(codes)
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        if r["business_date"] < starts[r["branch_code"]]:
            continue
        mine = out.setdefault(r["branch_code"], {})
        parts = ([(x.get("code"), _f(x.get("grams")), "GR") for x in (r["exploded"] or []) if x.get("isStock", True)]
                 if r["scope"] == "premix" else [(r["item_code"], _f(r["qty"]), r["unit"])])
        for code, qty, unit in parts:
            if not code:
                continue
            base, packs = units.get(code, ("", {}))
            q = mc.to_unit(qty, unit, base) if base else qty
            if q is None and str(unit or "").upper() in packs:
                q = qty * packs[str(unit or "").upper()]
            if q is not None:
                mine[code] = mine.get(code, 0.0) + q
    return out


def system_usage(starts: dict[str, date], d_to: date) -> dict[str, dict[str, dict]]:
    """{branch: {material key: {code, name, unit, qty}}}: what the branch used since its count by the recipe cards (the
    Sales Calculator's system usage, per branch and from each branch's own day)."""
    from app import menu_costing as mc
    from app import menu_options
    if not starts:
        return {}
    from app import test_branches
    branches, d_from = list(starts), min(starts.values())
    source = {b: test_branches.sources().get(b, b) for b in branches}  # a test branch sells what its source sells
    cards = {k: c for k, c in mc.resolve({r["code"]: r for r in mc.recipes()}).items() if c["isActive"]}
    mapped = {m["id"]: code for code, c in cards.items() for m in c["menus"]}
    wip = mc.wips()
    br = {"a": d_from, "b": d_to, "br": sorted(set(source.values())), "main": MAIN_COMPANY}
    company = f"coalesce(br.company, %(main)s)"
    sold = db.fetch(
        f"""WITH br AS ({mc.BRANCH_COMPANY})
            SELECT a.branch_code, a.sales_date, {company} || ':' || a.menu_id AS menu_id, sum(a.qty) AS qty
              FROM {P}.agg_menu_daily a LEFT JOIN br ON br.branch_code = a.branch_code
             WHERE a.sales_date BETWEEN %(a)s AND %(b)s AND a.menu_id <> '?' AND a.branch_code = ANY(%(br)s) GROUP BY 1, 2, 3""", br)
    try:
        menu_options.ensure_days(d_from, d_to)
        choices = db.fetch(
            f"""WITH br AS ({mc.BRANCH_COMPANY})
                SELECT a.branch_code, a.sales_date, {company} || ':' || a.parent_id AS parent, {company} || ':' || a.option_id AS option,
                       max(a.option_name) AS name, sum(a.qty) AS qty
                  FROM {P}.agg_menu_option_daily a LEFT JOIN br ON br.branch_code = a.branch_code
                 WHERE a.sales_date BETWEEN %(a)s AND %(b)s AND a.branch_code = ANY(%(br)s) GROUP BY 1, 2, 3, 4""", br)
        rule = menu_options.rules()
    except Exception:  # noqa: BLE001 - no choices data: the cards alone
        choices, rule = [], {}
    try:
        given = mc.non_sales_by_branch(d_from, d_to, sorted(set(source.values())))
    except Exception:  # noqa: BLE001 - the transactions unreachable: the sales alone
        given = []
    base = db.fetch(f"""SELECT branch_code, sales_date, coalesce(sum(bills) FILTER (WHERE tx_type = 'sales'), 0) AS bills,
                               bool_or(tx_type = 'sales' AND subtotal > 0) AS open
                          FROM {P}.agg_sales_daily WHERE sales_date BETWEEN %(a)s AND %(b)s AND branch_code = ANY(%(br)s)
                         GROUP BY 1, 2""", br)
    extras = [e for e in mc.extras() if e["isActive"]]
    bundles = {m for m, code in mapped.items() if cards[code].get("bundle")}

    by = {b: {"sold": [], "choices": [], "given": [], "base": []} for b in branches}
    takers: dict[str, list[str]] = {}
    for b, s in source.items():
        takers.setdefault(s, []).append(b)
    for key, rows in (("sold", sold), ("choices", choices), ("given", given), ("base", base)):
        for r in rows:
            for b in takers.get(r["branch_code"], []):
                if r["sales_date"] >= starts[b]:
                    by[b][key].append(r)
    out: dict[str, dict[str, dict]] = {}
    for b, d in by.items():
        system: dict[str, dict] = {}
        inside: dict[str, float] = {}
        ch = []
        for c in d["choices"]:
            if c["parent"] in bundles:
                inside[c["option"]] = inside.get(c["option"], 0.0) + _f(c["qty"])
            else:
                ch.append({"parent": c["parent"], "option": c["option"], "name": c["name"], "qty": _f(c["qty"])})
        qty: dict[str, float] = {}
        for s in d["sold"]:
            qty[s["menu_id"]] = qty.get(s["menu_id"], 0.0) + _f(s["qty"])
        portions = 0.0
        for menu, q in qty.items():
            q -= inside.get(menu, 0.0)
            code = mapped.get(menu)
            if code and q > 1e-9:
                mc.explode(cards[code], q, wip, system)
                portions += q
        deltas, rows, _ = menu_options.adjustments(ch, cards, mapped, wip, rule, mc.explode)
        for per in deltas.values():
            for k, dq in per.items():
                row = system.setdefault(k, {**rows[k], "qty": 0.0})
                row["qty"] = row.get("qty", 0.0) + dq
        for g in d["given"]:
            code = mapped.get(g["menu_id"])
            if code:
                mc.explode(cards[code], _f(g["qty"]), wip, system)
        counts = {"bill": sum(_f(x["bills"]) for x in d["base"]), "portion": portions, "day": sum(1 for x in d["base"] if x["open"])}
        for e in extras:
            used = e["qty"] * counts[e["basis"]]
            if used and e["code"]:
                row = system.setdefault(e["code"], {"code": e["code"], "name": e["name"], "unit": e["unit"], "qty": 0.0})
                row["qty"] = row.get("qty", 0.0) + used
        out[b] = system
    return out


_cache = TTLCache()


def clear() -> None:
    _cache.clear()


def running(branches: Optional[list[str]]) -> dict[str, dict]:
    """{branch: {so: {date, refs, final, statuses}, items: {code: {so, unit, moves: {kind: qty}, waste, used, estimate}}}}
    (cached CACHE_S seconds per set of branches)."""
    key = ",".join(sorted(branches)) if branches is not None else "*"
    hit = _cache.get(key)
    if hit is None:
        hit = _running(branches)
        _cache.set(key, hit, CACHE_S)
    return hit


def _running(branches: Optional[list[str]]) -> dict[str, dict]:
    from app import menu_costing as mc
    from app import stock_moves
    counts = latest_counts(branches)
    if not counts:
        return {}
    today = date.today()
    starts = {b: date.fromisoformat(c["date"]) for b, c in counts.items()}
    groups: dict[date, list[str]] = {}
    for b, d in starts.items():
        groups.setdefault(d, []).append(b)
    moved: dict[str, dict] = {}
    for d, bs in groups.items():
        moved.update(stock_moves.moves(d, today, bs))
    waste = _waste(starts, today)
    used = system_usage(starts, today)
    codes = sorted({c for v in counts.values() for c in v["items"]} | {c for v in used.values() for c in v if not c.startswith(("wip:", "n:", "o:"))})
    packs = mc.item_units(codes)
    out: dict[str, dict] = {}
    for b, c in counts.items():
        items: dict[str, dict] = {}
        units = {code: u for code, (_, u) in c["items"].items()}
        for k, v in used.get(b, {}).items():
            if v.get("code") and v["code"] not in units:
                units[v["code"]] = str(v.get("unit") or "").upper()
        for code, unit in units.items():
            so = c["items"].get(code, (0.0, unit))[0]
            mv = {}
            for kind, rows in moved.get(b, {}).items():
                q = mc._bought(rows, code, unit, c["esb"].get(code), packs.get(code))
                if q:
                    mv[kind] = q
            u = used.get(b, {}).get(code)
            use = mc.to_unit(u["qty"], u.get("unit"), unit) if u else 0.0
            w = waste.get(b, {}).get(code, 0.0)
            if not (so or mv or use or w):
                continue
            items[code] = {"so": so, "unit": unit, "moves": mv, "waste": w, "used": use or 0.0,
                           "estimate": so + sum(mv.values()) - w - (use or 0.0)}
        out[b] = {"so": {"date": c["date"], "refs": c["refs"], "final": c["final"], "statuses": c["statuses"]}, "items": items}
    return out

