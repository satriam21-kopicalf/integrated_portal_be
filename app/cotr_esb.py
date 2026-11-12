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
import re
from datetime import date
from typing import Any, Optional

from psycopg.types.json import Jsonb

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


# ---------------------------------------------------------------- recipe cards and items as in ESB (2026-11-12)

ITEM_CATEGORY = {"raw material": "raw", "roasted beans": "raw", "wip": "premix", "packaging": "packaging", "packaging bar": "packaging",
                 "general": "supply", "exp gas": "other"}


def _category(name: Optional[str]) -> Optional[str]:
    n = (name or "").strip().lower()
    if n == "finished good":
        return None  # the menus, not items
    if n.startswith("kitchen"):
        return "supply"
    return ITEM_CATEGORY.get(n, "other")


def _base_unit(uom: Optional[str]) -> str:
    u = (uom or "").strip().upper()
    return {"GR": "g", "GRAM": "g", "ML": "ml", "PCS": "pcs"}.get(u, (uom or "pcs").lower())


def sync_recipes(user: dict, apply: bool) -> dict:
    """The items and recipe cards of COTR made the same as ESB COTR (the request of the COTR team, 2026-11-12): every
    active BOM of ESB becomes a card (premix per gram with the batch of ESB, a finished good per portion), its products
    become items (base unit, stock unit, the last HPP of ESB), and each menu takes the active finished-good card named
    like it. Cards and items of the portal not in ESB stay."""
    menus = _menu_map()
    units: dict[str, dict[str, float]] = {}
    base_of: dict[str, str] = {}
    stock_of: dict[str, tuple[str, float]] = {}
    for r in db.fetch(f"""SELECT p.product_code, u.uom_name, u.qty, u.is_base, u.is_stock FROM {S}.master_product_units u
                            JOIN {S}.master_products p ON p.product_id = u.product_id WHERE u.is_active IS NOT FALSE"""):
        units.setdefault(r["product_code"], {})[r["uom_name"].upper()] = float(r["qty"] or 1)
        if r["is_base"]:
            base_of[r["product_code"]] = r["uom_name"]
        if r["is_stock"] and not r["is_base"]:
            stock_of[r["product_code"]] = (r["uom_name"], float(r["qty"] or 1))
    products = {r["product_code"]: r for r in db.fetch(
        f"SELECT product_code, product_name, raw_data->>'categoryName' AS category FROM {S}.master_products")}
    boms = db.fetch(f"""SELECT bom_id, bom_code, bom_name, product_name, raw_data->>'uomName' AS uom, raw_data->>'flagActive' AS active
                          FROM {S}.master_bom WHERE NOT coalesce(is_deleted, false)""")
    lines = {}
    for r in db.fetch(f"SELECT bom_id, line_no, product_code, product_name, uom_name, qty, last_hpp FROM {S}.master_bom_lines ORDER BY bom_id, line_no"):
        lines.setdefault(r["bom_id"], []).append(r)

    def to_base(code: str, uom: str, qty: float) -> float:
        return qty * units.get(code, {}).get((uom or "").upper(), 1.0)

    items: dict[str, dict] = {}
    cost: dict[str, float] = {}
    for ls in lines.values():
        for l in ls:
            factor = units.get(l["product_code"], {}).get((l["uom_name"] or "").upper(), 1.0)
            if l["last_hpp"] is not None and factor:
                cost[l["product_code"]] = float(l["last_hpp"]) / factor
    used = {l["product_code"] for ls in lines.values() for l in ls}
    # the products COTR really moves: in the recipes, or in its documents of the last 120 days (transfers, purchases,
    # stock opname, journals); the rest of the master of ESB is left out
    seen = {r["code"] for r in db.fetch(f"""SELECT DISTINCT l.product_code AS code FROM {S}.erp_document_lines l
                                              JOIN {S}.erp_documents d ON d.module = l.module AND d.doc_num = l.doc_num
                                             WHERE d.doc_date >= current_date - 120 AND l.product_code IS NOT NULL""")}
    for code, p in products.items():
        cat = _category(p["category"])
        if cat is None or not (code in used or code in seen):
            continue
        pack = stock_of.get(code)
        items[code] = {"code": code, "name": p["product_name"], "category": cat, "unit": _base_unit(base_of.get(code)),
                       "pack_unit": pack[0] if pack else None, "pack_qty": pack[1] if pack else None, "cost": cost.get(code)}
    cards = []
    for b in boms:
        if str(b["active"]) not in ("1", "true", "True", "None"):
            continue
        cat = _category((products.get(b["bom_code"]) or {}).get("category"))
        premix = cat == "premix" or "PREMIX" in (b["bom_name"] or "").upper()
        if premix:
            yield_qty = units.get(b["bom_code"], {}).get((b["uom"] or "").upper())
            if not yield_qty:
                m = re.search(r"@([\d.,]+)\s*(GR|ML)", b["uom"] or "", re.I)
                yield_qty = float(m.group(1).replace(".", "").replace(",", ".")) if m else 1.0
            unit = "g"
        else:
            yield_qty, unit = 1.0, "porsi"
        cards.append({"code": b["bom_code"], "name": b["bom_name"], "output": b["bom_code"], "kind": "premix" if premix else "menu",
                      "yield_qty": yield_qty, "yield_unit": unit,
                      "lines": [{"item": l["product_code"], "qty": round(to_base(l["product_code"], l["uom_name"], float(l["qty"] or 0)), 6),
                                 "unit": _base_unit(base_of.get(l["product_code"]) or l["uom_name"])} for l in lines.get(b["bom_id"], [])]})
    picks = {}
    for c in cards:
        if c["kind"] != "menu" or not c["lines"]:
            continue
        menu = menu_of(c["name"], menus) if "COTR" in (c["name"] or "").upper() else None
        if menu and menu not in picks:
            picks[menu] = c["code"]
    summary = {"items": len(items), "cards": len(cards), "premix": sum(1 for c in cards if c["kind"] == "premix"),
               "menus": {menus[m]["name"]: code for m, code in picks.items()}}
    if not apply:
        return summary
    with db.transaction() as conn:
        for it in items.values():
            conn.execute(f"""INSERT INTO {P}.cotr_item (code, name, category, unit, pack_unit, pack_qty, cost, is_stock, is_active, note, updated_by)
                             VALUES (%(code)s, %(name)s, %(category)s, %(unit)s, %(pack_unit)s, %(pack_qty)s, %(cost)s, true, true, 'ESB COTR', %(by)s)
                             ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, category = EXCLUDED.category, unit = EXCLUDED.unit,
                                 pack_unit = coalesce(EXCLUDED.pack_unit, {P}.cotr_item.pack_unit), pack_qty = coalesce(EXCLUDED.pack_qty, {P}.cotr_item.pack_qty),
                                 cost = coalesce(EXCLUDED.cost, {P}.cotr_item.cost), is_active = true, updated_at = now(), updated_by = EXCLUDED.updated_by""",
                         it | {"by": user["id"]})
        for c in cards:
            conn.execute(f"""INSERT INTO {P}.cotr_bom (code, name, output, yield_qty, yield_unit, lines, kind, note, updated_by)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, 'ESB COTR', %s)
                             ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, output = EXCLUDED.output, yield_qty = EXCLUDED.yield_qty,
                                 yield_unit = EXCLUDED.yield_unit, lines = EXCLUDED.lines, kind = EXCLUDED.kind, note = 'ESB COTR',
                                 updated_at = now(), updated_by = EXCLUDED.updated_by""",
                         (c["code"], c["name"], c["output"], c["yield_qty"], c["yield_unit"], Jsonb(c["lines"]), c["kind"], user["id"]))
        for menu, code in picks.items():
            conn.execute(f"UPDATE {P}.cotr_menu SET bom_code = %s WHERE code = %s", (code, menu))
    return summary | {"saved": True}


