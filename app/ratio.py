"""Outlet ratio (Cost Control): the figures of the Cost Control workbook "CALF - Rasio Outlet" (sheet per period, e.g.
SEP-W5) from the ESB data the portal already syncs, per outlet and period.

    Gross sales        POS "Sales" subtotal (agg_sales_daily)                       = workbook "Sub Total (GROSS SALES)"
    Nett sales         ESB nett sales; "Pendapatan" = gross x (1 - MDR) when the MDR of the month is entered
    Pembelian BB       purchase invoices from Calf Warehouse (bahan baku, packaging, supplies)
    Pembelian Espresso purchase invoices from Calf Central Kitchen (espresso / premix)
    Pembelian Dimsum   purchase invoices from Calf Central Kitchen Food
    COGS value         BB + Espresso + Dimsum
    SO awal / akhir    stock value of the ESB stock opname document of the day before the period / the last day
                       (sum qty x price; the closest document within -2..+1 days), or the corrected value
    Usage              COGS value + SO awal - SO akhir
    Ratios             BB ratio = (BB + Dimsum) / net, Espresso ratio = Espresso / net, % espresso of COGS,
                       COGS ratio gross & net, usage ratio gross & net, delta = COGS ratio net - usage ratio net
    Non Sales          "Other cost" bills (agg_sales_daily), Item Journal = value of item journals (waste, QC)

Checked against SEP-W5 (1-30 Sep 2026, 105 outlets): gross sales, dimsum, non sales and item journal identical for
every outlet; espresso identical for 101; BB within 1 % for 91 (invoices added or changed after the workbook was
made); SO values differ where the workbook carries a manual correction (use ratio_so_override).
"""
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from app import database as db
from app.database import SCHEMA

PORTAL = "integration_portal"
SUPPLIERS = {"Calf Warehouse": "bb", "Calf Central Kitchen": "espresso", "Calf Central Kitchen Food": "dimsum"}
SKIP_STATUSES = ("Rejected", "Cancelled", "Void")


def _f(v: Any) -> float:
    return float(v) if v is not None else 0.0


def _div(a: float, b: float) -> Optional[float]:
    return round(a / b, 6) if b else None


def branch_names() -> dict[str, str]:
    return {r["branch_code"]: r["branch_name"] for r in db.fetch(
        f"""SELECT DISTINCT ON (branch_code) branch_code, branch_name FROM {SCHEMA}.master_branches
            WHERE COALESCE(branch_code, '') <> '' ORDER BY branch_code, COALESCE(is_deleted, false)""")}


def so_values(day: date, codes: Optional[list[str]]) -> dict[str, dict]:
    """{branch: {value, doc, date, status}}: the stock opname document closest to `day` (within -2..+1 days)."""
    rows = db.fetch(
        f"""SELECT b.branch_code, d.doc_num, d.doc_date, d.status_name,
                   (SELECT sum(l.qty * l.price) FROM {SCHEMA}.erp_document_lines l
                    WHERE l.module = d.module AND l.doc_num = d.doc_num AND l.line_type = 'detail') AS value
            FROM {SCHEMA}.erp_documents d JOIN {SCHEMA}.master_branches b ON b.branch_id::text = d.branch_id::text
            WHERE d.module = 'stock_opname' AND d.doc_date BETWEEN %(lo)s AND %(hi)s AND d.deleted_at IS NULL
              AND d.status_name <> ALL(%(skip)s) AND (%(codes)s::text[] IS NULL OR b.branch_code = ANY(%(codes)s))""",
        {"lo": day - timedelta(days=2), "hi": day + timedelta(days=1), "skip": list(SKIP_STATUSES), "codes": codes},
    )
    out: dict[str, dict] = {}
    for r in rows:
        gap = abs((r["doc_date"] - day).days)
        cur = out.get(r["branch_code"])
        if cur is None or gap < cur["gap"] or (gap == cur["gap"] and _f(r["value"]) > cur["value"]):
            out[r["branch_code"]] = {"value": _f(r["value"]), "doc": r["doc_num"], "date": r["doc_date"].isoformat(),
                                     "status": r["status_name"], "gap": gap}
    return out


def overrides(days: list[date], codes: Optional[list[str]]) -> dict[tuple[str, date], dict]:
    rows = db.fetch(
        f"""SELECT o.branch_code, o.so_date, o.value, o.note, o.updated_at, u.username
            FROM {PORTAL}.ratio_so_override o LEFT JOIN {PORTAL}.user_account u ON u.id = o.updated_by
            WHERE o.so_date = ANY(%(days)s) AND (%(codes)s::text[] IS NULL OR o.branch_code = ANY(%(codes)s))""",
        {"days": days, "codes": codes},
    )
    return {(r["branch_code"], r["so_date"]): {"value": _f(r["value"]), "note": r["note"], "by": r["username"],
                                                "at": r["updated_at"].isoformat()} for r in rows}


