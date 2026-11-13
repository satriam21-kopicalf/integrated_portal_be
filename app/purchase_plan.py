"""Estimasi Belanja per outlet (Cost Control, page cc.purchase-plan, 2026-11-13; migration 100).

The Material Required Planning of the Sales Calculator plans the branches together with one buffer typed by hand and
subtracts the stock of every branch at once (the surplus of one outlet hides the shortage of another). This plans every
outlet on its own, with a buffer recommended from its own data, split in its two parts (management, 13 Nov: the buffer
comes from the waste — the waste is a loss, not the swing of the sales, so the two are kept apart):

    cover days   how many days the order must last: the median gap between the goods receipts of the outlet from the
                 main suppliers (Calf Warehouse, Calf Central Kitchen, CK Food) in the last GR_DAYS days; fewer than
                 MIN_GR receipts → DEFAULT_COVER; within COVER_MIN..COVER_MAX
    loss %       the approved portal waste of the outlet (app/waste.py with_values; the categories of EXCLUDE — what was
                 ordered too much, "Sisa Produk tidak terjual" — left out, a buffer must not reward them) ÷ the value
                 of its system usage (menus sold × recipe cards, app/running_stock.py system_usage — the ESB WASTE /
                 CUPPING bills are already in it, so they are not counted twice), the last WINDOW days; ≤ CAP_LOSS
    safety %     Z × the coefficient of variation of the daily sales of the outlet (open days) ÷ √cover days: the
                 safety stock for the swing of the sales over the cover days (service level ~90 %); ≤ CAP_SAFETY
    buffer %     loss % + safety %
Cost Control may set any of the three for an outlet (cc_buffer_setting, with a note); the rest stays recommended.

The plan of an outlet, per material of its recipe cards:
    per day    system usage of the last WINDOW days ÷ the days the outlet was open
    required   per day × cover days × (1 + buffer %)
    stock      the running stock now (app/running_stock.py: latest portal SO + moves − waste − usage), < 0 → 0
    to order   required − stock, rounded up to the pack the outlet receives the item in most (goods receipts of
               GR_DAYS days, kc_master.item_uom factor); the base unit when no pack is known
    value      to order × the Pricelist price (else the ESB HPP, marked)
The "normal" version (2026-11-13): a plain average of WINDOW days, no weekday pattern, promotions or trend yet.
`backtest` tells how good that average is: the plan made WINDOW days ago against what the outlet then used.
"""
import math
import statistics
from datetime import date, timedelta
from typing import Any, Optional

from app import database as db
from app.database import SCHEMA
from app.utils import Fresh, today

P = "integration_portal"
T_SET = f"{P}.cc_buffer_setting"
WINDOW = 28
GR_DAYS = 56
MIN_GR = 3
DEFAULT_COVER = 7
COVER_MIN, COVER_MAX = 1, 14
Z = 1.28            # ~90 % of the days covered
CAP_LOSS, CAP_SAFETY = 20.0, 30.0
MAIN_SUPPLIERS = ("Calf Warehouse", "Calf Central Kitchen", "Calf Central Kitchen Food")
EXCLUDE = ("tidak terjual",)  # waste categories (lower-case parts) left out of the loss: ordered too much
SKIP = ("New", "Draft", "Rejected", "Cancelled", "Canceled", "Void")
CACHE_S = 900  # every outlet computed together (like the running stock), refreshed in the background


class PlanError(ValueError):
    """Refused with a message for the user (HTTP 422)."""


def _f(v: Any) -> float:
    return float(v) if v is not None else 0.0


# ---------------------------------------------------------------- the formulas (pure)

def median_gap(days: list[date]) -> Optional[float]:
    """The median number of days between consecutive receipt days (one receipt a day counted once)."""
    ds = sorted(set(days))
    gaps = [(b - a).days for a, b in zip(ds, ds[1:])]
    return float(statistics.median(gaps)) if gaps else None


def cover_days(days: list[date]) -> tuple[int, str]:
    """(cover days, basis): 'gr' from the receipts, 'default' when there are too few."""
    if len(set(days)) < MIN_GR:
        return DEFAULT_COVER, "default"
    gap = median_gap(days) or DEFAULT_COVER
    return int(min(COVER_MAX, max(COVER_MIN, math.ceil(gap)))), "gr"


def loss_pct(waste_value: float, usage_value: float) -> Optional[float]:
    if usage_value <= 0:
        return None
    return round(min(CAP_LOSS, max(0.0, waste_value / usage_value * 100)), 2)


