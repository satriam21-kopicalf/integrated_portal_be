"""The ESB company COTR (CALF5) in the COTR menus (migration 090), read from integration_esb_cotr (the sync engine of
integrated-esbapi, ESB_PROFILE=cotr, every 30 minutes).

Per pool (cotr_pool.esb_branch_code) and day, the five menus (the finished products "... - COTR (145ml)" named like the
menus, cotr_sheet.menu_of; premixes and other materials are left out):
    arrived  = simple transfers from the storage (a branch that is not a pool HUB) to the HUB
    transfer = simple transfers between HUBs (+ in, − out)
    sold     = simple sales of the HUB
    waste / cupping = item journals of the HUB, purpose WASTING / CUPPING (their quantity is negative in ESB)
And the documents of every module, to list and open in the portal (tab ESB COTR).
"""
from datetime import date
from typing import Any, Optional

from app import database as db
from app.cotr import P, CotrError, _iso, _menu_map
from app.cotr_sheet import menu_of

S = "integration_esb_cotr"
MODULES = {
    "simple_sales": "Simple Sales", "simple_transfer": "Simple Transfer", "item_journal": "Item Journal",
    "simple_manufacturing": "Simple Manufacturing", "stock_opname": "Stock Opname", "goods_receipt": "Goods Receipt",
    "goods_delivery": "Goods Delivery", "purchase_request": "Purchase Request", "purchase_order": "Purchase Order",
    "purchase_invoice": "Purchase Invoice", "purchase_return": "Purchase Return", "simple_purchase": "Simple Purchase",
    "sales_order": "Sales Order", "sales_invoice": "Sales Invoice", "receipt": "Receipt", "memorial_journal": "Memorial Journal",
}


def branches() -> dict[str, dict]:
    """ESB branch name -> {code, pool} (the pools tied to it in Master Pool)."""
    pools = {r["esb_branch_code"]: r["code"] for r in db.fetch(
        f"SELECT code, esb_branch_code FROM {P}.cotr_pool WHERE esb_branch_code IS NOT NULL")}
    return {(r["branch_name"] or "").strip().upper(): {"code": r["branch_code"], "name": r["branch_name"], "pool": pools.get(r["branch_code"])}
            for r in db.fetch(f"SELECT branch_code, branch_name FROM {S}.master_branches")}


def figures(d0: date, d1: date) -> dict[str, dict[date, dict[str, dict[str, int]]]]:
    """{pool: {day: {menu: {kind: cups}}}} of the ESB documents in the period."""
    menus = _menu_map()
    br = branches()
    hubs = {v["pool"] for v in br.values() if v["pool"] and v["pool"].startswith("HUB")}
    pool_of = lambda name: (br.get((name or "").strip().upper()) or {}).get("pool")  # noqa: E731
    out: dict[str, dict[date, dict[str, dict[str, int]]]] = {}

    def put(pool: Optional[str], d: date, menu: Optional[str], kind: str, n: float) -> None:
        if pool and menu and n:
            m = out.setdefault(pool, {}).setdefault(d, {}).setdefault(menu, {})
            m[kind] = m.get(kind, 0) + int(round(n))
    rows = db.fetch(f"""SELECT d.module, d.doc_date, d.branch_name, d.counterparty_name, l.product_name, l.qty, l.raw_data->>'purposeName' AS purpose
                          FROM {S}.erp_documents d JOIN {S}.erp_document_lines l ON l.module = d.module AND l.doc_num = d.doc_num
                         WHERE d.module IN ('simple_sales', 'simple_transfer', 'item_journal') AND d.deleted_at IS NULL
                           AND d.doc_date BETWEEN %s AND %s""", (d0, d1))
    for r in rows:
        name = (r["product_name"] or "").upper()
        if "COTR" not in name or "PREMIX" in name:  # the cups the HUBs sell ("... - COTR (145ml)"), not the materials
            continue
        menu = menu_of(r["product_name"], menus)
        if not menu:
            continue
        qty = float(r["qty"] or 0)
        if r["module"] == "simple_sales":
            put(pool_of(r["branch_name"]), r["doc_date"], menu, "sold", qty)
        elif r["module"] == "item_journal":
            purpose = (r["purpose"] or "").upper()
            kind = "cupping" if "CUPPING" in purpose else "waste" if "WAST" in purpose else None
            if kind:
                put(pool_of(r["branch_name"]), r["doc_date"], menu, kind, abs(qty))
        else:  # simple transfer: from the storage = arrived, between HUBs = transfer
            src, dst = pool_of(r["branch_name"]), pool_of(r["counterparty_name"])
            if src in hubs:
                put(src, r["doc_date"], menu, "transfer", -qty)
                put(dst, r["doc_date"], menu, "transfer", qty)
            else:
                put(dst, r["doc_date"], menu, "arrived", qty)
    return out


def summary() -> dict:
    """The modules synced: documents, lines, first / last day, last sync."""
    rows = db.fetch(f"""SELECT module, count(*) AS docs, min(doc_date) AS first, max(doc_date) AS last, max(synced_at) AS synced
                          FROM {S}.erp_documents WHERE deleted_at IS NULL GROUP BY 1""")
    by = {r["module"]: r for r in rows}
    return {"modules": [{"module": m, "name": n, "docs": by[m]["docs"] if m in by else 0, "first": _iso(by[m]["first"]) if m in by else None,
                         "last": _iso(by[m]["last"]) if m in by else None, "synced": _iso(by[m]["synced"]) if m in by else None}
                        for m, n in MODULES.items()],
            "branches": [{"code": v["code"], "name": v["name"], "pool": v["pool"]} for v in sorted(branches().values(), key=lambda x: x["name"] or "")]}


