"""Stock moves of the outlets for the actual usage (COGS & Usage Ratio, 2026-11-06): every ESB document that moves stock
in or out of a branch between two stock opnames (integration_esb.erp_documents + erp_document_lines, integrated-esbapi).

    received   goods_receipt (status Invoice / Authorized: goods in the outlet, invoiced or not yet), with the corrections
               of Cost Control (app/goods_receipt.py, migration 072). It replaces the purchase invoices, which lag the
               goods received (2026-11: 5 % less quantity in a week, half of the items differ).
    outletBuy  simple_purchase detail lines: what the outlet buys itself (water, ice, bananas …); its cost lines are
               expenses (stationery, gas, utilities), no stock.
    produced   simple_manufacturing result lines (beans → espresso at the outlet): the result comes in …
    consumed   … its material lines go out.
    delivered  goods_delivery: goods the outlet sends out (to Calf Warehouse).
    returned   purchase_return, goods_receipt_return: back to the supplier.
    transferIn / transferOut  the Transfer Stock between outlets received in the period (TITO, migration 075, recorded
               in the portal only; app/stock_transfer.py), in the base unit of the item.

Drafts (New), rejected, cancelled and void documents are left out; item_journal (the adjustments of the stock opname)
too, the counts already hold them.
"""
from datetime import date
from typing import Optional

from app import database as db
from app.database import SCHEMA

# kind → (module, line type, sign)
MOVES = {
    "received": ("goods_receipt", "detail", 1),
    "outletBuy": ("simple_purchase", "detail", 1),
    "produced": ("simple_manufacturing", "result", 1),
    "consumed": ("simple_manufacturing", "material", -1),
    "delivered": ("goods_delivery", "detail", -1),
    "returned": ("purchase_return", "detail", -1),
    "returnedGr": ("goods_receipt_return", "detail", -1),
}
KINDS = ("received", "outletBuy", "produced", "consumed", "delivered", "returned", "transferIn", "transferOut")
SKIP = ("New", "Draft", "Rejected", "Cancelled", "Canceled", "Void")


def moves(d_from: date, d_to: date, branches: list[str]) -> dict[str, dict[str, dict[tuple[str, str], float]]]:
    """{branch: {kind: {(product code, UOM): signed qty}}} of the documents dated in [d_from, d_to]."""
    out: dict[str, dict[str, dict[tuple[str, str], float]]] = {}
    if not branches:
        return out
    from app import test_branches
    readers = test_branches.readers(branches)  # a test branch gets the ESB documents of its source branch
    asked, branches = branches, sorted(readers)
    pairs = sorted({(m, t) for m, t, _ in MOVES.values()})
    rows = db.fetch(
        f"""SELECT b.branch_code, d.module, l.line_type, l.product_code, upper(coalesce(l.uom_name, '')) AS uom, sum(l.qty) AS qty
              FROM {SCHEMA}.erp_documents d
              JOIN {SCHEMA}.erp_document_lines l ON l.module = d.module AND l.doc_num = d.doc_num
              JOIN {SCHEMA}.master_branches b ON b.branch_id::text = d.branch_id::text
             WHERE (d.module, l.line_type) IN (SELECT * FROM unnest(%s::text[], %s::text[]))
               AND d.doc_date BETWEEN %s AND %s AND d.deleted_at IS NULL AND coalesce(d.status_name, '') <> ALL(%s)
               AND b.branch_code = ANY(%s) AND l.product_code IS NOT NULL AND coalesce(l.qty, 0) <> 0
             GROUP BY 1, 2, 3, 4, 5""",
        ([m for m, _ in pairs], [t for _, t in pairs], d_from, d_to, list(SKIP), branches))
    kind_of = {(m, t): (k if k != "returnedGr" else "returned", s) for k, (m, t, s) in MOVES.items()}
    for r in rows:
        kind, sign = kind_of[(r["module"], r["line_type"])]
        for b in readers.get(r["branch_code"], []):
            bucket = out.setdefault(b, {}).setdefault(kind, {})
            key = (r["product_code"], r["uom"])
            bucket[key] = bucket.get(key, 0.0) + sign * float(r["qty"])
    # the transfers between outlets (portal, TITO)
    from app import stock_transfer
    for r in stock_transfer.moves(d_from, d_to, asked):
        bucket = out.setdefault(r["branch"], {}).setdefault(r["kind"], {})
        key = (r["item_code"], str(r["unit"] or "").upper())
        bucket[key] = bucket.get(key, 0.0) + float(r["qty"])
    # the Good Receipts Cost Control corrected (receipt date, quantity; migration 072)
    from app import goods_receipt

    for src, items in goods_receipt.adjustments(d_from, d_to, branches)["qty"].items():
        for branch in readers.get(src, []):
            bucket = out.setdefault(branch, {}).setdefault("received", {})
            for key, dq in items.items():
                bucket[key] = bucket.get(key, 0.0) + dq
    return out