def safety_pct(daily: list[float], cover: int) -> Optional[float]:
    """Z × CV of the daily sales ÷ √cover days, in %. None with fewer than 7 open days."""
    open_days = [v for v in daily if v > 0]
    if len(open_days) < 7:
        return None
    mean = statistics.fmean(open_days)
    if mean <= 0:
        return None
    cv = statistics.pstdev(open_days) / mean
    return round(min(CAP_SAFETY, max(0.0, Z * cv / math.sqrt(max(cover, 1)) * 100)), 2)


def to_pack(qty: float, factor: Optional[float]) -> Optional[int]:
    """Packs to order for `qty` base units (rounded up); None without a pack factor."""
    if not factor or factor <= 0:
        return None
    return int(math.ceil(round(qty / factor, 6)))


def excluded(category: Optional[str]) -> bool:
    c = (category or "").lower()
    return any(x in c for x in EXCLUDE)


# ---------------------------------------------------------------- the data

def _window(end: Optional[date] = None) -> tuple[date, date]:
    """The last WINDOW whole days before today (or ending on `end`)."""
    d_to = end or today() - timedelta(days=1)
    return d_to - timedelta(days=WINDOW - 1), d_to


def outlets(branches: Optional[list[str]]) -> dict[str, str]:
    """{branch code: name} of the outlets with POS sales in the last WINDOW days (the test branches left out)."""
    from app import test_branches
    a, b = _window()
    rows = db.fetch(
        f"""SELECT DISTINCT branch_code FROM {P}.agg_sales_daily
             WHERE sales_date BETWEEN %s AND %s AND tx_type = 'sales' AND subtotal > 0
               AND (%s::text[] IS NULL OR branch_code = ANY(%s))""", (a, b, branches, branches))
    codes = sorted(r["branch_code"] for r in rows if r["branch_code"] and not test_branches.is_test(r["branch_code"]))
    from app.pic_checkin import _names
    names = _names(codes) if codes else {}
    return {c: names.get(c, c) for c in codes}


def gr_days(codes: list[str], end: date) -> dict[str, list[date]]:
    """{branch: receipt days} of the goods receipts from the main suppliers, the GR_DAYS days to `end`."""
    if not codes:
        return {}
    rows = db.fetch(
        f"""SELECT DISTINCT b.branch_code, d.doc_date FROM {SCHEMA}.erp_documents d
              JOIN {SCHEMA}.master_branches b ON b.branch_id::text = d.branch_id::text
             WHERE d.module = 'goods_receipt' AND d.doc_date BETWEEN %s AND %s AND d.deleted_at IS NULL
               AND coalesce(d.status_name, '') <> ALL(%s) AND d.counterparty_name = ANY(%s) AND b.branch_code = ANY(%s)""",
        (end - timedelta(days=GR_DAYS - 1), end, list(SKIP), list(MAIN_SUPPLIERS), codes))
    out: dict[str, list[date]] = {c: [] for c in codes}
    for r in rows:
        out.setdefault(r["branch_code"], []).append(r["doc_date"])
    return out


def daily_sales(codes: list[str], d_from: date, d_to: date) -> dict[str, list[float]]:
    """{branch: [sales of every day of the window]} (0 for a day without sales; the CV uses the open days)."""
    out = {c: [0.0] * ((d_to - d_from).days + 1) for c in codes}
    if not codes:
        return out
    for r in db.fetch(
            f"""SELECT branch_code, sales_date, sum(subtotal) AS s FROM {P}.agg_sales_daily
                 WHERE sales_date BETWEEN %s AND %s AND tx_type = 'sales' AND branch_code = ANY(%s) GROUP BY 1, 2""",
            (d_from, d_to, codes)):
        if r["branch_code"] in out:
            out[r["branch_code"]][(r["sales_date"] - d_from).days] = _f(r["s"])
    return out