def documents(module: str, d0: date, d1: date, branch: Optional[str], q: Optional[str], limit: int = 2000) -> dict:
    if module not in MODULES:
        raise CotrError("Modul tidak dikenal")
    like = f"%{q.strip()}%" if q and q.strip() else None
    rows = db.fetch(f"""SELECT doc_num, doc_date, branch_name, location_name, counterparty_name, ref_num, trans_type, status_name, total,
                               additional_info, created_by, line_count
                          FROM {S}.erp_documents
                         WHERE module = %s AND deleted_at IS NULL AND doc_date BETWEEN %s AND %s
                           AND (%s::text IS NULL OR branch_name = %s OR counterparty_name = %s)
                           AND (%s::text IS NULL OR doc_num ILIKE %s OR additional_info ILIKE %s OR counterparty_name ILIKE %s)
                         ORDER BY doc_date DESC, doc_num DESC LIMIT %s""",
                    (module, d0, d1, branch, branch, branch, like, like, like, like, limit + 1))
    return {"module": module, "name": MODULES[module], "more": len(rows) > limit,
            "rows": [{"docNum": r["doc_num"], "day": _iso(r["doc_date"]), "branch": r["branch_name"], "location": r["location_name"],
                      "counterparty": r["counterparty_name"], "ref": r["ref_num"], "type": r["trans_type"], "status": r["status_name"],
                      "total": float(r["total"]) if r["total"] is not None else None, "info": r["additional_info"], "by": r["created_by"],
                      "lines": r["line_count"]} for r in rows[:limit]]}


def lines(module: str, d0: date, d1: date, branch: Optional[str]) -> list[dict]:
    """Every line of the documents of a module in the period (the Excel of the tab)."""
    if module not in MODULES:
        raise CotrError("Modul tidak dikenal")
    return [{"docNum": r["doc_num"], "day": _iso(r["doc_date"]), "branch": r["branch_name"], "counterparty": r["counterparty_name"],
             "status": r["status_name"], "info": r["additional_info"], "type": r["line_type"], "code": r["product_code"],
             "product": r["product_name"], "uom": r["uom_name"], "qty": float(r["qty"]) if r["qty"] is not None else None,
             "price": float(r["price"]) if r["price"] is not None else None, "total": float(r["total"]) if r["total"] is not None else None,
             "purpose": r["purpose"], "notes": r["notes"]}
            for r in db.fetch(f"""SELECT d.doc_num, d.doc_date, d.branch_name, d.counterparty_name, d.status_name, d.additional_info, l.line_type,
                                         l.product_code, l.product_name, l.uom_name, l.qty, l.price, l.total, l.notes, l.raw_data->>'purposeName' AS purpose
                                    FROM {S}.erp_documents d JOIN {S}.erp_document_lines l ON l.module = d.module AND l.doc_num = d.doc_num
                                   WHERE d.module = %s AND d.deleted_at IS NULL AND d.doc_date BETWEEN %s AND %s
                                     AND (%s::text IS NULL OR d.branch_name = %s OR d.counterparty_name = %s)
                                   ORDER BY d.doc_date DESC, d.doc_num, l.line_type, l.line_no LIMIT 50000""",
                                (module, d0, d1, branch, branch, branch))]


def document(module: str, doc_num: str) -> dict[str, Any]:
    if module not in MODULES:
        raise CotrError("Modul tidak dikenal")
    d = db.fetchrow(f"SELECT * FROM {S}.erp_documents WHERE module = %s AND doc_num = %s", (module, doc_num))
    if not d:
        raise CotrError("Dokumen tidak ditemukan")
    ls = db.fetch(f"""SELECT line_type, line_no, product_code, product_name, uom_name, qty, price, total, ref_num, notes, raw_data->>'purposeName' AS purpose
                        FROM {S}.erp_document_lines WHERE module = %s AND doc_num = %s ORDER BY line_type, line_no""", (module, doc_num))
    return {"module": module, "name": MODULES[module], "docNum": d["doc_num"], "day": _iso(d["doc_date"]), "branch": d["branch_name"],
            "location": d["location_name"], "counterparty": d["counterparty_name"], "ref": d["ref_num"], "type": d["trans_type"],
            "status": d["status_name"], "total": float(d["total"]) if d["total"] is not None else None, "info": d["additional_info"],
            "by": d["created_by"], "synced": _iso(d["synced_at"]),
            "lines": [{"type": r["line_type"], "no": r["line_no"], "code": r["product_code"], "product": r["product_name"], "uom": r["uom_name"],
                       "qty": float(r["qty"]) if r["qty"] is not None else None, "price": float(r["price"]) if r["price"] is not None else None,
                       "total": float(r["total"]) if r["total"] is not None else None, "ref": r["ref_num"], "notes": r["notes"], "purpose": r["purpose"]}
                      for r in ls]}