def mdr(month: date, codes: Optional[list[str]]) -> dict[str, dict]:
    rows = db.fetch(
        f"""SELECT branch_code, mdr_pct, note FROM {PORTAL}.ratio_mdr
            WHERE month = %(m)s AND (%(codes)s::text[] IS NULL OR branch_code = ANY(%(codes)s))""",
        {"m": month, "codes": codes},
    )
    return {r["branch_code"]: {"pct": _f(r["mdr_pct"]), "note": r["note"]} for r in rows}


def ratio(start: date, end: date, codes: Optional[list[str]] = None) -> dict:
    """Per outlet (with POS sales in the period) and in total, the columns of the workbook."""
    p = {"s": start, "e": end, "codes": codes}
    scope = "AND (%(codes)s::text[] IS NULL OR branch_code = ANY(%(codes)s))"
    sales = {r["branch_code"]: r for r in db.fetch(
        f"""SELECT branch_code,
                   sum(subtotal) FILTER (WHERE tx_type = 'sales') AS gross,
                   sum(nett_sales) FILTER (WHERE tx_type = 'sales') AS nett,
                   sum(bills) FILTER (WHERE tx_type = 'sales') AS bills,
                   sum(subtotal) FILTER (WHERE tx_type = 'other_cost') AS non_sales
            FROM {PORTAL}.agg_sales_daily WHERE sales_date BETWEEN %(s)s AND %(e)s {scope} GROUP BY 1""", p)}
    buy: dict[str, dict[str, float]] = {}
    for r in db.fetch(
        f"""SELECT b.branch_code, d.counterparty_name, sum(d.total) AS total, count(*) AS docs
            FROM {SCHEMA}.erp_documents d JOIN {SCHEMA}.master_branches b ON b.branch_id::text = d.branch_id::text
            WHERE d.module = 'purchase_invoice' AND d.doc_date BETWEEN %(s)s AND %(e)s AND d.deleted_at IS NULL
              AND (%(codes)s::text[] IS NULL OR b.branch_code = ANY(%(codes)s))
            GROUP BY 1, 2""", p):
        key = SUPPLIERS.get(r["counterparty_name"], "other")
        b = buy.setdefault(r["branch_code"], {})
        b[key] = b.get(key, 0.0) + _f(r["total"])
    journal = {r["branch_code"]: _f(r["value"]) for r in db.fetch(
        f"""SELECT b.branch_code, sum(abs(l.qty * l.price)) AS value
            FROM {SCHEMA}.erp_documents d
            JOIN {SCHEMA}.erp_document_lines l ON l.module = d.module AND l.doc_num = d.doc_num AND l.line_type = 'detail'
            JOIN {SCHEMA}.master_branches b ON b.branch_id::text = d.branch_id::text
            WHERE d.module = 'item_journal' AND d.doc_date BETWEEN %(s)s AND %(e)s AND d.deleted_at IS NULL
              AND (%(codes)s::text[] IS NULL OR b.branch_code = ANY(%(codes)s))
            GROUP BY 1""", p)}
    day_a, day_b = start - timedelta(days=1), end
    so_a, so_b = so_values(day_a, codes), so_values(day_b, codes)
    fixes = overrides([day_a, day_b], codes)
    month = start.replace(day=1)
    mdrs = mdr(month, codes) if start.replace(day=1) == end.replace(day=1) else {}
    names = branch_names()

    def so(code: str, day: date, found: dict) -> dict:
        doc = found.get(code)
        fix = fixes.get((code, day))
        return {"value": fix["value"] if fix else (doc["value"] if doc else None), "esb": doc["value"] if doc else None,
                "doc": doc["doc"] if doc else None, "docDate": doc["date"] if doc else None,
                "docStatus": doc["status"] if doc else None, "override": fix, "date": day.isoformat()}

    outlets = []
    for code, s in sales.items():
        gross = _f(s["gross"])
        if gross <= 0:
            continue  # bulk order / warehouse locations without POS sales
        b = buy.get(code, {})
        outlets.append(_row(code, names.get(code, code), gross, _f(s["nett"]), int(s["bills"] or 0), _f(s["non_sales"]),
                            b, journal.get(code, 0.0), so(code, day_a, so_a), so(code, day_b, so_b), mdrs.get(code)))
    outlets.sort(key=lambda o: o["branchName"])
    total = _total(outlets)
    return {"period": {"from": start.isoformat(), "to": end.isoformat(), "soStart": day_a.isoformat(), "soEnd": day_b.isoformat(),
                       "month": month.isoformat(), "mdrEditable": bool(start.replace(day=1) == end.replace(day=1))},
            "outlets": outlets, "total": total}