def waste_values(codes: list[str], d_from: date, d_to: date) -> dict[str, dict]:
    """{branch: {value, excluded, entries, unpriced, categories: {name: value}}} of the waste approved by Cost Control."""
    from app import waste
    out = {c: {"value": 0.0, "excluded": 0.0, "entries": 0, "unpriced": 0, "categories": {}} for c in codes}
    if not codes:
        return out
    rows = db.fetch(f"""SELECT {waste.LIGHT} FROM {waste.T} WHERE business_date BETWEEN %s AND %s AND branch_code = ANY(%s)
                          AND status = 'approved'""", (d_from, d_to, codes))
    for v in waste.with_values(rows):
        b = out.get(v["branchCode"])
        if b is None:
            continue
        b["entries"] += 1
        value = v.get("value")
        if value is None:
            b["unpriced"] += 1
            continue
        cat = v.get("category") or "-"
        b["categories"][cat] = round(b["categories"].get(cat, 0.0) + value, 2)
        if excluded(cat):
            b["excluded"] += value
        else:
            b["value"] += value
    return out


def _prices(codes: list[str], day: date) -> tuple[dict[str, float], set[str]]:
    """The Pricelist price of every item on `day`, else the ESB HPP (those codes returned apart)."""
    from app import menu_costing as mc
    price = mc.prices_on(day, codes or None)
    missing = [c for c in codes if c not in price]
    hpp = mc.esb_hpp(day) if missing else {}
    esb = {c for c in missing if c in hpp}
    price.update({c: hpp[c][0] for c in esb})
    return price, esb


def usage(codes: list[str], d_from: date, d_to: date) -> dict[str, dict[str, dict]]:
    """{branch: {material key: {code, name, unit, qty}}}: the system usage of every outlet over the window."""
    from app import running_stock
    if not codes:
        return {}
    return running_stock.system_usage({c: d_from for c in codes}, d_to)


def overrides(codes: Optional[list[str]] = None) -> dict[str, dict]:
    try:
        rows = db.fetch(
            f"""SELECT s.branch_code, s.cover_days, s.loss_pct, s.safety_pct, s.note, s.updated_at, u.username
                  FROM {T_SET} s LEFT JOIN {P}.user_account u ON u.id = s.updated_by
                 WHERE %s::text[] IS NULL OR s.branch_code = ANY(%s)""", (codes, codes))
    except Exception:  # noqa: BLE001 - before migration 100
        return {}
    return {r["branch_code"]: {"coverDays": r["cover_days"], "lossPct": _f(r["loss_pct"]) if r["loss_pct"] is not None else None,
                               "safetyPct": _f(r["safety_pct"]) if r["safety_pct"] is not None else None, "note": r["note"],
                               "by": r["username"], "at": r["updated_at"].isoformat() if r["updated_at"] else None} for r in rows}


# ---------------------------------------------------------------- buffer recommendation

def _buffer_row(code: str, name: str, days: list[date], sales: list[float], waste: dict, usage_value: float,
                fix: Optional[dict]) -> dict:
    cover, basis = cover_days(days)
    loss = loss_pct(waste["value"], usage_value)
    safety = safety_pct(sales, cover)
    open_days = [v for v in sales if v > 0]
    rec = {"coverDays": cover, "lossPct": loss, "safetyPct": safety,
           "bufferPct": round((loss or 0) + (safety or 0), 2)}
    fix = fix or {}
    used_cover = fix.get("coverDays") or cover
    used_loss = fix["lossPct"] if fix.get("lossPct") is not None else (loss or 0.0)
    used_safety = fix["safetyPct"] if fix.get("safetyPct") is not None else (safety or 0.0)
    notes = []
    if basis == "default":
        notes.append("fewGr")
    if loss is None:
        notes.append("noUsage")
    if safety is None:
        notes.append("fewSalesDays")
    if waste["unpriced"]:
        notes.append("wasteUnpriced")
    return {
        "branchCode": code, "branchName": name, "recommended": rec,
        "used": {"coverDays": int(used_cover), "lossPct": round(used_loss, 2), "safetyPct": round(used_safety, 2),
                 "bufferPct": round(used_loss + used_safety, 2)},
        "override": fix or None, "notes": notes,
        "basis": {"cover": basis, "grDays": sorted(d.isoformat() for d in set(days)), "medianGap": median_gap(days),
                  "wasteValue": round(waste["value"], 2), "wasteExcluded": round(waste["excluded"], 2),
                  "wasteEntries": waste["entries"], "wasteUnpriced": waste["unpriced"], "wasteCategories": waste["categories"],
                  "usageValue": round(usage_value, 2), "openDays": len(open_days),
                  "salesMean": round(statistics.fmean(open_days), 2) if open_days else None,
                  "salesStdev": round(statistics.pstdev(open_days), 2) if len(open_days) > 1 else None},
    }