def freshness() -> dict:
    """How fresh the figures of every pool / HUB are (recommendation #3): the newest ESB document per kind (sales,
    transfers, journals), the last day of the workbook, the last physical count and the last app shift, and when ESB
    was last synced — so the team knows whether today's figures are complete."""
    br = branches()
    pool_of = {(k or ""): v["pool"] for k, v in br.items() if v["pool"]}
    esb: dict[str, dict[str, Optional[date]]] = {}
    for r in db.fetch(f"""SELECT upper(trim(branch_name)) AS b, module, max(doc_date) AS last FROM {S}.erp_documents
                           WHERE deleted_at IS NULL AND module IN ('simple_sales', 'simple_transfer', 'item_journal') GROUP BY 1, 2"""):
        p = pool_of.get(r["b"] or "")
        if p:
            esb.setdefault(p, {})[r["module"]] = r["last"]
    sync = db.fetchrow(f"SELECT max(synced_at) AS at FROM {S}.erp_documents")
    def last(sql: str) -> dict[str, date]:
        return {r["pool"]: r["day"] for r in db.fetch(sql)}
    sheet = last(f"SELECT pool_code AS pool, max(day) AS day FROM {P}.cotr_pool_move WHERE source = 'sheet' GROUP BY 1")
    count = last(f"SELECT pool_code AS pool, max(day) AS day FROM {P}.cotr_pool_count GROUP BY 1")
    app = last(f"SELECT pool_code AS pool, max(day) AS day FROM {P}.cotr_shift GROUP BY 1")
    names = {r["code"]: r["name"] for r in db.fetch(f"SELECT code, name FROM {P}.cotr_pool WHERE kind = 'parent' OR kind IS NULL")}
    pools = sorted(set(names) | set(esb) | set(sheet) | set(count))
    return {"synced": _iso(sync["at"]) if sync else None, "today": _iso(date.today()),
            "pools": [{"pool": p, "name": names.get(p, p), "esbSales": _iso(esb.get(p, {}).get("simple_sales")),
                       "esbTransfer": _iso(esb.get(p, {}).get("simple_transfer")), "esbJournal": _iso(esb.get(p, {}).get("item_journal")),
                       "sheet": _iso(sheet.get(p)), "count": _iso(count.get(p)), "app": _iso(app.get(p))} for p in pools
                      if any((esb.get(p), sheet.get(p), count.get(p), app.get(p)))]}