def _row(code: str, name: str, gross: float, nett: float, bills: int, non_sales: float, buy: dict[str, float],
         journal: float, so_a: dict, so_b: dict, mdr_row: Optional[dict]) -> dict:
    bb, esp, dim, other = buy.get("bb", 0.0), buy.get("espresso", 0.0), buy.get("dimsum", 0.0), buy.get("other", 0.0)
    cogs = bb + esp + dim
    pendapatan = round(gross * (1 - mdr_row["pct"] / 100), 2) if mdr_row else None
    net = pendapatan if pendapatan is not None else nett
    have_so = so_a["value"] is not None and so_b["value"] is not None
    usage = cogs + so_a["value"] - so_b["value"] if have_so else None
    out = {
        "branchCode": code, "branchName": name, "bills": bills,
        "gross": gross, "nett": nett, "pendapatan": pendapatan, "mdrPct": mdr_row["pct"] if mdr_row else None,
        "netBasis": "pendapatan" if pendapatan is not None else "nett",
        "bb": bb, "espresso": esp, "dimsum": dim, "otherPurchases": other, "cogs": cogs,
        "espressoShare": _div(esp, cogs), "bbRatio": _div(bb + dim, net), "espressoRatio": _div(esp, net),
        "cogsRatioGross": _div(cogs, gross), "cogsRatioNet": _div(cogs, net),
        "soStart": so_a, "soEnd": so_b, "usage": usage,
        "usageRatioGross": _div(usage, gross) if usage is not None else None,
        "usageRatioNet": _div(usage, net) if usage is not None else None,
        "nonSales": non_sales, "itemJournal": journal,
        "nonSalesPct": _div(non_sales, gross), "itemJournalPct": _div(journal, gross),
    }
    out["delta"] = round(out["cogsRatioNet"] - out["usageRatioNet"], 6) if out["cogsRatioNet"] is not None and out["usageRatioNet"] is not None else None
    return out


def _total(rows: list[dict]) -> dict:
    keys = ("gross", "nett", "bb", "espresso", "dimsum", "otherPurchases", "cogs", "nonSales", "itemJournal", "bills")
    t = {k: sum(r[k] for r in rows) for k in keys}
    net = sum((r["pendapatan"] if r["pendapatan"] is not None else r["nett"]) for r in rows)
    with_so = [r for r in rows if r["usage"] is not None]
    so_a = sum(r["soStart"]["value"] for r in with_so)
    so_b = sum(r["soEnd"]["value"] for r in with_so)
    usage = sum(r["usage"] for r in with_so)
    gross_so = sum(r["gross"] for r in with_so)
    net_so = sum((r["pendapatan"] if r["pendapatan"] is not None else r["nett"]) for r in with_so)
    return {**t, "net": net, "outlets": len(rows), "outletsWithSo": len(with_so), "soStart": so_a, "soEnd": so_b, "usage": usage,
            "espressoShare": _div(t["espresso"], t["cogs"]), "bbRatio": _div(t["bb"] + t["dimsum"], net),
            "espressoRatio": _div(t["espresso"], net), "cogsRatioGross": _div(t["cogs"], t["gross"]),
            "cogsRatioNet": _div(t["cogs"], net), "usageRatioGross": _div(usage, gross_so), "usageRatioNet": _div(usage, net_so),
            "nonSalesPct": _div(t["nonSales"], t["gross"]), "itemJournalPct": _div(t["itemJournal"], t["gross"])}


# ---------------------------------------------------------------- corrections

def set_so_override(code: str, day: date, value: Optional[Decimal], note: str, actor_id: str) -> None:
    with db.transaction() as conn:
        if value is None:
            conn.execute(f"DELETE FROM {PORTAL}.ratio_so_override WHERE branch_code = %s AND so_date = %s", (code, day))
        else:
            conn.execute(
                f"""INSERT INTO {PORTAL}.ratio_so_override (branch_code, so_date, value, note, updated_by)
                    VALUES (%s, %s, %s, %s, %s) ON CONFLICT (branch_code, so_date)
                    DO UPDATE SET value = EXCLUDED.value, note = EXCLUDED.note, updated_by = EXCLUDED.updated_by, updated_at = now()""",
                (code, day, value, note.strip(), actor_id))


def set_mdr(code: str, month: date, pct: Optional[Decimal], note: Optional[str], actor_id: str) -> None:
    with db.transaction() as conn:
        if pct is None:
            conn.execute(f"DELETE FROM {PORTAL}.ratio_mdr WHERE branch_code = %s AND month = %s", (code, month))
        else:
            conn.execute(
                f"""INSERT INTO {PORTAL}.ratio_mdr (branch_code, month, mdr_pct, note, updated_by) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (branch_code, month) DO UPDATE SET mdr_pct = EXCLUDED.mdr_pct, note = EXCLUDED.note,
                    updated_by = EXCLUDED.updated_by, updated_at = now()""",
                (code, month, pct, note, actor_id))
