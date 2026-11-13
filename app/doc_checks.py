"""ESB documents with an unusual quantity and their corrections (Cost Control, page cc.cogs, 2026-11-12; migration 097).

A line of a document that moves the stock of an outlet (app/stock_moves.py: goods received, the outlet's own purchases,
production result and material, goods delivered, returns) is flagged when its quantity in the base unit of the item is
RATIO times the usual one or more (or that many times less): the usual one is the median of the same item and kind of
line over the HISTORY_DAYS before, over every outlet with sales. TGP04 entered its espresso production of 1 Nov 2026 in
KG instead of GR (27,720 KG): about Rp 2.6 bn of "unexplained" usage in the COGS & Usage, unseen until an audit.

Cost Control corrects the quantity of a flagged line (any line of those documents, in the unit of the document) with a
reason; ESB is never changed. The portal is the source of the figures (sales, counts, waste, TITO, prices move there),
so a correction is the quantity of the line, not a change: adjustments() gives new − the quantity ESB holds NOW per
branch × kind × (item, unit) and app/stock_moves.py adds it — when ESB is fixed later nothing is counted twice, and a
document cancelled / deleted in ESB drops its correction (it is usually entered again). The COGS & Usage, the Sales
Calculator and the Stock List read the corrected quantity. Goods receipts are flagged too but keep their own corrections
(page Good Receipt, app/goods_receipt.py, migration 072).
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from app import database as db
from app.database import SCHEMA

P = "integration_portal"
T = f"{P}.cc_doc_fix"
HISTORY_DAYS = 90
RATIO = 20           # a line this many times the usual quantity (or this many times less) is flagged
LIKELY_UNIT = 100    # this far from usual: almost surely the wrong unit
MIN_SAMPLES = 5      # lines of the same item and kind needed for a usual quantity
MIN_RP = 500_000     # an item with a price: a smaller value impact is left out
SKIP = ("New", "Draft", "Rejected", "Cancelled", "Canceled", "Void")  # as app/stock_moves.py
OWN_PAGE = {"goods_receipt"}  # corrected on their own page


class DocFixError(ValueError):
    """A correction refused with a message for the user (HTTP 422)."""


def _moves() -> dict[str, tuple[str, str, int]]:
    from app import stock_moves
    return stock_moves.MOVES


def _kind(module: str, line_type: str) -> Optional[tuple[str, int]]:
    for k, (m, t, s) in _moves().items():
        if m == module and t == line_type:
            return ("returned" if k == "returnedGr" else k), s
    return None


def _outlets(d0: date, d1: date, branches: Optional[list[str]]) -> list[str]:
    """The branches checked: those asked (test branches left out), else every outlet with sales in the period."""
    from app import test_branches
    if branches is None:
        branches = [r["branch_code"] for r in db.fetch(
            f"""SELECT DISTINCT branch_code FROM {P}.agg_sales_daily WHERE tx_type = 'sales' AND sales_date BETWEEN %s AND %s""",
            (d0, d1))]
    return sorted(b for b in branches if b and not test_branches.is_test(b))


def _pairs() -> tuple[list[str], list[str]]:
    pairs = sorted({(m, t) for m, t, _ in _moves().values()})
    return [m for m, _ in pairs], [t for _, t in pairs]


def _fixes(d_from: date, d_to: date, branches: Optional[list[str]]) -> list[dict]:
    return db.fetch(
        f"""SELECT * FROM {T} WHERE cancelled_at IS NULL AND doc_date BETWEEN %s AND %s
              AND (%s::text[] IS NULL OR branch_code = ANY(%s)) ORDER BY created_at""", (d_from, d_to, branches, branches))


Key = tuple[str, str, str, str, str]  # module, doc, line type, item, unit (upper case)


def _key(f: dict) -> Key:
    return (f["module"], f["doc_num"], f["line_type"], f["product_code"], f["uom"])


def _esb_now(fixes: list[dict]) -> dict[Key, float]:
    """The quantity ESB holds now for the lines of the corrections (documents still counted by app/stock_moves.py);
    a line missing here was cancelled or deleted in ESB."""
    if not fixes:
        return {}
    rows = db.fetch(
        f"""SELECT d.module, d.doc_num, l.line_type, l.product_code, upper(coalesce(l.uom_name, '')) AS uom, sum(l.qty) AS qty
              FROM {SCHEMA}.erp_documents d
              JOIN {SCHEMA}.erp_document_lines l ON l.module = d.module AND l.doc_num = d.doc_num
             WHERE (d.module, d.doc_num) IN (SELECT * FROM unnest(%s::text[], %s::text[]))
               AND d.deleted_at IS NULL AND coalesce(d.status_name, '') <> ALL(%s)
             GROUP BY 1, 2, 3, 4, 5""",
        ([f["module"] for f in fixes], [f["doc_num"] for f in fixes], list(SKIP)))
    return {(r["module"], r["doc_num"], r["line_type"], r["product_code"], r["uom"]): float(r["qty"]) for r in rows}


def _fix_view(f: dict, now: Optional[float] = None, gone: bool = False) -> dict:
    """A correction; esbChanged: ESB holds another quantity than when it was corrected (fixed there, or changed again:
    the portal keeps its own); docGone: the document was cancelled / deleted in ESB, the correction no longer counts."""
    old = float(f["old_qty"])
    return {"id": f["id"], "newQty": float(f["new_qty"]), "oldQty": old, "reason": f["reason"],
            "by": f["created_by"], "at": f["created_at"].isoformat(), "esbNow": now,
            "esbChanged": now is not None and abs(abs(now) - old) > 1e-9, "docGone": gone}


CACHE_S = 300  # kept warm per period (2026-11-13); a correction clears it (save_fix / cancel_fix)
_views: dict = {}


def clear() -> None:
    _views.clear()


def check(d_from: date, d_to: date, branches: Optional[list[str]]) -> dict:
    """check() kept warm per period and branches (stale-while-revalidate)."""
    from app.utils import Fresh
    key = (d_from, d_to, tuple(sorted(branches)) if branches is not None else None)
    f = _views.get(key)
    if f is None:
        if len(_views) > 40:
            _views.clear()
        f = _views[key] = Fresh(lambda: _check(d_from, d_to, branches), CACHE_S)
    return f.get()


def _check(d_from: date, d_to: date, branches: Optional[list[str]]) -> dict:
    """The lines with an unusual quantity in the period (largest value impact first) and every line corrected in it."""
    from app import menu_costing as mc
    from app.pic_checkin import _names
    outlets = _outlets(d_from, d_to, branches)
    period = {"from": d_from.isoformat(), "to": d_to.isoformat()}
    if not outlets:
        return {"period": period, "data": [], "summary": {"lines": 0, "open": 0, "fixed": 0, "impact": 0.0}}
    mods, types = _pairs()
    base_sql = f"""FROM {SCHEMA}.erp_documents d
                   JOIN {SCHEMA}.erp_document_lines l ON l.module = d.module AND l.doc_num = d.doc_num
                   JOIN {SCHEMA}.master_branches b ON b.branch_id::text = d.branch_id::text
                  WHERE (d.module, l.line_type) IN (SELECT * FROM unnest(%(m)s::text[], %(t)s::text[]))
                    AND d.deleted_at IS NULL AND coalesce(d.status_name, '') <> ALL(%(skip)s)
                    AND l.product_code IS NOT NULL AND coalesce(l.qty, 0) <> 0"""
    h0 = d_from - timedelta(days=HISTORY_DAYS)
    p = {"m": mods, "t": types, "skip": list(SKIP), "a": d_from, "b": d_to, "h0": h0, "br": outlets}
    # the usual quantity of one document line: median per item × kind of line × unit, over every outlet with sales
    usual = db.fetch(
        f"""WITH x AS (SELECT d.module, l.line_type, l.product_code, upper(coalesce(l.uom_name, '')) AS uom, d.doc_num,
                              abs(sum(l.qty)) AS q
                         {base_sql} AND d.doc_date BETWEEN %(h0)s AND %(b)s
                          AND b.branch_code IN (SELECT DISTINCT branch_code FROM {P}.agg_sales_daily
                                                 WHERE tx_type = 'sales' AND sales_date BETWEEN %(h0)s AND %(b)s)
                        GROUP BY 1, 2, 3, 4, 5)
            SELECT module, line_type, product_code, uom, percentile_cont(0.5) WITHIN GROUP (ORDER BY q) AS med, count(*) AS n
              FROM x GROUP BY 1, 2, 3, 4""", p)
    lines = db.fetch(
        f"""SELECT b.branch_code, d.module, d.doc_num, d.doc_date, d.status_name, l.line_type, l.product_code,
                   max(l.product_name) AS product_name, upper(coalesce(l.uom_name, '')) AS uom, sum(l.qty) AS qty
              {base_sql} AND d.doc_date BETWEEN %(a)s AND %(b)s AND b.branch_code = ANY(%(br)s)
             GROUP BY 1, 2, 3, 4, 5, 6, 7, 9""", p)
    codes = sorted({r["product_code"] for r in lines})
    units = mc.item_units(codes)

    def to_base(code: str, qty: float, uom: str) -> Optional[float]:
        b, packs = units.get(code, ("", {}))
        if not b:
            return None
        v = mc.to_unit(qty, uom, b)
        if v is None and uom in packs:
            v = qty * packs[uom]
        return v

    # usual quantity in the base unit (the unit most lines use leads), else per unit of the document
    groups: dict[tuple[str, str, str], list[tuple[int, float]]] = {}
    raw: dict[tuple[str, str, str, str], tuple[float, int]] = {}
    for u in usual:
        med, n = float(u["med"] or 0), int(u["n"])
        raw[(u["module"], u["line_type"], u["product_code"], u["uom"])] = (med, n)
        mb = to_base(u["product_code"], med, u["uom"])
        if mb is not None:
            groups.setdefault((u["module"], u["line_type"], u["product_code"]), []).append((n, mb))
    typical = {k: (max(g)[1], sum(n for n, _ in g)) for k, g in groups.items()}
    price = mc.prices_on(d_to, codes or None)
    if any(c not in price for c in codes):
        hpp = mc.esb_hpp(d_to)
        price.update({c: hpp[c][0] for c in codes if c not in price and c in hpp})
    try:
        fixes = {(f["module"], f["doc_num"], f["line_type"], f["product_code"], f["uom"]): f for f in _fixes(d_from, d_to, outlets)}
    except Exception:  # noqa: BLE001 - before migration 097: no correction yet
        fixes = {}
    names = _names(sorted({r["branch_code"] for r in lines}))
    out = []
    for r in lines:
        code, uom, qty = r["product_code"], r["uom"], float(r["qty"])
        key = (r["module"], r["doc_num"], r["line_type"], code, uom)
        qb = to_base(code, abs(qty), uom)
        t = typical.get((r["module"], r["line_type"], code))
        if qb is not None and t and t[0] > 0:
            usual_q, n, unit = t[0], t[1], units[code][0]
            cmp = qb
        else:
            med, n = raw.get((r["module"], r["line_type"], code, uom), (0.0, 0))
            usual_q, unit, cmp = med, uom, abs(qty)
        factor = cmp / usual_q if usual_q else None
        # far less than usual counts only outside the goods receipts (a small receipt of one pack is common there)
        unusual = n >= MIN_SAMPLES and factor is not None and (
            factor >= RATIO or (factor <= 1 / RATIO and r["module"] not in OWN_PAGE))
        fix = fixes.get(key)
        if not unusual and not fix:
            continue
        pr = price.get(code) if unit == (units.get(code, ("", {}))[0]) else None
        impact = abs(cmp - usual_q) * pr if pr is not None else None
        if unusual and not fix and impact is not None and impact < MIN_RP:
            continue
        kind = _kind(r["module"], r["line_type"])
        out.append({
            "module": r["module"], "lineType": r["line_type"], "kind": kind[0] if kind else r["module"],
            "docNum": r["doc_num"], "docDate": r["doc_date"].isoformat(), "status": r["status_name"],
            "branch": r["branch_code"], "branchName": names.get(r["branch_code"], r["branch_code"]),
            "code": code, "name": r["product_name"] or code, "uom": uom, "qty": qty,
            "baseQty": round(cmp, 3), "unit": unit, "usual": round(usual_q, 3), "samples": n,
            "factor": round(factor, 2) if factor is not None else None, "unusual": unusual,
            # 100× or more (or less): almost surely a unit typed wrong (KG for GR, a pack for a piece)
            "severe": factor is not None and (factor >= LIKELY_UNIT or factor <= 1 / LIKELY_UNIT),
            "price": pr, "impact": round(impact, 2) if impact is not None else None,
            "ownPage": r["module"] in OWN_PAGE, "fix": _fix_view(fix, qty) if fix else None})
        fixes.pop(key, None)
    # corrections of documents cancelled / deleted in ESB since: shown, no longer counted
    gone = list(fixes.values())
    names.update(_names(sorted({f["branch_code"] for f in gone} - set(names))) if gone else {})
    for f in gone:
        kind = _kind(f["module"], f["line_type"])
        out.append({
            "module": f["module"], "lineType": f["line_type"], "kind": kind[0] if kind else f["module"],
            "docNum": f["doc_num"], "docDate": f["doc_date"].isoformat(), "status": None,
            "branch": f["branch_code"], "branchName": names.get(f["branch_code"], f["branch_code"]),
            "code": f["product_code"], "name": f["product_code"], "uom": f["uom"], "qty": 0.0, "baseQty": 0.0, "unit": f["uom"],
            "usual": 0.0, "samples": 0, "factor": None, "unusual": False, "severe": False, "price": None, "impact": None,
            "ownPage": False, "fix": _fix_view(f, None, gone=True)})
    out.sort(key=lambda x: (x["fix"] is not None, -(x["impact"] or 0)))
    open_rows = [x for x in out if x["unusual"] and not x["fix"]]
    return {"period": period, "data": out, "summary": {
        "lines": len(out), "open": len(open_rows), "fixed": sum(1 for x in out if x["fix"]),
        "impact": round(sum(x["impact"] or 0 for x in open_rows), 2)}}


def _line(module: str, doc_num: str, line_type: str, code: str, uom: str) -> Optional[dict]:
    return db.fetchrow(
        f"""SELECT b.branch_code, d.doc_date, sum(l.qty) AS qty
              FROM {SCHEMA}.erp_documents d
              JOIN {SCHEMA}.erp_document_lines l ON l.module = d.module AND l.doc_num = d.doc_num
              JOIN {SCHEMA}.master_branches b ON b.branch_id::text = d.branch_id::text
             WHERE d.module = %s AND d.doc_num = %s AND l.line_type = %s AND l.product_code = %s
               AND upper(coalesce(l.uom_name, '')) = %s AND d.deleted_at IS NULL AND coalesce(d.status_name, '') <> ALL(%s)
             GROUP BY 1, 2""", (module, doc_num, line_type, code, uom, list(SKIP)))


def save_fix(user: dict, body: dict) -> dict:
    """Correct the quantity of a document line (replacing its previous correction): {module, docNum, lineType, code, uom,
    qty (in the unit of the document), reason}."""
    module, line_type = str(body.get("module") or ""), str(body.get("lineType") or "")
    if module in OWN_PAGE:
        raise DocFixError("Good Receipt dikoreksi di halaman Good Receipt")
    if _kind(module, line_type) is None:
        raise DocFixError("Jenis dokumen ini tidak dikoreksi di sini")
    reason = str(body.get("reason") or "").strip()
    if len(reason) < 5:
        raise DocFixError("Tulis alasan koreksi (minimal 5 huruf)")
    try:
        qty = Decimal(str(body.get("qty")))
    except (InvalidOperation, TypeError):
        raise DocFixError("Jumlah baru tidak valid") from None
    if qty < 0:
        raise DocFixError("Jumlah baru tidak boleh negatif")
    doc, code, uom = str(body.get("docNum") or ""), str(body.get("code") or ""), str(body.get("uom") or "").upper()
    line = _line(module, doc, line_type, code, uom)
    if not line:
        raise DocFixError("Baris dokumen tidak ditemukan")
    old = Decimal(str(line["qty"]))
    if qty == abs(old):
        raise DocFixError("Jumlah baru sama dengan ESB")
    by = user.get("full_name") or user.get("username")
    with db.transaction() as conn:
        conn.execute(f"""UPDATE {T} SET cancelled_at = now(), cancelled_by = %s WHERE cancelled_at IS NULL AND module = %s
                         AND doc_num = %s AND line_type = %s AND product_code = %s AND uom = %s""",
                     (by, module, doc, line_type, code, uom))
        row = conn.execute(
            f"""INSERT INTO {T} (module, doc_num, line_type, product_code, uom, branch_code, doc_date, old_qty, new_qty, reason, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (module, doc, line_type, code, uom, line["branch_code"], line["doc_date"], abs(old), qty, reason[:500], by)).fetchone()
    from app import running_stock
    running_stock.clear()  # the Stock List reads the corrected quantity now
    return {**_fix_view(row), "docNum": doc, "code": code, "uom": uom, "branch": line["branch_code"]}