def _compute() -> dict:
    """Every outlet's data of the window at once (the system usage of all outlets is one computation)."""
    d_from, d_to = _window()
    names = outlets(None)
    codes = list(names)
    grs = gr_days(codes, d_to)
    sales = daily_sales(codes, d_from, d_to)
    wastes = waste_values(codes, d_from, d_to)
    used = usage(codes, d_from, d_to)
    item_codes = sorted({v["code"] for u in used.values() for v in u.values() if v.get("code")})
    price, esb = _prices(item_codes, d_to)
    rows = {}
    for c in codes:
        value = sum(v["qty"] * price[v["code"]] for v in used.get(c, {}).values() if v.get("code") in price)
        rows[c] = {"branchCode": c, "branchName": names[c], "gr": grs.get(c, []), "sales": sales[c], "waste": wastes[c],
                   "usageValue": value, "used": used.get(c, {})}
    return {"window": {"from": d_from.isoformat(), "to": d_to.isoformat(), "days": WINDOW, "grDays": GR_DAYS},
            "outlets": rows, "price": price, "esb": esb, "packs": received_packs(codes, d_to)}


_day: Optional[date] = None
_all = Fresh(_compute, CACHE_S)


def _base() -> dict:
    """The data of every outlet, kept warm CACHE_S seconds (served at once while it refreshes); a new day computes again."""
    global _day, _all
    d = today()
    if _day != d:
        _day, _all = d, Fresh(_compute, CACHE_S)
    return _all.get()


def reset() -> None:
    """Drop the values (tests, a new setting of the cards)."""
    global _day
    _day = None


def recommendations(branches: Optional[list[str]] = None) -> dict:
    """The buffer of every outlet (or of `branches`); an override of Cost Control shows at once."""
    base = _base()
    keep = set(branches) if branches is not None else None
    rows = [r for c, r in base["outlets"].items() if keep is None or c in keep]
    fixes = overrides([r["branchCode"] for r in rows])
    data = [_buffer_row(r["branchCode"], r["branchName"], r["gr"], r["sales"], r["waste"], r["usageValue"], fixes.get(r["branchCode"]))
            for r in rows]
    data.sort(key=lambda r: r["branchName"] or "")
    return {"window": base["window"], "data": data, "settings": settings()}


def settings() -> dict:
    return {"window": WINDOW, "grDays": GR_DAYS, "minGr": MIN_GR, "defaultCover": DEFAULT_COVER, "coverMin": COVER_MIN,
            "coverMax": COVER_MAX, "z": Z, "capLoss": CAP_LOSS, "capSafety": CAP_SAFETY, "suppliers": list(MAIN_SUPPLIERS),
            "excludedCategories": list(EXCLUDE)}


def save_setting(code: str, cover: Optional[int], loss: Optional[float], safety: Optional[float], note: str, actor_id: str) -> None:
    """Cost Control sets the buffer of an outlet (any part; all None = back to the recommendation)."""
    if cover is not None and not COVER_MIN <= cover <= 31:
        raise PlanError(f"Buffer hari {COVER_MIN}–31")
    for v in (loss, safety):
        if v is not None and not 0 <= v <= 100:
            raise PlanError("Persentase 0–100")
    with db.transaction() as conn:
        if cover is None and loss is None and safety is None:
            conn.execute(f"DELETE FROM {T_SET} WHERE branch_code = %s", (code,))
            return
        if not note.strip():
            raise PlanError("Alasan wajib diisi")
        conn.execute(
            f"""INSERT INTO {T_SET} (branch_code, cover_days, loss_pct, safety_pct, note, updated_by) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (branch_code) DO UPDATE SET cover_days = EXCLUDED.cover_days, loss_pct = EXCLUDED.loss_pct,
                safety_pct = EXCLUDED.safety_pct, note = EXCLUDED.note, updated_by = EXCLUDED.updated_by, updated_at = now()""",
            (code, cover, loss, safety, note.strip()[:300], actor_id))


# ---------------------------------------------------------------- the plan of an outlet

