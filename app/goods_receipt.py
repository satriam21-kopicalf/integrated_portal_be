"""Good Receipt (Cost Control, page cc.goods-receipt, migration 072): the GRs of ESB with the corrections of Cost Control.

ESB's GR (integration_esb.erp_documents module goods_receipt + erp_document_lines, synced by integrated-esbapi) is never
changed. Cost Control corrects, with a reason and at least one evidence file:
  * the receipt date of a GR (the goods arrived another day than ESB says: the purchase belongs to that day), and
  * the quantity of a line (in the unit of the line).
A new correction of the same date / line replaces the previous one (kept as history); a correction can be cancelled.

adjustments() gives what the corrections change in a period — quantities per branch × item × unit and their value —
for the COGS & Usage of the Sales Calculator (menu_costing._purchases) and the Outlet Ratio (ratio.ratio). A line is
valued with the price of its purchase order (GR ref_num), else the Pricelist (× the pack factor), else not at all.
"""
import base64
import binascii
import re
from datetime import date
from typing import Any, Optional

from app import database as db
from app.database import SCHEMA

P = "integration_portal"
T, T_EV = f"{P}.cc_gr_correction", f"{P}.cc_gr_evidence"
ACTIVE = "replaced_at IS NULL AND cancelled_at IS NULL"
SKIP_STATUSES = ("Rejected", "Cancelled", "Void")
MAX_FILE = 5 * 1024 * 1024
MAX_FILES = 5
_DATA_URL = re.compile(r"^data:([\w.+/-]+);base64,(.*)$", re.S)
TYPES = {"image/jpeg": b"\xff\xd8", "image/png": b"\x89PNG", "image/webp": b"RIFF", "application/pdf": b"%PDF"}


class GrError(ValueError):
    """A correction refused with a message for the user (HTTP 422)."""