def cancel_fix(user: dict, fix_id: int) -> dict:
    row = db.fetchrow(f"""UPDATE {T} SET cancelled_at = now(), cancelled_by = %s WHERE id = %s AND cancelled_at IS NULL
                          RETURNING id, doc_num, product_code, uom, branch_code""", (user.get("full_name") or user.get("username"), fix_id))
    if not row:
        raise DocFixError("Koreksi tidak ditemukan atau sudah dibatalkan")
    from app import running_stock
    running_stock.clear()
    return dict(row)


def adjustments(d_from: date, d_to: date, branches: list[str]) -> dict[str, dict[str, dict[tuple[str, str], float]]]:
    """{branch: {kind: {(item, unit): signed change}}} of the active corrections of documents dated in the period."""
    out: dict[str, dict[str, dict[tuple[str, str], float]]] = {}
    if not branches:
        return out
    try:
        rows = _fixes(d_from, d_to, branches)
    except Exception:  # noqa: BLE001 - before migration 097: no correction
        return out
    now = _esb_now(rows)
    for f in rows:
        kind = _kind(f["module"], f["line_type"])
        esb = now.get(_key(f))
        if not kind or esb is None:  # the document cancelled / deleted in ESB: its correction no longer counts
            continue
        bucket = out.setdefault(f["branch_code"], {}).setdefault(kind[0], {})
        key = (f["product_code"], f["uom"])
        # the corrected quantity replaces what ESB holds now (ESB fixed later: no change; ESB qty as it is signed)
        bucket[key] = bucket.get(key, 0.0) + kind[1] * float(f["new_qty"]) - kind[1] * esb
    return out