def received_packs(codes: list[str], end: date) -> dict[str, dict[str, str]]:
    """{branch: {item code: the UOM the outlet received it in most}} (goods receipts of GR_DAYS days)."""
    if not codes:
        return {}
    rows = db.fetch(
        f"""SELECT DISTINCT ON (b.branch_code, l.product_code) b.branch_code, l.product_code, upper(coalesce(l.uom_name, '')) AS uom
              FROM {SCHEMA}.erp_documents d
              JOIN {SCHEMA}.erp_document_lines l ON l.module = d.module AND l.doc_num = d.doc_num AND l.line_type = 'detail'
              JOIN {SCHEMA}.master_branches b ON b.branch_id::text = d.branch_id::text
             WHERE d.module = 'goods_receipt' AND d.doc_date BETWEEN %s AND %s AND d.deleted_at IS NULL
               AND coalesce(d.status_name, '') <> ALL(%s) AND b.branch_code = ANY(%s) AND l.product_code IS NOT NULL
             GROUP BY 1, 2, 3 ORDER BY b.branch_code, l.product_code, count(*) DESC""",
        (end - timedelta(days=GR_DAYS - 1), end, list(SKIP), codes))
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        if r["uom"]:
            out.setdefault(r["branch_code"], {})[r["product_code"]] = r["uom"]
    return out


def plan_rows(used: dict[str, dict], open_days: int, cover: int, buffer_pct: float, stock: dict[str, dict],
              packs: dict[str, tuple[str, dict[str, float]]], pack_uom: dict[str, str], price: dict[str, float],
              esb_priced: set[str]) -> list[dict]:
    """The lines of a plan (pure: every input given)."""
    from app.menu_costing import to_unit
    rows = []
    for v in used.values():
        code = v.get("code")
        if not code:
            continue
        unit = str(v.get("unit") or "").upper()
        per_day = v["qty"] / open_days if open_days else 0.0
        need = per_day * cover * (1 + buffer_pct / 100)
        st = stock.get(code)
        have = to_unit(st["estimate"], st.get("unit"), unit) if st else None
        have = max(0.0, have) if have is not None else None
        order = max(0.0, need - (have or 0.0))
        base, factors = packs.get(code, (unit, {}))
        uom = pack_uom.get(code)
        factor = factors.get(uom) if uom else None
        base_qty = to_unit(order, unit, base) if factor else None
        n_packs = to_pack(base_qty, factor) if base_qty is not None else None
        if n_packs is not None:
            order = to_unit(n_packs * factor, base, unit) or order
        p = price.get(code)
        rows.append({"code": code, "name": v.get("name"), "unit": unit, "perDay": round(per_day, 3), "required": round(need, 3),
                     "stock": round(have, 3) if have is not None else None, "stockKnown": st is not None,
                     "toOrder": round(order, 3), "packs": n_packs, "packUom": uom if n_packs is not None else None,
                     "packFactor": factor if n_packs is not None else None,
                     "price": p, "priceSource": None if p is None else ("esb" if code in esb_priced else "pricelist"),
                     "value": round(order * p, 2) if p is not None else None})
    rows.sort(key=lambda r: (-(r["value"] or 0), r["name"] or ""))
    return rows


def _units(codes: list[str]) -> dict[str, tuple[str, dict[str, float]]]:
    from app import menu_costing as mc
    try:
        return mc.item_units(codes)
    except Exception:  # noqa: BLE001 - no item master: no packs
        return {}


def plan(code: str, cover: Optional[int] = None, buffer_pct: Optional[float] = None) -> dict:
    """The Estimasi Belanja of one outlet; `cover` / `buffer_pct` replace the outlet's buffer for this view."""
    from app import running_stock
    base = _base()
    o = base["outlets"].get(code)
    if o is None:
        raise PlanError("Outlet tanpa penjualan POS dalam 28 hari terakhir")
    rec = recommendations([code])["data"][0]
    use_cover = cover or rec["used"]["coverDays"]
    use_buffer = buffer_pct if buffer_pct is not None else rec["used"]["bufferPct"]
    used = o["used"]
    open_days = rec["basis"]["openDays"]
    try:
        run = running_stock.running([code]).get(code) or {"items": {}, "so": None}
    except Exception:  # noqa: BLE001 - no running stock: To Order = Required
        run = {"items": {}, "so": None}
    codes = sorted({v["code"] for v in used.values() if v.get("code")})
    rows = plan_rows(used, open_days, use_cover, use_buffer, run["items"], _units(codes), base["packs"].get(code, {}),
                     base["price"], base["esb"])
    w = base["window"]
    return {"branchCode": code, "branchName": o["branchName"], "window": {"from": w["from"], "to": w["to"], "openDays": open_days},
            "coverDays": use_cover, "bufferPct": use_buffer, "buffer": rec, "so": run.get("so"), "data": rows,
            "total": {"items": len(rows), "toOrder": sum(1 for r in rows if r["toOrder"] > 0),
                      "value": round(sum(r["value"] or 0 for r in rows), 2),
                      "unpriced": sum(1 for r in rows if r["price"] is None and r["toOrder"] > 0),
                      "esbPriced": sum(1 for r in rows if r["priceSource"] == "esb" and r["toOrder"] > 0),
                      "noStock": sum(1 for r in rows if not r["stockKnown"]),
                      "packs": sum(1 for r in rows if r["packs"] is not None)}}