def _f(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


def _scope_sql(branches: Optional[list[str]]) -> str:
    return "" if branches is None else " AND b.branch_code = ANY(%(br)s)"


DOCS = f"""FROM {SCHEMA}.erp_documents d
           LEFT JOIN {SCHEMA}.master_branches b ON b.branch_id::text = d.branch_id::text
           LEFT JOIN {T} dc ON dc.doc_num = d.doc_num AND dc.kind = 'date' AND dc.{ACTIVE.replace(' AND ', ' AND dc.')}
          WHERE d.module = 'goods_receipt' AND d.deleted_at IS NULL"""


def documents(d_from: date, d_to: date, branches: Optional[list[str]], search: str = "", supplier: str = "",
              corrected: bool = False, limit: int = 1000) -> dict:
    """The GRs received in the period (by their corrected date), latest first."""
    params = {"a": d_from, "b": d_to, "br": branches or [], "q": f"%{search.strip()}%", "any": not search.strip(), "s": supplier, "lim": limit}
    rows = db.fetch(
        f"""SELECT d.doc_num, d.doc_date, coalesce(dc.new_date, d.doc_date) AS date, d.ref_num, b.branch_code,
                   coalesce(b.branch_name, d.branch_name) AS branch_name, d.location_name, d.counterparty_name AS supplier,
                   d.status_name, d.line_count, dc.id IS NOT NULL AS date_corrected,
                   (SELECT count(*) FROM {T} q WHERE q.doc_num = d.doc_num AND q.kind = 'qty' AND q.{ACTIVE.replace(' AND ', ' AND q.')})::int
                       AS qty_corrected
            {DOCS} AND coalesce(dc.new_date, d.doc_date) BETWEEN %(a)s AND %(b)s{_scope_sql(branches)}
              AND (%(any)s OR d.doc_num ILIKE %(q)s OR d.ref_num ILIKE %(q)s OR d.counterparty_name ILIKE %(q)s)
              AND (%(s)s = '' OR d.counterparty_name = %(s)s)
            ORDER BY 3 DESC, d.doc_num DESC LIMIT %(lim)s""", params)
    rows = [r for r in rows if not corrected or r["date_corrected"] or r["qty_corrected"]]
    suppliers = sorted({r["supplier"] for r in rows if r["supplier"]})
    return {"data": [_doc_view(r) for r in rows], "suppliers": suppliers, "truncated": len(rows) >= limit}


def _doc_view(r: dict) -> dict:
    return {"docNum": r["doc_num"], "esbDate": r["doc_date"].isoformat() if r["doc_date"] else None,
            "date": r["date"].isoformat() if r["date"] else None, "poNum": r["ref_num"], "branchCode": r["branch_code"],
            "branchName": r["branch_name"], "location": r["location_name"], "supplier": r["supplier"],
            "status": r["status_name"], "lines": r["line_count"], "dateCorrected": bool(r["date_corrected"]),
            "qtyCorrected": int(r["qty_corrected"] or 0)}


def _head(doc_num: str, branches: Optional[list[str]]) -> Optional[dict]:
    return db.fetchrow(
        f"""SELECT d.doc_num, d.doc_date, coalesce(dc.new_date, d.doc_date) AS date, d.ref_num, b.branch_code,
                   coalesce(b.branch_name, d.branch_name) AS branch_name, d.location_name, d.counterparty_name AS supplier,
                   d.status_name, d.line_count, dc.id IS NOT NULL AS date_corrected, 0 AS qty_corrected,
                   d.additional_info, d.created_by, d.esb_created_at
            {DOCS} AND d.doc_num = %(n)s{_scope_sql(branches)}""", {"n": doc_num, "br": branches or []})


def _lines(doc_num: str) -> list[dict]:
    return db.fetch(
        f"""SELECT l.line_no, l.product_code, l.product_name, l.uom_name, l.qty, l.raw_data->>'categoryName' AS category,
                   c.new_qty, c.id AS correction_id
              FROM {SCHEMA}.erp_document_lines l
              LEFT JOIN {T} c ON c.doc_num = l.doc_num AND c.kind = 'qty' AND c.line_no = l.line_no
                             AND c.{ACTIVE.replace(' AND ', ' AND c.')}
             WHERE l.module = 'goods_receipt' AND l.doc_num = %s AND l.line_type = 'detail' ORDER BY l.line_no""", (doc_num,))


def detail(doc_num: str, branches: Optional[list[str]]) -> Optional[dict]:
    head = _head(doc_num, branches)
    if head is None:
        return None
    lines = _lines(doc_num)
    history = db.fetch(f"SELECT * FROM {T} WHERE doc_num = %s ORDER BY created_at DESC, id DESC", (doc_num,))
    files = db.fetch(f"""SELECT id, correction_id, file_name, content_type, bytes, uploaded_by, uploaded_at FROM {T_EV}
                         WHERE doc_num = %s ORDER BY uploaded_at, id""", (doc_num,))
    by_corr: dict[int, list[dict]] = {}
    for f in files:
        by_corr.setdefault(f["correction_id"], []).append(
            {"id": f["id"], "name": f["file_name"], "type": f["content_type"], "bytes": f["bytes"]})
    return {
        **_doc_view({**head, "qty_corrected": sum(1 for ln in lines if ln["correction_id"])}),
        "note": head["additional_info"], "createdBy": head["created_by"],
        "items": [{"lineNo": ln["line_no"], "code": ln["product_code"], "name": ln["product_name"], "unit": ln["uom_name"],
                   "category": ln["category"], "esbQty": _f(ln["qty"]),
                   "qty": _f(ln["new_qty"]) if ln["correction_id"] else _f(ln["qty"]),
                   "corrected": ln["correction_id"] is not None} for ln in lines],
        "corrections": [{
            "id": c["id"], "kind": c["kind"], "lineNo": c["line_no"], "code": c["product_code"], "unit": c["uom"],
            "oldDate": c["old_date"].isoformat() if c["old_date"] else None,
            "newDate": c["new_date"].isoformat() if c["new_date"] else None,
            "oldQty": _f(c["old_qty"]), "newQty": _f(c["new_qty"]), "reason": c["reason"], "by": c["created_by"],
            "at": c["created_at"].isoformat(),
            "state": "cancelled" if c["cancelled_at"] else "replaced" if c["replaced_at"] else "active",
            "cancelledBy": c["cancelled_by"], "files": by_corr.get(c["id"], [])} for c in history],
    }


def parse_file(item: dict) -> tuple[str, str, bytes]:
    m = _DATA_URL.match(str(item.get("data") or "").strip())
    if not m:
        raise GrError("Evidence harus berupa foto (JPG, PNG, WebP) atau PDF")
    mime = m.group(1).lower()
    if mime not in TYPES:
        raise GrError("Evidence harus berupa foto (JPG, PNG, WebP) atau PDF")
    try:
        data = base64.b64decode(re.sub(r"\s", "", m.group(2)), validate=True)
    except (binascii.Error, ValueError):
        raise GrError("File evidence rusak") from None
    if len(data) > MAX_FILE:
        raise GrError("Ukuran evidence maksimal 5 MB per file")
    if not data.startswith(TYPES[mime]):
        raise GrError("File evidence tidak valid")
    name = re.sub(r"[\r\n\"/\\]", "_", str(item.get("name") or "evidence").strip())[:160] or "evidence"
    return name, mime, data


def correct(doc_num: str, body: dict, user: dict, branches: Optional[list[str]]) -> dict:
    """{"date"?: receipt date, "lines"?: [{"lineNo", "qty"}], "reason", "files": [{"name", "data": data URL}]}: the changes
    against the current figures (a value back to ESB's ends the correction)."""
    head = _head(doc_num, branches)
    if head is None:
        raise LookupError(doc_num)
    reason = " ".join(str(body.get("reason") or "").split())
    if len(reason) < 3:
        raise GrError("Tulis alasan koreksi")
    files = [parse_file(f) for f in (body.get("files") or [])]
    if not files:
        raise GrError("Lampirkan minimal satu evidence (foto surat jalan, PDF …)")
    if len(files) > MAX_FILES:
        raise GrError(f"Maksimal {MAX_FILES} file evidence")
    lines = {ln["line_no"]: ln for ln in _lines(doc_num)}
    by = user.get("full_name") or user["username"]
    changes: list[tuple] = []  # (kind, line_no, code, uom, old_date, new_date, old_qty, new_qty, back_to_esb)
    if body.get("date"):
        new = date.fromisoformat(str(body["date"]))
        if new != head["date"]:
            changes.append(("date", None, None, None, head["doc_date"], new, None, None, new == head["doc_date"]))
    for item in body.get("lines") or []:
        ln = lines.get(int(item.get("lineNo") or 0))
        if ln is None:
            raise GrError(f"Baris {item.get('lineNo')} tidak ada di {doc_num}")
        try:
            qty = round(float(str(item.get("qty")).replace(",", ".")), 6)
        except ValueError:
            raise GrError(f"Qty {ln['product_name']} harus angka") from None
        if qty < 0 or qty > 1_000_000_000:
            raise GrError(f"Qty {ln['product_name']} tidak valid")
        now = _f(ln["new_qty"]) if ln["correction_id"] else _f(ln["qty"])
        if now is None or abs(qty - now) > 1e-9:
            changes.append(("qty", ln["line_no"], ln["product_code"], ln["uom_name"], None, None, _f(ln["qty"]), qty,
                            abs(qty - (_f(ln["qty"]) or 0)) < 1e-9))
    if not changes:
        raise GrError("Tidak ada perubahan dibanding data saat ini")
    with db.transaction() as conn:
        first: Optional[int] = None
        for kind, line_no, code, uom, old_d, new_d, old_q, new_q, back in changes:
            conn.execute(f"""UPDATE {T} SET replaced_at = now() WHERE doc_num = %s AND kind = %s
                             AND coalesce(line_no, 0) = %s AND {ACTIVE}""", (doc_num, kind, line_no or 0))
            row = conn.execute(
                f"""INSERT INTO {T} (doc_num, kind, line_no, product_code, uom, old_date, new_date, old_qty, new_qty, reason,
                                      created_by, replaced_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END) RETURNING id""",
                (doc_num, kind, line_no, code, uom, old_d, new_d, old_q, new_q, reason, by, back)).fetchone()
            first = first or row["id"]
        for name, mime, data in files:
            conn.execute(f"""INSERT INTO {T_EV} (correction_id, doc_num, file_name, content_type, bytes, data, uploaded_by)
                             VALUES (%s, %s, %s, %s, %s, %s, %s)""", (first, doc_num, name, mime, len(data), data, by))
    return detail(doc_num, branches) or {}


def cancel(doc_num: str, correction_id: int, user: dict, branches: Optional[list[str]]) -> dict:
    if _head(doc_num, branches) is None:
        raise LookupError(doc_num)
    row = db.fetchrow(f"""UPDATE {T} SET cancelled_at = now(), cancelled_by = %s WHERE id = %s AND doc_num = %s AND {ACTIVE}
                          RETURNING id""", (user.get("full_name") or user["username"], correction_id, doc_num))
    if row is None:
        raise GrError("Koreksi ini sudah tidak aktif")
    return detail(doc_num, branches) or {}


def evidence(doc_num: str, file_id: int, branches: Optional[list[str]]) -> Optional[dict]:
    if _head(doc_num, branches) is None:
        return None
    return db.fetchrow(f"SELECT file_name, content_type, data FROM {T_EV} WHERE id = %s AND doc_num = %s", (file_id, doc_num))


# ---------------------------------------------------------------- what the corrections change in a period

def adjustments(d_from: date, d_to: date, branches: Optional[list[str]] = None) -> dict:
    """{"qty": {branch: {(product code, UOM): Δqty}}, "value": {branch: {supplier: Δvalue}}, "unpriced": lines}: the
    corrected GRs against ESB's for the period — a line counts in the period of its (corrected) receipt date with its
    (corrected) quantity, instead of ESB's date and quantity."""
    out: dict = {"qty": {}, "value": {}, "unpriced": 0}
    try:
        rows = db.fetch(
            f"""SELECT d.doc_num, d.doc_date, coalesce(dc.new_date, d.doc_date) AS eff_date, d.ref_num, b.branch_code,
                       d.counterparty_name AS supplier, l.product_code, upper(coalesce(l.uom_name, '')) AS uom, l.qty,
                       coalesce(qc.new_qty, l.qty) AS eff_qty, po.price
                  {DOCS}
                  JOIN {SCHEMA}.erp_document_lines l ON l.module = d.module AND l.doc_num = d.doc_num AND l.line_type = 'detail'
                  LEFT JOIN {T} qc ON qc.doc_num = d.doc_num AND qc.kind = 'qty' AND qc.line_no = l.line_no
                                  AND qc.{ACTIVE.replace(' AND ', ' AND qc.')}
                  LEFT JOIN LATERAL (SELECT p.price FROM {SCHEMA}.erp_document_lines p
                                      WHERE p.module = 'purchase_order' AND p.doc_num = d.ref_num AND p.line_type = 'detail'
                                        AND p.product_code = l.product_code AND p.price > 0
                                      ORDER BY (upper(coalesce(p.uom_name, '')) = upper(coalesce(l.uom_name, ''))) DESC LIMIT 1) po ON true
                 WHERE (dc.id IS NOT NULL OR qc.id IS NOT NULL) AND d.status_name <> ALL(%(skip)s)
                   AND (d.doc_date BETWEEN %(a)s AND %(b)s OR coalesce(dc.new_date, d.doc_date) BETWEEN %(a)s AND %(b)s)
                   {_scope_sql(branches)}""",
            {"a": d_from, "b": d_to, "br": branches or [], "skip": list(SKIP_STATUSES)})
    except Exception:  # noqa: BLE001 - migration 072 not applied yet: no corrections
        return out
    fallback = _fallback_prices([r["product_code"] for r in rows if r["price"] is None and r["product_code"]], d_to)
    for r in rows:
        if not r["branch_code"] or not r["product_code"]:
            continue
        before = float(r["qty"] or 0) if d_from <= r["doc_date"] <= d_to else 0.0
        after = float(r["eff_qty"] or 0) if d_from <= r["eff_date"] <= d_to else 0.0
        dq = after - before
        if abs(dq) < 1e-9:
            continue
        key = (r["product_code"], r["uom"])
        q = out["qty"].setdefault(r["branch_code"], {})
        q[key] = q.get(key, 0.0) + dq
        price = float(r["price"]) if r["price"] is not None else fallback.get(key)
        if price is None:
            out["unpriced"] += 1
            continue
        v = out["value"].setdefault(r["branch_code"], {})
        v[r["supplier"] or ""] = v.get(r["supplier"] or "", 0.0) + dq * price
    return out


def _fallback_prices(codes: list[str], day: date) -> dict[tuple[str, str], float]:
    """{(code, UOM): price of one UOM} from the Pricelist (price per base unit × the base units in the UOM)."""
    if not codes:
        return {}
    from app import menu_costing as mc

    prices = mc.prices_on(day, sorted(set(codes)))
    out: dict[tuple[str, str], float] = {}
    for code, (base, packs) in mc.item_units(sorted(set(codes))).items():
        if code not in prices:
            continue
        out[(code, base)] = prices[code]
        for uom, factor in packs.items():
            out[(code, uom)] = prices[code] * factor
    return out