def summary(branches: Optional[list[str]] = None) -> dict:
    """Every outlet's estimate in Rp (each with its own stock and buffer; never the stock of outlets together)."""
    rec = recommendations(branches)
    out = []
    for r in rec["data"]:
        try:
            p = plan(r["branchCode"])
            out.append({"branchCode": r["branchCode"], "branchName": r["branchName"], "coverDays": p["coverDays"], "bufferPct": p["bufferPct"],
                        "value": p["total"]["value"], "items": p["total"]["toOrder"], "unpriced": p["total"]["unpriced"],
                        "so": p["so"]})
        except PlanError:
            continue
    return {"window": rec["window"], "data": out, "total": round(sum(x["value"] for x in out), 2)}


# ---------------------------------------------------------------- how good is the average (backtest)

def backtest(code: str) -> dict:
    """The plan made WINDOW days ago (its average of the WINDOW days before) against the next WINDOW days:
    forecast = per day × WINDOW; system = what the recipe cards say was used; actual = SO awal + moves − SO akhir
    (only with a portal / ESB count at both ends). WAPE in value (Σ|forecast − x| × price ÷ Σ x × price) and the
    share of the materials the buffer would have covered (forecast × (1 + buffer %) ≥ x)."""
    from app import menu_costing as mc
    end = today() - timedelta(days=1)
    t_from, t_to = end - timedelta(days=WINDOW - 1), end
    b_from, b_to = t_from - timedelta(days=WINDOW), t_from - timedelta(days=1)
    before = usage([code], b_from, b_to).get(code, {})
    after = usage([code], t_from, t_to).get(code, {})
    days_before = sum(1 for v in daily_sales([code], b_from, b_to)[code] if v > 0)
    days_after = sum(1 for v in daily_sales([code], t_from, t_to)[code] if v > 0)
    units = {v["code"]: str(v.get("unit") or "").upper() for v in {**before, **after}.values() if v.get("code")}
    names = {v["code"]: v.get("name") for v in {**before, **after}.values() if v.get("code")}
    actual = mc.actual_usage(t_from, t_to, [code], units)
    codes = sorted(units)
    price, _ = _prices(codes, t_to)
    rec = recommendations([code])["data"]
    buf = rec[0]["used"]["bufferPct"] if rec else 0.0
    by = lambda d: {v["code"]: v["qty"] for v in d.values() if v.get("code")}  # noqa: E731
    qb, qa = by(before), by(after)
    rows = []
    for c in codes:
        f = qb.get(c, 0.0) / days_before * days_after if days_before else 0.0
        a = actual["items"].get(c, {}).get("actual") if code in actual["covered"] else None
        rows.append({"code": c, "name": names.get(c), "unit": units[c], "forecast": round(f, 3), "system": round(qa.get(c, 0.0), 3),
                     "actual": round(a, 3) if a is not None else None, "price": price.get(c)})

    def score(key: str) -> Optional[dict]:
        rs = [r for r in rows if r[key] is not None and r["price"]]
        base = sum(r[key] * r["price"] for r in rs)
        if not rs or base <= 0:
            return None
        err = sum(abs(r["forecast"] - r[key]) * r["price"] for r in rs)
        covered = sum(1 for r in rs if r["forecast"] * (1 + buf / 100) >= r[key] - 1e-9)
        return {"wape": round(err / base * 100, 2), "covered": round(covered / len(rs) * 100, 2), "items": len(rs)}
    return {"branchCode": code, "base": {"from": b_from.isoformat(), "to": b_to.isoformat(), "openDays": days_before},
            "test": {"from": t_from.isoformat(), "to": t_to.isoformat(), "openDays": days_after}, "bufferPct": buf,
            "vsSystem": score("system"), "vsActual": score("actual"), "hasCounts": code in actual["covered"],
            "data": sorted(rows, key=lambda r: -abs((r["forecast"] - r["system"]) * (r["price"] or 0)))[:200]}
